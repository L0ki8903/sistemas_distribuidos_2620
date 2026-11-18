"""Aplicación FastAPI de un nodo backend del clúster de pedidos."""
from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from . import __version__
from .chaos import ChaosState
from .config import Settings
from .database import DatabaseCluster, DatabaseUnavailableError, describe_error
from .bootstrap import bootstrap_admin
from .peers import PeerClient
from .routers import auth as auth_router
from .routers import chaos as chaos_router
from .routers import orders as orders_router
from .routers import system as system_router
from .security import AuthenticationError, LoginRateLimiter, PermissionDeniedError, TooManyAttemptsError
from .services import (
    InvalidTransitionError,
    OrderIdConflictError,
    OrderNotEditableError,
    OrderNotFoundError,
)
from .users import UsernameTakenError

logger = logging.getLogger("pedidos")
CRASH_PROTECTED_PREFIX = "/api/orders"


def configure_logging(node_id: str) -> None:
    root = logging.getLogger("pedidos")
    if root.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(f"%(asctime)s %(levelname)-7s [{node_id}] %(name)s: %(message)s"))
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    root.propagate = False


async def _sync_loop(cluster: DatabaseCluster, interval: float) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(cluster.background_tick)
        except Exception:  # noqa: BLE001 - el bucle nunca debe morir
            logger.exception("Error en la reconciliación periódica")


def create_app(settings: Settings | None = None, peer_client: PeerClient | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    configure_logging(settings.node_id)

    chaos = ChaosState(settings.node_id, settings.simulate_crash, settings.simulate_db_primary_down)
    cluster = DatabaseCluster(settings, chaos)
    peers = peer_client or PeerClient(settings.peers, settings.peer_timeout_seconds)
    rate_limiter = LoginRateLimiter(settings.login_max_failures, settings.login_window_seconds)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await asyncio.to_thread(cluster.ensure_schema)
        await asyncio.to_thread(bootstrap_admin, cluster, settings)
        if settings.jwt_secret_is_weak:
            logger.warning(
                "SEGURIDAD: JWT_SECRET es el valor por defecto o tiene menos de 32 caracteres. "
                "Define un secreto largo y aleatorio (el MISMO en todos los nodos) antes de exponer el sistema."
            )
        logger.info(
            "Nodo %s listo (peers=%s, sync cada %ss)",
            settings.node_id, [p.name or p.url for p in settings.peers], settings.sync_interval_seconds,
        )
        task = None
        if settings.sync_interval_seconds > 0:
            task = asyncio.create_task(_sync_loop(cluster, settings.sync_interval_seconds))
        yield
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        peers.close()
        cluster.dispose()

    app = FastAPI(
        title="Sistema Distribuido de Gestión de Pedidos",
        description=(
            "Nodo backend tolerante a fallos (failover de BD, escritura dual y Chaos Engineering). "
            "Autenticación con JWT: usa POST /api/auth/login y el botón Authorize."
        ),
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.settings = settings
    app.state.chaos = chaos
    app.state.cluster = cluster
    app.state.peers = peers
    app.state.rate_limiter = rate_limiter

    @app.middleware("http")
    async def node_identity_and_chaos(request: Request, call_next):
        """Añade ``X-Handled-By-Node`` y, si SIMULATE_CRASH está activo, responde 503 en /api/orders*."""
        if chaos.simulate_crash and request.url.path.startswith(CRASH_PROTECTED_PREFIX):
            response = JSONResponse(
                status_code=503,
                content={
                    "detail": f"{settings.node_id} en caída simulada (SIMULATE_CRASH)",
                    "node": settings.node_id,
                    "simulated_crash": True,
                },
                headers={"Retry-After": "5"},
            )
        else:
            response = await call_next(request)
        response.headers["X-Handled-By-Node"] = settings.node_id
        return response

    def _error(status_code: int, detail: str, **extra) -> JSONResponse:
        return JSONResponse(status_code=status_code, content={"detail": detail, "node": settings.node_id, **extra})

    @app.exception_handler(AuthenticationError)
    async def _unauthenticated(_: Request, exc: AuthenticationError):
        response = _error(401, str(exc))
        response.headers["WWW-Authenticate"] = "Bearer"
        return response

    @app.exception_handler(PermissionDeniedError)
    async def _forbidden(_: Request, exc: PermissionDeniedError):
        return _error(403, str(exc))

    @app.exception_handler(TooManyAttemptsError)
    async def _too_many(_: Request, exc: TooManyAttemptsError):
        response = _error(429, str(exc), retry_after=exc.retry_after)
        response.headers["Retry-After"] = str(exc.retry_after)
        return response

    @app.exception_handler(UsernameTakenError)
    async def _username_taken(_: Request, exc: UsernameTakenError):
        return _error(409, str(exc))

    @app.exception_handler(OrderNotFoundError)
    async def _not_found(_: Request, exc: OrderNotFoundError):
        return _error(404, str(exc))

    @app.exception_handler(InvalidTransitionError)
    async def _invalid_transition(_: Request, exc: InvalidTransitionError):
        return _error(409, str(exc), current_status=exc.current, allowed_next=exc.allowed)

    @app.exception_handler(OrderNotEditableError)
    async def _not_editable(_: Request, exc: OrderNotEditableError):
        return _error(409, str(exc))

    @app.exception_handler(OrderIdConflictError)
    async def _id_conflict(_: Request, exc: OrderIdConflictError):
        return _error(409, str(exc))

    @app.exception_handler(IntegrityError)
    async def _integrity(_: Request, exc: IntegrityError):
        return _error(409, f"Conflicto de integridad: {describe_error(exc)}")

    @app.exception_handler(DatabaseUnavailableError)
    async def _db_unavailable(_: Request, exc: DatabaseUnavailableError):
        # Nunca 500: 503 indica indisponibilidad temporal (NGINX puede probar el otro nodo).
        return _error(503, str(exc), db_used=None)

    @app.exception_handler(SQLAlchemyError)
    async def _db_error(_: Request, exc: SQLAlchemyError):
        logger.error("Error de base de datos no recuperable: %s", describe_error(exc))
        return _error(503, f"Error de base de datos: {describe_error(exc)}", db_used=None)

    app.include_router(system_router.router)
    app.include_router(auth_router.router)
    app.include_router(orders_router.router)
    app.include_router(chaos_router.router)
    return app


app = create_app()
