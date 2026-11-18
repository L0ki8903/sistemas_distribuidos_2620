"""Métricas y eventos de un nodo (en memoria) para el panel de monitoreo.

Cada nodo mide su propio tráfico; ``/api/monitoring/cluster`` reúne los datos de todos los nodos por
REST. No hay base de datos de métricas a propósito: si el monitoreo dependiera de db-primary, se
quedaría ciego justo cuando más se necesita (durante un failover).

- Tráfico por estado HTTP, 503 simulados por caos y respuestas servidas por la réplica.
- Latencia: últimas 2000 muestras → media, p50, p95 y máximo en la ventana pedida.
- Serie temporal en cubos de 10 s (últimos 15 min), alineados al reloj para combinar nodos.
- Eventos: se capturan de los logs que ya emite el nodo (FAILOVER, CHAOS, SYNC, AUTH…), sin tocar la
  lógica distribuida; los repetidos se agrupan con un contador.
"""
from __future__ import annotations

import logging
import math
import re
import threading
import time
from collections import Counter, deque
from typing import Any, Callable

BUCKET_SECONDS = 10
MAX_BUCKETS = 90  # 15 minutos
LATENCY_SAMPLES = 2000
MAX_EVENTS = 200

# Rutas que no cuentan como tráfico: el propio sondeo del panel, la salud y las llamadas entre nodos.
UNTRACKED_PREFIXES = ("/api/monitoring", "/api/health", "/api/chaos/local")


def route_group(path: str) -> str | None:
    if path.startswith(UNTRACKED_PREFIXES):
        return None
    for prefix, group in (
        ("/api/orders", "orders"),
        ("/api/auth", "auth"),
        ("/api/chaos", "chaos"),
        ("/api/replication", "replication"),
    ):
        if path.startswith(prefix):
            return group
    return "other"


def percentile(sorted_values: list[float], fraction: float) -> float | None:
    if not sorted_values:
        return None
    index = max(0, min(len(sorted_values) - 1, math.ceil(fraction * len(sorted_values)) - 1))
    return sorted_values[index]


def _empty_bucket(start: int) -> dict[str, Any]:
    return {
        "t": start,
        "requests": 0,
        "ok": 0,
        "client_errors": 0,
        "server_errors": 0,
        "simulated_crash": 0,
        "replica_served": 0,
        "latency_sum_ms": 0.0,
        "latency_max_ms": 0.0,
    }


class NodeMetrics:
    def __init__(self, node_id: str, clock: Callable[[], float] = time.time):
        self.node_id = node_id
        self.clock = clock
        self.started_at = clock()
        self._lock = threading.Lock()
        self.totals: Counter[str] = Counter()
        self.by_group: Counter[str] = Counter()
        self._latencies: deque[tuple[float, float]] = deque(maxlen=LATENCY_SAMPLES)
        self._buckets: deque[dict[str, Any]] = deque()
        self.events: deque[dict[str, Any]] = deque(maxlen=MAX_EVENTS)

    # ------------------------------------------------------------------ registro
    def record_request(
        self, path: str, status: int, duration_ms: float, db_used: str | None = None, simulated_crash: bool = False
    ) -> None:
        group = route_group(path)
        if group is None:
            return
        now = self.clock()
        kind = "ok" if status < 400 else "client_errors" if status < 500 else "server_errors"
        with self._lock:
            self.totals["requests"] += 1
            self.totals[kind] += 1
            self.by_group[group] += 1
            if simulated_crash:
                self.totals["simulated_crash"] += 1
            if db_used == "replica_failover":
                self.totals["replica_served"] += 1
            self._latencies.append((now, duration_ms))

            bucket = self._bucket_for(now)
            bucket["requests"] += 1
            bucket[kind] += 1
            bucket["simulated_crash"] += int(simulated_crash)
            bucket["replica_served"] += int(db_used == "replica_failover")
            bucket["latency_sum_ms"] += duration_ms
            bucket["latency_max_ms"] = max(bucket["latency_max_ms"], duration_ms)

    def _bucket_for(self, now: float) -> dict[str, Any]:
        start = int(now // BUCKET_SECONDS) * BUCKET_SECONDS
        if not self._buckets or self._buckets[-1]["t"] != start:
            self._buckets.append(_empty_bucket(start))
            while len(self._buckets) > MAX_BUCKETS:
                self._buckets.popleft()
        return self._buckets[-1]

    def add_event(self, level: str, kind: str, message: str) -> None:
        now = self.clock()
        with self._lock:
            last = self.events[-1] if self.events else None
            if last and last["kind"] == kind and last["message"] == message and last["level"] == level:
                last["count"] += 1
                last["at"] = now
                return
            self.events.append({"at": now, "level": level, "kind": kind, "message": message, "count": 1})

    # ------------------------------------------------------------------ lectura
    def snapshot(self, window_seconds: int = 300) -> dict[str, Any]:
        now = self.clock()
        with self._lock:
            recent = sorted(ms for ts, ms in self._latencies if now - ts <= window_seconds)
            buckets = {b["t"]: dict(b) for b in self._buckets}
            totals = dict(self.totals)
            by_group = dict(self.by_group)
            events = [dict(e) for e in reversed(self.events)]

        last_bucket = int(now // BUCKET_SECONDS) * BUCKET_SECONDS
        count = max(1, min(MAX_BUCKETS, window_seconds // BUCKET_SECONDS))
        series = []
        for start in range(last_bucket - (count - 1) * BUCKET_SECONDS, last_bucket + 1, BUCKET_SECONDS):
            b = buckets.get(start) or _empty_bucket(start)
            b["latency_avg_ms"] = round(b["latency_sum_ms"] / b["requests"], 2) if b["requests"] else None
            b["latency_max_ms"] = round(b["latency_max_ms"], 2)
            del b["latency_sum_ms"]
            series.append(b)

        in_window = [b for b in series if now - b["t"] <= window_seconds]
        window_requests = sum(b["requests"] for b in in_window)
        window_errors = sum(b["server_errors"] for b in in_window)
        last_minute = sum(b["requests"] for b in series if now - b["t"] < 60)
        return {
            "uptime_seconds": round(now - self.started_at, 1),
            "started_at": self.started_at,
            "window_seconds": window_seconds,
            "bucket_seconds": BUCKET_SECONDS,
            "traffic": {
                "total": {k: totals.get(k, 0) for k in ("requests", "ok", "client_errors", "server_errors", "simulated_crash", "replica_served")},
                "by_group": by_group,
                "window_requests": window_requests,
                "window_server_errors": window_errors,
                "window_replica_served": sum(b["replica_served"] for b in in_window),
                "window_simulated_crash": sum(b["simulated_crash"] for b in in_window),
                "requests_per_minute": last_minute,
                "error_rate": round(window_errors / window_requests, 4) if window_requests else 0.0,
            },
            "latency_ms": {
                "samples": len(recent),
                "avg": round(sum(recent) / len(recent), 2) if recent else None,
                "p50": round(percentile(recent, 0.50), 2) if recent else None,
                "p95": round(percentile(recent, 0.95), 2) if recent else None,
                "max": round(recent[-1], 2) if recent else None,
            },
            "timeseries": series,
            "events": events[:60],
        }


# ---------------------------------------------------------------------- eventos desde los logs
_EVENT_KINDS = {
    "FAILOVER": "failover",
    "WRITE": "failover",
    "REPLICATION": "replication",
    "SYNC": "sync",
    "CHAOS": "chaos",
    "AUTH": "auth",
    "ADMIN": "auth",
    "SCHEMA": "schema",
    "SEGURIDAD": "security",
    "Nodo": "lifecycle",
}
# Mensajes INFO que sí son eventos (el resto de INFO, como "REPLICATION ... OK", es ruido).
_INFO_KINDS = {"sync", "schema", "lifecycle"}
_INFO_PREFIXES = ("ADMIN inicial",)
_REDACTIONS = (re.compile(r"(contraseña generada: )\S+"),)


class EventLogHandler(logging.Handler):
    """Convierte los logs relevantes del nodo en eventos del panel de monitoreo."""

    def __init__(self, metrics: NodeMetrics):
        super().__init__(level=logging.INFO)
        self.metrics = metrics

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            kind = _EVENT_KINDS.get(message.split(" ", 1)[0], "other")
            if record.levelno < logging.WARNING and kind not in _INFO_KINDS and not message.startswith(_INFO_PREFIXES):
                return
            for pattern in _REDACTIONS:  # nunca exponer secretos en el panel
                message = pattern.sub(r"\1[oculta]", message)
            self.metrics.add_event(record.levelname.lower(), kind, message[:300])
        except Exception:  # noqa: BLE001 - un handler de logging nunca debe romper la petición
            self.handleError(record)


# ---------------------------------------------------------------------- gateway (NGINX stub_status)
_STUB_RE = re.compile(
    r"Active connections:\s*(?P<active>\d+).*?"
    r"(?P<accepts>\d+)\s+(?P<handled>\d+)\s+(?P<requests>\d+).*?"
    r"Reading:\s*(?P<reading>\d+)\s+Writing:\s*(?P<writing>\d+)\s+Waiting:\s*(?P<waiting>\d+)",
    re.S,
)


def parse_stub_status(text: str) -> dict[str, int]:
    match = _STUB_RE.search(text)
    if not match:
        raise ValueError("Formato de stub_status no reconocido")
    return {key: int(value) for key, value in match.groupdict().items()}
