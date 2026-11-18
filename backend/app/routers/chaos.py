"""Chaos Engineering: simular la caída de un nodo o de db-primary.

Detrás de NGINX no se puede elegir qué nodo recibe una petición, así que los endpoints con
``{target_node}`` reenvían la orden por REST al nodo correcto (comunicación entre nodos).
"""
from __future__ import annotations

import logging
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request

from ..chaos import ChaosState
from ..config import Settings
from ..database import DatabaseCluster
from ..deps import get_chaos, get_cluster, get_peers, get_settings
from ..peers import FORWARDED_HEADER, PeerClient
from ..schemas import Switch

logger = logging.getLogger("pedidos.chaos")
router = APIRouter(prefix="/api/chaos", tags=["Chaos Engineering"])


def _forwarded(request: Request) -> bool:
    return FORWARDED_HEADER in request.headers


def _peer_error_detail(response: httpx.Response) -> str:
    try:
        return response.json().get("detail") or response.text
    except ValueError:
        return response.text


def _set_crash(
    target_node: str,
    enabled: bool | None,
    request: Request,
    chaos: ChaosState,
    peers: PeerClient,
    settings: Settings,
) -> dict:
    """``enabled=None`` alterna el estado; ``True/False`` lo fija explícitamente."""
    if target_node == settings.node_id:
        new_value = (not chaos.simulate_crash) if enabled is None else enabled
        chaos.set_crash(new_value)
        logger.warning("CHAOS SIMULATE_CRASH=%s en %s", new_value, settings.node_id)
        return {"applied_by": settings.node_id, "forwarded": False, **chaos.snapshot()}

    if _forwarded(request):  # nunca reenviar dos veces (evita bucles)
        raise HTTPException(status_code=404, detail=f"'{target_node}' no es este nodo ({settings.node_id})")

    peer = peers.find(target_node)
    if peer is None:
        known = [settings.node_id, *(p.name for p in peers.peers if p.name)]
        raise HTTPException(status_code=404, detail=f"Nodo desconocido '{target_node}'. Nodos conocidos: {known}")

    path_target = quote(target_node, safe="")
    try:
        if enabled is None:
            response = peers.request(peer, "POST", f"/api/chaos/toggle/{path_target}", settings.node_id)
        else:
            response = peers.request(
                peer, "PUT", f"/api/chaos/crash/{path_target}", settings.node_id, json={"enabled": enabled}
            )
    except httpx.HTTPError as exc:
        # 409 (no 5xx) para que NGINX no reintente la orden de caos en otro nodo.
        raise HTTPException(status_code=409, detail=f"No se pudo contactar a {target_node} ({peer.url}): {exc}")
    if response.status_code >= 400:
        status_code = response.status_code if response.status_code < 500 else 409
        raise HTTPException(status_code=status_code, detail=_peer_error_detail(response))
    return {**response.json(), "forwarded": True, "forwarded_by": settings.node_id}


def _apply_db_flag(
    enabled: bool,
    propagate: bool,
    chaos: ChaosState,
    cluster: DatabaseCluster,
    peers: PeerClient,
    settings: Settings,
) -> dict:
    """La BD primaria es compartida: la simulación se propaga a todos los nodos."""
    changed = chaos.set_db_primary_down(enabled)
    logger.warning("CHAOS SIMULATE_DB_PRIMARY_DOWN=%s en %s", enabled, settings.node_id)
    # Al restaurar, este nodo reconcilia ANTES de avisar a los demás: así la respuesta informa
    # cuántas filas escritas durante la caída se copiaron de vuelta a la primaria.
    sync = cluster.reconcile(reason="restauración de db-primary") if not enabled else None
    propagation: list[dict] = [{"node": settings.node_id, "ok": True}]
    if propagate:
        for peer in peers.peers:
            label = peer.name or peer.url
            try:
                response = peers.request(
                    peer, "PUT", "/api/chaos/db-primary", settings.node_id, json={"enabled": enabled}
                )
                response.raise_for_status()
                propagation.append({"node": label, "ok": True, "sync": response.json().get("sync")})
            except (httpx.HTTPError, ValueError) as exc:
                propagation.append({"node": label, "ok": False, "error": str(exc) or exc.__class__.__name__})
    return {
        "simulate_db_primary_down": enabled,
        "changed": changed,
        "applied_by": settings.node_id,
        "propagated_to": propagation,
        "sync": sync,
    }


@router.get("/local", summary="Estado de caos de ESTE nodo (usado entre nodos)")
def local_state(chaos: ChaosState = Depends(get_chaos)):
    return chaos.snapshot()


@router.get("/status", summary="Vista del clúster: nodos, banderas de caos y bases de datos")
def cluster_status(
    chaos: ChaosState = Depends(get_chaos),
    cluster: DatabaseCluster = Depends(get_cluster),
    peers: PeerClient = Depends(get_peers),
    settings: Settings = Depends(get_settings),
):
    nodes = [{**chaos.snapshot(), "reachable": True, "self": True, "url": None}]
    nodes += [{**peers.local_state(peer, settings.node_id), "self": False} for peer in peers.peers]
    return {
        "reporter": settings.node_id,
        "nodes": nodes,
        "simulate_db_primary_down": chaos.simulate_db_primary_down,
        "databases": cluster.health(),
    }


@router.post("/toggle", summary="Alternar SIMULATE_CRASH en el nodo que recibe la petición")
def toggle_current_node(
    request: Request,
    chaos: ChaosState = Depends(get_chaos),
    peers: PeerClient = Depends(get_peers),
    settings: Settings = Depends(get_settings),
):
    return _set_crash(settings.node_id, None, request, chaos, peers, settings)


@router.post("/toggle/{target_node}", summary="Alternar SIMULATE_CRASH en un nodo concreto")
def toggle_node(
    target_node: str,
    request: Request,
    chaos: ChaosState = Depends(get_chaos),
    peers: PeerClient = Depends(get_peers),
    settings: Settings = Depends(get_settings),
):
    return _set_crash(target_node, None, request, chaos, peers, settings)


@router.put("/crash/{target_node}", summary="Fijar SIMULATE_CRASH (idempotente) en un nodo concreto")
def set_node_crash(
    target_node: str,
    body: Switch,
    request: Request,
    chaos: ChaosState = Depends(get_chaos),
    peers: PeerClient = Depends(get_peers),
    settings: Settings = Depends(get_settings),
):
    return _set_crash(target_node, body.enabled, request, chaos, peers, settings)


@router.post("/db-primary/toggle", summary="Alternar SIMULATE_DB_PRIMARY_DOWN en todo el clúster")
def toggle_db_primary(
    request: Request,
    chaos: ChaosState = Depends(get_chaos),
    cluster: DatabaseCluster = Depends(get_cluster),
    peers: PeerClient = Depends(get_peers),
    settings: Settings = Depends(get_settings),
):
    return _apply_db_flag(
        not chaos.simulate_db_primary_down, not _forwarded(request), chaos, cluster, peers, settings
    )


@router.put("/db-primary", summary="Fijar SIMULATE_DB_PRIMARY_DOWN (idempotente) en todo el clúster")
def set_db_primary(
    body: Switch,
    request: Request,
    chaos: ChaosState = Depends(get_chaos),
    cluster: DatabaseCluster = Depends(get_cluster),
    peers: PeerClient = Depends(get_peers),
    settings: Settings = Depends(get_settings),
):
    return _apply_db_flag(body.enabled, not _forwarded(request), chaos, cluster, peers, settings)
