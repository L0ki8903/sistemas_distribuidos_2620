import { useState } from "react";
import { api } from "../api.js";

const USERNAME_RE = /^[A-Za-z0-9_.-]{3,50}$/;

function validateRegister(form) {
  if (!USERNAME_RE.test(form.username)) return "Usuario: 3 a 50 caracteres (letras, números, punto, guion o guion bajo).";
  if (form.full_name.trim().length < 2) return "Escribe tu nombre completo.";
  if (form.password.length < 8) return "La contraseña debe tener al menos 8 caracteres.";
  if (!/[A-Za-z]/.test(form.password) || !/\d/.test(form.password)) return "La contraseña debe tener letras y números.";
  if (form.password !== form.confirm) return "Las contraseñas no coinciden.";
  return null;
}

export default function LoginPage({ onAuthenticated, notice }) {
  const [mode, setMode] = useState("login");
  const [form, setForm] = useState({ username: "", full_name: "", password: "", confirm: "" });
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  const set = (field) => (event) => setForm((f) => ({ ...f, [field]: event.target.value }));
  const switchMode = (next) => {
    setMode(next);
    setError(null);
    setForm((f) => ({ ...f, password: "", confirm: "" }));
  };

  const submit = async (event) => {
    event.preventDefault();
    const problem =
      mode === "register" ? validateRegister(form) : !form.username || !form.password ? "Completa usuario y contraseña." : null;
    if (problem) {
      setError(problem);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const { data } =
        mode === "login"
          ? await api.login(form.username.trim(), form.password)
          : await api.register({ username: form.username.trim(), full_name: form.full_name.trim(), password: form.password });
      onAuthenticated(data);
    } catch (err) {
      setError(err.status === 429 ? `${err.message} (${err.body?.retry_after ?? "?"} s)` : err.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="login-shell">
      <section className="login-brand" aria-hidden="false">
        <div className="brand">
          <img src="/favicon.svg" alt="" width="40" height="40" />
          <div>
            <h1>Pedidos Distribuidos</h1>
            <p>Panel logístico · sistema tolerante a fallos</p>
          </div>
        </div>
        <ul className="login-points">
          <li>
            <strong>Siempre disponible.</strong> Dos servidores atienden por turnos; si uno cae, el otro sigue.
          </li>
          <li>
            <strong>Datos replicados.</strong> Cada pedido se guarda en dos bases de datos PostgreSQL.
          </li>
          <li>
            <strong>Acceso por roles.</strong> Los clientes gestionan sus pedidos; el administrador opera el clúster.
          </li>
        </ul>
      </section>

      <section className="login-panel">
        <form className="card login-card" onSubmit={submit} noValidate>
          <div className="login-tabs" role="tablist">
            <button type="button" role="tab" aria-selected={mode === "login"} className={mode === "login" ? "active" : ""} onClick={() => switchMode("login")}>
              Iniciar sesión
            </button>
            <button type="button" role="tab" aria-selected={mode === "register"} className={mode === "register" ? "active" : ""} onClick={() => switchMode("register")}>
              Crear cuenta
            </button>
          </div>

          {notice && <p className="callout callout-amber">{notice}</p>}

          <label className="field">
            <span>Usuario</span>
            <input value={form.username} onChange={set("username")} autoComplete="username" placeholder="p. ej. ana.torres" maxLength={50} autoFocus />
          </label>
          {mode === "register" && (
            <label className="field">
              <span>Nombre completo</span>
              <input value={form.full_name} onChange={set("full_name")} autoComplete="name" placeholder="Ana Torres" maxLength={120} />
            </label>
          )}
          <label className="field">
            <span>Contraseña</span>
            <input
              type="password"
              value={form.password}
              onChange={set("password")}
              autoComplete={mode === "login" ? "current-password" : "new-password"}
              maxLength={128}
            />
          </label>
          {mode === "register" && (
            <label className="field">
              <span>Confirmar contraseña</span>
              <input type="password" value={form.confirm} onChange={set("confirm")} autoComplete="new-password" maxLength={128} />
            </label>
          )}

          {error && (
            <p className="form-error" role="alert">
              {error}
            </p>
          )}
          <button type="submit" className="btn btn-primary btn-block" disabled={busy}>
            {busy ? "Procesando…" : mode === "login" ? "Entrar" : "Crear cuenta"}
          </button>
          <p className="hint muted">
            {mode === "login"
              ? "¿Eres administrador? Usa la cuenta configurada con ADMIN_USERNAME / ADMIN_PASSWORD."
              : "Las cuentas nuevas tienen rol cliente: crean y siguen sus propios pedidos."}
          </p>
        </form>
      </section>
    </div>
  );
}
