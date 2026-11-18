"""CRUD de pedidos: /api/orders (requiere sesión; ver permisos en ``services.py``)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Path, Query, Response

from .. import services
from ..config import Settings
from ..database import DatabaseCluster, WriteResult
from ..deps import get_cluster, get_current_actor, get_settings
from ..schemas import (
    DeleteResponse,
    OrderCreate,
    OrderDetailResponse,
    OrderListResponse,
    OrderResponse,
    OrderStatus,
    OrderUpdate,
)
from ..security import Actor

router = APIRouter(prefix="/api/orders", tags=["Pedidos"])

OrderId = Path(..., min_length=1, max_length=36, description="UUID del pedido")


def _headers(response: Response, db_used: str, replication: str | None = None) -> None:
    response.headers["X-DB-Used"] = db_used
    if replication:
        response.headers["X-Replication"] = replication


def _envelope(settings: Settings, result: WriteResult, response: Response) -> dict:
    _headers(response, result.db_used, result.replication)
    return {
        "node": settings.node_id,
        "db_used": result.db_used,
        "replication": result.replication,
        "data": services.to_public(result.row),
    }


@router.get("", response_model=OrderListResponse, summary="Listar pedidos (admin: todos · user: los suyos)")
def list_orders(
    response: Response,
    status: OrderStatus | None = Query(None, description="Filtrar por estado"),
    customer: str | None = Query(None, max_length=120, description="Buscar por nombre de cliente"),
    actor: Actor = Depends(get_current_actor),
    cluster: DatabaseCluster = Depends(get_cluster),
    settings: Settings = Depends(get_settings),
):
    rows, db_used = cluster.read(
        lambda session: services.list_orders(session, actor, status=status, customer=customer)
    )
    _headers(response, db_used)
    return {
        "node": settings.node_id,
        "db_used": db_used,
        "count": len(rows),
        "data": [services.to_public(row) for row in rows],
    }


@router.get("/{order_id}", response_model=OrderDetailResponse, summary="Obtener un pedido")
def get_order(
    response: Response,
    order_id: str = OrderId,
    actor: Actor = Depends(get_current_actor),
    cluster: DatabaseCluster = Depends(get_cluster),
    settings: Settings = Depends(get_settings),
):
    row, db_used = cluster.read(lambda session: services.get_order(session, order_id, actor))
    _headers(response, db_used)
    return {"node": settings.node_id, "db_used": db_used, "data": services.to_public(row)}


@router.post("", status_code=201, response_model=OrderResponse, summary="Crear pedido (queda a nombre del usuario)")
def create_order(
    payload: OrderCreate,
    response: Response,
    actor: Actor = Depends(get_current_actor),
    cluster: DatabaseCluster = Depends(get_cluster),
    settings: Settings = Depends(get_settings),
):
    result = cluster.write(lambda session: services.create_order(session, payload, settings.node_id, actor))
    if not result.info:  # el id ya existía: reintento idempotente
        response.status_code = 200
        response.headers["X-Idempotent-Replay"] = "true"
    return _envelope(settings, result, response)


@router.patch(
    "/{order_id}",
    response_model=OrderResponse,
    summary="Editar pedido (dueño o admin, sólo 'pendiente') o cambiar estado (sólo admin)",
)
def update_order(
    payload: OrderUpdate,
    response: Response,
    order_id: str = OrderId,
    actor: Actor = Depends(get_current_actor),
    cluster: DatabaseCluster = Depends(get_cluster),
    settings: Settings = Depends(get_settings),
):
    result = cluster.write(
        lambda session: services.update_order(session, order_id, payload, settings.node_id, actor)
    )
    return _envelope(settings, result, response)


@router.delete(
    "/{order_id}",
    response_model=DeleteResponse,
    summary="Eliminar pedido (admin: cualquiera · user: los suyos en 'pendiente')",
)
def delete_order(
    response: Response,
    order_id: str = OrderId,
    actor: Actor = Depends(get_current_actor),
    cluster: DatabaseCluster = Depends(get_cluster),
    settings: Settings = Depends(get_settings),
):
    result = cluster.write(lambda session: services.delete_order(session, order_id, settings.node_id, actor))
    _headers(response, result.db_used, result.replication)
    return {
        "node": settings.node_id,
        "db_used": result.db_used,
        "replication": result.replication,
        "data": {"id": result.row["id"], "deleted": True, "already_deleted": bool(result.info)},
    }
