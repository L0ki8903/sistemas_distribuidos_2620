# Informe de Pruebas — Sistema Distribuido de Gestión de Pedidos Tolerante a Fallos

> Fecha de ejecución: 17 de noviembre de 2026 · Todas las salidas de este documento son **literales**
> de las ejecuciones reales (no editadas salvo recortes señalados con `…`).

## Contenido

1. [Objetivo y alcance](#1-objetivo-y-alcance)
2. [Entorno de pruebas](#2-entorno-de-pruebas)
3. [Estrategia](#3-estrategia)
4. [Catálogo de casos de prueba](#4-catálogo-de-casos-de-prueba)
5. [Salida real de pytest](#5-salida-real-de-pytest)
6. [Pruebas de fallo sobre el clúster Docker](#6-pruebas-de-fallo-sobre-el-clúster-docker)
7. [Pruebas de interfaz (navegador real)](#7-pruebas-de-interfaz-navegador-real)
8. [Resultados](#8-resultados)
9. [Defectos encontrados y corregidos](#9-defectos-encontrados-y-corregidos)
10. [Conclusiones](#10-conclusiones)
11. [Limitaciones y trabajo futuro](#11-limitaciones-y-trabajo-futuro)

---

## 1. Objetivo y alcance

Verificar que el sistema:

1. Gestiona pedidos correctamente: **crear, listar/filtrar, actualizar estado, editar y eliminar**, con
   la máquina de estados `pendiente → en proceso → enviado → entregado`.
2. **Replica** cada escritura de `db-primary` en `db-replica`.
3. **Tolera la caída de un nodo** de aplicación (el nodo responde 503 y NGINX reintenta en el otro).
4. **Tolera la caída de la base primaria** (failover a la réplica sin HTTP 500) y **se recupera**
   (reconciliación de las escrituras hechas durante la caída).
5. Ofrece una interfaz que refleja todo lo anterior y permite provocarlo.

Requisito mínimo del curso: 3 pruebas unitarias/integración + 1 de fallo. Requisito del proyecto:
5 pruebas en `backend/tests/test_orders.py` (crear, actualizar estado, eliminar, fallo de nodo 503,
failover de BD). **Se implementaron 15 + 4 e2e + 8 verificaciones manuales de clúster + 26 de interfaz.**

---

## 2. Entorno de pruebas

| Elemento | Versión / detalle |
|----------|-------------------|
| Sistema anfitrión | Windows 11 (x86_64) con Docker Desktop — Docker 29.9.2, Compose v5.6.1 |
| Python (suite oficial) | **3.11.18** en contenedor `python:3.11-slim` |
| Python (e2e desde el anfitrión) | 3.14.7 (entorno virtual `backend/.venv`) |
| Librerías | FastAPI 0.163.2 · Starlette 1.10.0 · SQLAlchemy 2.0.58 · Pydantic 2.14.3 · psycopg 3.3.9 · httpx 0.28.1 · Uvicorn 0.54.0 · pytest 9.2.0 |
| Bases de datos | (a) **SQLite** en dos archivos temporales por test (primaria/réplica) · (b) **PostgreSQL 16.16** real (base `orders_test` en `db-primary` y `db-replica`) |
| Gateway | NGINX 1.28.3 (`nginx:1.28-alpine`) |
| Frontend / UI | React 18.3.1 + Vite 6.4.6 · Google Chrome headless controlado con puppeteer-core 24 |

---

## 3. Estrategia

| Nivel | Herramienta | Qué valida | Cómo se ejecuta |
|-------|-------------|-----------|-----------------|
| Unitarias / API | pytest + `TestClient` | Reglas de negocio, máquina de estados, validaciones, escritura dual, failover, circuit breaker, reconciliación, middleware de caos, comunicación entre nodos (2 apps en el mismo proceso) | `python -m pytest -v` (SQLite) |
| Integración con PostgreSQL | pytest + PostgreSQL 16 real | Lo mismo con el dialecto real: `JSONB`, `TIMESTAMPTZ`, `ON CONFLICT`, `FOR UPDATE` | `TEST_PRIMARY_DB_URL=… TEST_REPLICA_DB_URL=… python -m pytest tests/test_orders.py` |
| End-to-end del clúster | pytest + httpx contra NGINX | Round-Robin real, failover de nodo transparente, failover de BD y reconciliación entre contenedores, servicio del frontend | `CLUSTER_BASE_URL=http://localhost:8080 python -m pytest tests/test_cluster_e2e.py` |
| Fallos reales | `curl` + `docker compose stop/start` | Comportamiento ante caídas **no simuladas** de un nodo y de la primaria | Pasos del comando `/verify-cluster` |
| Interfaz | Chrome headless + puppeteer-core | Los botones y formularios funcionan y la UI refleja nodo/BD/failover | `cd e2e-ui && node ui-test.mjs` |

Para las pruebas de fallo se usan dos técnicas: **inyección de fallos** (banderas `SIMULATE_CRASH` y
`SIMULATE_DB_PRIMARY_DOWN`) y **fallos reales** (URL de base inalcanzable en los tests; contenedores
detenidos en el clúster).

---

## 4. Catálogo de casos de prueba

### 4.1 `backend/tests/test_orders.py` (unitarias, integración y fallo)

| ID | Tipo | Test | Descripción | Resultado esperado | Resultado obtenido |
|----|------|------|-------------|--------------------|--------------------|
| U-01 | Unitaria · **REQ-1** | `test_crear_pedido_se_escribe_en_primaria_y_se_replica` | Crear pedido | 201, `pendiente`, total 1251.0 calculado en servidor, UUID, `X-Handled-By-Node`, fila idéntica en primaria y réplica | ✅ PASSED |
| U-02 | Unitaria · **REQ-2** | `test_actualizar_estado_recorre_todo_el_ciclo` | Recorrer `pendiente → en proceso → enviado → entregado` | 200 en cada paso, cambio replicado, `next_status = None` al final | ✅ PASSED |
| U-03 | Unitaria | `test_actualizar_estado_rechaza_transiciones_invalidas` | Saltar, retroceder, estado inexistente, repetir estado | 409, 409, 422 y 200 (idempotente) | ✅ PASSED |
| U-04 | Unitaria · **REQ-3** | `test_eliminar_pedido_con_borrado_logico_replicado` | Eliminar pedido | 200; luego 404; no aparece en el listado; tombstone en ambas bases; 2.º DELETE idempotente; id inexistente 404 | ✅ PASSED |
| U-05 | Fallo · **REQ-4** | `test_fallo_de_nodo_devuelve_503_en_orders` | Activar `SIMULATE_CRASH` | GET/POST/PATCH/DELETE en `/api/orders*` → 503 con `X-Handled-By-Node` y `Retry-After`; `/api/health` y `/api/chaos/status` siguen en 200; al restaurar vuelve a 200 | ✅ PASSED |
| U-06 | Fallo · **REQ-5** | `test_failover_bd_primaria_a_replica_y_reconciliacion` | Activar `SIMULATE_DB_PRIMARY_DOWN`, leer y escribir, restaurar | Lecturas/escrituras 200/201 con `db_used = replica_failover`; primaria atrasada; al restaurar `copied_to_primary = 2`, `in_sync = true` y lecturas desde `primary` | ✅ PASSED |
| U-07 | Fallo real | `test_failover_cuando_la_primaria_es_inalcanzable_de_verdad` | Primaria con URL inalcanzable | 201/200 con `replica_failover`; circuit breaker abierto; health: primaria `down`, réplica `up` | ✅ PASSED |
| U-08 | Fallo real | `test_si_caen_ambas_bd_responde_503_y_nunca_500` | Ambas bases inalcanzables | 503 con mensaje claro (nunca 500); `/api/health` 200 | ✅ PASSED |
| U-09 | Fallo real | `test_replica_caida_no_bloquea_escrituras_en_primaria` | Réplica inalcanzable | 201 en primaria con `replication = pending` y `needs_resync` | ✅ PASSED |
| U-10 | Integración | `test_listar_y_filtrar_por_estado_y_cliente` | Filtros `status` y `customer` | Resultados correctos, búsqueda sin distinguir mayúsculas, `%` escapado | ✅ PASSED |
| U-11 | Unitaria | `test_editar_pedido_recalcula_total_y_solo_si_esta_pendiente` | Editar cliente/ítems | Total recalculado (599.97) y replicado; 409 si ya no está pendiente | ✅ PASSED |
| U-12 | Unitaria | `test_creacion_idempotente_con_id_generado_por_el_cliente` | Repetir POST con el mismo UUID | 201 y luego 200 con `X-Idempotent-Replay`; una sola fila por base | ✅ PASSED |
| U-13 | Unitaria | `test_validaciones_de_entrada` | Datos inválidos | 422 para ítems vacíos, cantidad 0, precio negativo, nombre corto, campos no permitidos, PATCH vacío | ✅ PASSED |
| U-14 | Unitaria | `test_health_y_cabecera_x_handled_by_node` | Salud y cabecera de nodo | 200, ambas BD `up`, `deep=false` sin BD, cabecera también en 404 | ✅ PASSED |
| U-15 | Integración | `test_comunicacion_entre_nodos_reenvia_caos_y_propaga_caida_de_bd` | Dos nodos reales en el mismo proceso | Alpha reenvía por REST la caída de Beta; la caída de BD se propaga; al restaurar, el receptor reconcilia (1 fila) y el peer ya está al día | ✅ PASSED |

### 4.2 `backend/tests/test_cluster_e2e.py` (end-to-end contra NGINX + Docker)

| ID | Test | Resultado esperado | Resultado obtenido |
|----|------|--------------------|--------------------|
| E-01 | `test_e2e_round_robin_alterna_entre_los_dos_nodos` | En 6 peticiones aparecen ambos nodos, al menos 2 veces cada uno | ✅ PASSED |
| E-02 | `test_e2e_caida_de_nodo_es_transparente_para_el_cliente` | Con Alpha caído: 8/8 respuestas 200 desde Beta, `X-Upstream-Status` contiene 503; POST 201 | ✅ PASSED |
| E-03 | `test_e2e_failover_de_bd_primaria_y_reconciliacion` | Propagación a 2 nodos; `replica_failover`; `in_sync` falso durante la caída y verdadero tras restaurar; lectura posterior desde `primary` | ✅ PASSED |
| E-04 | `test_e2e_gateway_sirve_el_frontend_react` | `/` y rutas del SPA devuelven `index.html` (`try_files`) | ✅ PASSED |

### 4.3 Verificaciones de clúster con fallos simulados y reales (`/verify-cluster`)

| ID | Verificación | Resultado |
|----|--------------|-----------|
| F-01 | Los 5 contenedores llegan a `healthy` | ✅ |
| F-02 | Round-Robin alterna `Nodo-Beta-02` / `Nodo-Alpha-01` | ✅ |
| F-03 | Caída simulada de Alpha → 6/6 respuestas 200 vía Beta (`503, 200` en la primera) | ✅ |
| F-04 | Escritura durante la caída del nodo → 201 vía Beta | ✅ |
| F-05 | Caída simulada de `db-primary` → 200/201 con `replica_failover`, sin 500 | ✅ |
| F-06 | Restauración → 1 fila copiada réplica → primaria, `in_sync: true` | ✅ |
| F-07 | Fallos reales: `stop backend-node-1` (200 vía Beta, `504, 200`) y `stop db-primary` (failover + circuit breaker + reconciliación automática al arrancarla) | ✅ |
| F-08 | Suite e2e de pytest contra el clúster | ✅ 4/4 |

---

## 5. Salida real de pytest

### 5.1 Suite completa — Python 3.11 + SQLite (stack oficial)

Comando (desde `backend/`):

```bash
docker run --rm -v "$PWD":/src:ro python:3.11-slim sh -c \
  "cp -r /src /app && cd /app && pip install -q -r requirements-dev.txt && python -m pytest -v -p no:cacheprovider --durations=5"
```

```text
============================= test session starts ==============================
platform linux -- Python 3.11.18, pytest-9.2.0, pluggy-1.7.0 -- /usr/local/bin/python
rootdir: /app
configfile: pytest.ini
testpaths: tests
plugins: anyio-4.16.0
collecting ... collected 19 items

tests/test_cluster_e2e.py::test_e2e_round_robin_alterna_entre_los_dos_nodos SKIPPED [  5%]
tests/test_cluster_e2e.py::test_e2e_caida_de_nodo_es_transparente_para_el_cliente SKIPPED [ 10%]
tests/test_cluster_e2e.py::test_e2e_failover_de_bd_primaria_y_reconciliacion SKIPPED [ 15%]
tests/test_cluster_e2e.py::test_e2e_gateway_sirve_el_frontend_react SKIPPED [ 21%]
tests/test_orders.py::test_crear_pedido_se_escribe_en_primaria_y_se_replica PASSED [ 26%]
tests/test_orders.py::test_actualizar_estado_recorre_todo_el_ciclo PASSED [ 31%]
tests/test_orders.py::test_actualizar_estado_rechaza_transiciones_invalidas PASSED [ 36%]
tests/test_orders.py::test_eliminar_pedido_con_borrado_logico_replicado PASSED [ 42%]
tests/test_orders.py::test_fallo_de_nodo_devuelve_503_en_orders PASSED   [ 47%]
tests/test_orders.py::test_failover_bd_primaria_a_replica_y_reconciliacion PASSED [ 52%]
tests/test_orders.py::test_failover_cuando_la_primaria_es_inalcanzable_de_verdad PASSED [ 57%]
tests/test_orders.py::test_si_caen_ambas_bd_responde_503_y_nunca_500 PASSED [ 63%]
tests/test_orders.py::test_replica_caida_no_bloquea_escrituras_en_primaria PASSED [ 68%]
tests/test_orders.py::test_listar_y_filtrar_por_estado_y_cliente PASSED  [ 73%]
tests/test_orders.py::test_editar_pedido_recalcula_total_y_solo_si_esta_pendiente PASSED [ 78%]
tests/test_orders.py::test_creacion_idempotente_con_id_generado_por_el_cliente PASSED [ 84%]
tests/test_orders.py::test_validaciones_de_entrada PASSED                [ 89%]
tests/test_orders.py::test_health_y_cabecera_x_handled_by_node PASSED    [ 94%]
tests/test_orders.py::test_comunicacion_entre_nodos_reenvia_caos_y_propaga_caida_de_bd PASSED [100%]

============================= slowest 5 durations ==============================
0.27s call     tests/test_orders.py::test_comunicacion_entre_nodos_reenvia_caos_y_propaga_caida_de_bd
0.14s call     tests/test_orders.py::test_listar_y_filtrar_por_estado_y_cliente
0.13s call     tests/test_orders.py::test_failover_bd_primaria_a_replica_y_reconciliacion
0.12s call     tests/test_orders.py::test_actualizar_estado_recorre_todo_el_ciclo
0.11s call     tests/test_orders.py::test_eliminar_pedido_con_borrado_logico_replicado
=========================== short test summary info ============================
SKIPPED [1] tests/test_cluster_e2e.py:41: Define CLUSTER_BASE_URL (p. ej. http://localhost:8080) con el clúster arriba
SKIPPED [1] tests/test_cluster_e2e.py:52: Define CLUSTER_BASE_URL (p. ej. http://localhost:8080) con el clúster arriba
SKIPPED [1] tests/test_cluster_e2e.py:70: Define CLUSTER_BASE_URL (p. ej. http://localhost:8080) con el clúster arriba
SKIPPED [1] tests/test_cluster_e2e.py:97: Define CLUSTER_BASE_URL (p. ej. http://localhost:8080) con el clúster arriba
======================== 15 passed, 4 skipped in 2.57s =========================
```

Los 4 `SKIPPED` son los tests e2e, que por diseño sólo corren con el clúster levantado (§5.3).

### 5.2 Misma suite contra PostgreSQL 16 real

Comando (contenedor en la red del clúster, base aislada `orders_test` para no tocar los datos reales):

```bash
docker run --rm --network act5_default \
  -e TEST_PRIMARY_DB_URL=postgresql://admin:secretpassword@db-primary:5432/orders_test \
  -e TEST_REPLICA_DB_URL=postgresql://admin:secretpassword@db-replica:5432/orders_test \
  -v "$PWD":/src:ro python:3.11-slim sh -c \
  "cp -r /src /app && cd /app && pip install -q -r requirements-dev.txt && python -m pytest -v -p no:cacheprovider tests/test_orders.py"
```

```text
============================= test session starts ==============================
platform linux -- Python 3.11.18, pytest-9.2.0, pluggy-1.7.0 -- /usr/local/bin/python
rootdir: /app
configfile: pytest.ini
plugins: anyio-4.16.0
collecting ... collected 15 items

tests/test_orders.py::test_crear_pedido_se_escribe_en_primaria_y_se_replica PASSED [  6%]
tests/test_orders.py::test_actualizar_estado_recorre_todo_el_ciclo PASSED [ 13%]
tests/test_orders.py::test_actualizar_estado_rechaza_transiciones_invalidas PASSED [ 20%]
tests/test_orders.py::test_eliminar_pedido_con_borrado_logico_replicado PASSED [ 26%]
tests/test_orders.py::test_fallo_de_nodo_devuelve_503_en_orders PASSED   [ 33%]
tests/test_orders.py::test_failover_bd_primaria_a_replica_y_reconciliacion PASSED [ 40%]
tests/test_orders.py::test_failover_cuando_la_primaria_es_inalcanzable_de_verdad PASSED [ 46%]
tests/test_orders.py::test_si_caen_ambas_bd_responde_503_y_nunca_500 PASSED [ 53%]
tests/test_orders.py::test_replica_caida_no_bloquea_escrituras_en_primaria PASSED [ 60%]
tests/test_orders.py::test_listar_y_filtrar_por_estado_y_cliente PASSED  [ 66%]
tests/test_orders.py::test_editar_pedido_recalcula_total_y_solo_si_esta_pendiente PASSED [ 73%]
tests/test_orders.py::test_creacion_idempotente_con_id_generado_por_el_cliente PASSED [ 80%]
tests/test_orders.py::test_validaciones_de_entrada PASSED                [ 86%]
tests/test_orders.py::test_health_y_cabecera_x_handled_by_node PASSED    [ 93%]
tests/test_orders.py::test_comunicacion_entre_nodos_reenvia_caos_y_propaga_caida_de_bd PASSED [100%]

============================== 15 passed in 4.43s ==============================
```

### 5.3 Pruebas end-to-end contra el clúster Docker

Comando (desde `backend/`, con `docker compose up -d` activo):

```bash
CLUSTER_BASE_URL=http://localhost:8080 python -m pytest -v -p no:cacheprovider tests/test_cluster_e2e.py
```

```text
============================= test session starts =============================
platform win32 -- Python 3.14.7, pytest-9.2.0, pluggy-1.7.0 -- C:\Users\…\backend\.venv\Scripts\python.exe
rootdir: C:\Users\…\backend
configfile: pytest.ini
plugins: anyio-4.16.0
collecting ... collected 4 items

tests/test_cluster_e2e.py::test_e2e_round_robin_alterna_entre_los_dos_nodos PASSED [ 25%]
tests/test_cluster_e2e.py::test_e2e_caida_de_nodo_es_transparente_para_el_cliente PASSED [ 50%]
tests/test_cluster_e2e.py::test_e2e_failover_de_bd_primaria_y_reconciliacion PASSED [ 75%]
tests/test_cluster_e2e.py::test_e2e_gateway_sirve_el_frontend_react PASSED [100%]

============================== 4 passed in 6.92s ==============================
```

---

## 6. Pruebas de fallo sobre el clúster Docker

Salidas reales de los pasos de `/verify-cluster` (`BASE=http://localhost:8080`).

### 6.1 Estado inicial y Round-Robin (F-01, F-02)

```text
$ docker compose ps
SERVICE          STATUS                    PORTS
backend-node-1   Up 8 seconds (healthy)    8000/tcp
backend-node-2   Up 7 seconds (healthy)    8000/tcp
db-primary       Up 14 seconds (healthy)   5432/tcp
db-replica       Up 14 seconds (healthy)   5432/tcp
gateway          Up 7 seconds (healthy)    0.0.0.0:8080->80/tcp, [::]:8080->80/tcp

$ curl -s $BASE/api/chaos/status  (resumen)
reporter: Nodo-Alpha-01
  nodo Nodo-Alpha-01 reachable= True simulate_crash= False
  nodo Nodo-Beta-02 reachable= True simulate_crash= False
  db-primary: up | db-replica: up | circuit_breaker: closed

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

### 6.2 Caída simulada de un nodo (F-03, F-04)

```text
$ curl -s -X POST $BASE/api/chaos/toggle/Nodo-Alpha-01
{"applied_by":"Nodo-Alpha-01","forwarded":true,"node":"Nodo-Alpha-01","simulate_crash":true,"simulate_db_primary_down":false,"updated_at":"2026-11-18T04:19:03.826356+00:00","forwarded_by":"Nodo-Beta-02"}

$ for i in 1..6; do curl -s -i $BASE/api/orders ...; done
HTTP/1.1 200 OK x-handled-by-node: Nodo-Beta-02 X-Upstream-Status: 503, 200
HTTP/1.1 200 OK x-handled-by-node: Nodo-Beta-02 X-Upstream-Status: 200
HTTP/1.1 200 OK x-handled-by-node: Nodo-Beta-02 X-Upstream-Status: 200
HTTP/1.1 200 OK x-handled-by-node: Nodo-Beta-02 X-Upstream-Status: 200
HTTP/1.1 200 OK x-handled-by-node: Nodo-Beta-02 X-Upstream-Status: 200
HTTP/1.1 200 OK x-handled-by-node: Nodo-Beta-02 X-Upstream-Status: 200

$ curl -s -i -X POST $BASE/api/orders  (escritura con Alpha caído)
HTTP/1.1 201 Created
x-db-used: primary
x-replication: synced
x-handled-by-node: Nodo-Beta-02
X-Upstream-Status: 201
body: {'node': 'Nodo-Beta-02', 'db_used': 'primary', 'replication': 'synced'} id= f031817b-6b8c-4945-8d87-1e0be3556fb9

$ docker compose exec -T backend-node-1 python -c "urllib GET http://localhost:8000/api/orders"   (nodo caído, sin NGINX)
HTTP Error 503: Service Unavailable | {"detail":"Nodo-Alpha-01 en caída simulada (SIMULATE_CRASH)","node":"Nodo-Alpha-01","simulated_crash":true}

$ curl -s -X POST $BASE/api/chaos/toggle/Nodo-Alpha-01   (restaurar)
{"applied_by":"Nodo-Alpha-01","forwarded":true,"node":"Nodo-Alpha-01","simulate_crash":false,"simulate_db_primary_down":false,"updated_at":"2026-11-18T04:19:05.698483+00:00","forwarded_by":"Nodo-Beta-02"}
x-handled-by-node: Nodo-Beta-02
x-handled-by-node: Nodo-Beta-02
x-handled-by-node: Nodo-Alpha-01
x-handled-by-node: Nodo-Beta-02
```

**Análisis:** la orden de caída la recibió `Nodo-Beta-02` y la reenvió por REST a Alpha
(`forwarded_by`). La primera petición que le tocaba a Alpha recibió 503 y NGINX la reintentó en Beta
(`503, 200`): el cliente obtuvo 200. Tras restaurar (y vencer `fail_timeout` de 10 s) Alpha vuelve a la
rotación.

### 6.3 Caída simulada de la base primaria y reconciliación (F-05, F-06)

```text
# Comprobación previa: ¿el gateway alcanza ambos nodos recreados?
x-handled-by-node: Nodo-Alpha-01
x-handled-by-node: Nodo-Beta-02
x-handled-by-node: Nodo-Alpha-01
x-handled-by-node: Nodo-Beta-02

$ curl -s -X POST $BASE/api/chaos/db-primary/toggle
simulate_db_primary_down = True | propagated_to = [('Nodo-Alpha-01', True), ('Nodo-Beta-02', True)]

$ curl -s -i $BASE/api/orders   (x2, uno por nodo)
HTTP/1.1 200 OK x-db-used: replica_failover x-handled-by-node: Nodo-Beta-02 | body db_used = replica_failover | count = 5
HTTP/1.1 200 OK x-db-used: replica_failover x-handled-by-node: Nodo-Alpha-01 | body db_used = replica_failover | count = 5

$ curl -s -X POST $BASE/api/orders   (escritura con db-primary caída)
{'node': 'Nodo-Beta-02', 'db_used': 'replica_failover', 'replication': 'pending'} id = e233253a-a5e6-44fb-abaf-501dec17f851

$ curl -s $BASE/api/replication/status
{'in_sync': False, 'divergent_orders': None, 'needs_resync': True}
  primary: {'status': 'simulated_down', 'detail': 'caída simulada de db-primary (SIMULATE_DB_PRIMARY_DOWN)'}
  replica: {'status': 'up', 'rows': 6, 'active_orders': 6}

$ curl -s -X POST $BASE/api/chaos/db-primary/toggle   (restaurar)
simulate_db_primary_down = False
sync local = {'status': 'ok', 'copied_to_primary': 1, 'copied_to_replica': 0, 'rows_primary': 6, 'rows_replica': 6}
propagated_to = [('Nodo-Beta-02', True, None), ('Nodo-Alpha-01', True, 0)]

$ curl -s $BASE/api/replication/status
{'in_sync': True, 'divergent_orders': 0}
  primary: {'status': 'up', 'rows': 6, 'active_orders': 6}
  replica: {'status': 'up', 'rows': 6, 'active_orders': 6}
$ curl -s $BASE/api/orders/e233253a-a5e6-44fb-abaf-501dec17f851
db_used = primary | customer = QA Failover BD | handled_by_node = Nodo-Beta-02
```

La "comprobación previa" se hizo justo después de **recrear los contenedores de los nodos** (nuevas
IP) sin reiniciar el gateway: NGINX los alcanzó gracias a `resolve` (re-resolución DNS).

Logs de los nodos durante la prueba:

```text
backend-node-2-1  | … WARNING [Nodo-Beta-02] pedidos.chaos: CHAOS SIMULATE_DB_PRIMARY_DOWN=True en Nodo-Beta-02
backend-node-2-1  | … WARNING [Nodo-Beta-02] pedidos.db: FAILOVER lectura: db-primary no disponible (caída simulada de db-primary (SIMULATE_DB_PRIMARY_DOWN)) -> db-replica
backend-node-2-1  | … WARNING [Nodo-Beta-02] pedidos.db: FAILOVER escritura: db-primary no disponible (caída simulada de db-primary (SIMULATE_DB_PRIMARY_DOWN)) -> db-replica
backend-node-2-1  | … WARNING [Nodo-Beta-02] pedidos.db: WRITE order=e233253a-a5e6-44fb-abaf-501dec17f851 en db-replica (failover); copia a db-primary PENDIENTE
backend-node-1-1  | … WARNING [Nodo-Alpha-01] pedidos.chaos: CHAOS SIMULATE_DB_PRIMARY_DOWN=True en Nodo-Alpha-01
backend-node-1-1  | … WARNING [Nodo-Alpha-01] pedidos.db: FAILOVER lectura: db-primary no disponible (caída simulada de db-primary (SIMULATE_DB_PRIMARY_DOWN)) -> db-replica
backend-node-1-1  | … WARNING [Nodo-Alpha-01] pedidos.chaos: CHAOS SIMULATE_DB_PRIMARY_DOWN=False en Nodo-Alpha-01
backend-node-2-1  | … WARNING [Nodo-Beta-02] pedidos.chaos: CHAOS SIMULATE_DB_PRIMARY_DOWN=False en Nodo-Beta-02
backend-node-2-1  | … INFO    [Nodo-Beta-02] pedidos.db: SYNC (restauración de db-primary): 1 fila(s) réplica->primaria, 0 fila(s) primaria->réplica
```

### 6.4 Fallos reales: contenedores detenidos (F-07)

```text
## 7.1 Nodo apagado de verdad
$ docker compose stop backend-node-1
 Container act5-backend-node-1-1 Stopped
$ for i in 1..6: curl $BASE/api/orders -> código / nodo / intentos NGINX
HTTP/1.1 200 OK x-handled-by-node: Nodo-Beta-02 X-Upstream-Status: 504, 200
HTTP/1.1 200 OK x-handled-by-node: Nodo-Beta-02 X-Upstream-Status: 200
HTTP/1.1 200 OK x-handled-by-node: Nodo-Beta-02 X-Upstream-Status: 200
HTTP/1.1 200 OK x-handled-by-node: Nodo-Beta-02 X-Upstream-Status: 200
HTTP/1.1 200 OK x-handled-by-node: Nodo-Beta-02 X-Upstream-Status: 200
HTTP/1.1 200 OK x-handled-by-node: Nodo-Beta-02 X-Upstream-Status: 200
$ curl -s $BASE/api/chaos/status  (vista desde el nodo sobreviviente)
   Nodo-Beta-02 reachable = True
   Nodo-Alpha-01 reachable = False
$ docker compose start backend-node-1
 Container act5-backend-node-1-1 Started
x-handled-by-node: Nodo-Beta-02
x-handled-by-node: Nodo-Beta-02
x-handled-by-node: Nodo-Alpha-01
x-handled-by-node: Nodo-Beta-02

## 7.2 BD primaria apagada de verdad
$ docker compose stop db-primary
 Container act5-db-primary-1 Stopped
HTTP/1.1 200 OK x-db-used: replica_failover x-handled-by-node: Nodo-Alpha-01 | db_used = replica_failover | count = 6
HTTP/1.1 200 OK x-db-used: replica_failover x-handled-by-node: Nodo-Beta-02 | db_used = replica_failover | count = 6
$ curl -s -X POST $BASE/api/orders   (escritura con db-primary detenida)
  [HTTP 201]
{'node': 'Nodo-Alpha-01', 'db_used': 'replica_failover', 'replication': 'pending'}
$ curl -s $BASE/api/health  (databases)
  node = Nodo-Beta-02 | primary = {'status': 'down', 'detail': "failed to resolve host 'db-primary': [Errno -2] Name or service not known"} | replica = up | circuit_breaker = open
$ docker compose start db-primary
 Container act5-db-primary-1 Started
# esperando la reconciliación periódica (cada 5 s)...
{'in_sync': True, 'divergent_orders': 0} | primary rows = 7 | replica rows = 7
db_used = primary | count = 7
```

```text
backend-node-1-1  | … WARNING [Nodo-Alpha-01] pedidos.db: FAILOVER lectura: db-primary no disponible (failed to resolve host 'db-primary': [Errno -2] Name or service not known) -> db-replica
backend-node-1-1  | … WARNING [Nodo-Alpha-01] pedidos.db: FAILOVER escritura: db-primary no disponible (circuit breaker abierto: db-primary falló recientemente) -> db-replica
backend-node-1-1  | … WARNING [Nodo-Alpha-01] pedidos.db: WRITE order=9cb6045c-a359-4912-b030-f46e533629af en db-replica (failover); copia a db-primary PENDIENTE
backend-node-1-1  | … INFO    [Nodo-Alpha-01] pedidos.db: SYNC (periódica) no completada: db-primary no disponible (failed to resolve host 'db-primary': [Errno -2] Name or service not known)
backend-node-2-1  | … WARNING [Nodo-Beta-02] pedidos.db: FAILOVER lectura: db-primary no disponible (circuit breaker abierto: db-primary falló recientemente) -> db-replica
backend-node-1-1  | … INFO    [Nodo-Alpha-01] pedidos.db: SYNC (periódica): 1 fila(s) réplica->primaria, 0 fila(s) primaria->réplica
```

**Análisis:**

- Con el nodo **apagado de verdad**, la primera petición esperó el `proxy_connect_timeout` (2 s →
  `504`) y NGINX la reintentó en Beta; el resto fue directo a Beta. La vista del clúster marca a Alpha
  como `reachable = False`.
- Con la primaria **apagada de verdad**, el primer acceso falló por DNS y abrió el **circuit breaker**;
  las siguientes operaciones fueron directo a la réplica ("circuit breaker abierto"). Al arrancar la
  primaria, la **reconciliación periódica** copió la escritura pendiente sin intervención manual.

---

## 7. Pruebas de interfaz (navegador real)

Script `e2e-ui/ui-test.mjs`: abre el dashboard en Chrome headless, **hace clic en los botones reales**
y comprueba lo que se muestra. También genera las capturas de `docs/img/` usadas en el manual de
usuario. Ejecutado sobre un clúster recién creado (`docker compose down -v && docker compose up -d --build`):

```text
PASS  Carga los 3 pedidos de ejemplo de init.sql  -> María Fernanda López | Ferretería El Tornillo | Distribuidora Andina S.A.S.
PASS  Panel muestra ambos nodos operativos
PASS  Panel muestra db-primary y db-replica operativas
PASS  Formulario calcula el total estimado (20×4,75 + 5×12,90)  -> $ 159,50
PASS  Crear pedido: aparece en la tabla como Pendiente
PASS  Panel 'Última petición' muestra el nodo que atendió  -> Nodo-Alpha-01
PASS  Panel 'Última petición' muestra BD Primary
PASS  Avanzar estado: pendiente → en proceso
PASS  Avanzar estado: en proceso → enviado
PASS  Avanzar estado: enviado → entregado (fin del ciclo)
PASS  Editar pedido pendiente: total recalculado (40×6,20)  -> $ 248,00
PASS  Probar balanceo: 6 respuestas repartidas entre α y β  -> βαβαβα
PASS  Clic en 'Simular Caída de Nodo' (Alpha)
PASS  Con Alpha caído, las 6 peticiones las atiende Beta (sin errores)  -> ββββββ
PASS  El registro evidencia el reintento de NGINX (503 → 200)  -> 503 → 200
PASS  Crear pedido con un nodo caído funciona (lo atiende Beta)
PASS  Clic en 'Restaurar nodo'
PASS  Nodo Alpha vuelve a Operativo
PASS  Con db-primary caída, el pedido se guarda en 'Replica Failover'
PASS  Se muestra el aviso de failover de BD
PASS  Al restaurar, la reconciliación copia 1 fila réplica → primaria  -> db-primary restaurada · reconciliación: 1 fila(s) copiadas réplica → primaria×
PASS  Panel indica 'Réplica en sincronía'
PASS  Filtro por estado 'En proceso'  -> Supermercado La Economía | Ferretería El Tornillo
PASS  Búsqueda por cliente ('clínica')  -> Clínica San Rafael
PASS  Eliminar pedidos con confirmación en dos pasos
PASS  Sin errores de JavaScript en la consola

RESULTADO UI: 26/26 verificaciones OK
```

Evidencia visual del failover de nodo capturada durante esta prueba:

![Registro con reintento 503 → 200](img/05_registro_peticiones_failover.png)

---

## 8. Resultados

| Suite | Ejecutados | Pasaron | Fallaron | Omitidos | Duración |
|-------|-----------:|--------:|---------:|---------:|---------:|
| pytest — Python 3.11 + SQLite | 19 | 15 | 0 | 4 (e2e, por diseño) | 2.57 s |
| pytest — Python 3.11 + PostgreSQL 16 | 15 | 15 | 0 | 0 | 4.43 s |
| pytest e2e — clúster Docker | 4 | 4 | 0 | 0 | 6.92 s |
| Verificaciones de clúster (simuladas + reales) | 8 | 8 | 0 | 0 | — |
| Interfaz (Chrome headless) | 26 | 26 | 0 | 0 | — |

**Tasa de éxito: 100 %** en todas las suites. Los 5 casos exigidos (REQ-1 a REQ-5) pasan tanto con
SQLite como con PostgreSQL real.

---

## 9. Defectos encontrados y corregidos

Las pruebas detectaron estos problemas, que se corrigieron antes de la versión final:

| # | Detectado por | Problema | Corrección |
|---|---------------|----------|------------|
| 1 | Verificación F-06 | Al restaurar la primaria, la respuesta informaba `copied_to_primary: 0` aunque sí se habían reconciliado filas: el nodo propagaba primero la orden al otro nodo, éste reconciliaba, y el receptor ya no encontraba diferencias. Correcto en datos, pero engañoso en la demo. | El nodo receptor reconcilia **antes** de propagar e incluye el resultado de cada par (`propagated_to[].sync`). Se añadió la aserción correspondiente a U-15. |
| 2 | Prueba de interfaz (captura) | A 1440 px de ancho la columna **Acciones** de la tabla quedaba oculta tras un desplazamiento horizontal. | Tabla reorganizada de 8 a 6 columnas (fecha bajo el ID, ítems bajo el cliente) y acciones que se ajustan en dos líneas. |
| 3 | Prueba de interfaz | En capturas de página completa la barra superior fija aparecía en medio de la imagen. | La barra deja de ser fija en móvil; el script de capturas la desfija y vuelve arriba antes de capturar. |
| 4 | Prueba de interfaz (re-ejecución) | El script fallaba al repetirse con datos existentes (nombres de cliente repetidos). | Espera a que aumente el número de filas en lugar de buscar el nombre. |

Observación (no es un defecto): al enviar JSON con tildes desde `curl` en Git Bash de Windows, la
consola mandó bytes no UTF-8 y la API respondió **HTTP 400** *"There was an error parsing the body"*,
que es el comportamiento correcto. Desde el navegador y desde clientes UTF-8 (httpx) las tildes se
guardan bien (verificado: `'Bruno D\xedaz'`).

---

## 10. Conclusiones

1. **CRUD y máquina de estados**: funcionan según lo especificado y rechazan con 409/422 las
   operaciones inválidas, en SQLite y en PostgreSQL 16.
2. **Replicación**: toda escritura confirmada en la primaria aparece de inmediato en la réplica
   (`replication: "synced"`); si la réplica falla, la escritura no se pierde y queda pendiente.
3. **Caída de nodo**: el cliente **nunca** recibió un error. NGINX reintentó en el nodo sano
   (`503, 200` con caída simulada, `504, 200` con el contenedor detenido) y excluyó el nodo caído.
4. **Caída de la base primaria**: lecturas y escrituras continuaron desde la réplica con
   `db_used: "replica_failover"` y **ningún HTTP 500**; al volver la primaria, la reconciliación la
   igualó (manual, al restaurar, o automática en ≤ 5 s) y `in_sync` volvió a `true`.
5. **Interfaz**: refleja en vivo qué nodo y qué base atendieron cada operación y permite provocar y
   revertir los fallos con un clic; los 26 escenarios probados con un navegador real pasaron.

El sistema cumple los objetivos de **disponibilidad**, **replicación** y **recuperación** planteados.

---

## 11. Limitaciones y trabajo futuro

- La replicación es a nivel de aplicación con *last-write-wins*: depende de relojes sincronizados y,
  ante modificaciones concurrentes del mismo pedido en bases distintas durante una partición, conserva
  sólo la más reciente. Alternativas: *streaming replication* con promoción automática, relojes
  vectoriales o una cola de mensajes (patrón outbox).
- Las pruebas e2e y de interfaz se ejecutaron en una sola máquina (los "nodos" son contenedores); no se
  evaluaron latencias de red reales ni particiones parciales entre nodos.
- No se realizaron pruebas de carga/estrés (p. ej. con k6 o Locust) ni de seguridad (no hay
  autenticación; los endpoints de caos son públicos por diseño de la demo).
- El gateway es un punto único de fallo; en producción se usaría un balanceador redundante.
