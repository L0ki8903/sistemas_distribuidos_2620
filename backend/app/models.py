"""Modelo SQLAlchemy de la tabla ``orders`` (idéntico en db-primary y db-replica)."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import JSON, Boolean, CheckConstraint, DateTime, Index, Numeric, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

ORDER_STATUSES: tuple[str, ...] = ("pendiente", "en proceso", "enviado", "entregado")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """Fecha/hora siempre en UTC con zona horaria.

    PostgreSQL la guarda como ``TIMESTAMPTZ``; SQLite (tests) como texto UTC sin zona.
    Al leer siempre se devuelve un ``datetime`` *aware* en UTC, de modo que las comparaciones
    last-write-wins entre ambas bases son consistentes.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        value = value.astimezone(timezone.utc)
        if dialect.name == "sqlite":
            return value.replace(tzinfo=None)
        return value

    def process_result_value(self, value: datetime | None, dialect: Dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class Base(DeclarativeBase):
    pass


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    customer_name: Mapped[str] = mapped_column(String(120), nullable=False)
    items: Mapped[list] = mapped_column(JSON().with_variant(JSONB(), "postgresql"), nullable=False)
    total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pendiente")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    # Borrado lógico (tombstone): permite replicar y reconciliar también las eliminaciones.
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    handled_by_node: Mapped[str] = mapped_column(String(50), nullable=False)
    # Usuario dueño del pedido (sin FOREIGN KEY a propósito: las tablas se replican y reconcilian
    # por separado y una FK podría bloquear la copia si el pedido llega antes que su usuario).
    owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('pendiente', 'en proceso', 'enviado', 'entregado')",
            name="ck_orders_status",
        ),
        Index("ix_orders_status", "status"),
        Index("ix_orders_updated_at", "updated_at"),
        Index("ix_orders_owner_id", "owner_id"),
    )


USER_ROLES: tuple[str, ...] = ("user", "admin")


class User(Base):
    """Usuarios del sistema. Se replica igual que ``orders`` (escritura dual + reconciliación)."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    username: Mapped[str] = mapped_column(String(50), nullable=False)
    full_name: Mapped[str] = mapped_column(String(120), nullable=False)
    # PBKDF2-SHA256: "pbkdf2_sha256$<iteraciones>$<salt b64>$<hash b64>". Nunca texto plano.
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(10), nullable=False, default="user")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    __table_args__ = (
        CheckConstraint("role IN ('user', 'admin')", name="ck_users_role"),
        UniqueConstraint("username", name="uq_users_username"),
        Index("ix_users_updated_at", "updated_at"),
    )


ORDER_COLUMNS: tuple[str, ...] = tuple(column.name for column in Order.__table__.columns)
USER_COLUMNS: tuple[str, ...] = tuple(column.name for column in User.__table__.columns)

# Tablas que se replican entre db-primary y db-replica (todas con id, updated_at y deleted_at).
REPLICATED_TABLES = (Order.__table__, User.__table__)

# Migraciones aditivas para bases creadas con versiones anteriores del esquema.
SCHEMA_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("orders", "owner_id", "VARCHAR(36)"),
)
