"""Panel de administración de datos: /api/admin (sólo administradores).

Todas las escrituras pasan por la capa distribuida (escritura dual + failover) e incluyen su entrada de
auditoría en la misma transacción.
"""
from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Path, Query, Response

from .. import admin, audit, services
from ..config import Settings
from ..database import DatabaseCluster, WriteResult, orders_table, users_table
from ..deps import get_cluster, get_settings, require_admin
from ..schemas import AdminOrderUpdate, AdminUserUpdate, OrderStatus, PasswordReset, UserRole
from ..security import Actor, hash_password

router = APIRouter(prefix="/api/admin", tags=["Administración"], dependencies=[Depends(require_admin)])

# Un tipo por parámetro: reutilizar el MISMO objeto Path(...) con nombres distintos hace que FastAPI
# busque el nombre del primer uso (las rutas de pedidos pedían 'user_id').
UserId = Annotated[str, Path(min_length=1, max_length=36, description="Id del usuario")]
OrderId = Annotated[str, Path(min_length=1, max_length=36, description="Id del pedido")]


def _envelope(settings: Settings, result: WriteResult, data: dict, response: Response) -> dict:
    response.headers["X-DB-Used"] = result.db_used
    response.headers["X-Replication"] = result.replication
    return {"node": settings.node_id, "db_used": result.db_used, "replication": result.replication, "data": data}


def _read(settings: Settings, cluster: DatabaseCluster, response: Response, fn) -> dict:
    data, db_used = cluster.read(fn)
    response.headers["X-DB-Used"] = db_used
    return {"node": settings.node_id, "db_used": db_used, "data": data}


# ---------------------------------------------------------------------- resumen y auditoría
@router.get("/summary", summary="Totales de usuarios, pedidos y auditoría")
def admin_summary(response: Response, cluster: DatabaseCluster = Depends(get_cluster), settings: Settings = Depends(get_settings)):
    return _read(settings, cluster, response, admin.summary)


@router.get("/audit", summary="Registro de auditoría (más reciente primero)")
def admin_audit(
    response: Response,
    entity: Literal["order", "user"] | None = None,
    entity_id: str | None = Query(None, max_length=36),
    actor: str | None = Query(None, max_length=50),
    limit: int = Query(100, ge=1, le=500),
    cluster: DatabaseCluster = Depends(get_cluster),
    settings: Settings = Depends(get_settings),
):
    return _read(settings, cluster, response, lambda s: audit.list_audit(s, entity, entity_id, actor, limit))


# ---------------------------------------------------------------------- usuarios
@router.get("/users", summary="Usuarios con su número de pedidos activos")
def admin_list_users(
    response: Response,
    search: str | None = Query(None, max_length=120),
    role: UserRole | None = None,
    status: Literal["active", "inactive", "deleted"] | None = None,
    cluster: DatabaseCluster = Depends(get_cluster),
    settings: Settings = Depends(get_settings),
):
    return _read(settings, cluster, response, lambda s: admin.list_users(s, search, role, status))


@router.patch("/users/{user_id}", summary="Editar nombre, rol o estado (activo/desactivado) de un usuario")
def admin_update_user(
    payload: AdminUserUpdate,
    user_id: UserId,
    response: Response,
    actor: Actor = Depends(require_admin),
    cluster: DatabaseCluster = Depends(get_cluster),
    settings: Settings = Depends(get_settings),
):
    result = cluster.write(lambda s: admin.update_user(s, user_id, payload, actor, settings.node_id), table=users_table)
    return _envelope(settings, result, {**admin.to_admin_user(result.row), "changed": result.info}, response)


@router.post("/users/{user_id}/reset-password", summary="Restablecer la contraseña de un usuario")
def admin_reset_password(
    payload: PasswordReset,
    user_id: UserId,
    response: Response,
    actor: Actor = Depends(require_admin),
    cluster: DatabaseCluster = Depends(get_cluster),
    settings: Settings = Depends(get_settings),
):
    password_hash = hash_password(payload.new_password, settings.password_hash_iterations)
    result = cluster.write(
        lambda s: admin.reset_password(s, user_id, password_hash, actor, settings.node_id), table=users_table
    )
    return _envelope(settings, result, admin.to_admin_user(result.row), response)


@router.delete("/users/{user_id}", summary="Eliminar (anonimizar) una cuenta, conservando o eliminando sus pedidos")
def admin_delete_user(
    user_id: UserId,
    response: Response,
    orders: Literal["keep", "delete"] = Query("keep", description="Qué hacer con sus pedidos activos"),
    actor: Actor = Depends(require_admin),
    cluster: DatabaseCluster = Depends(get_cluster),
    settings: Settings = Depends(get_settings),
):
    result = cluster.write(
        lambda s: admin.delete_user(s, user_id, orders, actor, settings.node_id), table=users_table
    )
    return _envelope(settings, result, {**admin.to_admin_user(result.row), **result.info}, response)


# ---------------------------------------------------------------------- pedidos
@router.get("/orders", summary="Todos los pedidos, opcionalmente incluidos los eliminados")
def admin_list_orders(
    response: Response,
    include_deleted: bool = False,
    status: OrderStatus | None = None,
    owner_id: str | None = Query(None, max_length=36),
    search: str | None = Query(None, max_length=120),
    cluster: DatabaseCluster = Depends(get_cluster),
    settings: Settings = Depends(get_settings),
):
    def fn(session):
        return [
            {**services.to_public(row), "deleted_at": row["deleted_at"]}
            for row in admin.list_all_orders(session, include_deleted, status, owner_id, search)
        ]

    return _read(settings, cluster, response, fn)


@router.patch("/orders/{order_id}", summary="Corrección administrativa de un pedido (cualquier campo y estado)")
def admin_update_order(
    payload: AdminOrderUpdate,
    order_id: OrderId,
    response: Response,
    actor: Actor = Depends(require_admin),
    cluster: DatabaseCluster = Depends(get_cluster),
    settings: Settings = Depends(get_settings),
):
    result = cluster.write(
        lambda s: admin.admin_update_order(s, order_id, payload, actor, settings.node_id), table=orders_table
    )
    return _envelope(settings, result, {**services.to_public(result.row), "changed": result.info}, response)


@router.post("/orders/{order_id}/restore", summary="Restaurar un pedido eliminado")
def admin_restore_order(
    order_id: OrderId,
    response: Response,
    actor: Actor = Depends(require_admin),
    cluster: DatabaseCluster = Depends(get_cluster),
    settings: Settings = Depends(get_settings),
):
    result = cluster.write(lambda s: admin.restore_order(s, order_id, actor, settings.node_id), table=orders_table)
    return _envelope(settings, result, services.to_public(result.row), response)
