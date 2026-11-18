"""Fixtures comunes.

Por defecto cada test usa DOS bases SQLite en archivos temporales (una "primaria" y una
"réplica"), de modo que la escritura dual, el failover y la reconciliación se prueban de verdad
sin necesitar PostgreSQL. Para ejecutar la misma suite contra PostgreSQL real, exporta
``TEST_PRIMARY_DB_URL`` y ``TEST_REPLICA_DB_URL`` (¡las tablas se vacían en cada test!).
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
from app.models import Base, Order

EXTERNAL_PRIMARY = os.getenv("TEST_PRIMARY_DB_URL")
EXTERNAL_REPLICA = os.getenv("TEST_REPLICA_DB_URL")
USING_EXTERNAL_DB = bool(EXTERNAL_PRIMARY and EXTERNAL_REPLICA)

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
                conn.execute(delete(Order.__table__))
            engine.dispose()
    yield


@pytest.fixture
def app(make_settings):
    return create_app(make_settings())


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


class DbInspector:
    """Lee directamente db-primary y db-replica (sin pasar por la API)."""

    def __init__(self, app) -> None:
        self.engines = app.state.cluster.engines

    def get(self, name: str, order_id: str) -> dict[str, Any] | None:
        table = Order.__table__
        with self.engines[name].connect() as conn:
            row = conn.execute(select(table).where(table.c.id == order_id)).mappings().first()
            return dict(row) if row else None

    def count(self, name: str) -> int:
        with self.engines[name].connect() as conn:
            return len(conn.execute(select(Order.__table__.c.id)).all())

    def primary(self, order_id: str) -> dict[str, Any] | None:
        return self.get(PRIMARY, order_id)

    def replica(self, order_id: str) -> dict[str, Any] | None:
        return self.get(REPLICA, order_id)


@pytest.fixture
def db(app) -> DbInspector:
    return DbInspector(app)


def create_sample_order(client: TestClient, customer_name: str = "Ana Torres", items=None) -> dict[str, Any]:
    response = client.post("/api/orders", json={"customer_name": customer_name, "items": items or SAMPLE_ITEMS})
    assert response.status_code == 201, response.text
    return response.json()
