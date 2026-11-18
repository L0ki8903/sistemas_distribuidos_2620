"""Reglas del panel de administración: gestión de usuarios y corrección de pedidos, siempre auditadas.

Protecciones:
- Un administrador no puede quitarse el rol, desactivarse ni eliminarse a sí mismo. Como quien actúa
  siempre es un admin activo, esto garantiza que el sistema nunca se quede sin administradores.
- Eliminar una cuenta la **anonimiza** (usuario ``eliminado-…``, nombre "Usuario eliminado", contraseña
  inutilizable) y deja un *tombstone* que se replica; el nombre de usuario original queda libre.
- Los pedidos eliminados se pueden **restaurar** (son borrados lógicos).
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from . import audit
from .database import queue_replication
from .models import Order, User, utcnow
from .schemas import AdminOrderUpdate, AdminUserUpdate
from .security import Actor
from .services import AUDITED_ORDER_FIELDS, NEXT_STATUS, OrderNotFoundError, compute_total, row_of
from .users import row_of_user

UNUSABLE_PASSWORD = "!cuenta-eliminada"


class UserNotFoundError(Exception):
    def __init__(self, user_id: str):
        super().__init__(f"Usuario {user_id} no encontrado")


class SelfModificationError(Exception):
    """El administrador intenta quitarse el rol, desactivarse o eliminarse a sí mismo."""


class AdminConflictError(Exception):
    """La operación no aplica al estado actual del registro."""


# ---------------------------------------------------------------------- utilidades
def to_admin_user(row: dict[str, Any], orders_count: int = 0) -> dict[str, Any]:
    return {
        "id": row["id"],
        "username": row["username"],
        "full_name": row["full_name"],
        "role": row["role"],
        "is_active": row["is_active"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "deleted_at": row["deleted_at"],
        "orders_count": orders_count,
    }


def _get_user_for_update(session: Session, user_id: str) -> User:
    user = session.execute(select(User).where(User.id == user_id).with_for_update()).scalar_one_or_none()
    if user is None or user.deleted_at is not None:
        raise UserNotFoundError(user_id)
    return user


def _active_orders_count():
    return (
        select(Order.owner_id, func.count(Order.id).label("n"))
        .where(Order.deleted_at.is_(None))
        .group_by(Order.owner_id)
        .subquery()
    )


# ---------------------------------------------------------------------- resumen
def summary(session: Session) -> dict[str, Any]:
    def count(model, *conditions):
        return session.execute(select(func.count()).select_from(model).where(*conditions)).scalar_one()

    by_status = dict(
        session.execute(
            select(Order.status, func.count()).where(Order.deleted_at.is_(None)).group_by(Order.status)
        ).all()
    )
    return {
        "users_total": count(User, User.deleted_at.is_(None)),
        "users_active": count(User, User.deleted_at.is_(None), User.is_active.is_(True)),
        "admins": count(User, User.deleted_at.is_(None), User.role == "admin"),
        "users_deleted": count(User, User.deleted_at.is_not(None)),
        "orders_active": count(Order, Order.deleted_at.is_(None)),
        "orders_deleted": count(Order, Order.deleted_at.is_not(None)),
        "orders_by_status": {status: by_status.get(status, 0) for status in NEXT_STATUS},
        "audit_entries": count(audit.AuditEntry),
    }


# ---------------------------------------------------------------------- usuarios
def list_users(
    session: Session, search: str | None = None, role: str | None = None, status: str | None = None
) -> list[dict[str, Any]]:
    counts = _active_orders_count()
    query = select(User, func.coalesce(counts.c.n, 0)).outerjoin(counts, counts.c.owner_id == User.id)
    if status == "deleted":
        query = query.where(User.deleted_at.is_not(None))
    else:
        query = query.where(User.deleted_at.is_(None))
        if status == "active":
            query = query.where(User.is_active.is_(True))
        elif status == "inactive":
            query = query.where(User.is_active.is_(False))
    if role:
        query = query.where(User.role == role)
    if search:
        term = search.lower()
        query = query.where(
            or_(
                func.lower(User.username).contains(term, autoescape=True),
                func.lower(User.full_name).contains(term, autoescape=True),
            )
        )
    query = query.order_by(User.created_at.desc())
    return [to_admin_user(row_of_user(user), n) for user, n in session.execute(query)]


def update_user(
    session: Session, user_id: str, data: AdminUserUpdate, actor: Actor, node_id: str
) -> tuple[dict[str, Any], bool]:
    user = _get_user_for_update(session, user_id)
    changes = data.model_dump(exclude_none=True)
    if user.id == actor.id and (
        changes.get("role", "admin") != "admin" or changes.get("is_active", True) is False
    ):
        raise SelfModificationError("No puedes quitarte el rol de administrador ni desactivar tu propia cuenta")

    before = row_of_user(user)
    for field, value in changes.items():
        setattr(user, field, value)
    delta = audit.diff(before, row_of_user(user), ("full_name", "role", "is_active"))
    if not delta:
        return before, False
    user.updated_at = utcnow()
    session.flush()

    if "role" in delta:
        action, what = "user.role", f"cambió el rol a '{user.role}'"
    elif "is_active" in delta:
        action, what = ("user.activate", "reactivó la cuenta") if user.is_active else ("user.deactivate", "desactivó la cuenta")
    else:
        action, what = "user.update", "editó los datos"
    extras = [f for f in delta if f not in {"role", "is_active"}]
    summary_text = f"{what[0].upper()}{what[1:]} de '{user.username}'" + (f" (además: {', '.join(extras)})" if extras and action != "user.update" else "")
    audit.record(session, actor, node_id, action, "user", user.id, summary_text, delta)
    return row_of_user(user), True


def reset_password(
    session: Session, user_id: str, password_hash: str, actor: Actor, node_id: str
) -> tuple[dict[str, Any], bool]:
    user = _get_user_for_update(session, user_id)
    user.password_hash = password_hash
    user.updated_at = utcnow()
    session.flush()
    # La contraseña (ni su hash) nunca se guarda en la auditoría.
    audit.record(session, actor, node_id, "user.password_reset", "user", user.id,
                 f"Restableció la contraseña de '{user.username}'")
    return row_of_user(user), True


def delete_user(
    session: Session, user_id: str, orders_mode: str, actor: Actor, node_id: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    if user_id == actor.id:
        raise SelfModificationError("No puedes eliminar tu propia cuenta")
    user = _get_user_for_update(session, user_id)
    now = utcnow()
    orders = session.scalars(
        select(Order).where(and_(Order.owner_id == user.id, Order.deleted_at.is_(None))).with_for_update()
    ).all()
    if orders_mode == "delete":
        for order in orders:
            order.deleted_at = now
            order.updated_at = now
            order.handled_by_node = node_id
        session.flush()
        for order in orders:  # cada pedido borrado también se replica en esta misma escritura
            queue_replication(session, Order.__table__, row_of(order))

    old_username = user.username
    user.username = f"eliminado-{user.id.replace('-', '')[:12]}"
    user.full_name = "Usuario eliminado"
    user.password_hash = UNUSABLE_PASSWORD
    user.is_active = False
    user.deleted_at = now
    user.updated_at = now
    session.flush()
    verb = "eliminando" if orders_mode == "delete" else "conservando"
    audit.record(
        session, actor, node_id, "user.delete", "user", user.id,
        f"Eliminó la cuenta '{old_username}' {verb} sus {len(orders)} pedido(s)",
        {"username": [old_username, user.username], "orders": orders_mode, "orders_affected": len(orders)},
    )
    return row_of_user(user), {"orders_mode": orders_mode, "orders_affected": len(orders)}


# ---------------------------------------------------------------------- pedidos
def list_all_orders(
    session: Session,
    include_deleted: bool = False,
    status: str | None = None,
    owner_id: str | None = None,
    search: str | None = None,
) -> list[dict[str, Any]]:
    query = select(Order, User.username).outerjoin(User, User.id == Order.owner_id)
    if not include_deleted:
        query = query.where(Order.deleted_at.is_(None))
    if status:
        query = query.where(Order.status == status)
    if owner_id:
        query = query.where(Order.owner_id == owner_id)
    if search:
        term = search.lower()
        query = query.where(
            or_(func.lower(Order.customer_name).contains(term, autoescape=True), Order.id.startswith(term))
        )
    rows = []
    for order, owner_username in session.execute(query.order_by(Order.created_at.desc())):
        row = row_of(order)
        row["owner_username"] = owner_username
        rows.append(row)
    return rows


def admin_update_order(
    session: Session, order_id: str, data: AdminOrderUpdate, actor: Actor, node_id: str
) -> tuple[dict[str, Any], bool]:
    """Corrección administrativa: cualquier campo, en cualquier estado (queda auditada)."""
    order = session.execute(select(Order).where(Order.id == order_id).with_for_update()).scalar_one_or_none()
    if order is None:
        raise OrderNotFoundError(order_id)
    if order.deleted_at is not None:
        raise AdminConflictError("El pedido está eliminado: restáuralo antes de corregirlo")
    before = row_of(order)
    changes = data.model_dump(exclude_none=True)
    if "customer_name" in changes:
        order.customer_name = changes["customer_name"]
    if "items" in changes:
        order.items = changes["items"]
        order.total = compute_total(changes["items"])
    if "status" in changes:
        order.status = changes["status"]
    delta = audit.diff(before, row_of(order), AUDITED_ORDER_FIELDS)
    if not delta:
        return before, False
    order.updated_at = utcnow()
    order.handled_by_node = node_id
    session.flush()
    audit.record(
        session, actor, node_id, "order.admin_update", "order", order_id,
        f"Corrección administrativa de #{order_id[:8]} ({', '.join(delta)})", delta,
    )
    return row_of(order), True


def restore_order(session: Session, order_id: str, actor: Actor, node_id: str) -> tuple[dict[str, Any], bool]:
    order = session.execute(select(Order).where(Order.id == order_id).with_for_update()).scalar_one_or_none()
    if order is None:
        raise OrderNotFoundError(order_id)
    if order.deleted_at is None:
        raise AdminConflictError("El pedido no está eliminado")
    order.deleted_at = None
    order.updated_at = utcnow()
    order.handled_by_node = node_id
    session.flush()
    audit.record(session, actor, node_id, "order.restore", "order", order_id,
                 f"Restauró el pedido eliminado #{order_id[:8]} de '{order.customer_name}'")
    return row_of(order), True
