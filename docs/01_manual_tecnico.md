# Manual Técnico — Sistema Distribuido de Gestión de Pedidos Tolerante a Fallos

> Proyecto final de Sistemas Distribuidos · Versión 1.2.0 (autenticación, seguridad y monitoreo) · Noviembre de 2026

## Contenido

1. [Resumen](#1-resumen)
2. [Arquitectura distribuida](#2-arquitectura-distribuida)
3. [Diagrama de componentes](#3-diagrama-de-componentes)
4. [Estructura del código](#4-estructura-del-código)
5. [Flujo de datos](#5-flujo-de-datos)
6. [Mecanismos de tolerancia a fallos](#6-mecanismos-de-tolerancia-a-fallos)
7. [Diagramas de secuencia de fallos](#7-diagramas-de-secuencia-de-fallos)
8. [Autenticación y seguridad](#8-autenticación-y-seguridad)
9. [Monitoreo de nodos](#9-monitoreo-de-nodos)
10. [Modelo de datos y máquina de estados](#10-modelo-de-datos-y-máquina-de-estados)
11. [Referencia de la API](#11-referencia-de-la-api)
12. [Ejemplo de fallo simulado paso a paso](#12-ejemplo-de-fallo-simulado-paso-a-paso)
13. [Tecnologías empleadas y justificación](#13-tecnologías-empleadas-y-justificación)
14. [Variables de entorno](#14-variables-de-entorno)
15. [Decisiones de diseño, limitaciones y trabajo futuro](#15-decisiones-de-diseño-limitaciones-y-trabajo-futuro)

---

## 1. Resumen

El sistema recibe, procesa y replica pedidos de clientes y **sigue operando aunque fallen
componentes individuales**. Para lograrlo combina tres capas de redundancia:

| Capa | Redundancia | Qué tolera |
|------|-------------|------------|
| Entrada | Gateway NGINX con balanceo **Round-Robin** y **failover pasivo** (`proxy_next_upstream`) | Caída, bloqueo o error 5xx de un nodo de aplicación |
| Aplicación | **Dos nodos FastAPI** idénticos y sin estado de negocio (`Nodo-Alpha-01`, `Nodo-Beta-02`) | Pérdida de un nodo completo |
| Datos | **Dos PostgreSQL 16** (`db-primary`, `db-replica`) con **escritura dual**, **failover automático** y **reconciliación** | Caída de la base primaria (o de la réplica) sin perder escrituras |

Además incluye **Chaos Engineering**: desde el dashboard o la API se puede "tumbar" un nodo o la base
primaria en caliente para demostrar el comportamiento ante fallos.

Desde la versión 1.1 el acceso está **autenticado** (JWT) y **autorizado por roles** (`user` = cliente,
`admin` = operador). La sesión sigue funcionando aunque caiga un nodo o la base primaria (§8).
La versión 1.2 añade un **panel de monitoreo** para el administrador: tráfico, latencia, errores y eventos
de cada nodo, del gateway y de las bases, reunidos por REST entre nodos (§9).

---

## 2. Arquitectura distribuida

Arquitectura cliente-servidor distribuida desplegada con Docker Compose (5 contenedores en una red
privada). El cliente sólo conoce el gateway; los nodos y las bases no se publican al exterior.

| Servicio (compose) | Imagen / build | Puerto interno | Rol |
|--------------------|----------------|----------------|-----|
| `gateway` | `nginx/Dockerfile` (multi-stage: Node 22 → `nginx:1.28-alpine`) | 80 (publicado como `${GATEWAY_PORT:-8080}`) | Sirve el build de React en `/` y balancea `/api/` entre los dos nodos con failover |
| `backend-node-1` | `backend/Dockerfile` (`python:3.11-slim`) | 8000 | Nodo FastAPI `NODE_ID=Nodo-Alpha-01` |
| `backend-node-2` | `backend/Dockerfile` (`python:3.11-slim`) | 8000 | Nodo FastAPI `NODE_ID=Nodo-Beta-02` |
| `db-primary` | `database/Dockerfile` (`postgres:16-alpine` + `init.sql`) | 5432 | Base de datos principal: todas las lecturas/escrituras en condiciones normales |
| `db-replica` | `database/Dockerfile` (`postgres:16-alpine` + `init.sql`) | 5432 | Copia sincronizada; asume lecturas y escrituras si la primaria falla |

**Comunicación:**

- Navegador ⇄ gateway: HTTP/REST con rutas **relativas** `/api/...` (el frontend nunca usa
  `http://localhost:8000`).
- Gateway ⇄ nodos: HTTP/1.1 con keep-alive (`upstream backend_cluster`).
- Nodo ⇄ nodo: **REST entre nodos** para reenviar órdenes de caos al nodo destino y construir la
  vista del clúster (`/api/chaos/local`, `/api/chaos/crash/{nodo}`, `/api/chaos/db-primary`). El
  reenvío lleva la misma cabecera `Authorization` del administrador que dio la orden (§8.1).
- Nodos ⇄ bases: SQLAlchemy 2 + psycopg 3, con *timeouts* de conexión (2 s) y de sentencia (5 s).

---

## 3. Diagrama de componentes

```mermaid
flowchart LR
    U["Navegador<br/>Dashboard React 18"] -->|"HTTP :8080 (host) → :80"| G

    subgraph DOCKER["Red Docker (docker compose)"]
        G["gateway · NGINX 1.28<br/>estáticos React + upstream backend_cluster"]
        subgraph APP["Nodos de aplicación · FastAPI"]
            N1["backend-node-1<br/>Nodo-Alpha-01"]
            N2["backend-node-2<br/>Nodo-Beta-02"]
        end
        subgraph DATA["Capa de datos · PostgreSQL 16"]
            P[("db-primary")]
            R[("db-replica")]
        end
    end

    G -->|"/api/* Round-Robin<br/>proxy_next_upstream"| N1
    G -->|"/api/* Round-Robin<br/>proxy_next_upstream"| N2
    N1 <-.->|"REST /api/chaos/*"| N2
    N1 -->|"lectura / escritura"| P
    N2 -->|"lectura / escritura"| P
    N1 -->|"réplica + failover"| R
    N2 -->|"réplica + failover"| R
    P <-.->|"reconciliación LWW<br/>(ejecutada por los nodos)"| R
```

---

## 4. Estructura del código

```text
.
├── backend/                     Nodo FastAPI (se construye una imagen y corre 2 veces)
│   ├── app/
│   │   ├── main.py              create_app(): middleware de caos, cabecera X-Handled-By-Node, errores
│   │   ├── config.py            Settings desde variables de entorno (NODE_ID, URLs, PEERS, JWT…)
│   │   ├── security.py          PBKDF2, emisión/validación de JWT, límite de intentos de login
│   │   ├── users.py             Reglas de usuarios: registro, búsqueda, administrador inicial
│   │   ├── bootstrap.py         Crea el administrador inicial al arrancar (id determinista)
│   │   ├── cli.py               python -m app.cli create-admin (crear/restablecer administradores)
│   │   ├── metrics.py           Métricas por nodo en memoria, eventos desde los logs, stub_status de NGINX
│   │   ├── database.py          DatabaseCluster: doble sesión, escritura dual, failover,
│   │   │                        circuit breaker y reconciliación
│   │   ├── services.py          Reglas de negocio: CRUD, máquina de estados, permisos por rol, idempotencia
│   │   ├── models.py            Modelos Order y User (UTCDateTime, JSON/JSONB), migraciones aditivas
│   │   ├── schemas.py           Esquemas Pydantic v2 de entrada/salida
│   │   ├── chaos.py             Banderas SIMULATE_CRASH / SIMULATE_DB_PRIMARY_DOWN
│   │   ├── peers.py             Cliente REST entre nodos (httpx)
│   │   ├── deps.py              Dependencias FastAPI: sesión (get_current_actor) y require_admin
│   │   └── routers/             auth.py · orders.py · chaos.py · system.py · monitoring.py
│   ├── tests/                   test_orders.py (15) · test_auth.py (13) · test_monitoring.py (8) · test_cluster_e2e.py (6)
│   └── Dockerfile, requirements*.txt, pytest.ini
├── frontend/                    React 18 + Vite (dashboard logístico)
│   └── src/ App.jsx (puerta de sesión) · Dashboard.jsx · auth.js · api.js · format.js ·
│            components/ (LoginPage, ClusterPanel, MonitoringPanel, LineChart, OrderForm, OrdersTable,
│                         RequestLog, Toasts) · styles.css
├── nginx/                       nginx.conf (upstream + failover) · security-headers.conf · Dockerfile multi-stage
├── database/                    init.sql (tablas orders/users + 3 pedidos) · Dockerfile
├── e2e-ui/                      Prueba de interfaz con Chrome (puppeteer-core) + capturas
├── .github/workflows/ci.yml     Integración continua (pytest SQLite/PostgreSQL, build, clúster completo)
├── docs/                        Manuales e informe de pruebas (img/ = capturas)
└── docker-compose.yml
```

---

## 5. Flujo de datos

### 5.1 Lectura normal

1. El navegador pide `GET /api/orders` al gateway.
2. NGINX elige el siguiente nodo por Round-Robin.
3. El nodo ejecuta `DatabaseCluster.read()`: consulta `db-primary`.
4. Responde `200` con `{"node": "...", "db_used": "primary", "count": n, "data": [...]}` y las
   cabeceras `X-Handled-By-Node` y `X-DB-Used`. NGINX añade `X-Upstream-Status`/`X-Upstream-Addr`.

### 5.2 Escritura normal (escritura dual)

```mermaid
sequenceDiagram
    autonumber
    participant C as Navegador
    participant G as NGINX
    participant N as Nodo (Alpha o Beta)
    participant P as db-primary
    participant R as db-replica
    C->>G: POST /api/orders {id: UUID, customer_name, items}
    G->>N: turno Round-Robin
    N->>P: BEGIN · INSERT orders · COMMIT
    P-->>N: OK
    N->>R: INSERT … ON CONFLICT (id) DO UPDATE … WHERE updated_at < nuevo
    R-->>N: OK
    N-->>G: 201 {db_used: "primary", replication: "synced"} + X-Handled-By-Node
    G-->>C: 201 + X-Upstream-Status: 201
```

- La escritura se **confirma primero en la primaria** y luego se **replica inmediatamente** en la
  réplica dentro de un `try/except` (`DatabaseCluster._replicate`). Si la réplica falla, la petición
  **no falla**: responde `"replication": "pending"` y la fila se copiará en la próxima reconciliación.
- `PATCH` (cambiar estado o editar) y `DELETE` siguen el mismo camino: el nodo calcula el **estado
  final completo** de la fila y ese estado es lo que se replica.
- `DELETE` es un **borrado lógico** (`deleted_at`, *tombstone*): así la eliminación también se replica
  y se reconcilia. Para el usuario el pedido desaparece (404 al consultarlo).

---

## 6. Mecanismos de tolerancia a fallos

### 6.1 Balanceo Round-Robin con failover pasivo (NGINX)

```nginx
upstream backend_cluster {
    zone backend_cluster 64k;
    server backend-node-1:8000 max_fails=1 fail_timeout=10s resolve;
    server backend-node-2:8000 max_fails=1 fail_timeout=10s resolve;
    keepalive 16;
}
...
proxy_connect_timeout 2s;
proxy_next_upstream error timeout http_500 http_502 http_503 http_504 non_idempotent;
proxy_next_upstream_tries 2;
```

- **Round-Robin** reparte las peticiones alternando nodos (evidencia: §12).
- Si un nodo **no acepta la conexión, excede el timeout o responde 5xx**, NGINX **reintenta la misma
  petición en el otro nodo** y el cliente recibe la respuesta buena. Tras un fallo, el nodo queda
  excluido 10 s (`max_fails=1 fail_timeout=10s`) y luego se vuelve a probar con tráfico real.
- `resolve` + `resolver 127.0.0.11` (DNS de Docker): si un nodo se recrea con otra IP, NGINX la
  vuelve a resolver sin reiniciar, y el gateway puede arrancar aunque un nodo esté caído.
- NGINX expone `X-Upstream-Status` (p. ej. `503, 200`) para **evidenciar el reintento** en el dashboard.
- `non_idempotent` permite reintentar también `POST`/`PATCH`. Es seguro porque todas las escrituras
  son idempotentes (§6.7).

### 6.2 Chaos Engineering de nodo (`SIMULATE_CRASH`)

Cada nodo lee `NODE_ID` y mantiene la bandera global `SIMULATE_CRASH`. Un middleware HTTP
(`node_identity_and_chaos` en `main.py`):

- Si la bandera está activa, responde **HTTP 503** a cualquier ruta `/api/orders*` (con
  `Retry-After: 5`) **sin tocar la base de datos** → NGINX salta al otro nodo.
- Añade **`X-Handled-By-Node`** a **todas** las respuestas (incluidos 404/409/503).
- `/api/health` y `/api/chaos/*` **no se bloquean**, para poder observar y restaurar el nodo.

Como detrás de NGINX no se elige qué nodo recibe la petición, `POST /api/chaos/toggle/{nodo}` y
`PUT /api/chaos/crash/{nodo}` **reenvían por REST** la orden al nodo destino si no es el receptor
(cabecera `X-Chaos-Forwarded-By` para evitar bucles). La lista de pares se configura con
`PEERS=Nodo-Beta-02=http://backend-node-2:8000`.

### 6.3 Doble sesión SQLAlchemy y escritura dual

`DatabaseCluster` crea **dos `Engine` y dos `sessionmaker`** (`PRIMARY_DB_URL` y `REPLICA_DB_URL`).
Toda la lógica de negocio (`services.py`) recibe una `Session` y no sabe qué base la atiende, de modo
que la misma función se ejecuta en la primaria o, si falla, en la réplica.

### 6.4 Failover automático de base de datos

`DatabaseCluster.read()` y `DatabaseCluster.write()`:

1. Intentan la operación en `db-primary`.
2. Si se produce un **error de infraestructura** (`OperationalError`, conexión rechazada, DNS, timeout,
   tabla inexistente…) o la bandera `SIMULATE_DB_PRIMARY_DOWN` está activa, registran el fallo y
   **repiten la operación en `db-replica`**.
3. La respuesta indica `"db_used": "replica_failover"` (y cabecera `X-DB-Used`). **Nunca HTTP 500.**
4. Si **ambas** bases fallan → **HTTP 503** con JSON explicativo (indisponibilidad temporal).

Los errores de negocio (pedido inexistente, transición inválida) **no** provocan failover: se
devuelven como 404/409. Los errores de integridad tampoco (`IntegrityError` → 409).

La bandera `SIMULATE_DB_PRIMARY_DOWN` es **de clúster**: como la primaria es compartida, al
activarla en un nodo éste la propaga por REST a los demás (`PUT /api/chaos/db-primary`).

### 6.5 Circuit breaker

Tras un **fallo real** de la primaria, el nodo la evita durante `BREAKER_COOLDOWN_SECONDS` (5 s) y va
directo a la réplica. Así no se paga el *timeout* de conexión en cada petición mientras la primaria
está caída. Pasado ese tiempo, la siguiente operación (o la reconciliación periódica) vuelve a probar
la primaria (*half-open*); si responde, el circuito se cierra.

### 6.6 Reconciliación anti-entropía (last-write-wins + tombstones)

Mientras la primaria está caída, las escrituras quedan sólo en la réplica. Al volver, hay que
**converger**. `DatabaseCluster.reconcile()`:

1. Lee `(id, updated_at)` de ambas bases.
2. Para cada `id`, la versión con `updated_at` más reciente gana (**last-write-wins**); las filas que
   faltan o están atrasadas se copian al otro lado.
3. La copia usa `INSERT … ON CONFLICT (id) DO UPDATE … WHERE destino.updated_at < origen.updated_at`:
   **atómica e idempotente**, por lo que dos nodos pueden reconciliar a la vez sin pisarse.
4. Los borrados viajan como *tombstones* (`deleted_at`), por lo que también se reconcilian.

Se ejecuta: (a) en cuanto se restaura la primaria simulada (el nodo que recibe la orden reconcilia
**antes** de propagarla y reporta las filas copiadas), (b) **antes de la primera operación** tras un
failover, (c) **periódicamente cada 5 s** en segundo plano en cada nodo, y (d) bajo demanda con
`POST /api/replication/sync`. `GET /api/replication/status` informa `in_sync` y `divergent_orders`.

### 6.7 Idempotencia (reintentos seguros)

| Operación | Por qué es idempotente |
|-----------|------------------------|
| `POST /api/orders` | El frontend envía un **UUID generado en el cliente**. Si ese `id` ya existe, se devuelve el pedido existente con `200` y `X-Idempotent-Replay: true` (no se duplica). |
| `PATCH` de estado | Pedir el estado en que ya está el pedido es un no-op (`200`). |
| `PATCH` de edición | Fija valores absolutos (nombre, ítems). |
| `DELETE` | Borrar un pedido ya borrado devuelve `200` con `already_deleted: true`. |

Esto es lo que permite activar `non_idempotent` en NGINX: si un nodo procesa un `POST` y muere antes
de responder, el reintento en el otro nodo no crea un duplicado.

### 6.8 IDs distribuidos (UUID)

Los IDs **no** son autoincrementales: con dos bases que pueden aceptar escrituras (failover), dos
secuencias `SERIAL` independientes colisionarían. El UUID se genera en la aplicación (o en el
cliente), sin coordinación.

### 6.9 Timeouts y health checks

- PostgreSQL: `connect_timeout=2`, `statement_timeout=5000 ms`, `pool_pre_ping=True` (descarta
  conexiones muertas tras una caída).
- NGINX: `proxy_connect_timeout 2s`, `proxy_read_timeout 15s`.
- Docker: `pg_isready` en las bases; `/api/health?deep=false` (liveness, sin tocar BD) en los nodos;
  `/healthz` en el gateway. Los nodos siguen *healthy* aunque la primaria esté caída, porque siguen
  atendiendo vía réplica.

### 6.10 Matriz de fallos

| Fallo | Detección | Reacción del sistema | Lo que ve el usuario |
|-------|-----------|----------------------|----------------------|
| Nodo con `SIMULATE_CRASH` | Middleware devuelve 503 | NGINX reintenta en el otro nodo y excluye el caído 10 s | `200`, atendido por el otro nodo (`X-Upstream-Status: 503, 200`) |
| Nodo apagado (`docker compose stop`) | Conexión rechazada / timeout 2 s | Igual que arriba | `200` (la primera petición tarda hasta 2 s más: `504, 200`) |
| `db-primary` caída (real o simulada) | Excepción SQLAlchemy / bandera | Failover a `db-replica`, circuit breaker, `needs_resync` | `200/201` con `db_used: "replica_failover"` |
| `db-primary` vuelve | Reconciliación (al restaurar, antes de operar o cada 5 s) | Copia a la primaria las filas escritas durante la caída | `db_used: "primary"` y `in_sync: true` |
| `db-replica` caída | Excepción al replicar | La escritura en primaria se confirma; replicación pendiente | `201` con `replication: "pending"` |
| Ambas bases caídas | Excepción en ambas | `503` JSON controlado | Mensaje de error claro, nunca `500` |
| Orden de caos llega al nodo equivocado | `target_node ≠ NODE_ID` | Reenvío REST al nodo destino | Transparente |
| Reintento de `POST` tras timeout | `id` ya existente | Devuelve el pedido existente | Sin duplicados |
| Cae el nodo que emitió el token | — | El otro nodo valida el JWT con el secreto compartido | La sesión sigue abierta |
| Petición sin sesión o con token inválido | Firma/expiración del JWT | `401` (NGINX no la reintenta) | Vuelve a la pantalla de inicio de sesión |

---

## 7. Diagramas de secuencia de fallos

### 7.1 Caída de un nodo

```mermaid
sequenceDiagram
    autonumber
    participant C as Navegador
    participant G as NGINX
    participant A as Nodo-Alpha-01 (SIMULATE_CRASH)
    participant B as Nodo-Beta-02
    C->>G: GET /api/orders
    G->>A: turno Round-Robin
    A-->>G: 503 Service Unavailable (middleware de caos)
    Note over G: proxy_next_upstream http_503<br/>Alpha excluido 10 s (max_fails=1)
    G->>B: mismo request (reintento)
    B-->>G: 200 OK
    G-->>C: 200 · X-Handled-By-Node: Nodo-Beta-02 · X-Upstream-Status: 503, 200
    C->>G: GET /api/orders (siguientes)
    G->>B: Alpha marcado como no disponible
    B-->>G: 200 OK
    G-->>C: 200 · X-Upstream-Status: 200
```

### 7.2 Failover de la base primaria y reconciliación

```mermaid
sequenceDiagram
    autonumber
    participant C as Cliente
    participant N as Nodo
    participant P as db-primary
    participant R as db-replica
    Note over P: caída (real o SIMULATE_DB_PRIMARY_DOWN)
    C->>N: POST /api/orders
    N-xP: INSERT (error de conexión o caída simulada)
    Note over N: FAILOVER · circuit breaker 5 s · needs_resync = true
    N->>R: INSERT en la réplica
    R-->>N: OK
    N-->>C: 201 {db_used: "replica_failover", replication: "pending"}
    Note over P: la primaria vuelve
    loop al restaurar, antes de operar y cada 5 s
        N->>P: SELECT id, updated_at
        N->>R: SELECT id, updated_at
        N->>P: UPSERT de las filas más recientes (LWW)
    end
    C->>N: GET /api/orders
    N->>P: SELECT
    N-->>C: 200 {db_used: "primary"} (con el pedido creado durante la caída)
```

---

## 8. Autenticación y seguridad

### 8.1 Sesión distribuida sin estado (JWT)

Detrás de un balanceador Round-Robin, dos peticiones seguidas del mismo usuario llegan a nodos
distintos, y cualquiera de ellos puede caer. Por eso la sesión **no se guarda en ningún nodo**:

- `POST /api/auth/login` verifica la contraseña y devuelve un **JWT HS256** con `sub` (id del usuario),
  `username`, `role`, `iat`, `exp` (8 h por defecto), `iss` y `jti`.
- Los dos nodos comparten `JWT_SECRET`, así que **cualquier nodo valida un token emitido por otro**
  sin consultar al nodo emisor. Si un nodo cae, la sesión continúa en el otro.
- En cada petición, además de la firma y la expiración, el nodo comprueba en la base de datos (con
  failover a la réplica) que el usuario **siga activo** y lee su **rol actual**. Desactivar una cuenta o
  cambiar su rol tiene efecto inmediato aunque el token siga vigente.
- La lista de algoritmos es fija (`HS256`), lo que impide el ataque `alg=none`.
- El reenvío de órdenes de caos entre nodos incluye la cabecera `Authorization` original: el nodo
  destino vuelve a validar que la orden viene de un administrador.

```mermaid
sequenceDiagram
    autonumber
    participant C as Navegador
    participant G as NGINX
    participant A as Nodo-Alpha-01
    participant B as Nodo-Beta-02
    participant P as db-primary
    participant R as db-replica
    C->>G: POST /api/auth/login {usuario, contraseña}
    G->>A: turno Round-Robin
    A-xP: SELECT usuario (primaria caída)
    A->>R: SELECT usuario (failover)
    Note over A: verifica PBKDF2 y firma el JWT<br/>con JWT_SECRET compartido
    A-->>C: 200 {access_token, db_used: "replica_failover"}
    C->>G: GET /api/orders (Authorization: Bearer token)
    G->>B: siguiente turno
    Note over B: valida firma y expiración<br/>sin preguntar a Alpha
    B->>R: ¿usuario activo? rol actual (failover)
    B-->>C: 200 · sólo los pedidos de ese usuario
```

### 8.2 Usuarios replicados

La tabla `users` (§10.2) se trata exactamente igual que `orders`: **escritura dual**, **failover** y
**reconciliación last-write-wins**. Por eso se puede **registrar e iniciar sesión con la primaria
caída**, y las cuentas creadas durante la caída se copian a la primaria al recuperarse (prueba S-10 del
informe). Si durante una partición se creara el mismo `username` en ambas bases con ids distintos, la
reconciliación registra un **conflicto** para esa fila (`conflicts` en la respuesta de sync) sin
bloquear el resto.

El **administrador inicial** se crea al arrancar si no existe (`ADMIN_USERNAME`/`ADMIN_PASSWORD`). Los
dos nodos le asignan el **mismo id determinista** (UUID v5 del nombre de usuario): si arrancan a la vez,
la clave primaria impide que haya dos administradores. Si `ADMIN_PASSWORD` está vacío se genera una
contraseña aleatoria que se muestra una sola vez en el log. Para crear o restablecer administradores:

```bash
docker compose exec backend-node-1 python -m app.cli create-admin --username admin --password 'NuevaClave2026'
```

### 8.3 Roles y permisos

Los permisos se aplican **en el servidor** (`services.py`, `deps.py`); la interfaz sólo oculta lo que
el usuario no puede hacer.

| Acción | Sin sesión | `user` (cliente) | `admin` |
|--------|:----------:|------------------|---------|
| `GET /api/health` | ✅ | ✅ | ✅ |
| Registrarse / iniciar sesión | ✅ | — | — |
| Listar / ver pedidos | 401 | sólo los suyos (ajenos → 404) | todos, con `owner_username` |
| Crear pedido | 401 | ✅ (queda como dueño) | ✅ |
| Editar cliente / ítems | 401 | los suyos en `pendiente` | en `pendiente` |
| Cambiar estado | 401 | 403 | ✅ |
| Eliminar | 401 | cancelar los suyos en `pendiente` | cualquiera |
| Caos (`/api/chaos/*`) y replicación (`/api/replication/*`) | 401 | 403 | ✅ |

Un pedido ajeno se responde como **inexistente (404)** para no revelar que existe. El registro sólo
crea cuentas `user`: el campo `role` no se acepta (422), así que nadie puede autoasignarse `admin`.

### 8.4 Contraseñas y fuerza bruta

- **PBKDF2-SHA256** con sal aleatoria de 16 bytes y **600 000 iteraciones** (recomendación OWASP), de la
  biblioteca estándar. Formato: `pbkdf2_sha256$<iteraciones>$<sal>$<hash>`. Se compara en tiempo
  constante (`hmac.compare_digest`) y el hash nunca sale de la API.
- Política mínima: 8–128 caracteres, con letras y números. El usuario se normaliza a minúsculas.
- El mismo mensaje y el mismo tiempo de respuesta para "usuario inexistente" y "contraseña incorrecta"
  (se ejecuta un PBKDF2 ficticio), para no permitir **enumerar usuarios**.
- **Límite de intentos**: 5 fallos en 5 minutos por (usuario, IP) devuelven **429** con `Retry-After`.
  El contador vive en la memoria de cada nodo (limitación en §15).
- El hash se calcula **fuera de la transacción** para no retener conexiones a la base.

### 8.5 Seguridad en el gateway

`nginx/security-headers.conf` se incluye en cada `location`:

| Cabecera | Valor | Protege contra |
|----------|-------|----------------|
| `Content-Security-Policy` (sólo la app React) | `default-src 'self'; script-src 'self'; style-src 'self'; …; frame-ancestors 'none'` | Inyección de scripts (XSS) y estilos |
| `X-Content-Type-Options` | `nosniff` | Interpretación errónea de tipos MIME |
| `X-Frame-Options` | `DENY` | Clickjacking |
| `Referrer-Policy` | `strict-origin-when-cross-origin` | Fuga de URLs |
| `Permissions-Policy` | sin cámara, micrófono ni ubicación | Abuso de APIs del navegador |

Además: `client_max_body_size 1m`, `server_tokens off`, y ya no se envía `X-Upstream-Addr` (revelaba IPs
internas). Los `401`/`403` **no** están en `proxy_next_upstream`: no son fallos del nodo y no se
reintentan. En producción (Coolify) Traefik termina HTTPS; la app no necesita cambios.

### 8.6 Migración de instalaciones existentes

Las bases creadas con la versión 1.0 no tienen `users` ni `orders.owner_id`. Al arrancar, cada nodo:
crea las tablas que falten, aplica migraciones aditivas (`ALTER TABLE … ADD COLUMN`) y crea los índices,
todo dentro de un **`pg_advisory_xact_lock`** para que los dos nodos no migren a la vez. Si una base
está caída al arrancar, la migración se reintenta en la reconciliación periódica cuando vuelve. Los
pedidos existentes quedan con `owner_id = NULL` (sólo visibles para el administrador).

---

## 9. Monitoreo de nodos

Pestaña **Monitoreo** del administrador (`frontend/src/components/MonitoringPanel.jsx`), alimentada por
`GET /api/monitoring/cluster`.

### 9.1 Qué se mide y dónde

Cada nodo se mide **a sí mismo, en memoria** (`app/metrics.py`). No hay una base de métricas a propósito:
si el monitoreo dependiera de `db-primary`, se quedaría ciego justo durante un failover.

| Métrica | Cómo se obtiene |
|---------|-----------------|
| Peticiones por estado (2xx/3xx, 4xx, 5xx) y por grupo (`orders`, `auth`, `chaos`, `replication`) | El mismo middleware que añade `X-Handled-By-Node` mide cada petición |
| 503 simulados | Peticiones rechazadas por `SIMULATE_CRASH` |
| Atendidas por la réplica | Respuestas con `X-DB-Used: replica_failover` |
| Latencia media, p50, p95 y máxima | Últimas 2 000 muestras, filtradas por la ventana pedida (5 o 15 min) |
| Serie temporal | Cubos de **10 s** alineados al reloj (múltiplos de 10 del tiempo Unix), así las series de distintos nodos encajan punto a punto |
| Failovers de BD, circuit breaker, último error (con hora), última reconciliación | Estado de `DatabaseCluster` |
| Uptime, versión, Python | Del proceso |

No cuentan como tráfico: el propio sondeo del panel (`/api/monitoring/*`), `/api/health` (healthchecks
de Docker) ni las llamadas entre nodos (cabecera `X-Chaos-Forwarded-By`). Así el panel no se mide a sí
mismo. La latencia p95 incluye el inicio de sesión, que es lento **a propósito** (PBKDF2, §8.4).

### 9.2 Eventos a partir de los logs

`EventLogHandler` se conecta al logger `pedidos` del nodo y convierte en eventos los mensajes que ya
existían: `FAILOVER`/`WRITE` (failover de BD), `REPLICATION`, `SYNC` (reconciliación), `CHAOS`, `AUTH`
(p. ej. login fallido), `SCHEMA`, `ADMIN` y el arranque del nodo. Así **no se tocó la lógica
distribuida** para observarla. Se guardan todos los `WARNING`/`ERROR` y sólo los `INFO` relevantes (una
replicación correcta no es un evento). Los mensajes repetidos seguidos se agrupan con un contador
(`×9`), y la contraseña generada del administrador se **oculta** antes de llegar al panel.

### 9.3 Vista agregada por REST

```mermaid
sequenceDiagram
    autonumber
    participant UI as Panel (admin)
    participant G as NGINX
    participant A as Nodo-Alpha-01
    participant B as Nodo-Beta-02
    participant S as gateway:8081
    participant D as db-primary / db-replica
    UI->>G: GET /api/monitoring/cluster (Bearer admin)
    G->>A: turno Round-Robin
    A->>B: GET /api/monitoring/local (mismo token)
    B-->>A: métricas y eventos de Beta
    A->>S: GET /nginx_status
    S-->>A: conexiones y peticiones del gateway
    A->>D: ping y comparación de filas
    A-->>UI: resumen + nodos + gateway + bases + eventos combinados
```

Si un nodo no responde, aparece como **inalcanzable** con el motivo y la vista sigue respondiendo 200.
El resumen suma peticiones por minuto, errores 5xx y lecturas desde la réplica. La latencia p95 del
clúster se muestra como la del **nodo más lento**, porque los percentiles no se pueden promediar.

### 9.4 Estado del gateway sin exponerlo

NGINX publica `stub_status` en un **segundo `server` en el puerto 8081**, que sólo existe dentro de la red
de Docker: no está en `ports`, y Traefik (Coolify) sólo enruta al 80. Los nodos lo leen en
`GATEWAY_STATUS_URL=http://gateway:8081/nginx_status` (conexiones activas, aceptadas, peticiones,
leyendo/escribiendo/en espera). La prueba e2e E-06 verifica que desde fuera no es accesible.

### 9.5 El panel

- **Filtro en una sola fila** (últimos 5 o 15 min, pausa) que aplica a todo lo de abajo; se actualiza
  cada 3 s y conserva el dibujo anterior mientras llegan los datos nuevos.
- **Indicadores**: nodos operativos, peticiones por minuto, % de errores 5xx, latencia p95, peticiones
  atendidas por la réplica y failovers de BD.
- **Gráficos** propios en SVG (`LineChart.jsx`), sin librerías:
  - un eje por gráfico (peticiones y latencia van por separado), líneas de 2 px y cuadrícula fina sólida;
  - leyenda, cruz de lectura con tooltip (también con el teclado) y **vista en tabla** equivalente;
  - un hueco en la línea cuando no hay tráfico, nunca un cero inventado.
- **Colores de nodo validados para daltonismo** con un script de contraste. El par anterior azul/violeta
  daba ΔE 0,4 en deuteranopía (indistinguible), así que Beta pasó a **naranja** en toda la aplicación
  (ΔE 29,9).
- Tarjetas por nodo, tarjeta del gateway, tarjeta de las bases (filas por tabla y divergencias) y
  **línea de tiempo de eventos** con filtros (failover, caos, reconciliación, seguridad, sistema).

---

## 10. Modelo de datos y máquina de estados

### 10.1 Tabla `orders` (idéntica en `db-primary` y `db-replica`)

| Columna | Tipo PostgreSQL | Descripción |
|---------|-----------------|-------------|
| `id` | `VARCHAR(36)` PK | UUID generado por la aplicación/cliente |
| `customer_name` | `VARCHAR(120)` | Nombre del cliente (2–120 caracteres) |
| `items` | `JSONB` | Lista `[{product, quantity, unit_price}]` (1–50 ítems) |
| `total` | `NUMERIC(12,2)` | Calculado **en el servidor**: Σ cantidad × precio |
| `status` | `VARCHAR(20)` + `CHECK` | `pendiente` · `en proceso` · `enviado` · `entregado` |
| `created_at` | `TIMESTAMPTZ` | Fecha de creación (UTC) |
| `updated_at` | `TIMESTAMPTZ` | Versión de la fila para last-write-wins |
| `deleted_at` | `TIMESTAMPTZ NULL` | Tombstone del borrado lógico |
| `handled_by_node` | `VARCHAR(50)` | Nodo que procesó la última escritura (`init.sql` en los datos de ejemplo) |
| `owner_id` | `VARCHAR(36) NULL` | Usuario dueño del pedido (sin FK a propósito: las tablas se reconcilian por separado) |

Índices: `ix_orders_status`, `ix_orders_updated_at`, `ix_orders_owner_id`. `database/init.sql` crea la tabla y **3 pedidos de
ejemplo con IDs y fechas fijos**, para que ambas bases arranquen idénticas.

### 10.2 Tabla `users` (idéntica en ambas bases)

| Columna | Tipo PostgreSQL | Descripción |
|---------|-----------------|-------------|
| `id` | `VARCHAR(36)` PK | UUID (el del administrador inicial es determinista) |
| `username` | `VARCHAR(50)` `UNIQUE` | En minúsculas; 3–50 caracteres `[a-z0-9_.-]` |
| `full_name` | `VARCHAR(120)` | Nombre visible |
| `password_hash` | `VARCHAR(255)` | `pbkdf2_sha256$…` (nunca texto plano) |
| `role` | `VARCHAR(10)` + `CHECK` | `user` · `admin` |
| `is_active` | `BOOLEAN` | Una cuenta desactivada no puede iniciar sesión y pierde la sesión actual |
| `created_at` / `updated_at` / `deleted_at` | `TIMESTAMPTZ` | Igual que en `orders`: versión LWW y tombstone |

### 10.3 Máquina de estados

```mermaid
stateDiagram-v2
    state "pendiente" as pendiente
    state "en proceso" as en_proceso
    state "enviado" as enviado
    state "entregado" as entregado
    [*] --> pendiente: POST /api/orders
    pendiente --> en_proceso: PATCH status
    en_proceso --> enviado: PATCH status
    enviado --> entregado: PATCH status
    entregado --> [*]
    note right of pendiente
        Único estado en el que se pueden
        editar cliente e ítems.
        DELETE es posible en cualquier estado.
    end note
```

Sólo se permite avanzar **al siguiente estado**. Saltar o retroceder devuelve **409** con
`allowed_next`. Cada pedido expone `next_status` para que la interfaz muestre el botón correcto.

---

## 11. Referencia de la API

Documentación interactiva (Swagger): **`http://localhost:8080/api/docs`**.

| Método | Ruta | Descripción | Acceso | Respuestas |
|--------|------|-------------|--------|------------|
| GET | `/api/health?deep=true` | Salud del nodo; con `deep` incluye ping a ambas BD | público | 200 |
| POST | `/api/auth/register` | Crear cuenta de cliente (`username`, `full_name`, `password`) y obtener token | público | 201, 403 (registro deshabilitado), 409, 422 |
| POST | `/api/auth/login` | Iniciar sesión → `{access_token, token_type, expires_in, user}` | público | 200, 401, 403 (cuenta desactivada), 429 |
| GET | `/api/auth/me` | Usuario de la sesión actual | sesión | 200, 401 |
| GET | `/api/orders?status=&customer=` | Lista pedidos (cliente: los suyos; admin: todos) | sesión | 200, 401, 503 |
| GET | `/api/orders/{id}` | Obtiene un pedido | sesión (dueño o admin) | 200, 401, 404, 503 |
| POST | `/api/orders` | Crea un pedido (estado inicial `pendiente`, dueño = usuario) | sesión | 201, 200 (replay idempotente), 401, 409, 422, 503 |
| PATCH | `/api/orders/{id}` | Edita `customer_name`/`items` (dueño o admin, en `pendiente`) o cambia `status` (admin) | sesión | 200, 401, 403, 404, 409, 422, 503 |
| DELETE | `/api/orders/{id}` | Borrado lógico (admin: cualquiera; cliente: los suyos en `pendiente`) | sesión | 200, 401, 404, 409, 503 |
| GET | `/api/chaos/status` | Vista del clúster: nodos (vía REST), banderas y estado de las BD | admin | 200, 401, 403 |
| GET | `/api/chaos/local` | Banderas de caos de **este** nodo (usado entre nodos) | admin | 200 |
| POST | `/api/chaos/toggle` | Alterna `SIMULATE_CRASH` en el nodo que recibe la petición | admin | 200 |
| POST | `/api/chaos/toggle/{nodo}` | Alterna `SIMULATE_CRASH` en un nodo concreto (reenvío REST) | admin | 200, 404, 409 |
| PUT | `/api/chaos/crash/{nodo}` | Fija `SIMULATE_CRASH` (`{"enabled": bool}`), idempotente | admin | 200, 404, 409 |
| POST | `/api/chaos/db-primary/toggle` | Alterna `SIMULATE_DB_PRIMARY_DOWN` en todo el clúster | admin | 200 |
| PUT | `/api/chaos/db-primary` | Fija `SIMULATE_DB_PRIMARY_DOWN` (`{"enabled": bool}`) en todo el clúster | admin | 200 |
| GET | `/api/replication/status` | Filas por base y tabla, `in_sync`, `divergent_by_table` | admin | 200 |
| POST | `/api/replication/sync` | Fuerza la reconciliación (por tabla en `tables`) | admin | 200 |
| GET | `/api/monitoring/local?window=300` | Métricas y eventos de **este** nodo (ventana 60–900 s) | admin | 200, 401, 403 |
| GET | `/api/monitoring/cluster?window=300` | Vista agregada: nodos (vía REST), gateway, bases y eventos | admin | 200, 401, 403 |

Las rutas con sesión requieren `Authorization: Bearer <access_token>`. En Swagger (`/api/docs`) se usa
el botón **Authorize**.

**Cabeceras de respuesta:** `X-Handled-By-Node` (todas), `X-DB-Used` y `X-Replication` (pedidos),
`X-Idempotent-Replay` (POST repetido), `Retry-After` (503 de caos y 429 de login), `WWW-Authenticate`
(401) y `X-Upstream-Status` (añadida por NGINX).

Ejemplo — iniciar sesión (respuesta real; el token se recorta):

```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIsInR5…",
  "token_type": "bearer",
  "expires_in": 28800,
  "user": {"id": "6981265a-39f7-4ef8-a173-4b760b032d25", "username": "ana.torres",
           "full_name": "Ana Torres", "role": "user", "is_active": true,
           "created_at": "2026-11-18T07:03:39.075525Z"},
  "node": "Nodo-Beta-02",
  "db_used": "primary"
}
```

Ejemplo — crear un pedido con ese token (lo atendió **otro nodo**, Alpha, sin problema):

```http
POST /api/orders
Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5…
Content-Type: application/json

{"id": "6f1c0e8a-3b1e-4c55-9a51-2d1f0b7c9e10",
 "customer_name": "Ana Torres",
 "items": [{"product": "Laptop", "quantity": 1, "unit_price": 1200},
           {"product": "Mouse", "quantity": 2, "unit_price": 25.5}]}
```

```json
{
  "node": "Nodo-Alpha-01",
  "db_used": "primary",
  "replication": "synced",
  "data": {
    "id": "6f1c0e8a-3b1e-4c55-9a51-2d1f0b7c9e10",
    "customer_name": "Ana Torres",
    "items": [{"product": "Laptop", "quantity": 1, "unit_price": 1200.0},
              {"product": "Mouse", "quantity": 2, "unit_price": 25.5}],
    "total": 1251.0,
    "status": "pendiente",
    "next_status": "en proceso",
    "created_at": "2026-11-18T07:03:39.271783Z",
    "updated_at": "2026-11-18T07:03:39.271783Z",
    "handled_by_node": "Nodo-Alpha-01",
    "owner_id": "6981265a-39f7-4ef8-a173-4b760b032d25",
    "owner_username": null
  }
}
```

Ejemplo — un cliente intenta cambiar el estado (sólo admin):

```json
{"detail": "Sólo un administrador puede cambiar el estado de un pedido", "node": "Nodo-Beta-02"}
```

Ejemplo — transición inválida (`pendiente → enviado`, como admin):

```json
{"detail": "Transición inválida 'pendiente' -> 'enviado': el único estado siguiente permitido es 'en proceso'",
 "node": "Nodo-Alpha-01", "current_status": "pendiente", "allowed_next": "en proceso"}
```

---

## 12. Ejemplo de fallo simulado paso a paso

Salidas **reales** obtenidas contra el clúster local (`BASE=http://localhost:8080`) con la versión 1.0.
Desde la 1.1 los endpoints de pedidos y de caos exigen sesión de administrador; basta con añadir el token
a cada `curl` (el comportamiento ante fallos es idéntico):

```bash
TOKEN=$(curl -s -X POST $BASE/api/auth/login -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"<ADMIN_PASSWORD>"}' | python -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
alias curl='curl -H "Authorization: Bearer $TOKEN"'
```

**1) Estado normal — Round-Robin alterna los nodos:**

```text
$ for i in 1 2 3 4; do curl -s -i $BASE/api/orders | grep -iE "^(HTTP/|x-handled-by-node|x-db-used|x-upstream-status)"; done
HTTP/1.1 200 OK
x-db-used: primary
x-handled-by-node: Nodo-Beta-02
X-Upstream-Status: 200
--
HTTP/1.1 200 OK
x-db-used: primary
x-handled-by-node: Nodo-Alpha-01
X-Upstream-Status: 200
--
HTTP/1.1 200 OK
x-db-used: primary
x-handled-by-node: Nodo-Beta-02
X-Upstream-Status: 200
--
HTTP/1.1 200 OK
x-db-used: primary
x-handled-by-node: Nodo-Alpha-01
X-Upstream-Status: 200
```

**2) Se "tumba" `Nodo-Alpha-01`.** La orden la recibió Beta y la reenvió por REST a Alpha:

```text
$ curl -s -X POST $BASE/api/chaos/toggle/Nodo-Alpha-01
{"applied_by":"Nodo-Alpha-01","forwarded":true,"node":"Nodo-Alpha-01","simulate_crash":true,
 "simulate_db_primary_down":false,"updated_at":"2026-11-18T04:19:03.826356+00:00","forwarded_by":"Nodo-Beta-02"}
```

**3) El cliente no ve el fallo:** la primera petición que iba a Alpha recibió 503 y NGINX la reintentó
en Beta (`503, 200`); las siguientes van directamente a Beta. Una escritura también funciona:

```text
HTTP/1.1 200 OK x-handled-by-node: Nodo-Beta-02 X-Upstream-Status: 503, 200
HTTP/1.1 200 OK x-handled-by-node: Nodo-Beta-02 X-Upstream-Status: 200
HTTP/1.1 200 OK x-handled-by-node: Nodo-Beta-02 X-Upstream-Status: 200
HTTP/1.1 200 OK x-handled-by-node: Nodo-Beta-02 X-Upstream-Status: 200

$ curl -s -i -X POST $BASE/api/orders  (escritura con Alpha caído)
HTTP/1.1 201 Created
x-db-used: primary
x-replication: synced
x-handled-by-node: Nodo-Beta-02
```

Consultado directamente (sin NGINX), el nodo caído responde 503:

```text
HTTP Error 503: Service Unavailable | {"detail":"Nodo-Alpha-01 en caída simulada (SIMULATE_CRASH)","node":"Nodo-Alpha-01","simulated_crash":true}
```

**4) Se simula la caída de `db-primary`** (propagada a ambos nodos). Lecturas y escrituras siguen
funcionando desde la réplica:

```text
$ curl -s -X POST $BASE/api/chaos/db-primary/toggle
simulate_db_primary_down = True | propagated_to = [('Nodo-Alpha-01', True), ('Nodo-Beta-02', True)]

$ curl -s -i $BASE/api/orders   (x2, uno por nodo)
HTTP/1.1 200 OK x-db-used: replica_failover x-handled-by-node: Nodo-Beta-02 | body db_used = replica_failover | count = 5
HTTP/1.1 200 OK x-db-used: replica_failover x-handled-by-node: Nodo-Alpha-01 | body db_used = replica_failover | count = 5

$ curl -s -X POST $BASE/api/orders   (escritura con db-primary caída)
{'node': 'Nodo-Beta-02', 'db_used': 'replica_failover', 'replication': 'pending'} id = e233253a-a5e6-44fb-abaf-501dec17f851
```

**5) Se restaura la primaria:** el nodo reconcilia y copia la fila escrita durante la caída; ambas
bases quedan en sincronía y el pedido se lee ya desde la primaria:

```text
$ curl -s -X POST $BASE/api/chaos/db-primary/toggle   (restaurar)
sync local = {'status': 'ok', 'copied_to_primary': 1, 'copied_to_replica': 0, 'rows_primary': 6, 'rows_replica': 6}

$ curl -s $BASE/api/replication/status
{'in_sync': True, 'divergent_orders': 0}

$ curl -s $BASE/api/orders/e233253a-a5e6-44fb-abaf-501dec17f851
db_used = primary | customer = QA Failover BD | handled_by_node = Nodo-Beta-02
```

Trazas de los nodos durante el ejemplo (`docker compose logs backend-node-1 backend-node-2`):

```text
[Nodo-Beta-02] pedidos.chaos: CHAOS SIMULATE_DB_PRIMARY_DOWN=True en Nodo-Beta-02
[Nodo-Beta-02] pedidos.db: FAILOVER escritura: db-primary no disponible (caída simulada de db-primary (SIMULATE_DB_PRIMARY_DOWN)) -> db-replica
[Nodo-Beta-02] pedidos.db: WRITE order=e233253a-a5e6-44fb-abaf-501dec17f851 en db-replica (failover); copia a db-primary PENDIENTE
[Nodo-Beta-02] pedidos.chaos: CHAOS SIMULATE_DB_PRIMARY_DOWN=False en Nodo-Beta-02
[Nodo-Beta-02] pedidos.db: SYNC (restauración de db-primary): 1 fila(s) réplica->primaria, 0 fila(s) primaria->réplica
```

El mismo comportamiento se verificó con **fallos reales** (`docker compose stop backend-node-1` y
`docker compose stop db-primary`); ver `docs/03_informe_pruebas.md`, §6.

---

## 13. Tecnologías empleadas y justificación

| Tecnología | Versión | Uso | Justificación |
|------------|---------|-----|---------------|
| Python | 3.11 | Lenguaje del backend | Requisito del proyecto; ecosistema maduro para APIs |
| FastAPI | 0.163 | Framework REST de los nodos | Alto rendimiento, validación automática con Pydantic, OpenAPI/Swagger integrado, middlewares para el caos |
| SQLAlchemy | 2.0 | ORM / Core | Permite **dos engines/sesiones** y ejecutar la misma lógica en cualquiera de las dos bases; `INSERT … ON CONFLICT` portable a PostgreSQL y SQLite |
| psycopg | 3.3 | Driver PostgreSQL | Driver moderno, `connect_timeout` y opciones de sesión para acotar la espera ante caídas |
| Pydantic | v2 | Esquemas de entrada/salida | Validación estricta (`extra="forbid"`), mensajes 422 claros |
| httpx | 0.28 | Cliente REST entre nodos y tests | API síncrona simple; `TestClient` de FastAPI se basa en él |
| PyJWT | 2.15 | Tokens de sesión | Librería pequeña, pura Python y auditada; algoritmo fijo HS256 y validación de `exp`/`iss` |
| hashlib (PBKDF2) | stdlib | Hash de contraseñas | Recomendado por OWASP; sin dependencias compiladas |
| Pytest | 9.2 | Pruebas | Fixtures para levantar el sistema con dos bases SQLite temporales o PostgreSQL real |
| PostgreSQL | 16 | Bases primaria y réplica | Requisito; `JSONB`, `TIMESTAMPTZ`, `CHECK`, `SELECT … FOR UPDATE`, `ON CONFLICT` |
| NGINX | 1.28 | Gateway / balanceador | Round-Robin y failover pasivo nativos (`proxy_next_upstream`), re-resolución DNS (`resolve`), sirve estáticos de forma eficiente |
| React | 18.3 | Interfaz | Requisito; componentes y estado declarativo para un dashboard en vivo |
| Vite | 6.4 | Build del frontend | Build rápido y ligero, sin librerías pesadas; proxy de desarrollo para `/api` |
| Docker Compose | v2+ | Orquestación | Simula los nodos distribuidos en una sola máquina; compatible con Coolify |

---

## 14. Variables de entorno

| Variable | Dónde | Valor por defecto | Descripción |
|----------|-------|-------------------|-------------|
| `GATEWAY_PORT` | `.env` / compose | `8080` | Puerto del **host** donde se publica el gateway (interno siempre 80) |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | `.env` / compose | `admin` / `secretpassword` / `orders_db` | Credenciales de ambas bases |
| `NODE_ID` | nodos | `Nodo-Local` | Identidad del nodo (`Nodo-Alpha-01`, `Nodo-Beta-02`) |
| `PRIMARY_DB_URL` | nodos | `sqlite:///./local_primary.db` | URL de `db-primary` (`postgresql://…` se convierte a psycopg 3) |
| `REPLICA_DB_URL` | nodos | `sqlite:///./local_replica.db` | URL de `db-replica` |
| `PEERS` | nodos | vacío | Otros nodos: `Nombre=http://host:puerto,…` |
| `SIMULATE_CRASH` | nodos | `false` | Arranca el nodo ya "caído" (`NODE1_SIMULATE_CRASH`/`NODE2_SIMULATE_CRASH` en `.env`) |
| `SIMULATE_DB_PRIMARY_DOWN` | nodos | `false` | Arranca con la primaria "caída" |
| `SYNC_INTERVAL_SECONDS` | nodos | `5` | Periodo de la reconciliación en segundo plano (`0` = desactivada) |
| `DB_CONNECT_TIMEOUT` | nodos | `2` | Timeout de conexión a PostgreSQL (s) |
| `BREAKER_COOLDOWN_SECONDS` | nodos | `5` | Tiempo que el circuit breaker evita la primaria tras un fallo real |
| `PEER_TIMEOUT_SECONDS` | nodos | `1.5` | Timeout de las llamadas REST entre nodos |
| `JWT_SECRET` | `.env` / nodos | valor de desarrollo (avisa en el log) | Secreto HS256; **largo, aleatorio e igual en ambos nodos** |
| `JWT_EXPIRES_MINUTES` | `.env` / nodos | `480` | Duración de la sesión |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | `.env` / nodos | `admin` / vacío (se genera) | Administrador inicial; sólo se usan si aún no existe |
| `ALLOW_REGISTRATION` | `.env` / nodos | `true` | Permite crear cuentas de cliente desde el login |
| `PASSWORD_HASH_ITERATIONS` | nodos | `600000` | Iteraciones PBKDF2 para hashes nuevos |
| `LOGIN_MAX_FAILURES` / `LOGIN_WINDOW_SECONDS` | nodos | `5` / `300` | Bloqueo temporal por intentos fallidos |
| `GATEWAY_STATUS_URL` | nodos (compose) | `http://gateway:8081/nginx_status` | `stub_status` interno de NGINX para el monitoreo; vacío = no se consulta |

---

## 15. Decisiones de diseño, limitaciones y trabajo futuro

**Decisiones:**

- **Replicación a nivel de aplicación** (escritura dual + reconciliación) en lugar de *streaming
  replication* de PostgreSQL: una réplica física es de sólo lectura y aceptar escrituras exigiría
  promoverla (Patroni/repmgr). La escritura dual permite que la réplica asuma **lecturas y escrituras**
  de inmediato y hace visible en el código y en la API (`db_used`, `replication`) qué ocurrió.
- **Un único worker de Uvicorn por nodo**: las banderas de caos viven en memoria del proceso; varios
  workers tendrían estados distintos.
- **Liveness ≠ readiness**: el healthcheck de Docker no consulta las bases, para que un nodo no se
  marque como enfermo cuando, en realidad, sigue sirviendo vía réplica.

**Limitaciones conocidas:**

- *Last-write-wins* depende de relojes sincronizados. En Docker todos los contenedores comparten el
  reloj del host; en varias máquinas se requiere NTP o versiones lógicas (relojes vectoriales).
- Si durante una partición se modifica **el mismo pedido** en ambas bases, gana la versión más reciente
  y la otra modificación se pierde (sin fusión de conflictos).
- Las banderas de caos no persisten: reiniciar un nodo lo restaura.
- Los *tombstones* no se purgan; la reconciliación compara la tabla completa (`id`, `updated_at`), lo
  cual es adecuado para el volumen del proyecto pero no para millones de filas.
- El gateway es un punto único de fallo (en producción: dos NGINX con IP virtual o el balanceador de la
  plataforma, p. ej. Traefik en Coolify).
- Las métricas del monitoreo viven en memoria: se reinician con el nodo y cubren 15 minutos. No hay
  alertas automáticas (el siguiente paso sería exportarlas a Prometheus/Grafana).
- El límite de intentos de login es por nodo (en memoria): con 2 nodos el atacante tiene el doble de
  intentos. Un límite global necesitaría estado compartido (Redis o `limit_req` en el gateway).
- El token vive en `localStorage` (simple y sin CSRF, pero legible ante un XSS; mitigado por la CSP).
  Alternativa: cookie `HttpOnly` + protección CSRF. No hay *refresh tokens*: al expirar se vuelve a entrar.
- Si la réplica cae de verdad mientras la primaria está "caída" por simulación, no se puede validar a
  ningún usuario y la orden de restaurar debe darse reiniciando los nodos.

**Trabajo futuro:** panel de administración de datos (usuarios y pedidos), notificaciones en tiempo real con WebSockets, cola de mensajes para la replicación (outbox), *streaming replication* con promoción
automática, purga de *tombstones* y métricas (Prometheus/Grafana).
