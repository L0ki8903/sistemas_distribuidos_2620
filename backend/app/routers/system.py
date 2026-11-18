"""Salud del nodo y estado de la replicación."""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Query

from .. import __version__
from ..chaos import ChaosState
from ..config import Settings
from ..database import DatabaseCluster
from ..deps import get_chaos, get_cluster, get_settings

router = APIRouter(tags=["Sistema"])
STARTED_AT = time.time()


@router.get("/api/health", summary="Salud del nodo (deep=true incluye ping a ambas BD)")
def health(
    deep: bool = Query(True, description="false = sólo liveness, sin consultar las bases"),
    chaos: ChaosState = Depends(get_chaos),
    cluster: DatabaseCluster = Depends(get_cluster),
    settings: Settings = Depends(get_settings),
):
    body = {
        "status": "ok",
        "node": settings.node_id,
        "version": __version__,
        "uptime_seconds": round(time.time() - STARTED_AT, 1),
        "simulate_crash": chaos.simulate_crash,
        "simulate_db_primary_down": chaos.simulate_db_primary_down,
    }
    if deep:
        body["databases"] = cluster.health()
    return body


@router.get("/api/replication/status", summary="Comparación db-primary vs db-replica")
def replication_status(cluster: DatabaseCluster = Depends(get_cluster)):
    return cluster.replication_status()


@router.post("/api/replication/sync", summary="Forzar reconciliación primaria <-> réplica")
def replication_sync(cluster: DatabaseCluster = Depends(get_cluster)):
    return cluster.reconcile(reason="manual")
