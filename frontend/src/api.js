// Cliente de la API. SIEMPRE usa rutas relativas (/api/...): el gateway NGINX las balancea.
import { getToken } from "./auth.js";

const listeners = new Set();
const unauthorizedListeners = new Set();
let sequence = 0;

/** Se dispara cuando el servidor rechaza la sesión (401): token expirado o usuario desactivado. */
export function onUnauthorized(listener) {
  unauthorizedListeners.add(listener);
  return () => unauthorizedListeners.delete(listener);
}

export function onApiEvent(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export class ApiError extends Error {
  constructor(message, status, meta, body) {
    super(message);
    this.status = status;
    this.meta = meta;
    this.body = body;
  }
}

function parseJson(text) {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

function errorMessage(status, body) {
  const detail = body?.detail;
  if (Array.isArray(detail)) {
    return detail.map((d) => `${(d.loc || []).slice(1).join(".") || "dato"}: ${d.msg}`).join(" · ");
  }
  if (typeof detail === "string") return detail;
  if (status === 502 || status === 504) return "El gateway no encontró ningún nodo backend disponible";
  return `Error HTTP ${status}`;
}

/** Genera un UUID v4 (con respaldo cuando crypto.randomUUID no existe, p. ej. HTTP sin TLS). */
export function uuid() {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
  const bytes = globalThis.crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

async function request(method, path, { body, silent = false, auth = true } = {}) {
  const started = performance.now();
  let response;
  let data = null;
  let networkError = null;
  const headers = {};
  if (body) headers["Content-Type"] = "application/json";
  const token = auth ? getToken() : null;
  if (token) headers.Authorization = `Bearer ${token}`;
  try {
    response = await fetch(path, {
      method,
      cache: "no-store",
      headers,
      body: body ? JSON.stringify(body) : undefined,
    });
    const text = await response.text();
    data = text ? parseJson(text) : null;
  } catch (error) {
    networkError = error;
  }

  const header = (name) => response?.headers.get(name) ?? null;
  const meta = {
    id: ++sequence,
    at: new Date(),
    method,
    path,
    silent,
    status: response?.status ?? 0,
    ok: Boolean(response?.ok),
    node: header("X-Handled-By-Node") ?? data?.node ?? null,
    dbUsed: header("X-DB-Used") ?? data?.db_used ?? null,
    replication: header("X-Replication") ?? data?.replication ?? null,
    upstreamStatus: header("X-Upstream-Status"),
    durationMs: Math.round(performance.now() - started),
  };
  listeners.forEach((listener) => listener(meta));

  if (networkError) throw new ApiError("No se pudo contactar al gateway", 0, meta, null);
  if (response.status === 401 && auth) {
    unauthorizedListeners.forEach((listener) => listener(errorMessage(401, data)));
  }
  if (!response.ok) throw new ApiError(errorMessage(response.status, data), response.status, meta, data);
  return { data, meta };
}

export const api = {
  login: (username, password) =>
    request("POST", "/api/auth/login", { body: { username, password }, auth: false }),
  register: (payload) => request("POST", "/api/auth/register", { body: payload, auth: false }),
  me: () => request("GET", "/api/auth/me", { silent: true }),
  listOrders: (opts) => request("GET", "/api/orders", opts),
  createOrder: (payload) => request("POST", "/api/orders", { body: { id: uuid(), ...payload } }),
  updateOrder: (id, payload) => request("PATCH", `/api/orders/${id}`, { body: payload }),
  deleteOrder: (id) => request("DELETE", `/api/orders/${id}`),
  clusterStatus: () => request("GET", "/api/chaos/status", { silent: true }),
  replicationStatus: () => request("GET", "/api/replication/status", { silent: true }),
  setNodeCrash: (node, enabled) =>
    request("PUT", `/api/chaos/crash/${encodeURIComponent(node)}`, { body: { enabled } }),
  setDbPrimaryDown: (enabled) => request("PUT", "/api/chaos/db-primary", { body: { enabled } }),
  syncReplication: () => request("POST", "/api/replication/sync"),
};
