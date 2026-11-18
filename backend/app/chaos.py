"""Estado de Chaos Engineering del nodo (banderas en memoria, por proceso)."""
from __future__ import annotations

import threading
from datetime import datetime, timezone


class ChaosState:
    """Banderas globales del nodo.

    - ``simulate_crash`` (SIMULATE_CRASH): el middleware responde HTTP 503 en ``/api/orders*``
      para que NGINX salte al otro nodo.
    - ``simulate_db_primary_down`` (SIMULATE_DB_PRIMARY_DOWN): el acceso a ``db-primary`` se
      trata como fallido y se conmuta a ``db-replica``.
    """

    def __init__(self, node_id: str, simulate_crash: bool = False, simulate_db_primary_down: bool = False):
        self.node_id = node_id
        self._lock = threading.Lock()
        self._simulate_crash = simulate_crash
        self._simulate_db_primary_down = simulate_db_primary_down
        self.updated_at = datetime.now(timezone.utc)

    @property
    def simulate_crash(self) -> bool:
        return self._simulate_crash

    @property
    def simulate_db_primary_down(self) -> bool:
        return self._simulate_db_primary_down

    def set_crash(self, enabled: bool) -> bool:
        with self._lock:
            changed = self._simulate_crash != enabled
            self._simulate_crash = enabled
            self.updated_at = datetime.now(timezone.utc)
            return changed

    def set_db_primary_down(self, enabled: bool) -> bool:
        with self._lock:
            changed = self._simulate_db_primary_down != enabled
            self._simulate_db_primary_down = enabled
            self.updated_at = datetime.now(timezone.utc)
            return changed

    def snapshot(self) -> dict:
        return {
            "node": self.node_id,
            "simulate_crash": self._simulate_crash,
            "simulate_db_primary_down": self._simulate_db_primary_down,
            "updated_at": self.updated_at.isoformat(),
        }
