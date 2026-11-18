"""Reglas de negocio de pedidos (independientes de qué base de datos atiende la operación).

Autorización (la aplica el servidor, no sólo la interfaz):

| Acción                         | user (cliente)                       | admin          |
|--------------------------------|--------------------------------------|----------------|
| Listar / ver                   | sólo sus pedidos                     | todos          |
| Crear                          | sí (queda como dueño)                | sí             |
| Editar cliente / ítems         | sus pedidos en estado ``pendiente``  | en ``pendiente`` |
| Cambiar estado                 | no (403)                             | sí             |
| Eliminar                       | sus pedidos en estado ``pendiente``  | cualquiera     |

Un pedido ajeno se trata como inexistente (404) para no revelar que existe.
"""
from __future__ import annotations

import copy
import uuid
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import ORDER_COLUMNS, Order, User, utcnow
from .schemas import OrderCreate, OrderUpdate
from .security import Actor, PermissionDeniedError

# Máquina de estados: sólo se permite avanzar al siguiente estado.
NEXT_STATUS: dict[str, str | None] = {
    "pendiente": "en proceso",
    "en proceso": "enviado",
    "enviado": "entregado",
    "entregado": None,
}
EDITABLE_STATUSES = {"pendiente"}


class OrderNotFoundError(Exception):
    def __init__(self, order_id: str):
        super().__init__(f"Pedido {order_id} no encontrado")
        self.order_id = order_id


class InvalidTransitionError(Exception):
    def __init__(self, current: str, requested: str):
        allowed = NEXT_STATUS.get(current)
        hint = f"el único estado siguiente permitido es '{allowed}'" if allowed else "el pedido ya fue entregado"
        super().__init__(f"Transición inválida '{current}' -> '{requested}': {hint}")
        self.current = current
        self.requested = requested
        self.allowed = allowed


class OrderNotEditableError(Exception):
    def __init__(self, status: str, message: str | None = None):
        super().__init__(
            message or f"Sólo se pueden editar cliente/ítems de pedidos 'pendiente' (estado actual: '{status}')"
        )


class OrderIdConflictError(Exception):
    def __init__(self, order_id: str, message: str | None = None):
        super().__init__(message or f"El id {order_id} pertenece a un pedido eliminado")


def row_of(order: Order) -> dict[str, Any]:
    """Estado completo de la fila (lo que se replica a la otra base)."""
    return {name: copy.deepcopy(getattr(order, name)) for name in ORDER_COLUMNS}


def to_public(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "customer_name": row["customer_name"],
        "items": row["items"],
        "total": float(row["total"]),
        "status": row["status"],
        "next_status": NEXT_STATUS.get(row["status"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "handled_by_node": row["handled_by_node"],
        "owner_id": row.get("owner_id"),
        "owner_username": row.get("owner_username"),
    }


def compute_total(items: list[dict[str, Any]]) -> Decimal:
    total = sum((Decimal(str(item["unit_price"])) * item["quantity"] for item in items), Decimal("0"))
    return total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _can_see(order: Order, actor: Actor) -> bool:
    return actor.is_admin or order.owner_id == actor.id


def _get_for_update(session: Session, order_id: str) -> Order | None:
    # FOR UPDATE serializa transiciones concurrentes en PostgreSQL (SQLite lo ignora).
    return session.execute(select(Order).where(Order.id == order_id).with_for_update()).scalar_one_or_none()


def list_orders(
    session: Session, actor: Actor, status: str | None = None, customer: str | None = None
) -> list[dict[str, Any]]:
    query = (
        select(Order, User.username)
        .outerjoin(User, User.id == Order.owner_id)
        .where(Order.deleted_at.is_(None))
    )
    if not actor.is_admin:
        query = query.where(Order.owner_id == actor.id)
    if status:
        query = query.where(Order.status == status)
    if customer:
        query = query.where(func.lower(Order.customer_name).contains(customer.lower(), autoescape=True))
    query = query.order_by(Order.created_at.desc())
    rows = []
    for order, owner_username in session.execute(query):
        row = row_of(order)
        row["owner_username"] = owner_username
        rows.append(row)
    return rows


def get_order(session: Session, order_id: str, actor: Actor) -> dict[str, Any]:
    order = session.get(Order, order_id)
    if order is None or order.deleted_at is not None or not _can_see(order, actor):
        raise OrderNotFoundError(order_id)
    return row_of(order)


def create_order(session: Session, data: OrderCreate, node_id: str, actor: Actor) -> tuple[dict[str, Any], bool]:
    """Devuelve ``(fila, creado)``. Si el id ya existe se devuelve el pedido existente (idempotencia)."""
    order_id = str(data.id or uuid.uuid4())
    existing = session.get(Order, order_id)
    if existing is not None:
        if existing.deleted_at is not None:
            raise OrderIdConflictError(order_id)
        if not _can_see(existing, actor):
            raise OrderIdConflictError(order_id, f"El id {order_id} ya está en uso")
        return row_of(existing), False

    items = [item.model_dump() for item in data.items]
    now = utcnow()
    order = Order(
        id=order_id,
        customer_name=data.customer_name,
        items=items,
        total=compute_total(items),
        status="pendiente",
        created_at=now,
        updated_at=now,
        deleted_at=None,
        handled_by_node=node_id,
        owner_id=actor.id,
    )
    session.add(order)
    session.flush()
    return row_of(order), True


def update_order(
    session: Session, order_id: str, data: OrderUpdate, node_id: str, actor: Actor
) -> tuple[dict[str, Any], bool]:
    """Devuelve ``(fila, cambió)``. Aplica permisos, la máquina de estados y la regla de edición."""
    order = _get_for_update(session, order_id)
    if order is None or order.deleted_at is not None or not _can_see(order, actor):
        raise OrderNotFoundError(order_id)

    changes = data.model_dump(exclude_none=True)
    new_status = changes.get("status")
    if new_status is not None and new_status != order.status and not actor.is_admin:
        raise PermissionDeniedError("Sólo un administrador puede cambiar el estado de un pedido")

    changed = False
    if "customer_name" in changes or "items" in changes:
        if order.status not in EDITABLE_STATUSES:
            raise OrderNotEditableError(order.status)
        if "customer_name" in changes and changes["customer_name"] != order.customer_name:
            order.customer_name = changes["customer_name"]
            changed = True
        if "items" in changes:
            order.items = changes["items"]
            order.total = compute_total(changes["items"])
            changed = True

    if new_status is not None and new_status != order.status:
        if NEXT_STATUS.get(order.status) != new_status:
            raise InvalidTransitionError(order.status, new_status)
        order.status = new_status
        changed = True

    if changed:
        order.updated_at = utcnow()
        order.handled_by_node = node_id
        session.flush()
    return row_of(order), changed


def delete_order(session: Session, order_id: str, node_id: str, actor: Actor) -> tuple[dict[str, Any], bool]:
    """Borrado lógico (tombstone). Devuelve ``(fila, ya_estaba_eliminado)``; es idempotente."""
    order = _get_for_update(session, order_id)
    if order is None or not _can_see(order, actor):
        raise OrderNotFoundError(order_id)
    if order.deleted_at is not None:
        return row_of(order), True
    if not actor.is_admin and order.status not in EDITABLE_STATUSES:
        raise OrderNotEditableError(
            order.status, f"Sólo puedes cancelar pedidos en estado 'pendiente' (estado actual: '{order.status}')"
        )
    now = utcnow()
    order.deleted_at = now
    order.updated_at = now
    order.handled_by_node = node_id
    session.flush()
    return row_of(order), False
