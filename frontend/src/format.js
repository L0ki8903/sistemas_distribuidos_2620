export const STATUSES = ["pendiente", "en proceso", "enviado", "entregado"];

export const STATUS_LABELS = {
  pendiente: "Pendiente",
  "en proceso": "En proceso",
  enviado: "Enviado",
  entregado: "Entregado",
};

const money = new Intl.NumberFormat("es-CO", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

export function formatMoney(value) {
  return `$ ${money.format(Number(value) || 0)}`;
}

export function formatDateTime(value) {
  if (!value) return "—";
  return new Date(value).toLocaleString("es-CO", { dateStyle: "short", timeStyle: "short" });
}

export function formatClock(value) {
  if (!value) return "—";
  return new Date(value).toLocaleTimeString("es-CO", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

const integer = new Intl.NumberFormat("es-CO", { maximumFractionDigits: 0 });

export function formatNumber(value) {
  return value == null ? "—" : integer.format(value);
}

export function formatPercent(fraction) {
  if (fraction == null) return "—";
  return `${(fraction * 100).toLocaleString("es-CO", { maximumFractionDigits: 1 })} %`;
}

export function formatDuration(seconds) {
  if (seconds == null) return "—";
  const s = Math.floor(seconds);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (h > 0) return `${h} h ${String(m).padStart(2, "0")} min`;
  if (m > 0) return `${m} min ${String(s % 60).padStart(2, "0")} s`;
  return `${s} s`;
}

export function shortId(id) {
  return id ? id.slice(0, 8) : "—";
}

/** Tono visual estable por nodo (Alpha = azul, Beta = naranja; par validado para daltonismo). */
export function nodeTone(node) {
  if (!node) return "unknown";
  if (/alpha/i.test(node)) return "alpha";
  if (/beta/i.test(node)) return "beta";
  return "other";
}

export function dbLabel(dbUsed) {
  if (dbUsed === "primary") return "Primary";
  if (dbUsed === "replica_failover") return "Replica Failover";
  return "—";
}

/** "503, 200" en X-Upstream-Status => NGINX reintentó en otro nodo. */
export function upstreamAttempts(upstreamStatus) {
  if (!upstreamStatus) return [];
  return upstreamStatus
    .split(/[,:]/)
    .map((s) => s.trim())
    .filter(Boolean);
}
