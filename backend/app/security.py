"""Seguridad: hash de contraseñas, tokens JWT y límite de intentos de login.

Decisiones para un sistema distribuido:

- **JWT sin estado (HS256)** firmados con ``JWT_SECRET`` compartido por todos los nodos: cualquier
  nodo valida un token emitido por otro, así que el Round-Robin y el failover de NGINX no rompen la
  sesión. Aun así, en cada petición se consulta el usuario en la BD (con failover) para que una
  desactivación o un cambio de rol tengan efecto inmediato.
- **PBKDF2-SHA256** (biblioteca estándar, sin dependencias compiladas) con sal aleatoria por usuario
  y 600 000 iteraciones por defecto.
- **Límite de intentos** en memoria por nodo, por (usuario, IP).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt

from .config import Settings

HASH_ALGORITHM = "pbkdf2_sha256"
JWT_ALGORITHM = "HS256"
JWT_ISSUER = "pedidos-distribuidos"


class AuthenticationError(Exception):
    """Credenciales ausentes, inválidas o expiradas (HTTP 401)."""


class PermissionDeniedError(Exception):
    """El usuario está autenticado pero no tiene permiso (HTTP 403)."""


class TooManyAttemptsError(Exception):
    """Demasiados intentos de login fallidos (HTTP 429)."""

    def __init__(self, retry_after: int):
        super().__init__("Demasiados intentos fallidos. Espera antes de volver a intentarlo.")
        self.retry_after = retry_after


# ---------------------------------------------------------------------- contraseñas
def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def hash_password(password: str, iterations: int) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{HASH_ALGORITHM}${iterations}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt, expected = encoded.split("$", 3)
        if algorithm != HASH_ALGORITHM:
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), base64.b64decode(salt), int(iterations))
        return hmac.compare_digest(digest, base64.b64decode(expected))
    except (ValueError, TypeError):
        return False


def dummy_verify(iterations: int) -> None:
    """Gasta el mismo tiempo que una verificación real (evita enumerar usuarios por tiempo)."""
    hashlib.pbkdf2_hmac("sha256", b"dummy-password", b"dummy-salt-16byt", iterations)


# ---------------------------------------------------------------------- tokens
@dataclass(frozen=True)
class Actor:
    """Usuario autenticado que hace la petición."""

    id: str
    username: str
    full_name: str
    role: str

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def create_access_token(user_id: str, username: str, role: str, settings: Settings) -> tuple[str, int]:
    now = datetime.now(timezone.utc)
    expires_in = settings.jwt_expires_minutes * 60
    claims = {
        "sub": user_id,
        "username": username,
        "role": role,
        "iss": JWT_ISSUER,
        "iat": now,
        "exp": now + timedelta(seconds=expires_in),
        "jti": secrets.token_hex(8),
    }
    return jwt.encode(claims, settings.jwt_secret, algorithm=JWT_ALGORITHM), expires_in


def decode_access_token(token: str, settings: Settings) -> dict[str, Any]:
    try:
        return jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[JWT_ALGORITHM],  # lista fija: impide el ataque "alg=none"
            issuer=JWT_ISSUER,
            options={"require": ["sub", "exp", "iat", "iss"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthenticationError("La sesión expiró, vuelve a iniciar sesión") from exc
    except jwt.PyJWTError as exc:
        raise AuthenticationError("Token inválido") from exc


# ---------------------------------------------------------------------- límite de intentos
class LoginRateLimiter:
    """Ventana deslizante de intentos fallidos por (usuario, IP). Estado en memoria del nodo."""

    def __init__(self, max_failures: int, window_seconds: int):
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self._failures: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, key: tuple[str, str], now: float) -> deque[float]:
        attempts = self._failures[key]
        while attempts and now - attempts[0] > self.window_seconds:
            attempts.popleft()
        return attempts

    def check(self, username: str, ip: str) -> None:
        if self.max_failures <= 0:
            return
        now = time.monotonic()
        with self._lock:
            attempts = self._prune((username.lower(), ip), now)
            if len(attempts) >= self.max_failures:
                raise TooManyAttemptsError(int(self.window_seconds - (now - attempts[0])) + 1)

    def record_failure(self, username: str, ip: str) -> None:
        with self._lock:
            self._failures[(username.lower(), ip)].append(time.monotonic())

    def reset(self, username: str, ip: str) -> None:
        with self._lock:
            self._failures.pop((username.lower(), ip), None)
