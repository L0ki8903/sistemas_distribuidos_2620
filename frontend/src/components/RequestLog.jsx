import { formatClock, upstreamAttempts } from "../format.js";
import { DbChip, NodeChip } from "./ClusterPanel.jsx";

export default function RequestLog({ entries, onClear }) {
  return (
    <section className="card log" aria-labelledby="log-title">
      <header className="card-head">
        <h3 id="log-title">Registro de peticiones</h3>
        <button type="button" className="btn btn-link" onClick={onClear} disabled={entries.length === 0}>
          Limpiar
        </button>
      </header>
      {entries.length === 0 ? (
        <p className="muted">Las peticiones que hagas aparecerán aquí con el nodo y la base de datos que las atendieron.</p>
      ) : (
        <div className="table-wrap">
          <table className="log-table">
            <thead>
              <tr>
                <th>Hora</th>
                <th>Petición</th>
                <th>HTTP</th>
                <th>Nodo</th>
                <th>BD</th>
                <th>Intentos NGINX</th>
                <th className="num">Duración</th>
              </tr>
            </thead>
            <tbody>
              {entries.map((entry) => {
                const attempts = upstreamAttempts(entry.upstreamStatus);
                return (
                  <tr key={entry.id}>
                    <td className="mono muted">{formatClock(entry.at)}</td>
                    <td className="mono">
                      <span className="method">{entry.method}</span> {entry.path}
                    </td>
                    <td>
                      <span className={`http-code ${entry.ok ? "ok" : "fail"}`}>{entry.status || "ERR"}</span>
                    </td>
                    <td><NodeChip node={entry.node} /></td>
                    <td><DbChip dbUsed={entry.dbUsed} /></td>
                    <td className="mono">
                      {attempts.length > 1 ? (
                        <span className="retry">{attempts.join(" → ")}</span>
                      ) : (
                        <span className="muted">{attempts[0] ?? "—"}</span>
                      )}
                    </td>
                    <td className="num mono muted">{entry.durationMs} ms</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
