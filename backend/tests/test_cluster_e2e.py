"""Pruebas end-to-end contra el clúster Docker real (NGINX + 2 nodos + 2 PostgreSQL).

Se omiten salvo que se defina ``CLUSTER_BASE_URL``, p. ej.::

    CLUSTER_BASE_URL=http://localhost:8080 python -m pytest tests/test_cluster_e2e.py -v
"""
from __future__ import annotations

import os
import time

import httpx
import pytest

BASE_URL = os.getenv("CLUSTER_BASE_URL")
NODES = {"Nodo-Alpha-01", "Nodo-Beta-02"}

pytestmark = [
    pytest.mark.cluster,
    pytest.mark.skipif(not BASE_URL, reason="Define CLUSTER_BASE_URL (p. ej. http://localhost:8080) con el clúster arriba"),
]

ITEMS = [{"product": "Caja e2e", "quantity": 2, "unit_price": 10.0}]


def _reset(http: httpx.Client) -> None:
    for node in NODES:
        http.put(f"/api/chaos/crash/{node}", json={"enabled": False})
    http.put("/api/chaos/db-primary", json={"enabled": False})
    time.sleep(0.5)


@pytest.fixture
def http():
    with httpx.Client(base_url=BASE_URL, timeout=15) as client:
        _reset(client)
        yield client
        _reset(client)


def test_e2e_round_robin_alterna_entre_los_dos_nodos(http):
    handled = []
    for _ in range(6):
        response = http.get("/api/orders")
        assert response.status_code == 200
        handled.append(response.headers["X-Handled-By-Node"])
    assert set(handled) == NODES, handled
    # Round-Robin: reparto equilibrado (tolera peticiones ajenas, p. ej. el dashboard abierto).
    assert all(handled.count(node) >= 2 for node in NODES), handled


def test_e2e_caida_de_nodo_es_transparente_para_el_cliente(http):
    assert http.put("/api/chaos/crash/Nodo-Alpha-01", json={"enabled": True}).json()["simulate_crash"] is True

    upstream_statuses = []
    for _ in range(8):
        response = http.get("/api/orders")
        assert response.status_code == 200  # el cliente nunca ve el 503
        assert response.headers["X-Handled-By-Node"] == "Nodo-Beta-02"
        upstream_statuses.append(response.headers.get("X-Upstream-Status", ""))
    assert any("503" in status for status in upstream_statuses), upstream_statuses  # NGINX reintentó

    created = http.post("/api/orders", json={"customer_name": "E2E caída de nodo", "items": ITEMS})
    assert created.status_code == 201
    assert created.headers["X-Handled-By-Node"] == "Nodo-Beta-02"
    http.put("/api/chaos/crash/Nodo-Alpha-01", json={"enabled": False})
    assert http.delete(f"/api/orders/{created.json()['data']['id']}").status_code == 200


def test_e2e_failover_de_bd_primaria_y_reconciliacion(http):
    down = http.put("/api/chaos/db-primary", json={"enabled": True}).json()
    assert all(p["ok"] for p in down["propagated_to"]) and len(down["propagated_to"]) == 2

    for _ in range(2):  # ambos nodos conmutan a la réplica
        listed = http.get("/api/orders")
        assert listed.status_code == 200
        assert listed.json()["db_used"] == "replica_failover"

    created = http.post("/api/orders", json={"customer_name": "E2E failover BD", "items": ITEMS})
    assert created.status_code == 201
    assert created.json()["db_used"] == "replica_failover"
    assert created.json()["replication"] == "pending"
    order_id = created.json()["data"]["id"]
    assert http.get("/api/replication/status").json()["in_sync"] is False

    up = http.put("/api/chaos/db-primary", json={"enabled": False}).json()
    assert up["sync"]["status"] == "ok"
    status = http.get("/api/replication/status").json()
    assert status["in_sync"] is True, status

    fetched = http.get(f"/api/orders/{order_id}")
    assert fetched.status_code == 200
    assert fetched.json()["db_used"] == "primary"
    assert http.delete(f"/api/orders/{order_id}").status_code == 200


def test_e2e_gateway_sirve_el_frontend_react(http):
    home = http.get("/")
    assert home.status_code == 200
    assert "text/html" in home.headers["content-type"]
    assert '<div id="root">' in home.text

    spa_route = http.get("/ruta/del/frontend")  # try_files -> index.html
    assert spa_route.status_code == 200
    assert '<div id="root">' in spa_route.text
