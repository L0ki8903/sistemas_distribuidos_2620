import { useEffect, useRef } from "react";

// Cuadro de diálogo modal accesible: foco en el primer campo, Escape para cerrar, clic fuera para cerrar.
export default function Dialog({ title, children, onClose, actions }) {
  const panelRef = useRef(null);

  useEffect(() => {
    const first = panelRef.current?.querySelector("input, select, textarea, button");
    first?.focus();
    const onKey = (event) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="dialog-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="dialog card" role="dialog" aria-modal="true" aria-labelledby="dialog-title" ref={panelRef}>
        <header className="card-head">
          <h3 id="dialog-title">{title}</h3>
          <button type="button" className="icon-btn" aria-label="Cerrar" onClick={onClose}>
            ×
          </button>
        </header>
        <div className="dialog-body">{children}</div>
        {actions && <footer className="dialog-actions">{actions}</footer>}
      </div>
    </div>
  );
}
