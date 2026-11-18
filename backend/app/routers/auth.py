"""Autenticación: /api/auth/register, /api/auth/login y /api/auth/me."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request

from ..config import Settings
from ..database import DatabaseCluster, users_table
from ..deps import get_cluster, get_current_actor, get_rate_limiter, get_settings
from ..schemas import LoginRequest, MeResponse, RegisterRequest, TokenResponse
from ..security import (
    Actor,
    AuthenticationError,
    LoginRateLimiter,
    PermissionDeniedError,
    create_access_token,
    dummy_verify,
    hash_password,
    verify_password,
)
from ..users import find_user_row, get_active_user, register_user, to_public_user

logger = logging.getLogger("pedidos.auth")
router = APIRouter(prefix="/api/auth", tags=["Autenticación"])

INVALID_CREDENTIALS = "Usuario o contraseña incorrectos"


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "desconocida"


def _token_response(row: dict, db_used: str, settings: Settings) -> dict:
    token, expires_in = create_access_token(row["id"], row["username"], row["role"], settings)
    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_in": expires_in,
        "user": to_public_user(row),
        "node": settings.node_id,
        "db_used": db_used,
    }


@router.post("/register", status_code=201, response_model=TokenResponse, summary="Crear cuenta de cliente (rol user)")
def register(
    payload: RegisterRequest,
    cluster: DatabaseCluster = Depends(get_cluster),
    settings: Settings = Depends(get_settings),
):
    if not settings.allow_registration:
        raise PermissionDeniedError("El registro de usuarios está deshabilitado")
    password_hash = hash_password(payload.password, settings.password_hash_iterations)  # fuera de la transacción
    result = cluster.write(
        lambda session: register_user(session, payload.username, payload.full_name, password_hash, settings.node_id),
        table=users_table,
    )
    logger.info("AUTH registro de '%s' (%s)", payload.username, result.db_used)
    return _token_response(result.row, result.db_used, settings)


@router.post("/login", response_model=TokenResponse, summary="Iniciar sesión y obtener un token JWT")
def login(
    payload: LoginRequest,
    request: Request,
    cluster: DatabaseCluster = Depends(get_cluster),
    settings: Settings = Depends(get_settings),
    limiter: LoginRateLimiter = Depends(get_rate_limiter),
):
    ip = _client_ip(request)
    limiter.check(payload.username, ip)
    row, db_used = cluster.read(lambda session: find_user_row(session, payload.username))

    if row is None or row["deleted_at"] is not None:
        dummy_verify(settings.password_hash_iterations)  # mismo tiempo que con un usuario real
        valid = False
    else:
        valid = verify_password(payload.password, row["password_hash"])
    if not valid:
        limiter.record_failure(payload.username, ip)
        logger.warning("AUTH login fallido para '%s' desde %s", payload.username, ip)
        raise AuthenticationError(INVALID_CREDENTIALS)
    if not row["is_active"]:
        raise PermissionDeniedError("La cuenta está desactivada")

    limiter.reset(payload.username, ip)
    logger.info("AUTH login de '%s' (%s, %s)", row["username"], row["role"], db_used)
    return _token_response(row, db_used, settings)


@router.get("/me", response_model=MeResponse, summary="Usuario de la sesión actual")
def me(
    actor: Actor = Depends(get_current_actor),
    cluster: DatabaseCluster = Depends(get_cluster),
    settings: Settings = Depends(get_settings),
):
    row, _ = cluster.read(lambda session: get_active_user(session, actor.id))
    return {"node": settings.node_id, "user": to_public_user(row)}
