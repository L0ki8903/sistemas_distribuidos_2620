import { useCallback, useEffect, useState } from "react";
import { api, onUnauthorized } from "./api.js";
import { clearSession, loadSession, saveSession } from "./auth.js";
import Dashboard from "./Dashboard.jsx";
import LoginPage from "./components/LoginPage.jsx";

// Puerta de sesión: sin sesión válida se muestra el login; con sesión, el panel según el rol.
export default function App() {
  const [session, setSession] = useState(() => loadSession());
  const [notice, setNotice] = useState(null);

  const logout = useCallback((message = null) => {
    clearSession();
    setSession(null);
    setNotice(message);
  }, []);

  // Si el servidor rechaza el token (expirado, usuario desactivado o JWT_SECRET cambiado) se vuelve al login.
  useEffect(() => onUnauthorized((message) => logout(`Tu sesión terminó: ${message}`)), [logout]);

  // Al recargar, confirma con el servidor que la sesión guardada sigue siendo válida (y el rol actual).
  useEffect(() => {
    if (!session) return;
    api
      .me()
      .then(({ data }) => {
        if (data.user.role !== session.user.role || data.user.full_name !== session.user.full_name) {
          setSession((current) => (current ? { ...current, user: data.user } : current));
        }
      })
      .catch(() => {
        /* 401 ya lo gestiona onUnauthorized; otros errores (gateway caído) no cierran la sesión */
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (!session) {
    return (
      <LoginPage
        notice={notice}
        onAuthenticated={(data) => {
          setNotice(null);
          setSession(saveSession(data));
        }}
      />
    );
  }
  return <Dashboard key={session.user.id} user={session.user} onLogout={() => logout(null)} />;
}
