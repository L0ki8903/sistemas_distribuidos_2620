"""Comunicación REST entre nodos backend (propagación de caos y vista del clúster)."""
from __future__ import annotations

import logging
from typing import Any

import httpx

from .config import Peer

logger = logging.getLogger("pedidos.peers")

# Cabecera que marca una petición reenviada por otro nodo: evita bucles de reenvío.
FORWARDED_HEADER = "X-Chaos-Forwarded-By"


class PeerClient:
    def __init__(self, peers: tuple[Peer, ...], timeout: float, client: httpx.Client | None = None):
        self.peers = peers
        self._owns_client = client is None
        self.client = client or httpx.Client(timeout=timeout)

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def find(self, node_name: str) -> Peer | None:
        for peer in self.peers:
            if peer.name == node_name:
                return peer
        return None

    def request(self, peer: Peer, method: str, path: str, origin_node: str, **kwargs: Any) -> httpx.Response:
        headers = {FORWARDED_HEADER: origin_node, **kwargs.pop("headers", {})}
        return self.client.request(method, f"{peer.url}{path}", headers=headers, **kwargs)

    def local_state(self, peer: Peer, origin_node: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
        """Estado de caos de un peer; si no responde se marca como inalcanzable."""
        try:
            response = self.request(peer, "GET", "/api/chaos/local", origin_node, headers=headers or {})
            if response.status_code in (401, 403):
                raise httpx.HTTPError(
                    f"el nodo rechazó la autenticación (HTTP {response.status_code}): ¿JWT_SECRET distinto entre nodos?"
                )
            response.raise_for_status()
            data = response.json()
            if peer.name and data.get("node") != peer.name:
                logger.warning("PEER %s respondió como %s", peer.name, data.get("node"))
            return {**data, "reachable": True, "url": peer.url}
        except (httpx.HTTPError, ValueError) as exc:
            return {
                "node": peer.name,
                "url": peer.url,
                "reachable": False,
                "simulate_crash": None,
                "simulate_db_primary_down": None,
                "error": str(exc) or exc.__class__.__name__,
            }
