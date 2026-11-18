"""Pruebas unitarias y de integración de la API de pedidos y de la tolerancia a fallos.

Los 5 casos exigidos están marcados con [REQ-n]; el resto amplía la cobertura. El fixture ``client``
tiene sesión de administrador (los permisos por rol se prueban en ``test_auth.py``).
"""
from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from app.config import Peer
from app.main import create_app
from app.peers import PeerClient
from tests.conftest import SAMPLE_ITEMS, authenticate, bearer, create_sample_order, token_for


# --------------------------------------------------------------------------- [REQ-1] crear
def test_crear_pedido_se_escribe_en_primaria_y_se_replica(client, db):
    response = client.post("/api/orders", json={"customer_name": "Ana Torres", "items": SAMPLE_ITEMS})

    assert response.status_code == 201
    assert response.headers["X-Handled-By-Node"] == "Nodo-Alpha-01"
    assert response.headers["X-DB-Used"] == "primary"
    body = response.json()
    assert body["node"] == "Nodo-Alpha-01"
    assert body["db_used"] == "primary"
    assert body["replication"] == "synced"

    order = body["data"]
    uuid.UUID(order["id"])  # id distribuido (UUID), no autoincremental
    assert order["customer_name"] == "Ana Torres"
    assert order["status"] == "pendiente"
    assert order["next_status"] == "en proceso"
    assert order["total"] == 1251.0  # 1 x 1200 + 2 x 25.5 (calculado en el servidor)
    assert order["handled_by_node"] == "Nodo-Alpha-01"
    assert order["items"] == SAMPLE_ITEMS
    assert order["created_at"]

    # Escritura dual: la fila existe, idéntica, en ambas bases.
    in_primary, in_replica = db.primary(order["id"]), db.replica(order["id"])
    assert in_primary is not None and in_replica is not None
    assert in_primary["updated_at"] == in_replica["updated_at"]
    assert in_replica["status"] == "pendiente"


# --------------------------------------------------------------------------- [REQ-2] actualizar estado
def test_actualizar_estado_recorre_todo_el_ciclo(client, db):
    order_id = create_sample_order(client)["data"]["id"]

    for expected in ("en proceso", "enviado", "entregado"):
        response = client.patch(f"/api/orders/{order_id}", json={"status": expected})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["data"]["status"] == expected
        assert body["replication"] == "synced"
        assert db.replica(order_id)["status"] == expected  # el cambio se replicó

    final = client.get(f"/api/orders/{order_id}").json()["data"]
    assert final["status"] == "entregado"
    assert final["next_status"] is None


def test_actualizar_estado_rechaza_transiciones_invalidas(client):
    order_id = create_sample_order(client)["data"]["id"]

    skip = client.patch(f"/api/orders/{order_id}", json={"status": "enviado"})  # salta "en proceso"
    assert skip.status_code == 409
    assert skip.json()["allowed_next"] == "en proceso"

    invalid = client.patch(f"/api/orders/{order_id}", json={"status": "cancelado"})
    assert invalid.status_code == 422

    client.patch(f"/api/orders/{order_id}", json={"status": "en proceso"})
    back = client.patch(f"/api/orders/{order_id}", json={"status": "pendiente"})  # retroceso
    assert back.status_code == 409

    same = client.patch(f"/api/orders/{order_id}", json={"status": "en proceso"})  # idempotente
    assert same.status_code == 200
    assert same.json()["data"]["status"] == "en proceso"


# --------------------------------------------------------------------------- [REQ-3] eliminar
def test_eliminar_pedido_con_borrado_logico_replicado(client, db):
    keep = create_sample_order(client, "Cliente que se queda")["data"]["id"]
    order_id = create_sample_order(client, "Cliente a eliminar")["data"]["id"]

    response = client.delete(f"/api/orders/{order_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["data"] == {"id": order_id, "deleted": True, "already_deleted": False}
    assert body["replication"] == "synced"

    assert client.get(f"/api/orders/{order_id}").status_code == 404
    listed = client.get("/api/orders").json()
    assert [o["id"] for o in listed["data"]] == [keep]

    # Tombstone en ambas bases: así el borrado también se reconcilia tras un failover.
    assert db.primary(order_id)["deleted_at"] is not None
    assert db.replica(order_id)["deleted_at"] is not None

    again = client.delete(f"/api/orders/{order_id}")  # DELETE idempotente
    assert again.status_code == 200
    assert again.json()["data"]["already_deleted"] is True
    assert client.delete(f"/api/orders/{uuid.uuid4()}").status_code == 404


# --------------------------------------------------------------------------- [REQ-4] caída de nodo
def test_fallo_de_nodo_devuelve_503_en_orders(client):
    order_id = create_sample_order(client)["data"]["id"]

    toggle = client.post("/api/chaos/toggle/Nodo-Alpha-01")
    assert toggle.status_code == 200
    assert toggle.json()["simulate_crash"] is True

    for method, path in (
        ("GET", "/api/orders"),
        ("GET", f"/api/orders/{order_id}"),
        ("POST", "/api/orders"),
        ("PATCH", f"/api/orders/{order_id}"),
        ("DELETE", f"/api/orders/{order_id}"),
    ):
        response = client.request(method, path, json={"customer_name": "X", "items": SAMPLE_ITEMS})
        assert response.status_code == 503, (method, path)
        assert response.headers["X-Handled-By-Node"] == "Nodo-Alpha-01"
        assert response.headers["Retry-After"] == "5"
        assert response.json()["simulated_crash"] is True

    # Salud y caos siguen accesibles para poder observar y restaurar el nodo.
    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["simulate_crash"] is True
    assert client.get("/api/chaos/status").status_code == 200

    restore = client.post("/api/chaos/toggle")  # alterna el nodo actual
    assert restore.json()["simulate_crash"] is False
    after = client.get("/api/orders")
    assert after.status_code == 200
    assert after.json()["count"] == 1


# --------------------------------------------------------------------------- [REQ-5] failover de BD
def test_failover_bd_primaria_a_replica_y_reconciliacion(client, db):
    order_a = create_sample_order(client, "Pedido antes de la caída")["data"]["id"]

    down = client.put("/api/chaos/db-primary", json={"enabled": True})
    assert down.status_code == 200
    assert down.json()["simulate_db_primary_down"] is True

    # Lectura: sin HTTP 500, atendida por la réplica.
    listed = client.get("/api/orders")
    assert listed.status_code == 200
    assert listed.json()["db_used"] == "replica_failover"
    assert listed.headers["X-DB-Used"] == "replica_failover"
    assert [o["id"] for o in listed.json()["data"]] == [order_a]

    # Escritura durante la caída: va a la réplica y queda pendiente para la primaria.
    created = client.post("/api/orders", json={"customer_name": "Pedido durante la caída", "items": SAMPLE_ITEMS})
    assert created.status_code == 201
    assert created.json()["db_used"] == "replica_failover"
    assert created.json()["replication"] == "pending"
    order_b = created.json()["data"]["id"]

    advanced = client.patch(f"/api/orders/{order_a}", json={"status": "en proceso"})
    assert advanced.status_code == 200
    assert advanced.json()["db_used"] == "replica_failover"

    assert db.primary(order_b) is None  # la primaria está atrasada...
    assert db.primary(order_a)["status"] == "pendiente"
    assert client.get("/api/replication/status").json()["in_sync"] is False

    # Recuperación: al restaurar la primaria se reconcilia automáticamente.
    up = client.put("/api/chaos/db-primary", json={"enabled": False})
    sync = up.json()["sync"]
    assert sync["status"] == "ok"
    assert sync["tables"]["orders"]["copied_to_primary"] == 2
    assert sync["tables"]["audit_log"]["copied_to_primary"] == 2  # la auditoría también se reconcilia

    assert db.primary(order_b) is not None
    assert db.primary(order_a)["status"] == "en proceso"
    status = client.get("/api/replication/status").json()
    assert status["in_sync"] is True and status["divergent_orders"] == 0

    final = client.get("/api/orders")
    assert final.json()["db_used"] == "primary"
    assert final.json()["count"] == 2


# --------------------------------------------------------------------------- fallos reales (no simulados)
def test_failover_cuando_la_primaria_es_inalcanzable_de_verdad(make_settings, broken_db_url):
    app = create_app(make_settings(primary_db_url=broken_db_url, breaker_cooldown_seconds=30))
    with TestClient(app) as client:
        authenticate(client)  # el login también funciona: el usuario se lee de la réplica
        created = client.post("/api/orders", json={"customer_name": "Sin primaria", "items": SAMPLE_ITEMS})
        assert created.status_code == 201
        assert created.json()["db_used"] == "replica_failover"

        listed = client.get("/api/orders")
        assert listed.status_code == 200
        assert listed.json()["db_used"] == "replica_failover"
        assert listed.json()["count"] == 1

        # El circuit breaker queda abierto: no se paga el timeout de la primaria en cada petición.
        assert app.state.cluster.primary_circuit_open() is True
        health = client.get("/api/health").json()["databases"]
        assert health["primary"]["status"] == "down"
        assert health["replica"]["status"] == "up"
        assert health["circuit_breaker"] == "open"


def test_si_caen_ambas_bd_responde_503_y_nunca_500(make_settings, broken_db_url, tmp_path):
    other_broken = f"sqlite:///{(tmp_path / 'tampoco_existe' / 'r.db').as_posix()}"
    settings = make_settings(primary_db_url=broken_db_url, replica_db_url=other_broken)
    app = create_app(settings)
    with TestClient(app, headers=bearer(token_for(settings))) as client:
        for response in (
            client.get("/api/orders"),
            client.post("/api/orders", json={"customer_name": "Nadie", "items": SAMPLE_ITEMS}),
        ):
            assert response.status_code == 503
            assert "Ni db-primary ni db-replica" in response.json()["detail"]
            assert response.headers["X-Handled-By-Node"] == "Nodo-Alpha-01"
        assert client.get("/api/health").status_code == 200


def test_replica_caida_no_bloquea_escrituras_en_primaria(make_settings, broken_db_url):
    app = create_app(make_settings(replica_db_url=broken_db_url))
    with TestClient(app) as client:
        authenticate(client)
        created = client.post("/api/orders", json={"customer_name": "Sin réplica", "items": SAMPLE_ITEMS})
        assert created.status_code == 201
        assert created.json()["db_used"] == "primary"
        assert created.json()["replication"] == "pending"
        assert app.state.cluster.needs_resync is True


# --------------------------------------------------------------------------- reglas de negocio y API
def test_listar_y_filtrar_por_estado_y_cliente(client):
    first = create_sample_order(client, "Ana Torres")["data"]["id"]
    create_sample_order(client, "Bruno Díaz")
    create_sample_order(client, "Carla Anaya")
    client.patch(f"/api/orders/{first}", json={"status": "en proceso"})

    assert client.get("/api/orders").json()["count"] == 3
    in_process = client.get("/api/orders", params={"status": "en proceso"}).json()
    assert [o["id"] for o in in_process["data"]] == [first]
    by_customer = client.get("/api/orders", params={"customer": "ANA"}).json()
    assert sorted(o["customer_name"] for o in by_customer["data"]) == ["Ana Torres", "Carla Anaya"]
    assert client.get("/api/orders", params={"customer": "%"}).json()["count"] == 0  # LIKE escapado


def test_editar_pedido_recalcula_total_y_solo_si_esta_pendiente(client, db):
    order_id = create_sample_order(client)["data"]["id"]

    edited = client.patch(
        f"/api/orders/{order_id}",
        json={"customer_name": "Ana T. Ruiz", "items": [{"product": "Monitor", "quantity": 3, "unit_price": 199.99}]},
    )
    assert edited.status_code == 200
    assert edited.json()["data"]["customer_name"] == "Ana T. Ruiz"
    assert edited.json()["data"]["total"] == 599.97
    assert float(db.replica(order_id)["total"]) == 599.97

    client.patch(f"/api/orders/{order_id}", json={"status": "en proceso"})
    locked = client.patch(f"/api/orders/{order_id}", json={"customer_name": "Otro nombre"})
    assert locked.status_code == 409


def test_creacion_idempotente_con_id_generado_por_el_cliente(client, db):
    payload = {"id": str(uuid.uuid4()), "customer_name": "Reintento", "items": SAMPLE_ITEMS}

    first = client.post("/api/orders", json=payload)
    retry = client.post("/api/orders", json=payload)  # p. ej. reintento de NGINX en otro nodo

    assert first.status_code == 201
    assert retry.status_code == 200
    assert retry.headers["X-Idempotent-Replay"] == "true"
    assert retry.json()["data"]["id"] == payload["id"]
    assert db.count("primary") == 1 and db.count("replica") == 1


def test_validaciones_de_entrada(client):
    cases = [
        {"customer_name": "Ana", "items": []},
        {"customer_name": "Ana", "items": [{"product": "X", "quantity": 0, "unit_price": 1}]},
        {"customer_name": "Ana", "items": [{"product": "X", "quantity": 1, "unit_price": -5}]},
        {"customer_name": "A", "items": SAMPLE_ITEMS},
        {"customer_name": "Ana", "items": SAMPLE_ITEMS, "status": "entregado"},  # no se elige el estado inicial
    ]
    for payload in cases:
        assert client.post("/api/orders", json=payload).status_code == 422, payload

    order_id = create_sample_order(client)["data"]["id"]
    assert client.patch(f"/api/orders/{order_id}", json={}).status_code == 422
    assert client.get(f"/api/orders/{uuid.uuid4()}").status_code == 404


def test_health_y_cabecera_x_handled_by_node(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.headers["X-Handled-By-Node"] == "Nodo-Alpha-01"
    body = response.json()
    assert body["status"] == "ok"
    assert body["databases"]["primary"]["status"] == "up"
    assert body["databases"]["replica"]["status"] == "up"

    liveness = client.get("/api/health", params={"deep": "false"}).json()
    assert "databases" not in liveness

    not_found = client.get(f"/api/orders/{uuid.uuid4()}")
    assert not_found.headers["X-Handled-By-Node"] == "Nodo-Alpha-01"


# --------------------------------------------------------------------------- comunicación entre nodos
def test_comunicacion_entre_nodos_reenvia_caos_y_propaga_caida_de_bd(make_settings):
    """Dos nodos reales en el mismo proceso, compartiendo primaria y réplica."""
    beta_app = create_app(make_settings(node_id="Nodo-Beta-02"))
    with TestClient(beta_app, base_url="http://beta") as beta:
        peers = (Peer(name="Nodo-Beta-02", url="http://beta"),)
        alpha_app = create_app(
            make_settings(node_id="Nodo-Alpha-01", peers=peers),
            peer_client=PeerClient(peers, timeout=2, client=beta),
        )
        with TestClient(alpha_app) as alpha:
            # Un solo login: el token sirve en ambos nodos porque comparten JWT_SECRET, y el reenvío
            # REST entre nodos lleva la misma cabecera Authorization.
            authenticate(alpha)
            beta.headers["Authorization"] = alpha.headers["Authorization"]
            # Alpha recibe la orden pero la reenvía por REST a Beta.
            toggled = alpha.post("/api/chaos/toggle/Nodo-Beta-02")
            assert toggled.status_code == 200
            assert toggled.json()["node"] == "Nodo-Beta-02"
            assert toggled.json()["forwarded_by"] == "Nodo-Alpha-01"
            assert toggled.json()["simulate_crash"] is True

            assert beta.get("/api/orders").status_code == 503
            assert alpha.get("/api/orders").status_code == 200

            status = alpha.get("/api/chaos/status").json()
            nodes = {node["node"]: node for node in status["nodes"]}
            assert nodes["Nodo-Beta-02"]["reachable"] is True
            assert nodes["Nodo-Beta-02"]["simulate_crash"] is True
            assert nodes["Nodo-Alpha-01"]["simulate_crash"] is False

            # La caída simulada de la BD primaria (compartida) se propaga a todo el clúster.
            down = alpha.put("/api/chaos/db-primary", json={"enabled": True}).json()
            assert [p["ok"] for p in down["propagated_to"]] == [True, True]
            assert beta.get("/api/chaos/local").json()["simulate_db_primary_down"] is True

            alpha.put("/api/chaos/crash/Nodo-Beta-02", json={"enabled": False})
            assert beta.get("/api/orders").json()["db_used"] == "replica_failover"
            during = beta.post("/api/orders", json={"customer_name": "Durante caída", "items": SAMPLE_ITEMS})
            assert during.json()["replication"] == "pending"

            # Al restaurar, el nodo que recibe la orden reconcilia primero y lo informa.
            up = alpha.put("/api/chaos/db-primary", json={"enabled": False}).json()
            assert up["sync"]["tables"]["orders"]["copied_to_primary"] == 1
            assert up["propagated_to"][1]["sync"]["copied_to_primary"] == 0  # ya estaba al día
            assert beta.get("/api/chaos/local").json()["simulate_db_primary_down"] is False

            assert alpha.post("/api/chaos/toggle/Nodo-Inexistente").status_code == 404
