"""Dependencias FastAPI: estado compartido del nodo y autenticación."""
from __future__ import annotations

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .chaos import ChaosState
from .config import Settings
from .database import DatabaseCluster
from .metrics import NodeMetrics
from .peers import PeerClient
from .security import Actor, AuthenticationError, LoginRateLimiter, PermissionDeniedError, decode_access_token
from .users import get_active_user

# auto_error=False: devolvemos nuestro propio 401 en español (y Swagger muestra el botón "Authorize").
bearer_scheme = HTTPBearer(auto_error=False, description="Token JWT obtenido en POST /api/auth/login")


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_chaos(request: Request) -> ChaosState:
    return request.app.state.chaos


def get_cluster(request: Request) -> DatabaseCluster:
    return request.app.state.cluster


def get_peers(request: Request) -> PeerClient:
    return request.app.state.peers


def get_metrics(request: Request) -> NodeMetrics:
    return request.app.state.metrics


def get_rate_limiter(request: Request) -> LoginRateLimiter:
    return request.app.state.rate_limiter


def get_current_actor(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    cluster: DatabaseCluster = Depends(get_cluster),
    settings: Settings = Depends(get_settings),
) -> Actor:
    """Valida el JWT y comprueba en la BD (con failover) que el usuario siga activo.

    La consulta en cada petición hace que desactivar a un usuario o cambiar su rol tenga efecto
    inmediato, aunque su token todavía no haya expirado.
    """
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise AuthenticationError("Debes iniciar sesión (falta el token Bearer)")
    claims = decode_access_token(credentials.credentials, settings)
    row, _ = cluster.read(lambda session: get_active_user(session, claims["sub"]))
    if row is None:
        raise AuthenticationError("El usuario no existe o está desactivado")
    actor = Actor(id=row["id"], username=row["username"], full_name=row["full_name"], role=row["role"])
    request.state.actor = actor
    return actor


def require_admin(actor: Actor = Depends(get_current_actor)) -> Actor:
    if not actor.is_admin:
        raise PermissionDeniedError("Esta acción requiere rol de administrador")
    return actor


def forwarded_auth_headers(request: Request) -> dict[str, str]:
    """Cabecera Authorization original, para reenviarla a otros nodos (comparten JWT_SECRET)."""
    authorization = request.headers.get("authorization")
    return {"Authorization": authorization} if authorization else {}
