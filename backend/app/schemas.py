"""Esquemas Pydantic v2 de entrada/salida de la API."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

OrderStatus = Literal["pendiente", "en proceso", "enviado", "entregado"]
DbUsed = Literal["primary", "replica_failover"]
ReplicationState = Literal["synced", "pending"]


class OrderItem(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    product: str = Field(min_length=1, max_length=120, examples=["Laptop"])
    quantity: int = Field(ge=1, le=10_000, examples=[1])
    unit_price: float = Field(ge=0, le=10_000_000, examples=[1200.0])


class OrderCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: uuid.UUID | None = Field(
        default=None,
        description="UUID opcional generado por el cliente: hace la creación idempotente ante reintentos.",
    )
    customer_name: str = Field(min_length=2, max_length=120, examples=["Ana Torres"])
    items: list[OrderItem] = Field(min_length=1, max_length=50)


class OrderUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    customer_name: str | None = Field(default=None, min_length=2, max_length=120)
    items: list[OrderItem] | None = Field(default=None, min_length=1, max_length=50)
    status: OrderStatus | None = None

    @model_validator(mode="after")
    def at_least_one_field(self) -> "OrderUpdate":
        if self.customer_name is None and self.items is None and self.status is None:
            raise ValueError("Debe enviar al menos uno de: customer_name, items, status")
        return self


class OrderOut(BaseModel):
    id: str
    customer_name: str
    items: list[OrderItem]
    total: float
    status: OrderStatus
    next_status: OrderStatus | None = Field(description="Siguiente estado permitido (None si ya fue entregado)")
    created_at: datetime
    updated_at: datetime
    handled_by_node: str = Field(description="Nodo que procesó la última escritura del pedido")


class OrderResponse(BaseModel):
    node: str
    db_used: DbUsed
    replication: ReplicationState
    data: OrderOut


class OrderDetailResponse(BaseModel):
    node: str
    db_used: DbUsed
    data: OrderOut


class OrderListResponse(BaseModel):
    node: str
    db_used: DbUsed
    count: int
    data: list[OrderOut]


class DeletedOrder(BaseModel):
    id: str
    deleted: bool = True
    already_deleted: bool = False


class DeleteResponse(BaseModel):
    node: str
    db_used: DbUsed
    replication: ReplicationState
    data: DeletedOrder


class Switch(BaseModel):
    enabled: bool = Field(description="true = activar la simulación de fallo, false = restaurar")
