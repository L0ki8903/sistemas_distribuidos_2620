"""Doble sesión SQLAlchemy (db-primary + db-replica) con tolerancia a fallos.

Mecanismos implementados:

1. **Escritura dual**: cada escritura se confirma en ``db-primary`` y se replica de inmediato en
   ``db-replica`` (upsert condicional *last-write-wins*) dentro de un ``try/except``. Si la réplica
   falla, la petición NO falla: la fila queda pendiente para la reconciliación.
2. **Failover de lectura/escritura**: si ``db-primary`` falla (de verdad o por la bandera
   ``SIMULATE_DB_PRIMARY_DOWN``) la operación se repite en ``db-replica`` y se informa
   ``db_used = "replica_failover"`` en lugar de devolver HTTP 500.
3. **Circuit breaker**: tras un fallo real de la primaria se evita durante
   ``breaker_cooldown_seconds`` para no pagar el timeout de conexión en cada petición.
4. **Reconciliación anti-entropía**: compara ``(id, updated_at)`` en ambas bases y copia la versión
   más reciente en la otra (incluye *tombstones* de borrado). Se ejecuta al recuperar la primaria,
   periódicamente en segundo plano y bajo demanda (``POST /api/replication/sync``).
"""
from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Iterator, TypeVar

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DataError, IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from .chaos import ChaosState
from .config import Settings
from .models import Base, Order, utcnow

logger = logging.getLogger("pedidos.db")

PRIMARY = "primary"
REPLICA = "replica"
DB_USED_PRIMARY = "primary"
DB_USED_FAILOVER = "replica_failover"

T = TypeVar("T")
orders_table = Order.__table__


class SimulatedPrimaryDownError(Exception):
    """``db-primary`` está 'caída' por la bandera SIMULATE_DB_PRIMARY_DOWN."""


class PrimaryCircuitOpenError(Exception):
    """El circuit breaker evita ``db-primary`` tras un fallo reciente."""


class DatabaseUnavailableError(Exception):
    """Ni ``db-primary`` ni ``db-replica`` pudieron atender la operación."""


@dataclass
class WriteResult:
    row: dict[str, Any]
    info: Any
    db_used: str
    replication: str  # "synced" | "pending"


def describe_error(exc: BaseException) -> str:
    """Primera línea legible de un error (sin el SQL completo)."""
    original = getattr(exc, "orig", None)
    message = str(original if original is not None else exc).strip() or exc.__class__.__name__
    return message.splitlines()[0][:200]


def normalize_db_url(url: str) -> str:
    """Acepta ``postgresql://`` o ``postgres://`` y usa el driver psycopg 3."""
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def build_engine(url: str, connect_timeout: int) -> Engine:
    url = normalize_db_url(url)
    if url.startswith("sqlite"):
        return create_engine(url, connect_args={"check_same_thread": False, "timeout": connect_timeout})
    return create_engine(
        url,
        pool_pre_ping=True,  # detecta conexiones muertas tras una caída de la BD
        pool_size=5,
        max_overflow=5,
        pool_timeout=connect_timeout + 1,
        pool_recycle=300,
        connect_args={"connect_timeout": connect_timeout, "options": "-c statement_timeout=5000"},
    )


def is_failover_error(exc: BaseException) -> bool:
    """¿El error indica que la base no está disponible (y conviene conmutar)?

    Los errores de integridad/datos son errores del *pedido*, no de la infraestructura.
    """
    if isinstance(exc, (SimulatedPrimaryDownError, PrimaryCircuitOpenError)):
        return True
    if isinstance(exc, (IntegrityError, DataError)):
        return False
    return isinstance(exc, SQLAlchemyError)


def upsert_order_row(conn: Connection, row: dict[str, Any]) -> None:
    """INSERT ... ON CONFLICT (id) DO UPDATE ... WHERE destino.updated_at < origen.updated_at.

    Es atómico e idempotente: aplicar la misma fila dos veces, o una versión más vieja, no
    cambia nada (last-write-wins). Funciona en PostgreSQL y SQLite.
    """
    if conn.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif conn.dialect.name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:  # pragma: no cover - sólo se soportan estos dos motores
        raise RuntimeError(f"Motor no soportado para replicación: {conn.dialect.name}")

    stmt = insert(orders_table).values(**row)
    stmt = stmt.on_conflict_do_update(
        index_elements=[orders_table.c.id],
        set_={column.name: stmt.excluded[column.name] for column in orders_table.columns if column.name != "id"},
        where=orders_table.c.updated_at < stmt.excluded.updated_at,
    )
    conn.execute(stmt)


def _chunks(values: list[str], size: int) -> Iterator[list[str]]:
    for start in range(0, len(values), size):
        yield values[start:start + size]


class DatabaseCluster:
    """Punto único de acceso a las dos bases de datos del clúster."""

    def __init__(self, settings: Settings, chaos: ChaosState) -> None:
        self.settings = settings
        self.chaos = chaos
        self.engines: dict[str, Engine] = {
            PRIMARY: build_engine(settings.primary_db_url, settings.db_connect_timeout),
            REPLICA: build_engine(settings.replica_db_url, settings.db_connect_timeout),
        }
        self._sessions = {
            name: sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
            for name, engine in self.engines.items()
        }
        self._state_lock = threading.Lock()
        self._sync_lock = threading.Lock()
        self._primary_open_until = 0.0
        self._next_resync_attempt = 0.0
        self._needs_resync = False
        self.failover_count = 0
        self.last_primary_error: str | None = None
        self.last_sync: dict[str, Any] | None = None

    # ------------------------------------------------------------------ ciclo de vida
    def ensure_schema(self) -> None:
        """Crea la tabla si no existe (en Docker ya la crea ``database/init.sql``)."""
        for name, engine in self.engines.items():
            try:
                Base.metadata.create_all(engine)
            except SQLAlchemyError as exc:
                logger.warning("SCHEMA no verificado en db-%s: %s", name, describe_error(exc))

    def dispose(self) -> None:
        for engine in self.engines.values():
            engine.dispose()

    # ------------------------------------------------------------------ circuit breaker
    @property
    def needs_resync(self) -> bool:
        return self._needs_resync

    def primary_circuit_open(self) -> bool:
        return time.monotonic() < self._primary_open_until

    def _guard(self, name: str, *, respect_breaker: bool = True) -> None:
        if name != PRIMARY:
            return
        if self.chaos.simulate_db_primary_down:
            raise SimulatedPrimaryDownError("caída simulada de db-primary (SIMULATE_DB_PRIMARY_DOWN)")
        if respect_breaker and self.primary_circuit_open():
            raise PrimaryCircuitOpenError("circuit breaker abierto: db-primary falló recientemente")

    def _record_primary_failure(self, exc: BaseException, operation: str) -> None:
        with self._state_lock:
            self._needs_resync = True
            self.failover_count += 1
            self.last_primary_error = describe_error(exc)
            if not isinstance(exc, (SimulatedPrimaryDownError, PrimaryCircuitOpenError)):
                self._primary_open_until = time.monotonic() + self.settings.breaker_cooldown_seconds
        logger.warning("FAILOVER %s: db-primary no disponible (%s) -> db-replica", operation, describe_error(exc))

    def _record_primary_success(self) -> None:
        if self._primary_open_until:
            with self._state_lock:
                self._primary_open_until = 0.0

    def _maybe_resync(self) -> None:
        """Si hubo failover, reconcilia antes de volver a leer/escribir en la primaria."""
        if not self._needs_resync or self.chaos.simulate_db_primary_down:
            return
        if self.primary_circuit_open() or time.monotonic() < self._next_resync_attempt:
            return
        self.reconcile(reason="recuperación de db-primary")

    # ------------------------------------------------------------------ sesiones
    @contextmanager
    def session(self, name: str) -> Iterator[Session]:
        self._guard(name)
        with self._sessions[name]() as session:
            yield session

    def _transaction(self, name: str, fn: Callable[[Session], T]) -> T:
        with self.session(name) as session, session.begin():
            return fn(session)

    def read(self, fn: Callable[[Session], T]) -> tuple[T, str]:
        """Lee en la primaria; si falla, conmuta a la réplica (``replica_failover``)."""
        self._maybe_resync()
        try:
            with self.session(PRIMARY) as session:
                result = fn(session)
            self._record_primary_success()
            return result, DB_USED_PRIMARY
        except Exception as exc:  # noqa: BLE001 - se filtra con is_failover_error
            if not is_failover_error(exc):
                raise
            self._record_primary_failure(exc, "lectura")
        try:
            with self.session(REPLICA) as session:
                return fn(session), DB_USED_FAILOVER
        except Exception as exc:  # noqa: BLE001
            if not is_failover_error(exc):
                raise
            logger.error("db-replica tampoco disponible: %s", describe_error(exc))
            raise DatabaseUnavailableError("Ni db-primary ni db-replica están disponibles") from exc

    def write(self, fn: Callable[[Session], tuple[dict[str, Any], Any]]) -> WriteResult:
        """Escribe en la primaria y replica en la réplica; failover a la réplica si la primaria falla.

        ``fn`` recibe la sesión (ya dentro de una transacción) y devuelve ``(fila, info)``, donde
        ``fila`` es el estado final completo del pedido afectado.
        """
        self._maybe_resync()
        try:
            row, info = self._transaction(PRIMARY, fn)
        except Exception as exc:  # noqa: BLE001
            if not is_failover_error(exc):
                raise
            self._record_primary_failure(exc, "escritura")
            try:
                row, info = self._transaction(REPLICA, fn)
            except Exception as exc_replica:  # noqa: BLE001
                if not is_failover_error(exc_replica):
                    raise
                logger.error("db-replica tampoco disponible: %s", describe_error(exc_replica))
                raise DatabaseUnavailableError("Ni db-primary ni db-replica están disponibles") from exc_replica
            with self._state_lock:
                self._needs_resync = True
            logger.warning("WRITE order=%s en db-replica (failover); copia a db-primary PENDIENTE", row["id"])
            return WriteResult(row=row, info=info, db_used=DB_USED_FAILOVER, replication="pending")

        self._record_primary_success()
        replication = self._replicate(row, REPLICA)
        return WriteResult(row=row, info=info, db_used=DB_USED_PRIMARY, replication=replication)

    def _replicate(self, row: dict[str, Any], target: str) -> str:
        """Replica una fila en ``target``. Nunca propaga el error: la escritura ya se confirmó."""
        try:
            self._guard(target)
            with self.engines[target].begin() as conn:
                upsert_order_row(conn, row)
        except Exception as exc:  # noqa: BLE001
            with self._state_lock:
                self._needs_resync = True
            logger.warning(
                "REPLICATION order=%s -> db-%s FALLÓ (%s); pendiente de reconciliación",
                row["id"], target, describe_error(exc),
            )
            return "pending"
        logger.info("REPLICATION order=%s -> db-%s OK", row["id"], target)
        return "synced"

    # ------------------------------------------------------------------ reconciliación
    def _index(self, name: str) -> dict[str, datetime]:
        self._guard(name, respect_breaker=False)
        with self.engines[name].connect() as conn:
            rows = conn.execute(select(orders_table.c.id, orders_table.c.updated_at))
            return {row.id: row.updated_at for row in rows}

    def _copy(self, source: str, target: str, ids: list[str]) -> int:
        if not ids:
            return 0
        copied = 0
        with self.engines[source].connect() as src, self.engines[target].begin() as dst:
            for chunk in _chunks(ids, 200):
                for row in src.execute(select(orders_table).where(orders_table.c.id.in_(chunk))).mappings():
                    upsert_order_row(dst, dict(row))
                    copied += 1
        return copied

    def reconcile(self, reason: str = "manual") -> dict[str, Any]:
        """Sincroniza primaria y réplica (last-write-wins por ``updated_at``)."""
        started = time.perf_counter()
        if not self._sync_lock.acquire(timeout=15):
            return {"status": "busy", "reason": reason}
        stage = PRIMARY
        try:
            primary_index = self._index(PRIMARY)
            stage = REPLICA
            replica_index = self._index(REPLICA)
            to_primary = [i for i, ts in replica_index.items() if i not in primary_index or ts > primary_index[i]]
            to_replica = [i for i, ts in primary_index.items() if i not in replica_index or ts > replica_index[i]]
            stage = PRIMARY
            copied_to_primary = self._copy(REPLICA, PRIMARY, to_primary)
            stage = REPLICA
            copied_to_replica = self._copy(PRIMARY, REPLICA, to_replica)
            with self._state_lock:
                self._needs_resync = False
                self._primary_open_until = 0.0
            result: dict[str, Any] = {
                "status": "ok",
                "reason": reason,
                "copied_to_primary": copied_to_primary,
                "copied_to_replica": copied_to_replica,
                "rows_primary": len(primary_index.keys() | set(to_primary)),
                "rows_replica": len(replica_index.keys() | set(to_replica)),
            }
            if copied_to_primary or copied_to_replica:
                logger.info(
                    "SYNC (%s): %d fila(s) réplica->primaria, %d fila(s) primaria->réplica",
                    reason, copied_to_primary, copied_to_replica,
                )
        except SimulatedPrimaryDownError as exc:
            result = {"status": "skipped", "reason": reason, "detail": str(exc)}
        except SQLAlchemyError as exc:
            with self._state_lock:
                self._next_resync_attempt = time.monotonic() + 3
                if stage == PRIMARY:
                    self._primary_open_until = time.monotonic() + self.settings.breaker_cooldown_seconds
            result = {"status": "error", "reason": reason, "failed_db": stage, "detail": describe_error(exc)}
            previous = (self.last_sync or {}).get("status")
            log = logger.info if previous == "error" else logger.warning
            log("SYNC (%s) no completada: db-%s no disponible (%s)", reason, stage, describe_error(exc))
        finally:
            self._sync_lock.release()
        result["at"] = utcnow().isoformat()
        result["duration_ms"] = round((time.perf_counter() - started) * 1000, 1)
        self.last_sync = result
        return result

    def background_tick(self) -> None:
        """Ejecutado periódicamente por el lifespan de la app."""
        if self.chaos.simulate_db_primary_down:
            return
        self.reconcile(reason="periódica")

    # ------------------------------------------------------------------ observabilidad
    def ping(self, name: str) -> dict[str, Any]:
        if name == PRIMARY and self.chaos.simulate_db_primary_down:
            return {"status": "simulated_down", "detail": "SIMULATE_DB_PRIMARY_DOWN activo"}
        started = time.perf_counter()
        try:
            with self.engines[name].connect() as conn:
                conn.execute(text("SELECT 1"))
        except SQLAlchemyError as exc:
            return {"status": "down", "detail": describe_error(exc)}
        return {"status": "up", "latency_ms": round((time.perf_counter() - started) * 1000, 1)}

    def health(self) -> dict[str, Any]:
        return {
            "primary": self.ping(PRIMARY),
            "replica": self.ping(REPLICA),
            "circuit_breaker": "open" if self.primary_circuit_open() else "closed",
            "needs_resync": self._needs_resync,
            "failover_count": self.failover_count,
            "last_primary_error": self.last_primary_error,
            "last_sync": self.last_sync,
        }

    def replication_status(self) -> dict[str, Any]:
        """Compara ambas bases: filas, pedidos activos y divergencias."""
        databases: dict[str, Any] = {}
        indexes: dict[str, dict[str, datetime]] = {}
        for name in (PRIMARY, REPLICA):
            try:
                self._guard(name, respect_breaker=False)
                with self.engines[name].connect() as conn:
                    active = conn.execute(
                        select(func.count()).select_from(orders_table).where(orders_table.c.deleted_at.is_(None))
                    ).scalar_one()
                    index = {
                        row.id: row.updated_at
                        for row in conn.execute(select(orders_table.c.id, orders_table.c.updated_at))
                    }
                indexes[name] = index
                databases[name] = {"status": "up", "rows": len(index), "active_orders": active}
            except SimulatedPrimaryDownError as exc:
                databases[name] = {"status": "simulated_down", "detail": str(exc)}
            except SQLAlchemyError as exc:
                databases[name] = {"status": "down", "detail": describe_error(exc)}

        divergent: int | None = None
        if len(indexes) == 2:
            primary_index, replica_index = indexes[PRIMARY], indexes[REPLICA]
            ids = set(primary_index) | set(replica_index)
            divergent = sum(1 for i in ids if primary_index.get(i) != replica_index.get(i))
        return {
            "in_sync": divergent == 0,
            "divergent_orders": divergent,
            "primary": databases[PRIMARY],
            "replica": databases[REPLICA],
            "needs_resync": self._needs_resync,
            "last_sync": self.last_sync,
        }
