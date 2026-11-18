"""Modelo SQLAlchemy de la tabla ``orders`` (idéntico en db-primary y db-replica)."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import JSON, CheckConstraint, DateTime, Index, Numeric, String
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

    __table_args__ = (
        CheckConstraint(
            "status IN ('pendiente', 'en proceso', 'enviado', 'entregado')",
            name="ck_orders_status",
        ),
        Index("ix_orders_status", "status"),
        Index("ix_orders_updated_at", "updated_at"),
    )


ORDER_COLUMNS: tuple[str, ...] = tuple(column.name for column in Order.__table__.columns)
