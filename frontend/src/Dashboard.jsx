import { useCallback, useEffect, useRef, useState } from "react";
import { api, onApiEvent } from "./api.js";
import ClusterPanel from "./components/ClusterPanel.jsx";
import MonitoringPanel from "./components/MonitoringPanel.jsx";
import OrderForm from "./components/OrderForm.jsx";
import OrdersTable from "./components/OrdersTable.jsx";
import RequestLog from "./components/RequestLog.jsx";
import Toasts from "./components/Toasts.jsx";
import { STATUS_LABELS, dbLabel, shortId } from "./format.js";

const POLL_MS = 3000;
const LOG_SIZE = 30;

export default function Dashboard({ user, onLogout }) {
  const isAdmin = user.role === "admin";
  // Pestañas del administrador; el hash (#monitoreo) permite enlazar directamente al panel.
  const [tab, setTab] = useState(() => (isAdmin && window.location.hash === "#monitoreo" ? "monitoreo" : "operacion"));
  const selectTab = (next) => {
    setTab(next);
    window.history.replaceState(null, "", next === "monitoreo" ? "#monitoreo" : window.location.pathname);
  };
  const [orders, setOrders] = useState([]);
  const [ordersState, setOrdersState] = useState({ loading: true, error: null, dbUsed: null });
  const [cluster, setCluster] = useState(null);
  const [replication, setReplication] = useState(null);
  const [gatewayUp, setGatewayUp] = useState(true);
  const [lastRequest, setLastRequest] = useState(null);
  const [log, setLog] = useState([]);
  const [handled, setHandled] = useState({});
  const [balance, setBalance] = useState([]);
  const [editing, setEditing] = useState(null);
  const [toasts, setToasts] = useState([]);
  const [pending, setPending] = useState({});
  const toastSeq = useRef(0);
  // Control de respuestas obsoletas: una recarga de la lista que empezó antes de un cambio local (o que
  // fue superada por otra más nueva) NO debe sobrescribir el estado. Sin esto, un GET lento que leyó la
  // BD antes de un PATCH devolvía el pedido a su estado anterior en pantalla.
  const ordersVersion = useRef(0);
  const listSeq = useRef(0);
  const applyList = useCallback((data, seq, versionAtStart) => {
    if (seq !== listSeq.current || versionAtStart !== ordersVersion.current) return false;
    setOrders(data.data);
    setOrdersState({ loading: false, error: null, dbUsed: data.db_used });
    return true;
  }, []);
  const mutateOrders = useCallback((updater) => {
    ordersVersion.current += 1;
    setOrders(updater);
  }, []);

  const dismiss = useCallback((id) => setToasts((list) => list.filter((t) => t.id !== id)), []);
  const notify = useCallback(
    (type, text) => {
      const id = ++toastSeq.current;
      setToasts((list) => [...list.slice(-3), { id, type, text }]);
      setTimeout(() => dismiss(id), 5000);
    },
    [dismiss],
  );

  // Cada respuesta de la API actualiza el panel "Última petición" y el registro.
  useEffect(
    () =>
      onApiEvent((meta) => {
        setGatewayUp(meta.status !== 0 && meta.status !== 502 && meta.status !== 504);
        if (meta.silent) return;
        setLastRequest(meta);
        setLog((list) => [meta, ...list].slice(0, LOG_SIZE));
        if (meta.node) setHandled((counts) => ({ ...counts, [meta.node]: (counts[meta.node] ?? 0) + 1 }));
      }),
    [],
  );

  const withPending = useCallback(async (key, fn) => {
    setPending((p) => ({ ...p, [key]: true }));
    try {
      return await fn();
    } finally {
      setPending((p) => ({ ...p, [key]: false }));
    }
  }, []);

  const loadOrders = useCallback(async ({ silent = false } = {}) => {
    const seq = ++listSeq.current;
    const versionAtStart = ordersVersion.current;
    try {
      const { data } = await api.listOrders({ silent });
      applyList(data, seq, versionAtStart);
      return data;
    } catch (error) {
      setOrdersState((s) => ({ ...s, loading: false, error: error.message }));
      throw error;
    }
  }, []);

  const refreshCluster = useCallback(async () => {
    if (!isAdmin || document.location.hash === "#monitoreo") return; // sólo admin, y sólo en "Operación"
    const [status, repl] = await Promise.allSettled([api.clusterStatus(), api.replicationStatus()]);
    if (status.status === "fulfilled") setCluster(status.value.data);
    if (repl.status === "fulfilled") setReplication(repl.value.data);
  }, [isAdmin]);

  useEffect(() => {
    loadOrders().catch(() => {});
    refreshCluster();
    const timer = setInterval(() => {
      if (!document.hidden) refreshCluster();
    }, POLL_MS);
    return () => clearInterval(timer);
  }, [loadOrders, refreshCluster]);

  // Las respuestas de escritura no traen owner_username: se conserva el que ya conocíamos.
  const upsertLocal = (order) =>
    mutateOrders((list) => {
      const exists = list.some((o) => o.id === order.id);
      return exists
        ? list.map((o) => (o.id === order.id ? { ...o, ...order, owner_username: order.owner_username ?? o.owner_username } : o))
        : [{ ...order, owner_username: order.owner_username ?? user.username }, ...list];
    });

  const where = (data) => `${data.node} · ${dbLabel(data.db_used)}`;

  // ------------------------------------------------------------------ pedidos
  const createOrder = (payload) =>
    withPending("form", async () => {
      try {
        const { data } = await api.createOrder(payload);
        upsertLocal(data.data);
        notify("success", `Pedido #${shortId(data.data.id)} creado (${where(data)})`);
        loadOrders({ silent: true }).catch(() => {});
      } catch (error) {
        notify("error", `No se pudo crear el pedido: ${error.message}`);
        throw error;
      }
    });

  const updateOrder = (id, payload) =>
    withPending("form", async () => {
      try {
        const { data } = await api.updateOrder(id, payload);
        upsertLocal(data.data);
        setEditing(null);
        notify("success", `Pedido #${shortId(id)} actualizado (${where(data)})`);
      } catch (error) {
        notify("error", `No se pudo actualizar: ${error.message}`);
        throw error;
      }
    });

  const advanceOrder = (order) =>
    withPending(`order:${order.id}`, async () => {
      try {
        const { data } = await api.updateOrder(order.id, { status: order.next_status });
        upsertLocal(data.data);
        notify("success", `#${shortId(order.id)} → ${STATUS_LABELS[data.data.status]} (${where(data)})`);
      } catch (error) {
        notify("error", `No se pudo cambiar el estado: ${error.message}`);
        loadOrders({ silent: true }).catch(() => {});
      }
    });

  const deleteOrder = (order) =>
    withPending(`order:${order.id}`, async () => {
      try {
        const { data } = await api.deleteOrder(order.id);
        mutateOrders((list) => list.filter((o) => o.id !== order.id));
        if (editing?.id === order.id) setEditing(null);
        notify("success", `Pedido #${shortId(order.id)} eliminado (${where(data)})`);
      } catch (error) {
        notify("error", `No se pudo eliminar: ${error.message}`);
      }
    });

  const refreshOrders = () =>
    withPending("refresh", () => loadOrders().catch((error) => notify("error", error.message)));

  // ------------------------------------------------------------------ caos
  const toggleNode = (node, enabled) =>
    withPending(`node:${node}`, async () => {
      try {
        await api.setNodeCrash(node, enabled);
        notify(
          enabled ? "warn" : "success",
          enabled
            ? `${node} caído (simulado): responde 503 y NGINX reenvía al otro nodo`
            : `${node} restaurado: vuelve a recibir tráfico`,
        );
      } catch (error) {
        notify("error", `No se pudo cambiar ${node}: ${error.message}`);
      } finally {
        refreshCluster();
      }
    });

  const toggleDb = (enabled) =>
    withPending("db", async () => {
      try {
        const { data } = await api.setDbPrimaryDown(enabled);
        if (enabled) {
          notify("warn", "db-primary caída (simulado) en todo el clúster: lecturas y escrituras van a db-replica");
        } else {
          const copied = data.sync?.copied_to_primary ?? 0;
          notify("success", `db-primary restaurada · reconciliación: ${copied} fila(s) copiadas réplica → primaria`);
        }
        loadOrders({ silent: true }).catch(() => {});
      } catch (error) {
        notify("error", `No se pudo cambiar db-primary: ${error.message}`);
      } finally {
        refreshCluster();
      }
    });

  const syncNow = () =>
    withPending("sync", async () => {
      try {
        const { data } = await api.syncReplication();
        if (data.status === "ok") {
          notify("success", `Reconciliación OK: +${data.copied_to_primary} a primaria, +${data.copied_to_replica} a réplica`);
        } else {
          notify("warn", `Reconciliación ${data.status}: ${data.detail ?? "primaria no disponible"}`);
        }
      } catch (error) {
        notify("error", `No se pudo sincronizar: ${error.message}`);
      } finally {
        refreshCluster();
      }
    });

  const testBalancing = () =>
    withPending("balance", async () => {
      const results = [];
      for (let i = 0; i < 6; i += 1) {
        try {
          // misma protección: si hubo un cambio local mientras tanto, esta lectura ya es vieja
          const seq = ++listSeq.current;
          const versionAtStart = ordersVersion.current;
          const { data, meta } = await api.listOrders();
          results.push(meta);
          applyList(data, seq, versionAtStart);
        } catch (error) {
          results.push(error.meta);
        }
        setBalance([...results]);
      }
    });

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <img src="/favicon.svg" alt="" width="32" height="32" />
          <div>
            <h1>Pedidos Distribuidos</h1>
            <p>Panel logístico · sistema tolerante a fallos</p>
          </div>
        </div>
        <div className="topbar-right">
          <span className={`gateway-pill ${gatewayUp ? "up" : "down"}`}>
            <span className="dot" /> Gateway NGINX {gatewayUp ? "conectado" : "sin backends"}
          </span>
          {isAdmin && (
            <a className="topbar-link" href="/api/docs" target="_blank" rel="noreferrer">
              API docs
            </a>
          )}
          <span className="user-chip" title={`Sesión: ${user.username}`}>
            <span className="user-name">{user.full_name}</span>
            <span className={`role-badge role-${user.role}`}>{isAdmin ? "Administrador" : "Cliente"}</span>
          </span>
          <button type="button" className="topbar-link topbar-button" onClick={onLogout}>
            Cerrar sesión
          </button>
        </div>
      </header>

      <main className="container">
        {isAdmin && (
          <nav className="tabs" role="tablist" aria-label="Secciones del panel">
            <button type="button" role="tab" aria-selected={tab === "operacion"} className={tab === "operacion" ? "tab active" : "tab"} onClick={() => selectTab("operacion")}>
              Operación
            </button>
            <button type="button" role="tab" aria-selected={tab === "monitoreo"} className={tab === "monitoreo" ? "tab active" : "tab"} onClick={() => selectTab("monitoreo")}>
              Monitoreo
            </button>
          </nav>
        )}
        {tab === "monitoreo" ? (
          <MonitoringPanel />
        ) : (
          <>
        <ClusterPanel
          isAdmin={isAdmin}
          lastRequest={lastRequest}
          cluster={cluster}
          replication={replication}
          handled={handled}
          pending={pending}
          balance={balance}
          onToggleNode={toggleNode}
          onToggleDb={toggleDb}
          onSync={syncNow}
          onTestBalancing={testBalancing}
        />

        <div className="workspace">
          <OrderForm
            defaultCustomer={isAdmin ? "" : user.full_name}
            editing={editing}
            busy={pending.form}
            onCreate={createOrder}
            onUpdate={updateOrder}
            onCancelEdit={() => setEditing(null)}
          />
          <OrdersTable
            isAdmin={isAdmin}
            orders={orders}
            state={ordersState}
            pending={pending}
            editingId={editing?.id}
            onAdvance={advanceOrder}
            onEdit={setEditing}
            onDelete={deleteOrder}
            onRefresh={refreshOrders}
          />
        </div>

        <RequestLog entries={log} onClear={() => setLog([])} />
          </>
        )}
      </main>

      <footer className="footer muted">
        React 18 + Vite · NGINX (Round-Robin + failover) · FastAPI ×2 · PostgreSQL 16 primaria + réplica
      </footer>
      <Toasts toasts={toasts} onDismiss={dismiss} />
    </div>
  );
}
