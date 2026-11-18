"""Registro de auditoría (``audit_log``): quién cambió qué, cuándo y en qué nodo."""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .database import queue_replication
from .models import AUDIT_COLUMNS, AuditEntry, utcnow
from .security import Actor


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def diff(before: dict[str, Any], after: dict[str, Any], fields: tuple[str, ...]) -> dict[str, list[Any]]:
    """``{campo: [antes, después]}`` sólo de los campos que cambiaron."""
    return {f: [_jsonable(before.get(f)), _jsonable(after.get(f))] for f in fields if before.get(f) != after.get(f)}


def row_of_audit(entry: AuditEntry) -> dict[str, Any]:
    return {name: getattr(entry, name) for name in AUDIT_COLUMNS}


def record(
    session: Session,
    actor: Actor | None,
    node_id: str,
    action: str,
    entity: str,
    entity_id: str,
    summary: str,
    changes: dict[str, Any] | None = None,
    actor_label: str = "sistema",
) -> dict[str, Any]:
    """Agrega una entrada en la transacción actual y la deja en cola para replicarla."""
    now = utcnow()
    entry = AuditEntry(
        id=str(uuid.uuid4()),
        created_at=now,
        updated_at=now,
        actor_id=actor.id if actor else None,
        actor_username=actor.username if actor else actor_label,
        action=action,
        entity=entity,
        entity_id=entity_id,
        summary=summary[:300],
        changes=_jsonable(changes) if changes else None,
        node=node_id,
    )
    session.add(entry)
    session.flush()
    row = row_of_audit(entry)
    queue_replication(session, AuditEntry.__table__, row)
    return row


def to_public_audit(row: dict[str, Any]) -> dict[str, Any]:
    return {key: row[key] for key in ("id", "created_at", "actor_id", "actor_username", "action", "entity", "entity_id", "summary", "changes", "node")}


def list_audit(
    session: Session,
    entity: str | None = None,
    entity_id: str | None = None,
    actor: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    query = select(AuditEntry)
    if entity:
        query = query.where(AuditEntry.entity == entity)
    if entity_id:
        query = query.where(AuditEntry.entity_id == entity_id)
    if actor:
        query = query.where(AuditEntry.actor_username == actor.lower())
    query = query.order_by(AuditEntry.created_at.desc()).limit(limit)
    return [to_public_audit(row_of_audit(entry)) for entry in session.scalars(query)]
