"""Fixtures comunes.

Por defecto cada test usa DOS bases SQLite en archivos temporales (una "primaria" y una
"réplica"), de modo que la escritura dual, el failover y la reconciliación se prueban de verdad
sin necesitar PostgreSQL. Para ejecutar la misma suite contra PostgreSQL real, exporta
``TEST_PRIMARY_DB_URL`` y ``TEST_REPLICA_DB_URL`` (¡las tablas se vacían en cada test!).

Autenticación: el fixture ``client`` ya inició sesión como el administrador inicial; ``raw_client``
no tiene sesión (para probar permisos se pasan cabeceras explícitas con ``bearer()``).
"""
from __future__ import annotations

import os
from typing import Any, Callable

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.config import Settings
from app.database import PRIMARY, REPLICA, build_engine
from app.main import create_app
from app.models import REPLICATED_TABLES, Base, Order, User
from app.security import create_access_token

EXTERNAL_PRIMARY = os.getenv("TEST_PRIMARY_DB_URL")
EXTERNAL_REPLICA = os.getenv("TEST_REPLICA_DB_URL")
USING_EXTERNAL_DB = bool(EXTERNAL_PRIMARY and EXTERNAL_REPLICA)

ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "Admin12345"
TEST_JWT_SECRET = "secreto-de-pruebas-compartido-por-los-nodos-0123456789"
USER_PASSWORD = "Cliente123"

SAMPLE_ITEMS = [
    {"product": "Laptop", "quantity": 1, "unit_price": 1200.0},
    {"product": "Mouse", "quantity": 2, "unit_price": 25.5},
]


@pytest.fixture
def db_urls(tmp_path) -> tuple[str, str]:
    if USING_EXTERNAL_DB:
        return EXTERNAL_PRIMARY, EXTERNAL_REPLICA  # type: ignore[return-value]
    return (
        f"sqlite:///{(tmp_path / 'primary.db').as_posix()}",
        f"sqlite:///{(tmp_path / 'replica.db').as_posix()}",
    )


@pytest.fixture
def broken_db_url(tmp_path) -> str:
    """URL de una base inalcanzable de verdad (el directorio no existe)."""
    return f"sqlite:///{(tmp_path / 'no_existe' / 'caida.db').as_posix()}"


@pytest.fixture
def make_settings(db_urls) -> Callable[..., Settings]:
    def _make(**overrides: Any) -> Settings:
        primary, replica = db_urls
        values: dict[str, Any] = {
            "node_id": "Nodo-Alpha-01",
            "primary_db_url": primary,
            "replica_db_url": replica,
            "peers": (),
            "sync_interval_seconds": 0,  # los tests disparan la reconciliación explícitamente
            "breaker_cooldown_seconds": 0.0,
            "jwt_secret": TEST_JWT_SECRET,
            "admin_username": ADMIN_USERNAME,
            "admin_password": ADMIN_PASSWORD,
            "password_hash_iterations": 1_000,  # rápido en tests; en producción 600 000
        }
        values.update(overrides)
        return Settings(**values)

    return _make


@pytest.fixture(autouse=True)
def clean_external_databases():
    """Con PostgreSQL real, cada test parte de tablas vacías (con SQLite ya son archivos nuevos)."""
    if USING_EXTERNAL_DB:
        for url in (EXTERNAL_PRIMARY, EXTERNAL_REPLICA):
            engine = build_engine(url, connect_timeout=3)
            Base.metadata.create_all(engine)
            with engine.begin() as conn:
                for table in REPLICATED_TABLES:
                    conn.execute(delete(table))
            engine.dispose()
    yield


# ------------------------------------------------------------------------ autenticación
def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def login(client: TestClient, username: str = ADMIN_USERNAME, password: str = ADMIN_PASSWORD) -> str:
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def authenticate(client: TestClient, username: str = ADMIN_USERNAME, password: str = ADMIN_PASSWORD) -> TestClient:
    """Deja la sesión en las cabeceras por defecto del cliente."""
    client.headers["Authorization"] = f"Bearer {login(client, username, password)}"
    return client


def register(client: TestClient, username: str, password: str = USER_PASSWORD, full_name: str = "Cliente Demo") -> dict:
    response = client.post(
        "/api/auth/register", json={"username": username, "full_name": full_name, "password": password}
    )
    assert response.status_code == 201, response.text
    return response.json()


def token_for(settings: Settings, user_id: str = "sin-bd", username: str = ADMIN_USERNAME, role: str = "admin") -> str:
    """Token firmado directamente (para escenarios en los que no hay BD para iniciar sesión)."""
    return create_access_token(user_id, username, role, settings)[0]


# ------------------------------------------------------------------------ app y clientes
@pytest.fixture
def app(make_settings):
    return create_app(make_settings())


@pytest.fixture
def raw_client(app):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def client(raw_client):
    return authenticate(raw_client)


class DbInspector:
    """Lee directamente db-primary y db-replica (sin pasar por la API)."""

    def __init__(self, app) -> None:
        self.engines = app.state.cluster.engines

    def get(self, name: str, order_id: str, table=Order.__table__) -> dict[str, Any] | None:
        with self.engines[name].connect() as conn:
            row = conn.execute(select(table).where(table.c.id == order_id)).mappings().first()
            return dict(row) if row else None

    def count(self, name: str, table=Order.__table__) -> int:
        with self.engines[name].connect() as conn:
            return len(conn.execute(select(table.c.id)).all())

    def user(self, name: str, username: str) -> dict[str, Any] | None:
        table = User.__table__
        with self.engines[name].connect() as conn:
            row = conn.execute(select(table).where(table.c.username == username)).mappings().first()
            return dict(row) if row else None

    def primary(self, order_id: str) -> dict[str, Any] | None:
        return self.get(PRIMARY, order_id)

    def replica(self, order_id: str) -> dict[str, Any] | None:
        return self.get(REPLICA, order_id)


@pytest.fixture
def db(app) -> DbInspector:
    return DbInspector(app)


def create_sample_order(client: TestClient, customer_name: str = "Ana Torres", items=None, headers=None) -> dict[str, Any]:
    response = client.post(
        "/api/orders", json={"customer_name": customer_name, "items": items or SAMPLE_ITEMS}, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()
