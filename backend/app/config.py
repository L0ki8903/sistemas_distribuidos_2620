"""Configuración del nodo leída desde variables de entorno."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "si", "sí", "on"}


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    try:
        return float(value) if value not in (None, "") else default
    except ValueError:
        return default


DEFAULT_JWT_SECRET = "dev-insecure-jwt-secret-change-me"


@dataclass(frozen=True)
class Peer:
    """Otro nodo backend del clúster, alcanzable por REST."""

    name: str | None
    url: str


def parse_peers(raw: str | None) -> tuple[Peer, ...]:
    """Convierte ``"Nodo-Beta-02=http://backend-node-2:8000,..."`` en una tupla de ``Peer``.

    El nombre es opcional: ``"http://backend-node-2:8000"`` también es válido.
    """
    peers: list[Peer] = []
    for chunk in (raw or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        name: str | None = None
        url = chunk
        if "=" in chunk and not chunk.startswith("http"):
            name, url = (part.strip() for part in chunk.split("=", 1))
        peers.append(Peer(name=name or None, url=url.rstrip("/")))
    return tuple(peers)


@dataclass(frozen=True)
class Settings:
    node_id: str = "Nodo-Local"
    # Fallback a SQLite (archivos locales) cuando no hay PostgreSQL configurado.
    primary_db_url: str = "sqlite:///./local_primary.db"
    replica_db_url: str = "sqlite:///./local_replica.db"
    peers: tuple[Peer, ...] = field(default_factory=tuple)
    simulate_crash: bool = False
    simulate_db_primary_down: bool = False
    # Intervalo de la reconciliación periódica primaria <-> réplica (0 = desactivada).
    sync_interval_seconds: float = 5.0
    db_connect_timeout: int = 2
    peer_timeout_seconds: float = 1.5
    # Tiempo que el circuit breaker evita la primaria tras un fallo real.
    breaker_cooldown_seconds: float = 5.0

    # ---------------------------------------------------------------- autenticación
    # Secreto HS256 COMPARTIDO por todos los nodos: un token emitido por un nodo es válido en el otro.
    jwt_secret: str = DEFAULT_JWT_SECRET
    jwt_expires_minutes: int = 480
    # Administrador inicial. Si ADMIN_PASSWORD está vacío se genera una contraseña aleatoria
    # (se imprime UNA vez en el log del nodo que crea al administrador).
    admin_username: str = "admin"
    admin_password: str = ""
    allow_registration: bool = True
    password_hash_iterations: int = 600_000  # PBKDF2-SHA256 (recomendación OWASP)
    login_max_failures: int = 5
    login_window_seconds: int = 300
    # URL interna del stub_status de NGINX (p. ej. http://gateway:8081/nginx_status); vacío = no se consulta.
    gateway_status_url: str | None = None

    @property
    def jwt_secret_is_weak(self) -> bool:
        return self.jwt_secret == DEFAULT_JWT_SECRET or len(self.jwt_secret) < 32

    @classmethod
    def from_env(cls) -> "Settings":
        defaults = cls()
        return cls(
            jwt_secret=os.getenv("JWT_SECRET") or defaults.jwt_secret,
            jwt_expires_minutes=int(_env_float("JWT_EXPIRES_MINUTES", defaults.jwt_expires_minutes)),
            admin_username=os.getenv("ADMIN_USERNAME") or defaults.admin_username,
            admin_password=os.getenv("ADMIN_PASSWORD", defaults.admin_password),
            allow_registration=_env_bool("ALLOW_REGISTRATION", defaults.allow_registration),
            password_hash_iterations=int(
                _env_float("PASSWORD_HASH_ITERATIONS", defaults.password_hash_iterations)
            ),
            login_max_failures=int(_env_float("LOGIN_MAX_FAILURES", defaults.login_max_failures)),
            login_window_seconds=int(_env_float("LOGIN_WINDOW_SECONDS", defaults.login_window_seconds)),
            gateway_status_url=os.getenv("GATEWAY_STATUS_URL") or None,
            node_id=os.getenv("NODE_ID", defaults.node_id),
            primary_db_url=os.getenv("PRIMARY_DB_URL") or defaults.primary_db_url,
            replica_db_url=os.getenv("REPLICA_DB_URL") or defaults.replica_db_url,
            peers=parse_peers(os.getenv("PEERS")),
            simulate_crash=_env_bool("SIMULATE_CRASH"),
            simulate_db_primary_down=_env_bool("SIMULATE_DB_PRIMARY_DOWN"),
            sync_interval_seconds=_env_float("SYNC_INTERVAL_SECONDS", defaults.sync_interval_seconds),
            db_connect_timeout=int(_env_float("DB_CONNECT_TIMEOUT", defaults.db_connect_timeout)),
            peer_timeout_seconds=_env_float("PEER_TIMEOUT_SECONDS", defaults.peer_timeout_seconds),
            breaker_cooldown_seconds=_env_float(
                "BREAKER_COOLDOWN_SECONDS", defaults.breaker_cooldown_seconds
            ),
        )
