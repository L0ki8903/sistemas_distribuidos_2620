-- =============================================================================
-- Sistema Distribuido de Gestión de Pedidos — esquema inicial
-- Se ejecuta en db-primary Y en db-replica la primera vez que se crea su volumen
-- (docker-entrypoint-initdb.d). Ambas bases parten idénticas.
-- =============================================================================

CREATE TABLE IF NOT EXISTS orders (
    id              VARCHAR(36)    PRIMARY KEY,                -- UUID generado por la aplicación
    customer_name   VARCHAR(120)   NOT NULL,
    items           JSONB          NOT NULL,                   -- [{product, quantity, unit_price}]
    total           NUMERIC(12, 2) NOT NULL,                   -- calculado por el backend
    status          VARCHAR(20)    NOT NULL DEFAULT 'pendiente',
    created_at      TIMESTAMPTZ    NOT NULL,
    updated_at      TIMESTAMPTZ    NOT NULL,                   -- versión para last-write-wins
    deleted_at      TIMESTAMPTZ    NULL,                       -- tombstone (borrado lógico)
    handled_by_node VARCHAR(50)    NOT NULL,                   -- nodo de la última escritura
    owner_id        VARCHAR(36)    NULL,                       -- usuario dueño (NULL en los de ejemplo)
    CONSTRAINT ck_orders_status CHECK (status IN ('pendiente', 'en proceso', 'enviado', 'entregado'))
);

CREATE INDEX IF NOT EXISTS ix_orders_status ON orders (status);
CREATE INDEX IF NOT EXISTS ix_orders_updated_at ON orders (updated_at);
CREATE INDEX IF NOT EXISTS ix_orders_owner_id ON orders (owner_id);

-- Usuarios (también replicados). El administrador inicial lo crea la aplicación al arrancar
-- (ADMIN_USERNAME / ADMIN_PASSWORD): así ninguna contraseña queda escrita en el repositorio.
CREATE TABLE IF NOT EXISTS users (
    id            VARCHAR(36)  PRIMARY KEY,
    username      VARCHAR(50)  NOT NULL,
    full_name     VARCHAR(120) NOT NULL,
    password_hash VARCHAR(255) NOT NULL,                      -- PBKDF2-SHA256, nunca texto plano
    role          VARCHAR(10)  NOT NULL DEFAULT 'user',
    is_active     BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ  NOT NULL,
    updated_at    TIMESTAMPTZ  NOT NULL,
    deleted_at    TIMESTAMPTZ  NULL,
    CONSTRAINT ck_users_role CHECK (role IN ('user', 'admin')),
    CONSTRAINT uq_users_username UNIQUE (username)
);

CREATE INDEX IF NOT EXISTS ix_users_updated_at ON users (updated_at);

-- Pedidos de ejemplo. IDs y fechas FIJOS para que primaria y réplica sean idénticas.
INSERT INTO orders (id, customer_name, items, total, status, created_at, updated_at, deleted_at, handled_by_node)
VALUES
    (
        '7c9e6679-7425-40de-944b-e07fc1f90ae7',
        'Distribuidora Andina S.A.S.',
        '[{"product": "Caja de cartón 40x40", "quantity": 50, "unit_price": 2.5},
          {"product": "Cinta de embalaje", "quantity": 10, "unit_price": 3.2}]',
        157.00, 'pendiente',
        '2026-11-10 09:00:00+00', '2026-11-10 09:00:00+00', NULL, 'init.sql'
    ),
    (
        '3f2504e0-4f89-41d3-9a0c-0305e82c3301',
        'Ferretería El Tornillo',
        '[{"product": "Taladro percutor", "quantity": 2, "unit_price": 89.9},
          {"product": "Juego de brocas", "quantity": 3, "unit_price": 15.75}]',
        227.05, 'en proceso',
        '2026-11-11 14:30:00+00', '2026-11-12 08:15:00+00', NULL, 'init.sql'
    ),
    (
        'a8098c1a-f86e-41b5-8c3d-5b2e1c0f9d42',
        'María Fernanda López',
        '[{"product": "Laptop 14 pulgadas", "quantity": 1, "unit_price": 1250},
          {"product": "Mouse inalámbrico", "quantity": 1, "unit_price": 24.99}]',
        1274.99, 'enviado',
        '2026-11-12 16:45:00+00', '2026-11-13 10:20:00+00', NULL, 'init.sql'
    )
ON CONFLICT (id) DO NOTHING;
