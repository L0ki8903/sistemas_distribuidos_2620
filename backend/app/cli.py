"""Utilidades de administración por línea de comandos.

Uso (dentro de un contenedor de nodo, p. ej. ``docker compose exec backend-node-1 ...`` o la
terminal de Coolify)::

    python -m app.cli create-admin --username admin --password 'NuevaClave2026'

Crea el administrador o, si ya existe, restablece su contraseña y lo reactiva. La escritura pasa por
la misma capa distribuida (escritura dual + failover), así que queda en ambas bases.
"""
from __future__ import annotations

import argparse
import sys

from pydantic import ValidationError

from .chaos import ChaosState
from .config import Settings
from .database import DatabaseCluster, DatabaseUnavailableError, users_table
from .schemas import RegisterRequest
from .security import hash_password
from .users import upsert_admin


def create_admin(username: str, password: str, full_name: str, settings: Settings | None = None) -> int:
    settings = settings or Settings.from_env()
    try:
        data = RegisterRequest(username=username, full_name=full_name, password=password)
    except ValidationError as exc:
        for error in exc.errors():
            print(f"Dato inválido ({'.'.join(map(str, error['loc']))}): {error['msg']}", file=sys.stderr)
        return 2
    cluster = DatabaseCluster(settings, ChaosState(settings.node_id))
    try:
        cluster.ensure_schema()
        password_hash = hash_password(data.password, settings.password_hash_iterations)
        result = cluster.write(
            lambda session: upsert_admin(session, data.username, password_hash, data.full_name), table=users_table
        )
    except DatabaseUnavailableError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        cluster.dispose()
    action = "creado" if result.info else "actualizado (contraseña restablecida)"
    print(f"Administrador '{data.username}' {action} · db_used={result.db_used} · replicación={result.replication}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="Administración del sistema de pedidos")
    sub = parser.add_subparsers(dest="command", required=True)
    admin = sub.add_parser("create-admin", help="Crear un administrador o restablecer su contraseña")
    admin.add_argument("--username", required=True)
    admin.add_argument("--password", required=True)
    admin.add_argument("--full-name", default="Administrador")
    args = parser.parse_args(argv)
    if args.command == "create-admin":
        return create_admin(args.username, args.password, args.full_name)
    return 1  # pragma: no cover


if __name__ == "__main__":
    sys.exit(main())
