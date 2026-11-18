"""Esquemas Pydantic v2 de entrada/salida de la API."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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
    owner_id: str | None = Field(default=None, description="Usuario dueño del pedido")
    owner_username: str | None = Field(default=None, description="Usuario dueño (sólo en listados)")


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


# ---------------------------------------------------------------------- autenticación
# Ojo: estos modelos NO usan str_strip_whitespace, para no alterar las contraseñas.
UserRole = Literal["user", "admin"]
USERNAME_PATTERN = r"^[A-Za-z0-9_.-]{3,50}$"


class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(pattern=USERNAME_PATTERN, examples=["ana.torres"],
                          description="3-50 caracteres: letras, números, punto, guion y guion bajo")
    full_name: str = Field(min_length=2, max_length=120, examples=["Ana Torres"])
    password: str = Field(min_length=8, max_length=128, examples=["Pedidos2026"],
                          description="Mínimo 8 caracteres, con al menos una letra y un número")

    @field_validator("username")
    @classmethod
    def normalize_username(cls, value: str) -> str:
        return value.lower()

    @field_validator("full_name")
    @classmethod
    def strip_full_name(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 2:
            raise ValueError("El nombre debe tener al menos 2 caracteres")
        return value

    @field_validator("password")
    @classmethod
    def password_strength(cls, value: str) -> str:
        if not any(c.isalpha() for c in value) or not any(c.isdigit() for c in value):
            raise ValueError("La contraseña debe tener al menos una letra y un número")
        return value


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=50, examples=["admin"])
    password: str = Field(min_length=1, max_length=128)

    @field_validator("username")
    @classmethod
    def normalize_username(cls, value: str) -> str:
        return value.strip().lower()


class UserOut(BaseModel):
    id: str
    username: str
    full_name: str
    role: UserRole
    is_active: bool
    created_at: datetime


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int = Field(description="Segundos de validez del token")
    user: UserOut
    node: str
    db_used: DbUsed


class MeResponse(BaseModel):
    node: str
    user: UserOut


# ---------------------------------------------------------------------- administración
def _check_password_strength(value: str) -> str:
    if not any(c.isalpha() for c in value) or not any(c.isdigit() for c in value):
        raise ValueError("La contraseña debe tener al menos una letra y un número")
    return value


class AdminUserUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    full_name: str | None = Field(default=None, min_length=2, max_length=120)
    role: UserRole | None = None
    is_active: bool | None = None

    @field_validator("full_name")
    @classmethod
    def strip_full_name(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else value

    @model_validator(mode="after")
    def at_least_one_field(self) -> "AdminUserUpdate":
        if self.full_name is None and self.role is None and self.is_active is None:
            raise ValueError("Debe enviar al menos uno de: full_name, role, is_active")
        return self


class PasswordReset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    new_password: str = Field(min_length=8, max_length=128)

    @field_validator("new_password")
    @classmethod
    def password_strength(cls, value: str) -> str:
        return _check_password_strength(value)


class AdminOrderUpdate(BaseModel):
    """Corrección administrativa: a diferencia de PATCH /api/orders, admite cualquier estado."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    customer_name: str | None = Field(default=None, min_length=2, max_length=120)
    items: list[OrderItem] | None = Field(default=None, min_length=1, max_length=50)
    status: OrderStatus | None = None

    @model_validator(mode="after")
    def at_least_one_field(self) -> "AdminOrderUpdate":
        if self.customer_name is None and self.items is None and self.status is None:
            raise ValueError("Debe enviar al menos uno de: customer_name, items, status")
        return self


class AdminUserOut(BaseModel):
    id: str
    username: str
    full_name: str
    role: UserRole
    is_active: bool
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None
    orders_count: int


class AuditOut(BaseModel):
    id: str
    created_at: datetime
    actor_id: str | None
    actor_username: str
    action: str
    entity: str
    entity_id: str
    summary: str
    changes: dict | None
    node: str
