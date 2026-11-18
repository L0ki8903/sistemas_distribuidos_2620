"""Pruebas del panel de monitoreo de nodos (métricas, eventos, vista agregada y gateway)."""
from __future__ import annotations

import httpx
from fastapi.testclient import TestClient

from app.config import Peer
from app.main import create_app
from app.metrics import NodeMetrics, parse_stub_status
from app.peers import PeerClient
from app.routers.monitoring import fetch_gateway_status
from app.users import admin_user_id
from tests.conftest import USER_PASSWORD, authenticate, bearer, create_sample_order, login, register, token_for

STUB_STATUS = """Active connections: 3
server accepts handled requests
 120 120 457
Reading: 0 Writing: 1 Waiting: 2
"""


def local(client: TestClient, window: int = 300) -> dict:
    response = client.get("/api/monitoring/local", params={"window": window})
    assert response.status_code == 200, response.text
    return response.json()


def test_monitoreo_es_solo_para_administradores(raw_client):
    user = bearer(register(raw_client, "monitor.cliente")["access_token"])
    for path in ("/api/monitoring/local", "/api/monitoring/cluster"):
        assert raw_client.get(path).status_code == 401
        assert raw_client.get(path, headers=user).status_code == 403
        assert raw_client.get(path, headers=bearer(login(raw_client))).status_code == 200


def test_metricas_cuentan_peticiones_por_estado_y_latencia(client):
    before = local(client)["traffic"]["total"]
    for _ in range(3):
        assert client.get("/api/orders").status_code == 200
    create_sample_order(client)
    assert client.get("/api/orders/no-existe").status_code == 404
    assert client.post("/api/orders", json={"customer_name": "X", "items": []}).status_code == 422

    snapshot = local(client)
    total = snapshot["traffic"]["total"]
    assert total["requests"] - before["requests"] == 6
    assert total["ok"] - before["ok"] == 4
    assert total["client_errors"] - before["client_errors"] == 2
    assert snapshot["traffic"]["by_group"]["orders"] >= 6
    assert snapshot["traffic"]["requests_per_minute"] >= 6
    latency = snapshot["latency_ms"]
    assert latency["samples"] >= 6 and latency["p50"] <= latency["p95"] <= latency["max"]
    assert snapshot["timeseries"][-1]["requests"] >= 1  # cubo actual de 10 s
    assert snapshot["bucket_seconds"] == 10 and len(snapshot["timeseries"]) == 30  # 300 s / 10 s


def test_las_consultas_de_monitoreo_y_salud_no_inflan_las_metricas(client):
    first = local(client)["traffic"]["total"]["requests"]
    for _ in range(5):
        client.get("/api/health")
        client.get("/api/monitoring/cluster")
    assert local(client)["traffic"]["total"]["requests"] == first


def test_caidas_simuladas_lecturas_desde_replica_y_eventos(client):
    client.put("/api/chaos/crash/Nodo-Alpha-01", json={"enabled": True})
    assert client.get("/api/orders").status_code == 503
    client.put("/api/chaos/crash/Nodo-Alpha-01", json={"enabled": False})

    client.put("/api/chaos/db-primary", json={"enabled": True})
    for _ in range(3):
        assert client.get("/api/orders").json()["db_used"] == "replica_failover"
    client.put("/api/chaos/db-primary", json={"enabled": False})

    snapshot = local(client)
    assert snapshot["traffic"]["total"]["simulated_crash"] == 1
    assert snapshot["traffic"]["total"]["server_errors"] >= 1
    assert snapshot["traffic"]["window_replica_served"] == 3
    assert snapshot["database"]["failover_count"] >= 3

    kinds = {event["kind"] for event in snapshot["events"]}
    assert {"chaos", "failover", "lifecycle"} <= kinds
    failovers = [e for e in snapshot["events"] if e["kind"] == "failover"]
    assert max(e["count"] for e in failovers) >= 2  # los repetidos se agrupan con contador


def test_eventos_de_seguridad_y_contrasena_generada_oculta(make_settings):
    settings = make_settings(admin_password="")  # el admin inicial recibe una contraseña aleatoria
    with TestClient(create_app(settings), headers=bearer(token_for(settings, admin_user_id("admin")))) as client:
        register(client, "victima")
        client.post("/api/auth/login", json={"username": "victima", "password": "Equivocada1"})
        events = local(client)["events"]

    admin_event = next(e for e in events if "ADMIN inicial" in e["message"])
    assert "[oculta]" in admin_event["message"]  # la contraseña nunca llega al panel
    assert any(e["kind"] == "auth" and "login fallido" in e["message"] and "victima" in e["message"] for e in events)
    assert all(USER_PASSWORD not in e["message"] for e in events)


def test_vista_de_cluster_agrega_nodos_por_rest_y_marca_los_inalcanzables(make_settings):
    beta_app = create_app(make_settings(node_id="Nodo-Beta-02"))
    with TestClient(beta_app, base_url="http://beta") as beta:
        authenticate(beta)
        for _ in range(4):
            beta.get("/api/orders")
        peers = (Peer(name="Nodo-Beta-02", url="http://beta"), Peer(name="Nodo-Fantasma", url="http://fantasma"))

        def route(request: httpx.Request) -> httpx.Response:  # "fantasma" no responde (caído de verdad)
            if request.url.host == "fantasma":
                raise httpx.ConnectError("conexión rechazada", request=request)
            return beta.request(request.method, str(request.url), headers=dict(request.headers))

        alpha_app = create_app(
            make_settings(node_id="Nodo-Alpha-01", peers=peers),
            peer_client=PeerClient(peers, timeout=2, client=httpx.Client(transport=httpx.MockTransport(route))),
        )
        with TestClient(alpha_app) as alpha:
            authenticate(alpha)
            alpha.get("/api/orders")
            view = alpha.get("/api/monitoring/cluster", params={"window": 120}).json()

    nodes = {n["node"]: n for n in view["nodes"]}
    assert nodes["Nodo-Alpha-01"]["self"] is True and nodes["Nodo-Alpha-01"]["reachable"] is True
    assert nodes["Nodo-Beta-02"]["reachable"] is True
    assert nodes["Nodo-Beta-02"]["traffic"]["window_requests"] >= 4  # medido por Beta, leído vía REST
    assert nodes["Nodo-Fantasma"]["reachable"] is False and "conexión rechazada" in nodes["Nodo-Fantasma"]["error"]

    summary = view["summary"]
    assert summary["nodes_total"] == 3 and summary["nodes_up"] == 2
    assert summary["window_requests"] == sum(
        n["traffic"]["window_requests"] for n in view["nodes"] if n["reachable"]
    )
    assert summary["in_sync"] is True
    assert view["gateway"] == {"configured": False}
    assert len(view["nodes"][0]["timeseries"]) == 12  # 120 s / 10 s
    assert all(view["events"][i]["at"] >= view["events"][i + 1]["at"] for i in range(len(view["events"]) - 1))


def test_estado_del_gateway_desde_stub_status_de_nginx():
    assert parse_stub_status(STUB_STATUS) == {
        "active": 3, "accepts": 120, "handled": 120, "requests": 457, "reading": 0, "writing": 1, "waiting": 2,
    }
    ok = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=STUB_STATUS)))
    assert fetch_gateway_status(ok, "http://gateway:8081/nginx_status")["requests"] == 457
    down = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(502, text="bad gateway")))
    assert fetch_gateway_status(down, "http://gateway:8081/nginx_status")["reachable"] is False
    assert fetch_gateway_status(ok, None) == {"configured": False}


def test_serie_temporal_alineada_y_percentiles():
    clock = [1_000_000.0]
    metrics = NodeMetrics("Nodo-Test", clock=lambda: clock[0])
    for ms in (10, 20, 30, 40, 100):
        metrics.record_request("/api/orders", 200, ms)
    clock[0] += 25  # 2 cubos más tarde
    metrics.record_request("/api/orders", 500, 50)
    metrics.record_request("/api/health", 200, 1)  # no cuenta

    snap = metrics.snapshot(60)
    assert [b["t"] % 10 for b in snap["timeseries"]] == [0] * 6  # cubos alineados al reloj
    counts = [b["requests"] for b in snap["timeseries"]]
    assert counts[-1] == 1 and counts[-3] == 5 and sum(counts) == 6
    assert snap["timeseries"][-3]["latency_avg_ms"] == 40.0
    assert snap["timeseries"][-2]["latency_avg_ms"] is None  # sin tráfico: hueco, no cero
    assert snap["latency_ms"]["p50"] == 30 and snap["latency_ms"]["p95"] == 100
    assert snap["traffic"]["error_rate"] == round(1 / 6, 4)
