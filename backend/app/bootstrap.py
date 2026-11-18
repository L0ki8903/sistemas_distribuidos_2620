"""Creación del administrador inicial al arrancar un nodo."""
from __future__ import annotations

import logging
import secrets

from sqlalchemy.exc import IntegrityError

from .config import Settings
from .database import DatabaseCluster, DatabaseUnavailableError, users_table
from .security import hash_password
from .users import ensure_admin, find_user_row

logger = logging.getLogger("pedidos.auth")


def bootstrap_admin(cluster: DatabaseCluster, settings: Settings) -> str:
    """Garantiza que exista ``ADMIN_USERNAME``. Devuelve "exists", "created", "race" o "unavailable".

    - Si ya existe, no se toca (cambiar ADMIN_PASSWORD después NO cambia la contraseña; para eso
      está ``python -m app.cli create-admin``).
    - Si ``ADMIN_PASSWORD`` está vacío, se genera una contraseña aleatoria y se muestra una sola vez
      en el log del nodo que crea al administrador.
    """
    username = settings.admin_username.lower()
    try:
        existing, _ = cluster.read(lambda session: find_user_row(session, username))
    except DatabaseUnavailableError:
        logger.warning("ADMIN no verificado: ninguna base disponible (se reintentará en el próximo arranque)")
        return "unavailable"
    if existing is not None:
        if existing["role"] != "admin":
            logger.error("ADMIN '%s' existe pero NO tiene rol admin; usa 'python -m app.cli create-admin'", username)
        return "exists"

    generated = not settings.admin_password
    password = settings.admin_password or secrets.token_urlsafe(12)
    password_hash = hash_password(password, settings.password_hash_iterations)
    try:
        result = cluster.write(lambda session: ensure_admin(session, username, password_hash), table=users_table)
    except IntegrityError:
        logger.info("ADMIN '%s' ya lo creó otro nodo", username)
        return "race"
    except DatabaseUnavailableError:
        logger.warning("ADMIN no creado: ninguna base disponible")
        return "unavailable"
    if not result.info:
        return "exists"
    if generated:
        logger.warning(
            "ADMIN inicial creado -> usuario: %s | contraseña generada: %s "
            "(guárdala; defínela con ADMIN_PASSWORD o cámbiala con 'python -m app.cli create-admin')",
            username, password,
        )
    else:
        logger.info("ADMIN inicial '%s' creado con la contraseña de ADMIN_PASSWORD", username)
    return "created"
