"""Pruebas de autenticación, autorización por roles y seguridad en el entorno distribuido."""
from __future__ import annotations

import logging
import re

from fastapi.testclient import TestClient
from sqlalchemy import update

from app.cli import create_admin
from app.config import Peer
from app.database import PRIMARY, REPLICA
from app.main import create_app
from app.models import User
from app.peers import PeerClient
from app.users import admin_user_id
from tests.conftest import (
    ADMIN_PASSWORD,
    USER_PASSWORD,
    SAMPLE_ITEMS,
    DbInspector,
    bearer,
    create_sample_order,
    login,
    register,
    token_for,
)


# --------------------------------------------------------------------------- registro y login
def test_registro_y_login_devuelven_token_y_el_usuario_se_replica(raw_client, db):
    body = register(raw_client, "Ana.Torres", full_name="Ana Torres")

    assert body["token_type"] == "bearer" and body["access_token"]
    assert body["user"]["username"] == "ana.torres"  # normalizado a minúsculas
    assert body["user"]["role"] == "user"
    assert "password" not in str(body).lower().replace("password_hash", "") and "pbkdf2" not in str(body)

    me = raw_client.get("/api/auth/me", headers=bearer(body["access_token"]))
    assert me.status_code == 200 and me.json()["user"]["full_name"] == "Ana Torres"

    relogin = raw_client.post("/api/auth/login", json={"username": "ANA.TORRES", "password": USER_PASSWORD})
    assert relogin.status_code == 200

    # El usuario se escribió en ambas bases y la contraseña está hasheada (PBKDF2 con sal).
    in_primary, in_replica = db.user(PRIMARY, "ana.torres"), db.user(REPLICA, "ana.torres")
    assert in_primary and in_replica and in_primary["updated_at"] == in_replica["updated_at"]
    assert in_primary["password_hash"].startswith("pbkdf2_sha256$")
    assert USER_PASSWORD not in in_primary["password_hash"]


def test_login_invalido_no_revela_si_el_usuario_existe(raw_client):
    register(raw_client, "bruno")

    wrong_password = raw_client.post("/api/auth/login", json={"username": "bruno", "password": "Incorrecta1"})
    unknown_user = raw_client.post("/api/auth/login", json={"username": "nadie", "password": "Incorrecta1"})

    assert wrong_password.status_code == unknown_user.status_code == 401
    assert wrong_password.json()["detail"] == unknown_user.json()["detail"] == "Usuario o contraseña incorrectos"
    assert wrong_password.headers["WWW-Authenticate"] == "Bearer"


def test_validaciones_de_registro_y_no_se_puede_auto_asignar_admin(raw_client):
    base = {"username": "carla", "full_name": "Carla", "password": "Segura2026"}
    invalid = [
        {**base, "password": "sinnumeros"},
        {**base, "password": "Ab1"},
        {**base, "username": "con espacio"},
        {**base, "username": "ab"},
        {**base, "role": "admin"},  # escalada de privilegios: campo no permitido
    ]
    for payload in invalid:
        assert raw_client.post("/api/auth/register", json=payload).status_code == 422, payload

    assert raw_client.post("/api/auth/register", json=base).status_code == 201
    duplicate = raw_client.post("/api/auth/register", json={**base, "username": "CARLA"})
    assert duplicate.status_code == 409


# --------------------------------------------------------------------------- tokens
def test_endpoints_protegidos_rechazan_tokens_ausentes_falsos_o_expirados(raw_client, make_settings):
    assert raw_client.get("/api/orders").status_code == 401
    assert raw_client.get("/api/orders", headers=bearer("no-es-un-jwt")).json()["detail"] == "Token inválido"

    admin_id = admin_user_id("admin")
    other_secret = token_for(make_settings(jwt_secret="otro-secreto-distinto-al-del-cluster-123456789"), admin_id)
    assert raw_client.get("/api/orders", headers=bearer(other_secret)).status_code == 401

    expired = token_for(make_settings(jwt_expires_minutes=-1), admin_id)
    response = raw_client.get("/api/orders", headers=bearer(expired))
    assert response.status_code == 401 and "expiró" in response.json()["detail"]

    assert raw_client.get("/api/health").status_code == 200  # la salud sigue siendo pública
    assert raw_client.get("/api/orders", headers=bearer(token_for(make_settings(), admin_id))).status_code == 200


def test_token_de_un_nodo_es_valido_en_el_otro_pero_no_con_otro_secreto(make_settings):
    alpha_app = create_app(make_settings(node_id="Nodo-Alpha-01"))
    beta_app = create_app(make_settings(node_id="Nodo-Beta-02"))
    intruder_app = create_app(make_settings(node_id="Nodo-Intruso", jwt_secret="secreto-diferente-0123456789-abcdefghij"))
    with TestClient(alpha_app) as alpha, TestClient(beta_app) as beta, TestClient(intruder_app) as intruder:
        token = login(alpha)  # emitido por Alpha
        on_beta = beta.get("/api/auth/me", headers=bearer(token))
        assert on_beta.status_code == 200
        assert on_beta.json()["node"] == "Nodo-Beta-02"  # validado por otro nodo sin estado compartido
        assert intruder.get("/api/auth/me", headers=bearer(token)).status_code == 401


def test_usuario_desactivado_pierde_el_acceso_aunque_su_token_no_haya_expirado(raw_client, app):
    token = register(raw_client, "diego")["access_token"]
    assert raw_client.get("/api/orders", headers=bearer(token)).status_code == 200

    for engine in app.state.cluster.engines.values():
        with engine.begin() as conn:
            conn.execute(update(User.__table__).where(User.__table__.c.username == "diego").values(is_active=False))

    assert raw_client.get("/api/orders", headers=bearer(token)).status_code == 401
    assert raw_client.post("/api/auth/login", json={"username": "diego", "password": USER_PASSWORD}).status_code == 403


def test_bloqueo_temporal_tras_varios_intentos_fallidos(make_settings):
    app = create_app(make_settings(login_max_failures=3, login_window_seconds=60))
    with TestClient(app) as client:
        register(client, "elena")
        for _ in range(3):
            assert client.post("/api/auth/login", json={"username": "elena", "password": "Mala12345"}).status_code == 401
        blocked = client.post("/api/auth/login", json={"username": "elena", "password": USER_PASSWORD})
        assert blocked.status_code == 429  # incluso con la contraseña correcta
        assert int(blocked.headers["Retry-After"]) > 0
        assert client.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PASSWORD}).status_code == 200


# --------------------------------------------------------------------------- autorización por rol
def test_cliente_solo_ve_y_gestiona_sus_propios_pedidos(raw_client):
    admin = bearer(login(raw_client))
    ana = bearer(register(raw_client, "ana")["access_token"])
    bruno = bearer(register(raw_client, "bruno")["access_token"])

    order_ana = create_sample_order(raw_client, "Pedido de Ana", headers=ana)["data"]
    order_bruno = create_sample_order(raw_client, "Pedido de Bruno", headers=bruno)["data"]["id"]
    create_sample_order(raw_client, "Pedido del admin", headers=admin)

    mine = raw_client.get("/api/orders", headers=ana).json()
    assert [o["id"] for o in mine["data"]] == [order_ana["id"]]
    assert mine["data"][0]["owner_id"] == order_ana["owner_id"]
    # Un pedido ajeno se comporta como inexistente (no se revela que existe).
    for method in ("GET", "PATCH", "DELETE"):
        response = raw_client.request(method, f"/api/orders/{order_bruno}", json={"customer_name": "X Y"}, headers=ana)
        assert response.status_code == 404, method

    everything = raw_client.get("/api/orders", headers=admin).json()
    assert everything["count"] == 3
    assert {o["customer_name"]: o["owner_username"] for o in everything["data"]}["Pedido de Ana"] == "ana"

    assert raw_client.patch(f"/api/orders/{order_ana['id']}", json={"customer_name": "Ana T."}, headers=ana).status_code == 200
    forbidden = raw_client.patch(f"/api/orders/{order_ana['id']}", json={"status": "en proceso"}, headers=ana)
    assert forbidden.status_code == 403  # sólo el admin cambia estados

    assert raw_client.patch(f"/api/orders/{order_ana['id']}", json={"status": "en proceso"}, headers=admin).status_code == 200
    assert raw_client.patch(f"/api/orders/{order_ana['id']}", json={"customer_name": "Otro"}, headers=ana).status_code == 409
    assert raw_client.delete(f"/api/orders/{order_ana['id']}", headers=ana).status_code == 409  # ya no está pendiente

    cancel_me = create_sample_order(raw_client, "Para cancelar", headers=ana)["data"]["id"]
    assert raw_client.delete(f"/api/orders/{cancel_me}", headers=ana).status_code == 200


def test_cliente_no_accede_al_caos_ni_a_la_replicacion(raw_client):
    user = bearer(register(raw_client, "fabian")["access_token"])
    admin = bearer(login(raw_client))
    attempts = [
        ("GET", "/api/chaos/status", None),
        ("POST", "/api/chaos/toggle", None),
        ("PUT", "/api/chaos/crash/Nodo-Alpha-01", {"enabled": True}),
        ("PUT", "/api/chaos/db-primary", {"enabled": True}),
        ("GET", "/api/replication/status", None),
        ("POST", "/api/replication/sync", None),
    ]
    for method, path, body in attempts:
        assert raw_client.request(method, path, json=body, headers=user).status_code == 403, path
        assert raw_client.request(method, path, json=body).status_code == 401, path
    assert raw_client.get("/api/chaos/status", headers=admin).status_code == 200
    assert raw_client.get("/api/orders", headers=user).status_code == 200  # el caos no se activó


# --------------------------------------------------------------------------- tolerancia a fallos + auth
def test_login_y_registro_funcionan_con_la_bd_primaria_caida(raw_client, db):
    register(raw_client, "gabriela")
    admin = bearer(login(raw_client))
    assert raw_client.put("/api/chaos/db-primary", json={"enabled": True}, headers=admin).status_code == 200

    session = raw_client.post("/api/auth/login", json={"username": "gabriela", "password": USER_PASSWORD})
    assert session.status_code == 200 and session.json()["db_used"] == "replica_failover"

    new_user = raw_client.post(
        "/api/auth/register", json={"username": "hugo", "full_name": "Hugo", "password": USER_PASSWORD}
    )
    assert new_user.status_code == 201 and new_user.json()["db_used"] == "replica_failover"
    hugo = bearer(new_user.json()["access_token"])
    order = create_sample_order(raw_client, "Pedido de Hugo", headers=hugo)
    assert order["db_used"] == "replica_failover"
    assert db.user(PRIMARY, "hugo") is None  # la primaria está atrasada

    restored = raw_client.put("/api/chaos/db-primary", json={"enabled": False}, headers=admin).json()
    assert restored["sync"]["copied_to_primary"] == 2
    assert restored["sync"]["tables"]["users"]["copied_to_primary"] == 1
    assert restored["sync"]["tables"]["orders"]["copied_to_primary"] == 1
    assert db.user(PRIMARY, "hugo") is not None
    status = raw_client.get("/api/replication/status", headers=admin).json()
    assert status["in_sync"] is True and status["divergent_by_table"] == {"orders": 0, "users": 0}


def test_admin_inicial_se_crea_una_sola_vez_y_con_contrasena_generada(make_settings, caplog):
    auth_logger = logging.getLogger("pedidos")
    auth_logger.addHandler(caplog.handler)  # el logger "pedidos" no propaga a la raíz
    try:
        alpha_app = create_app(make_settings(admin_password=""))
        with caplog.at_level(logging.INFO), TestClient(alpha_app) as alpha:
            match = re.search(r"contraseña generada: (\S+)", caplog.text)
            assert match, caplog.text
            generated = match.group(1)
            assert login(alpha, "admin", generated)

            beta_app = create_app(make_settings(node_id="Nodo-Beta-02", admin_password="OtraClave123"))
            with TestClient(beta_app) as beta:
                assert login(beta, "admin", generated)  # el segundo nodo NO lo recrea ni lo cambia
            inspector = DbInspector(alpha_app)
            for name in (PRIMARY, REPLICA):
                assert inspector.count(name, User.__table__) == 1
                assert inspector.user(name, "admin")["id"] == admin_user_id("admin")  # id determinista
    finally:
        auth_logger.removeHandler(caplog.handler)


def test_cli_crea_o_restablece_un_administrador(make_settings):
    settings = make_settings()
    assert create_admin("soporte", "Soporte2026", "Mesa de ayuda", settings) == 0
    assert create_admin("soporte", "corta", "Mesa de ayuda", settings) == 2  # contraseña débil

    with TestClient(create_app(settings)) as client:
        token = login(client, "soporte", "Soporte2026")
        assert client.get("/api/auth/me", headers=bearer(token)).json()["user"]["role"] == "admin"

        assert create_admin("soporte", "Restablecida99", "Mesa de ayuda", settings) == 0
        assert client.post("/api/auth/login", json={"username": "soporte", "password": "Soporte2026"}).status_code == 401
        assert login(client, "soporte", "Restablecida99")


def test_comunicacion_entre_nodos_reenvia_la_autorizacion(make_settings):
    """El reenvío de caos entre nodos exige admin también en el nodo destino."""
    beta_app = create_app(make_settings(node_id="Nodo-Beta-02"))
    with TestClient(beta_app, base_url="http://beta") as beta:
        peers = (Peer(name="Nodo-Beta-02", url="http://beta"),)
        alpha_app = create_app(
            make_settings(node_id="Nodo-Alpha-01", peers=peers),
            peer_client=PeerClient(peers, timeout=2, client=beta),
        )
        with TestClient(alpha_app) as alpha:
            admin = bearer(login(alpha))
            user = bearer(register(alpha, "ivan")["access_token"])

            assert alpha.put("/api/chaos/crash/Nodo-Beta-02", json={"enabled": True}, headers=user).status_code == 403
            done = alpha.put("/api/chaos/crash/Nodo-Beta-02", json={"enabled": True}, headers=admin)
            assert done.status_code == 200 and done.json()["forwarded_by"] == "Nodo-Alpha-01"
            nodes = {n["node"]: n for n in alpha.get("/api/chaos/status", headers=admin).json()["nodes"]}
            assert nodes["Nodo-Beta-02"]["reachable"] is True and nodes["Nodo-Beta-02"]["simulate_crash"] is True
            assert beta.get("/api/orders", headers=user).status_code == 503
            alpha.put("/api/chaos/crash/Nodo-Beta-02", json={"enabled": False}, headers=admin)
            assert create_sample_order(beta, headers=user)["node"] == "Nodo-Beta-02"
