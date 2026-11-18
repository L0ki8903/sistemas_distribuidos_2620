"""Monitoreo de nodos (sólo administradores).

- ``GET /api/monitoring/local``: métricas y eventos de ESTE nodo.
- ``GET /api/monitoring/cluster``: el nodo que recibe la petición consulta ``/local`` de los demás
  nodos por REST (con el mismo token), el ``stub_status`` de NGINX y el estado de ambas bases, y lo
  combina en una sola vista. Un nodo que no responde aparece como inalcanzable, sin romper la vista.
"""
from __future__ import annotations

import os
import platform
import time

import httpx
from fastapi import APIRouter, Depends, Query, Request

from .. import __version__
from ..chaos import ChaosState
from ..config import Settings
from ..database import DatabaseCluster
from ..deps import forwarded_auth_headers, get_chaos, get_cluster, get_metrics, get_peers, get_settings, require_admin
from ..metrics import NodeMetrics, parse_stub_status
from ..peers import PeerClient

router = APIRouter(prefix="/api/monitoring", tags=["Monitoreo"], dependencies=[Depends(require_admin)])

WindowQuery = Query(300, ge=60, le=900, description="Ventana de análisis en segundos (60-900)")


def local_snapshot(
    settings: Settings, chaos: ChaosState, cluster: DatabaseCluster, metrics: NodeMetrics, window: int
) -> dict:
    return {
        "node": settings.node_id,
        "version": __version__,
        "python": platform.python_version(),
        "pid": os.getpid(),
        "chaos": chaos.snapshot(),
        "database": {
            "failover_count": cluster.failover_count,
            "circuit_breaker": "open" if cluster.primary_circuit_open() else "closed",
            "needs_resync": cluster.needs_resync,
            "last_primary_error": cluster.last_primary_error,
            "last_primary_error_at": cluster.last_primary_error_at,
            "last_sync": cluster.last_sync,
        },
        **metrics.snapshot(window),
    }


def fetch_gateway_status(client: httpx.Client, url: str | None) -> dict:
    """Lee el ``stub_status`` de NGINX (puerto interno, no publicado)."""
    if not url:
        return {"configured": False}
    try:
        response = client.get(url)
        response.raise_for_status()
        return {"configured": True, "reachable": True, **parse_stub_status(response.text)}
    except (httpx.HTTPError, ValueError) as exc:
        return {"configured": True, "reachable": False, "error": str(exc) or exc.__class__.__name__}


@router.get("/local", summary="Métricas y eventos de ESTE nodo")
def monitoring_local(
    window: int = WindowQuery,
    settings: Settings = Depends(get_settings),
    chaos: ChaosState = Depends(get_chaos),
    cluster: DatabaseCluster = Depends(get_cluster),
    metrics: NodeMetrics = Depends(get_metrics),
):
    return local_snapshot(settings, chaos, cluster, metrics, window)


@router.get("/cluster", summary="Vista agregada: todos los nodos, gateway y bases de datos")
def monitoring_cluster(
    request: Request,
    window: int = WindowQuery,
    settings: Settings = Depends(get_settings),
    chaos: ChaosState = Depends(get_chaos),
    cluster: DatabaseCluster = Depends(get_cluster),
    metrics: NodeMetrics = Depends(get_metrics),
    peers: PeerClient = Depends(get_peers),
):
    nodes = [{**local_snapshot(settings, chaos, cluster, metrics, window), "reachable": True, "self": True}]
    auth = forwarded_auth_headers(request)
    for peer in peers.peers:
        try:
            response = peers.request(
                peer, "GET", f"/api/monitoring/local?window={window}", settings.node_id, headers=auth
            )
            if response.status_code in (401, 403):
                raise httpx.HTTPError(f"el nodo rechazó la autenticación (HTTP {response.status_code})")
            response.raise_for_status()
            nodes.append({**response.json(), "reachable": True, "self": False})
        except (httpx.HTTPError, ValueError) as exc:
            nodes.append({
                "node": peer.name or peer.url,
                "reachable": False,
                "self": False,
                "error": str(exc) or exc.__class__.__name__,
            })

    reachable = [n for n in nodes if n["reachable"]]
    replication = cluster.replication_status()
    window_requests = sum(n["traffic"]["window_requests"] for n in reachable)
    window_errors = sum(n["traffic"]["window_server_errors"] for n in reachable)
    p95_values = [n["latency_ms"]["p95"] for n in reachable if n["latency_ms"]["p95"] is not None]
    summary = {
        "nodes_total": len(nodes),
        "nodes_up": sum(1 for n in reachable if not n["chaos"]["simulate_crash"]),
        "requests_per_minute": sum(n["traffic"]["requests_per_minute"] for n in reachable),
        "window_requests": window_requests,
        "window_server_errors": window_errors,
        "error_rate": round(window_errors / window_requests, 4) if window_requests else 0.0,
        "latency_p95_worst_ms": max(p95_values) if p95_values else None,
        "db_failovers": sum(n["database"]["failover_count"] for n in reachable),
        "window_replica_served": sum(n["traffic"]["window_replica_served"] for n in reachable),
        "in_sync": replication["in_sync"],
    }
    events = sorted(
        ({**event, "node": n["node"]} for n in reachable for event in n["events"]),
        key=lambda e: e["at"],
        reverse=True,
    )[:80]
    return {
        "reporter": settings.node_id,
        "generated_at": time.time(),
        "window_seconds": window,
        "summary": summary,
        "nodes": nodes,
        "gateway": fetch_gateway_status(peers.client, settings.gateway_status_url),
        "databases": {"health": cluster.health(), "replication": replication},
        "events": events,
    }
