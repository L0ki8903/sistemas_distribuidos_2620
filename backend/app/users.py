"""Reglas de negocio de usuarios (registro, autenticación y administrador inicial).

Las funciones reciben una ``Session`` y no saben si la atiende db-primary o db-replica, igual que
``services.py``. El hash de la contraseña se calcula FUERA de la transacción (PBKDF2 es lento a
propósito) para no retener conexiones de la base mientras tanto.
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import USER_COLUMNS, User, utcnow

# Espacio de nombres fijo: el administrador inicial tiene el MISMO id en todos los nodos, de modo
# que si ambos nodos intentan crearlo a la vez la clave primaria evita duplicados.
ADMIN_ID_NAMESPACE = uuid.UUID("6b1f9a52-3c0e-4c3a-9a57-2f8e5d0c1a77")


class UsernameTakenError(Exception):
    def __init__(self, username: str):
        super().__init__(f"El usuario '{username}' ya existe")
        self.username = username


def row_of_user(user: User) -> dict[str, Any]:
    return {name: getattr(user, name) for name in USER_COLUMNS}


def to_public_user(row: dict[str, Any]) -> dict[str, Any]:
    """Datos del usuario que se pueden devolver por la API (nunca el hash)."""
    return {
        "id": row["id"],
        "username": row["username"],
        "full_name": row["full_name"],
        "role": row["role"],
        "is_active": row["is_active"],
        "created_at": row["created_at"],
    }


def admin_user_id(username: str) -> str:
    return str(uuid.uuid5(ADMIN_ID_NAMESPACE, username.lower()))


def find_user_row(session: Session, username: str) -> dict[str, Any] | None:
    user = session.execute(select(User).where(User.username == username.lower())).scalar_one_or_none()
    return row_of_user(user) if user is not None else None


def get_active_user(session: Session, user_id: str) -> dict[str, Any] | None:
    user = session.get(User, user_id)
    if user is None or user.deleted_at is not None or not user.is_active:
        return None
    return row_of_user(user)


def register_user(session: Session, username: str, full_name: str, password_hash: str) -> tuple[dict[str, Any], bool]:
    if find_user_row(session, username) is not None:
        raise UsernameTakenError(username)
    now = utcnow()
    user = User(
        id=str(uuid.uuid4()),
        username=username.lower(),
        full_name=full_name,
        password_hash=password_hash,
        role="user",
        is_active=True,
        created_at=now,
        updated_at=now,
        deleted_at=None,
    )
    session.add(user)
    session.flush()
    return row_of_user(user), True


def ensure_admin(session: Session, username: str, password_hash: str) -> tuple[dict[str, Any], bool]:
    """Crea el administrador inicial si no existe. Devuelve ``(fila, creado)``. No cambia uno existente."""
    existing = session.execute(select(User).where(User.username == username.lower())).scalar_one_or_none()
    if existing is not None:
        return row_of_user(existing), False
    now = utcnow()
    user = User(
        id=admin_user_id(username),
        username=username.lower(),
        full_name="Administrador",
        password_hash=password_hash,
        role="admin",
        is_active=True,
        created_at=now,
        updated_at=now,
        deleted_at=None,
    )
    session.add(user)
    session.flush()
    return row_of_user(user), True


def upsert_admin(session: Session, username: str, password_hash: str, full_name: str) -> tuple[dict[str, Any], bool]:
    """Crea o restablece un administrador (usado por ``python -m app.cli``)."""
    user = session.execute(select(User).where(User.username == username.lower())).scalar_one_or_none()
    now = utcnow()
    if user is None:
        user = User(
            id=admin_user_id(username),
            username=username.lower(),
            full_name=full_name,
            password_hash=password_hash,
            role="admin",
            is_active=True,
            created_at=now,
            updated_at=now,
            deleted_at=None,
        )
        session.add(user)
        created = True
    else:
        user.password_hash = password_hash
        user.full_name = full_name or user.full_name
        user.role = "admin"
        user.is_active = True
        user.deleted_at = None
        user.updated_at = now
        created = False
    session.flush()
    return row_of_user(user), created
