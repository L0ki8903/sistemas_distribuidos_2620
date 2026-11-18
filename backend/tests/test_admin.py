"""Pruebas del panel de administración de datos y del registro de auditoría."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import Peer
from app.database import PRIMARY, REPLICA
from app.main import create_app
from app.models import AuditEntry
from app.peers import PeerClient
from app.users import admin_user_id
from tests.conftest import USER_PASSWORD, authenticate, bearer, create_sample_order, login, register


def audit_entries(client: TestClient, **params) -> list[dict]:
    response = client.get("/api/admin/audit", params=params)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def user_row(client: TestClient, username: str, status: str | None = None) -> dict:
    params = {"search": username, **({"status": status} if status else {})}
    rows = client.get("/api/admin/users", params=params).json()["data"]
    return next(u for u in rows if u["username"] == username)


def test_panel_de_administracion_es_solo_para_administradores(raw_client):
    user = bearer(register(raw_client, "curioso")["access_token"])
    for method, path in (
        ("GET", "/api/admin/summary"),
        ("GET", "/api/admin/users"),
        ("PATCH", f"/api/admin/users/{admin_user_id('admin')}"),
        ("GET", "/api/admin/orders"),
        ("GET", "/api/admin/audit"),
    ):
        body = {"full_name": "Hackeado"} if method == "PATCH" else None
        assert raw_client.request(method, path, json=body).status_code == 401, path
        assert raw_client.request(method, path, json=body, headers=user).status_code == 403, path


def test_lista_de_usuarios_con_pedidos_filtros_y_resumen(client):
    ana = bearer(register(client, "ana", full_name="Ana Torres")["access_token"])
    register(client, "bruno", full_name="Bruno Díaz")
    create_sample_order(client, "Pedido 1 de Ana", headers=ana)
    create_sample_order(client, "Pedido 2 de Ana", headers=ana)

    users = {u["username"]: u for u in client.get("/api/admin/users").json()["data"]}
    assert set(users) == {"admin", "ana", "bruno"}
    assert users["ana"]["orders_count"] == 2 and users["bruno"]["orders_count"] == 0
    assert "password_hash" not in users["ana"]
    assert [u["username"] for u in client.get("/api/admin/users", params={"role": "admin"}).json()["data"]] == ["admin"]
    assert [u["username"] for u in client.get("/api/admin/users", params={"search": "díaz"}).json()["data"]] == ["bruno"]

    summary = client.get("/api/admin/summary").json()["data"]
    assert summary["users_total"] == 3 and summary["admins"] == 1
    assert summary["orders_active"] == 2 and summary["orders_by_status"]["pendiente"] == 2
    assert summary["audit_entries"] >= 4  # 2 registros + 2 pedidos


def test_cambiar_rol_tiene_efecto_inmediato_y_queda_auditado(client):
    carla = register(client, "carla")
    carla_token = bearer(carla["access_token"])
    assert client.get("/api/chaos/status", headers=carla_token).status_code == 403

    promoted = client.patch(f"/api/admin/users/{carla['user']['id']}", json={"role": "admin", "full_name": "Carla Ruiz"})
    assert promoted.status_code == 200 and promoted.json()["data"]["role"] == "admin"
    assert promoted.json()["replication"] == "synced"
    # El mismo token (emitido cuando era cliente) ya tiene permisos de admin: el rol se lee de la BD.
    assert client.get("/api/chaos/status", headers=carla_token).status_code == 200

    entry = audit_entries(client, entity="user", entity_id=carla["user"]["id"])[0]
    assert entry["action"] == "user.role" and entry["actor_username"] == "admin"
    assert entry["changes"] == {"full_name": ["Cliente Demo", "Carla Ruiz"], "role": ["user", "admin"]}
    assert entry["node"] == "Nodo-Alpha-01"

    client.patch(f"/api/admin/users/{carla['user']['id']}", json={"role": "user"})
    assert client.get("/api/chaos/status", headers=carla_token).status_code == 403


def test_desactivar_y_reactivar_una_cuenta(client):
    diego = register(client, "diego")
    token = bearer(diego["access_token"])
    client.patch(f"/api/admin/users/{diego['user']['id']}", json={"is_active": False})

    assert client.get("/api/orders", headers=token).status_code == 401  # su sesión muere al instante
    assert client.post("/api/auth/login", json={"username": "diego", "password": USER_PASSWORD}).status_code == 403
    assert user_row(client, "diego", "inactive")["is_active"] is False

    client.patch(f"/api/admin/users/{diego['user']['id']}", json={"is_active": True})
    assert client.post("/api/auth/login", json={"username": "diego", "password": USER_PASSWORD}).status_code == 200
    actions = [e["action"] for e in audit_entries(client, entity_id=diego["user"]["id"])]
    assert actions[:2] == ["user.activate", "user.deactivate"]


def test_restablecer_contrasena_sin_dejarla_en_la_auditoria(client):
    elena = register(client, "elena")["user"]
    weak = client.post(f"/api/admin/users/{elena['id']}/reset-password", json={"new_password": "corta"})
    assert weak.status_code == 422
    assert client.post(f"/api/admin/users/{elena['id']}/reset-password", json={"new_password": "NuevaClave77"}).status_code == 200

    assert client.post("/api/auth/login", json={"username": "elena", "password": USER_PASSWORD}).status_code == 401
    assert client.post("/api/auth/login", json={"username": "elena", "password": "NuevaClave77"}).status_code == 200
    entry = audit_entries(client, entity_id=elena["id"])[0]
    assert entry["action"] == "user.password_reset" and entry["changes"] is None
    assert "NuevaClave77" not in str(audit_entries(client))


def test_el_admin_no_puede_quitarse_permisos_ni_eliminarse(client):
    me = admin_user_id("admin")
    for body in ({"role": "user"}, {"is_active": False}):
        response = client.patch(f"/api/admin/users/{me}", json=body)
        assert response.status_code == 409, body
    assert client.delete(f"/api/admin/users/{me}").status_code == 409
    assert client.patch(f"/api/admin/users/{me}", json={"full_name": "Jefa de logística"}).status_code == 200
    assert client.get("/api/auth/me").json()["user"]["role"] == "admin"


def test_eliminar_cuenta_conservando_sus_pedidos_la_anonimiza(client, db):
    fabian = register(client, "fabian", full_name="Fabián Gómez")
    order_id = create_sample_order(client, "Pedido de Fabián", headers=bearer(fabian["access_token"]))["data"]["id"]

    deleted = client.delete(f"/api/admin/users/{fabian['user']['id']}", params={"orders": "keep"})
    assert deleted.status_code == 200
    data = deleted.json()["data"]
    assert data["username"].startswith("eliminado-") and data["full_name"] == "Usuario eliminado"
    assert data["orders_mode"] == "keep" and data["orders_affected"] == 1

    for name in (PRIMARY, REPLICA):  # anonimizado y con tombstone en ambas bases
        assert db.user(name, "fabian") is None
        assert db.get(name, fabian["user"]["id"], table=__import__("app.models").models.User.__table__)["deleted_at"] is not None
    assert client.post("/api/auth/login", json={"username": "fabian", "password": USER_PASSWORD}).status_code == 401
    assert client.get(f"/api/orders/{order_id}").status_code == 200  # el pedido se conserva
    assert register(client, "fabian")["user"]["username"] == "fabian"  # el nombre de usuario quedó libre
    assert any(u["full_name"] == "Usuario eliminado" for u in client.get("/api/admin/users", params={"status": "deleted"}).json()["data"])


def test_eliminar_cuenta_con_sus_pedidos_y_restaurar_uno(client, db):
    gina = register(client, "gina")
    token = bearer(gina["access_token"])
    ids = [create_sample_order(client, f"Pedido {i} de Gina", headers=token)["data"]["id"] for i in range(2)]

    result = client.delete(f"/api/admin/users/{gina['user']['id']}", params={"orders": "delete"}).json()
    assert result["data"]["orders_affected"] == 2 and result["replication"] == "synced"
    for order_id in ids:
        assert client.get(f"/api/orders/{order_id}").status_code == 404
        assert db.replica(order_id)["deleted_at"] is not None  # los borrados viajaron en la misma escritura

    with_deleted = client.get("/api/admin/orders", params={"include_deleted": "true"}).json()["data"]
    assert {o["id"] for o in with_deleted if o["deleted_at"]} >= set(ids)
    restored = client.post(f"/api/admin/orders/{ids[0]}/restore")
    assert restored.status_code == 200
    assert client.get(f"/api/orders/{ids[0]}").status_code == 200
    assert client.post(f"/api/admin/orders/{ids[0]}/restore").status_code == 409  # ya no está eliminado
    assert audit_entries(client, entity_id=ids[0])[0]["action"] == "order.restore"


def test_correccion_administrativa_de_pedidos(client):
    order_id = create_sample_order(client, "Clnica San Rafel")["data"]["id"]
    for status in ("en proceso", "enviado"):
        client.patch(f"/api/orders/{order_id}", json={"status": status})
    # La ruta normal no permite editar un pedido ya enviado ni retroceder su estado...
    assert client.patch(f"/api/orders/{order_id}", json={"customer_name": "Clínica San Rafael"}).status_code == 409
    assert client.patch(f"/api/orders/{order_id}", json={"status": "en proceso"}).status_code == 409
    # ...la corrección administrativa sí, y queda auditada.
    fixed = client.patch(
        f"/api/admin/orders/{order_id}",
        json={"customer_name": "Clínica San Rafael", "status": "en proceso",
              "items": [{"product": "Guantes", "quantity": 10, "unit_price": 6.2}]},
    )
    assert fixed.status_code == 200
    data = fixed.json()["data"]
    assert data["customer_name"] == "Clínica San Rafael" and data["status"] == "en proceso" and data["total"] == 62.0
    entry = audit_entries(client, entity_id=order_id)[0]
    assert entry["action"] == "order.admin_update"
    assert entry["changes"]["status"] == ["enviado", "en proceso"]
    assert entry["changes"]["customer_name"] == ["Clnica San Rafel", "Clínica San Rafael"]
    actions = [e["action"] for e in audit_entries(client, entity_id=order_id)]
    assert actions == ["order.admin_update", "order.status", "order.status", "order.create"]


def test_la_auditoria_registra_quien_hizo_cada_operacion_y_desde_que_nodo(raw_client):
    hugo = register(raw_client, "hugo")
    hugo_token = bearer(hugo["access_token"])
    order_id = create_sample_order(raw_client, "Pedido de Hugo", headers=hugo_token)["data"]["id"]
    admin = bearer(login(raw_client))
    raw_client.patch(f"/api/orders/{order_id}", json={"status": "en proceso"}, headers=admin)

    entries = raw_client.get("/api/admin/audit", params={"entity_id": order_id}, headers=admin).json()["data"]
    assert [(e["action"], e["actor_username"]) for e in entries] == [("order.status", "admin"), ("order.create", "hugo")]
    assert all(e["node"] == "Nodo-Alpha-01" for e in entries)
    register_entry = raw_client.get("/api/admin/audit", params={"entity_id": hugo["user"]["id"]}, headers=admin).json()["data"][0]
    assert register_entry["action"] == "user.register" and register_entry["actor_username"] == "hugo"


def test_auditoria_y_acciones_de_admin_con_la_primaria_caida(client, db):
    ivan = register(client, "ivan")["user"]
    client.put("/api/chaos/db-primary", json={"enabled": True})
    changed = client.patch(f"/api/admin/users/{ivan['id']}", json={"full_name": "Iván Pérez"})
    assert changed.status_code == 200 and changed.json()["db_used"] == "replica_failover"
    assert changed.json()["replication"] == "pending"

    with db.engines[PRIMARY].connect() as conn:  # la primaria aún no tiene esa entrada de auditoría
        primary_actions = [r.action for r in conn.execute(AuditEntry.__table__.select())]
    assert "user.update" not in primary_actions

    sync = client.put("/api/chaos/db-primary", json={"enabled": False}).json()["sync"]
    assert sync["tables"]["users"]["copied_to_primary"] == 1
    assert sync["tables"]["audit_log"]["copied_to_primary"] == 1
    assert db.user(PRIMARY, "ivan")["full_name"] == "Iván Pérez"
    status = client.get("/api/replication/status").json()
    assert status["in_sync"] is True and status["divergent_by_table"]["audit_log"] == 0


def test_desactivar_en_un_nodo_revoca_la_sesion_en_el_otro(make_settings):
    """Revocación distribuida: sin estado compartido de sesiones, sólo con la BD replicada."""
    beta_app = create_app(make_settings(node_id="Nodo-Beta-02"))
    with TestClient(beta_app, base_url="http://beta") as beta:
        peers = (Peer(name="Nodo-Beta-02", url="http://beta"),)
        alpha_app = create_app(
            make_settings(node_id="Nodo-Alpha-01", peers=peers),
            peer_client=PeerClient(peers, timeout=2, client=beta),
        )
        with TestClient(alpha_app) as alpha:
            user = register(alpha, "julia")
            token = bearer(user["access_token"])
            assert beta.get("/api/orders", headers=token).status_code == 200
            authenticate(alpha).patch(f"/api/admin/users/{user['user']['id']}", json={"is_active": False})
            assert beta.get("/api/orders", headers=token).status_code == 401
