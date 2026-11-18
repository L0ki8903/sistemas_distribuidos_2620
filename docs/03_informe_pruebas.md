# Informe de Pruebas — Sistema Distribuido de Gestión de Pedidos Tolerante a Fallos

> Versión 1.2 (autenticación, seguridad y monitoreo) · Ejecución: 18 de noviembre de 2026 · Todas las salidas de este documento son **literales**
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
6. **Protege el acceso**: sesión con JWT válida en cualquier nodo, roles `user`/`admin`, contraseñas con
   hash, bloqueo por intentos fallidos y autorización aplicada en el servidor, sin perder la tolerancia a fallos.
7. **Permite observarlo**: panel de monitoreo con métricas y eventos de cada nodo, del gateway y de las
   bases, que sigue funcionando aunque un nodo no responda.

Requisito mínimo del curso: 3 pruebas unitarias/integración + 1 de fallo. Requisito del proyecto:
5 pruebas en `backend/tests/test_orders.py` (crear, actualizar estado, eliminar, fallo de nodo 503,
failover de BD). **Se implementaron 36 pruebas pytest (15 de pedidos/fallos + 13 de seguridad + 8 de
monitoreo) + 6 e2e + 8 verificaciones manuales de clúster + 44 de interfaz.**

---

## 2. Entorno de pruebas

| Elemento | Versión / detalle |
|----------|-------------------|
| Sistema anfitrión | Windows 11 (x86_64) con Docker Desktop — Docker 29.9.2, Compose v5.6.1 |
| Python (suite oficial) | **3.11.18** en contenedor `python:3.11-slim` |
| Python (e2e desde el anfitrión) | 3.14.7 (entorno virtual `backend/.venv`) |
| Librerías | FastAPI 0.163.2 · Starlette 1.10.0 · SQLAlchemy 2.0.58 · Pydantic 2.14.3 · psycopg 3.3.9 · httpx 0.28.1 · Uvicorn 0.54.0 · PyJWT 2.15.1 · pytest 9.2.0 |
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
| Monitoreo | pytest + `TestClient` + `httpx.MockTransport` | Conteo por estado y latencia, exclusión del propio sondeo, 503 simulados y lecturas desde la réplica, eventos (con la contraseña oculta), vista agregada con un nodo inalcanzable, `stub_status` y series alineadas | `python -m pytest tests/test_monitoring.py` |
| Seguridad | pytest + `TestClient` | Registro/login, JWT ausente/falso/expirado/de otro secreto, roles, propiedad de pedidos, bloqueo por intentos, admin inicial, CLI, sesión con la primaria caída y entre nodos | `python -m pytest tests/test_auth.py` |
| Fallos reales | `curl` + `docker compose stop/start` | Comportamiento ante caídas **no simuladas** de un nodo y de la primaria | Pasos del comando `/verify-cluster` |
| Interfaz | Chrome headless + puppeteer-core | Login, roles, botones y formularios funcionan y la UI refleja nodo/BD/failover | `cd e2e-ui && node ui-test.mjs` |

Para las pruebas de fallo se usan dos técnicas: **inyección de fallos** (banderas `SIMULATE_CRASH` y
`SIMULATE_DB_PRIMARY_DOWN`) y **fallos reales** (URL de base inalcanzable en los tests; contenedores
detenidos en el clúster).

---

## 4. Catálogo de casos de prueba

### 4.1 `backend/tests/test_orders.py` (unitarias, integración y fallo)

Se ejecutan con sesión de administrador (fixture `client`); los permisos por rol están en §4.2; los de monitoreo, en §4.3.

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

### 4.2 `backend/tests/test_auth.py` (autenticación y seguridad)

| ID | Tipo | Test | Resultado esperado | Resultado obtenido |
|----|------|------|--------------------|--------------------|
| S-01 | Unitaria | `test_registro_y_login_devuelven_token_y_el_usuario_se_replica` | 201 con token y rol `user`; usuario normalizado en minúsculas; nunca se devuelve el hash; usuario idéntico en ambas bases con hash `pbkdf2_sha256$…` | ✅ PASSED |
| S-02 | Seguridad | `test_login_invalido_no_revela_si_el_usuario_existe` | Contraseña errónea y usuario inexistente → mismo 401 y mismo mensaje, con `WWW-Authenticate: Bearer` | ✅ PASSED |
| S-03 | Seguridad | `test_validaciones_de_registro_y_no_se_puede_auto_asignar_admin` | 422 con contraseña débil, usuario inválido o campo `role`; 409 con usuario repetido | ✅ PASSED |
| S-04 | Seguridad | `test_endpoints_protegidos_rechazan_tokens_ausentes_falsos_o_expirados` | 401 sin token, con token falso, firmado con otro secreto o expirado; `/api/health` sigue pública | ✅ PASSED |
| S-05 | Distribuida | `test_token_de_un_nodo_es_valido_en_el_otro_pero_no_con_otro_secreto` | Token emitido por Alpha aceptado por Beta (secreto compartido) y rechazado por un nodo con otro secreto | ✅ PASSED |
| S-06 | Seguridad | `test_usuario_desactivado_pierde_el_acceso_aunque_su_token_no_haya_expirado` | Tras desactivar la cuenta: 401 con el token vigente y 403 al iniciar sesión | ✅ PASSED |
| S-07 | Seguridad | `test_bloqueo_temporal_tras_varios_intentos_fallidos` | Tras 3 fallos, 429 con `Retry-After` incluso con la contraseña correcta; otros usuarios no se ven afectados | ✅ PASSED |
| S-08 | Autorización | `test_cliente_solo_ve_y_gestiona_sus_propios_pedidos` | El cliente sólo lista los suyos; pedidos ajenos → 404; no cambia estados (403); edita/cancela sólo en `pendiente` (409 si no); el admin ve todos con `owner_username` | ✅ PASSED |
| S-09 | Autorización | `test_cliente_no_accede_al_caos_ni_a_la_replicacion` | 6 endpoints de caos/replicación: 403 con rol `user`, 401 sin sesión, 200 con admin | ✅ PASSED |
| S-10 | Fallo + seguridad | `test_login_y_registro_funcionan_con_la_bd_primaria_caida` | Login y registro con `db_used = replica_failover`; al restaurar se reconcilian 1 usuario y 1 pedido (`tables.users` / `tables.orders`) e `in_sync` | ✅ PASSED |
| S-11 | Distribuida | `test_admin_inicial_se_crea_una_sola_vez_y_con_contrasena_generada` | Sin `ADMIN_PASSWORD` se genera y registra una contraseña válida; el segundo nodo no lo recrea; un solo admin con id determinista en ambas bases | ✅ PASSED |
| S-12 | Unitaria | `test_cli_crea_o_restablece_un_administrador` | `python -m app.cli create-admin` crea un admin, rechaza contraseñas débiles y restablece la contraseña | ✅ PASSED |
| S-13 | Distribuida | `test_comunicacion_entre_nodos_reenvia_la_autorizacion` | Un `user` no puede ordenar caos (403); el admin sí y el reenvío REST al otro nodo lleva su token | ✅ PASSED |

### 4.3 `backend/tests/test_monitoring.py` (monitoreo de nodos)

| ID | Tipo | Test | Resultado esperado | Resultado obtenido |
|----|------|------|--------------------|--------------------|
| M-01 | Autorización | `test_monitoreo_es_solo_para_administradores` | `/local` y `/cluster`: 401 sin sesión, 403 con rol `user`, 200 con admin | ✅ PASSED |
| M-02 | Unitaria | `test_metricas_cuentan_peticiones_por_estado_y_latencia` | 6 peticiones → 4 correctas y 2 de cliente (404, 422); p50 ≤ p95 ≤ máx; 30 cubos de 10 s en 300 s | ✅ PASSED |
| M-03 | Unitaria | `test_las_consultas_de_monitoreo_y_salud_no_inflan_las_metricas` | Consultar `/api/health` y el propio panel no cambia el contador de tráfico | ✅ PASSED |
| M-04 | Fallo | `test_caidas_simuladas_lecturas_desde_replica_y_eventos` | 1 `503` simulado contado; 3 lecturas servidas por la réplica; eventos `chaos`, `failover` y `lifecycle`; repetidos agrupados (×N) | ✅ PASSED |
| M-05 | Seguridad | `test_eventos_de_seguridad_y_contrasena_generada_oculta` | El evento del admin inicial muestra `[oculta]` en lugar de la contraseña; el login fallido aparece como evento `auth` | ✅ PASSED |
| M-06 | Distribuida | `test_vista_de_cluster_agrega_nodos_por_rest_y_marca_los_inalcanzables` | Alpha lee a Beta por REST; un tercer nodo caído aparece `reachable: false` con el motivo; el resumen suma sólo los alcanzables | ✅ PASSED |
| M-07 | Unitaria | `test_estado_del_gateway_desde_stub_status_de_nginx` | Se interpreta el texto de `stub_status`; un 502 del gateway se informa como no alcanzable; sin URL → `configured: false` | ✅ PASSED |
| M-08 | Unitaria | `test_serie_temporal_alineada_y_percentiles` | Cubos alineados a múltiplos de 10 s; hueco (`null`) sin tráfico; p50 = 30 y p95 = 100 con datos conocidos | ✅ PASSED |

### 4.4 `backend/tests/test_cluster_e2e.py` (end-to-end contra NGINX + Docker)

| ID | Test | Resultado esperado | Resultado obtenido |
|----|------|--------------------|--------------------|
| E-06 | `test_e2e_monitoreo_agrega_ambos_nodos_y_el_gateway` | Ambos nodos alcanzables, `nodes_up = 2`, el gateway reporta peticiones y su puerto interno 8081 **no** responde desde fuera | ✅ PASSED |
| E-05 | `test_e2e_sesion_valida_en_ambos_nodos_y_sin_reintentos_en_401` | Un mismo token es aceptado por ambos nodos a través de NGINX; sin sesión → 401 sin reintento (`X-Upstream-Status: 401`); cliente en caos → 403 | ✅ PASSED |
| E-01 | `test_e2e_round_robin_alterna_entre_los_dos_nodos` | En 6 peticiones aparecen ambos nodos, al menos 2 veces cada uno | ✅ PASSED |
| E-02 | `test_e2e_caida_de_nodo_es_transparente_para_el_cliente` | Con Alpha caído: 8/8 respuestas 200 desde Beta, `X-Upstream-Status` contiene 503; POST 201 | ✅ PASSED |
| E-03 | `test_e2e_failover_de_bd_primaria_y_reconciliacion` | Propagación a 2 nodos; `replica_failover`; `in_sync` falso durante la caída y verdadero tras restaurar; lectura posterior desde `primary` | ✅ PASSED |
| E-04 | `test_e2e_gateway_sirve_el_frontend_react` | `/` y rutas del SPA devuelven `index.html` (`try_files`) | ✅ PASSED |

### 4.5 Verificaciones de clúster con fallos simulados y reales (`/verify-cluster`)

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
collecting ... collected 42 items

tests/test_auth.py::test_registro_y_login_devuelven_token_y_el_usuario_se_replica PASSED [  2%]
tests/test_auth.py::test_login_invalido_no_revela_si_el_usuario_existe PASSED [  4%]
tests/test_auth.py::test_validaciones_de_registro_y_no_se_puede_auto_asignar_admin PASSED [  7%]
tests/test_auth.py::test_endpoints_protegidos_rechazan_tokens_ausentes_falsos_o_expirados PASSED [  9%]
tests/test_auth.py::test_token_de_un_nodo_es_valido_en_el_otro_pero_no_con_otro_secreto PASSED [ 11%]
tests/test_auth.py::test_usuario_desactivado_pierde_el_acceso_aunque_su_token_no_haya_expirado PASSED [ 14%]
tests/test_auth.py::test_bloqueo_temporal_tras_varios_intentos_fallidos PASSED [ 16%]
tests/test_auth.py::test_cliente_solo_ve_y_gestiona_sus_propios_pedidos PASSED [ 19%]
tests/test_auth.py::test_cliente_no_accede_al_caos_ni_a_la_replicacion PASSED [ 21%]
tests/test_auth.py::test_login_y_registro_funcionan_con_la_bd_primaria_caida PASSED [ 23%]
tests/test_auth.py::test_admin_inicial_se_crea_una_sola_vez_y_con_contrasena_generada PASSED [ 26%]
tests/test_auth.py::test_cli_crea_o_restablece_un_administrador PASSED   [ 28%]
tests/test_auth.py::test_comunicacion_entre_nodos_reenvia_la_autorizacion PASSED [ 30%]
tests/test_cluster_e2e.py::test_e2e_sesion_valida_en_ambos_nodos_y_sin_reintentos_en_401 SKIPPED [ 33%]
tests/test_cluster_e2e.py::test_e2e_round_robin_alterna_entre_los_dos_nodos SKIPPED [ 35%]
tests/test_cluster_e2e.py::test_e2e_caida_de_nodo_es_transparente_para_el_cliente SKIPPED [ 38%]
tests/test_cluster_e2e.py::test_e2e_failover_de_bd_primaria_y_reconciliacion SKIPPED [ 40%]
tests/test_cluster_e2e.py::test_e2e_gateway_sirve_el_frontend_react SKIPPED [ 42%]
tests/test_cluster_e2e.py::test_e2e_monitoreo_agrega_ambos_nodos_y_el_gateway SKIPPED [ 45%]
tests/test_monitoring.py::test_monitoreo_es_solo_para_administradores PASSED [ 47%]
tests/test_monitoring.py::test_metricas_cuentan_peticiones_por_estado_y_latencia PASSED [ 50%]
tests/test_monitoring.py::test_las_consultas_de_monitoreo_y_salud_no_inflan_las_metricas PASSED [ 52%]
tests/test_monitoring.py::test_caidas_simuladas_lecturas_desde_replica_y_eventos PASSED [ 54%]
tests/test_monitoring.py::test_eventos_de_seguridad_y_contrasena_generada_oculta PASSED [ 57%]
tests/test_monitoring.py::test_vista_de_cluster_agrega_nodos_por_rest_y_marca_los_inalcanzables PASSED [ 59%]
tests/test_monitoring.py::test_estado_del_gateway_desde_stub_status_de_nginx PASSED [ 61%]
tests/test_monitoring.py::test_serie_temporal_alineada_y_percentiles PASSED [ 64%]
tests/test_orders.py::test_crear_pedido_se_escribe_en_primaria_y_se_replica PASSED [ 66%]
tests/test_orders.py::test_actualizar_estado_recorre_todo_el_ciclo PASSED [ 69%]
tests/test_orders.py::test_actualizar_estado_rechaza_transiciones_invalidas PASSED [ 71%]
tests/test_orders.py::test_eliminar_pedido_con_borrado_logico_replicado PASSED [ 73%]
tests/test_orders.py::test_fallo_de_nodo_devuelve_503_en_orders PASSED   [ 76%]
tests/test_orders.py::test_failover_bd_primaria_a_replica_y_reconciliacion PASSED [ 78%]
tests/test_orders.py::test_failover_cuando_la_primaria_es_inalcanzable_de_verdad PASSED [ 80%]
tests/test_orders.py::test_si_caen_ambas_bd_responde_503_y_nunca_500 PASSED [ 83%]
tests/test_orders.py::test_replica_caida_no_bloquea_escrituras_en_primaria PASSED [ 85%]
tests/test_orders.py::test_listar_y_filtrar_por_estado_y_cliente PASSED  [ 88%]
tests/test_orders.py::test_editar_pedido_recalcula_total_y_solo_si_esta_pendiente PASSED [ 90%]
tests/test_orders.py::test_creacion_idempotente_con_id_generado_por_el_cliente PASSED [ 92%]
tests/test_orders.py::test_validaciones_de_entrada PASSED                [ 95%]
tests/test_orders.py::test_health_y_cabecera_x_handled_by_node PASSED    [ 97%]
tests/test_orders.py::test_comunicacion_entre_nodos_reenvia_caos_y_propaga_caida_de_bd PASSED [100%]

============================= slowest 5 durations ==============================
0.41s call     tests/test_orders.py::test_comunicacion_entre_nodos_reenvia_caos_y_propaga_caida_de_bd
0.32s call     tests/test_monitoring.py::test_vista_de_cluster_agrega_nodos_por_rest_y_marca_los_inalcanzables
0.22s call     tests/test_auth.py::test_cliente_solo_ve_y_gestiona_sus_propios_pedidos
0.20s call     tests/test_auth.py::test_comunicacion_entre_nodos_reenvia_la_autorizacion
0.20s call     tests/test_auth.py::test_token_de_un_nodo_es_valido_en_el_otro_pero_no_con_otro_secreto
=========================== short test summary info ============================
SKIPPED [1] tests/test_cluster_e2e.py:50: Define CLUSTER_BASE_URL (p. ej. http://localhost:8080) y CLUSTER_ADMIN_PASSWORD con el clúster arriba
SKIPPED [1] tests/test_cluster_e2e.py:73: Define CLUSTER_BASE_URL (p. ej. http://localhost:8080) y CLUSTER_ADMIN_PASSWORD con el clúster arriba
SKIPPED [1] tests/test_cluster_e2e.py:84: Define CLUSTER_BASE_URL (p. ej. http://localhost:8080) y CLUSTER_ADMIN_PASSWORD con el clúster arriba
SKIPPED [1] tests/test_cluster_e2e.py:102: Define CLUSTER_BASE_URL (p. ej. http://localhost:8080) y CLUSTER_ADMIN_PASSWORD con el clúster arriba
SKIPPED [1] tests/test_cluster_e2e.py:129: Define CLUSTER_BASE_URL (p. ej. http://localhost:8080) y CLUSTER_ADMIN_PASSWORD con el clúster arriba
SKIPPED [1] tests/test_cluster_e2e.py:140: Define CLUSTER_BASE_URL (p. ej. http://localhost:8080) y CLUSTER_ADMIN_PASSWORD con el clúster arriba
======================== 36 passed, 6 skipped in 6.45s =========================
```

Los 6 `SKIPPED` son los tests e2e, que por diseño sólo corren con el clúster levantado y la contraseña
del administrador (§5.3).

### 5.2 Misma suite contra PostgreSQL 16 real

Comando (contenedor en la red del clúster, base aislada `orders_test` para no tocar los datos reales):

```bash
docker run --rm --network act5_default \
  -e TEST_PRIMARY_DB_URL=postgresql://admin:secretpassword@db-primary:5432/orders_test \
  -e TEST_REPLICA_DB_URL=postgresql://admin:secretpassword@db-replica:5432/orders_test \
  -v "$PWD":/src:ro python:3.11-slim sh -c \
  "cp -r /src /app && cd /app && pip install -q -r requirements-dev.txt && python -m pytest -v -p no:cacheprovider tests/test_orders.py tests/test_auth.py tests/test_monitoring.py"
```

```text
============================= test session starts ==============================
platform linux -- Python 3.11.18, pytest-9.2.0, pluggy-1.7.0 -- /usr/local/bin/python
rootdir: /app
configfile: pytest.ini
plugins: anyio-4.16.0
collecting ... collected 36 items

tests/test_orders.py::test_crear_pedido_se_escribe_en_primaria_y_se_replica PASSED [  2%]
tests/test_orders.py::test_actualizar_estado_recorre_todo_el_ciclo PASSED [  5%]
tests/test_orders.py::test_actualizar_estado_rechaza_transiciones_invalidas PASSED [  8%]
tests/test_orders.py::test_eliminar_pedido_con_borrado_logico_replicado PASSED [ 11%]
tests/test_orders.py::test_fallo_de_nodo_devuelve_503_en_orders PASSED   [ 13%]
tests/test_orders.py::test_failover_bd_primaria_a_replica_y_reconciliacion PASSED [ 16%]
tests/test_orders.py::test_failover_cuando_la_primaria_es_inalcanzable_de_verdad PASSED [ 19%]
tests/test_orders.py::test_si_caen_ambas_bd_responde_503_y_nunca_500 PASSED [ 22%]
tests/test_orders.py::test_replica_caida_no_bloquea_escrituras_en_primaria PASSED [ 25%]
tests/test_orders.py::test_listar_y_filtrar_por_estado_y_cliente PASSED  [ 27%]
tests/test_orders.py::test_editar_pedido_recalcula_total_y_solo_si_esta_pendiente PASSED [ 30%]
tests/test_orders.py::test_creacion_idempotente_con_id_generado_por_el_cliente PASSED [ 33%]
tests/test_orders.py::test_validaciones_de_entrada PASSED                [ 36%]
tests/test_orders.py::test_health_y_cabecera_x_handled_by_node PASSED    [ 38%]
tests/test_orders.py::test_comunicacion_entre_nodos_reenvia_caos_y_propaga_caida_de_bd PASSED [ 41%]
tests/test_auth.py::test_registro_y_login_devuelven_token_y_el_usuario_se_replica PASSED [ 44%]
tests/test_auth.py::test_login_invalido_no_revela_si_el_usuario_existe PASSED [ 47%]
tests/test_auth.py::test_validaciones_de_registro_y_no_se_puede_auto_asignar_admin PASSED [ 50%]
tests/test_auth.py::test_endpoints_protegidos_rechazan_tokens_ausentes_falsos_o_expirados PASSED [ 52%]
tests/test_auth.py::test_token_de_un_nodo_es_valido_en_el_otro_pero_no_con_otro_secreto PASSED [ 55%]
tests/test_auth.py::test_usuario_desactivado_pierde_el_acceso_aunque_su_token_no_haya_expirado PASSED [ 58%]
tests/test_auth.py::test_bloqueo_temporal_tras_varios_intentos_fallidos PASSED [ 61%]
tests/test_auth.py::test_cliente_solo_ve_y_gestiona_sus_propios_pedidos PASSED [ 63%]
tests/test_auth.py::test_cliente_no_accede_al_caos_ni_a_la_replicacion PASSED [ 66%]
tests/test_auth.py::test_login_y_registro_funcionan_con_la_bd_primaria_caida PASSED [ 69%]
tests/test_auth.py::test_admin_inicial_se_crea_una_sola_vez_y_con_contrasena_generada PASSED [ 72%]
tests/test_auth.py::test_cli_crea_o_restablece_un_administrador PASSED   [ 75%]
tests/test_auth.py::test_comunicacion_entre_nodos_reenvia_la_autorizacion PASSED [ 77%]
tests/test_monitoring.py::test_monitoreo_es_solo_para_administradores PASSED [ 80%]
tests/test_monitoring.py::test_metricas_cuentan_peticiones_por_estado_y_latencia PASSED [ 83%]
tests/test_monitoring.py::test_las_consultas_de_monitoreo_y_salud_no_inflan_las_metricas PASSED [ 86%]
tests/test_monitoring.py::test_caidas_simuladas_lecturas_desde_replica_y_eventos PASSED [ 88%]
tests/test_monitoring.py::test_eventos_de_seguridad_y_contrasena_generada_oculta PASSED [ 91%]
tests/test_monitoring.py::test_vista_de_cluster_agrega_nodos_por_rest_y_marca_los_inalcanzables PASSED [ 94%]
tests/test_monitoring.py::test_estado_del_gateway_desde_stub_status_de_nginx PASSED [ 97%]
tests/test_monitoring.py::test_serie_temporal_alineada_y_percentiles PASSED [100%]

============================= 36 passed in 15.46s ==============================
```

### 5.3 Pruebas end-to-end contra el clúster Docker

Comando (desde `backend/`, con `docker compose up -d` activo):

```bash
CLUSTER_BASE_URL=http://localhost:8080 CLUSTER_ADMIN_PASSWORD=<contraseña del admin> \
  python -m pytest -v -p no:cacheprovider tests/test_cluster_e2e.py
```

```text
============================= test session starts =============================
platform win32 -- Python 3.14.7, pytest-9.2.0, pluggy-1.7.0 -- C:\Users\…\backend\.venv\Scripts\python.exe
rootdir: C:\Users\…\backend
configfile: pytest.ini
plugins: anyio-4.16.0
collecting ... collected 6 items

tests/test_cluster_e2e.py::test_e2e_sesion_valida_en_ambos_nodos_y_sin_reintentos_en_401 PASSED [ 16%]
tests/test_cluster_e2e.py::test_e2e_round_robin_alterna_entre_los_dos_nodos PASSED [ 33%]
tests/test_cluster_e2e.py::test_e2e_caida_de_nodo_es_transparente_para_el_cliente PASSED [ 50%]
tests/test_cluster_e2e.py::test_e2e_failover_de_bd_primaria_y_reconciliacion PASSED [ 66%]
tests/test_cluster_e2e.py::test_e2e_gateway_sirve_el_frontend_react PASSED [ 83%]
tests/test_cluster_e2e.py::test_e2e_monitoreo_agrega_ambos_nodos_y_el_gateway PASSED [100%]

============================= 6 passed in 15.42s ==============================
```

---

## 6. Pruebas de fallo sobre el clúster Docker

Salidas reales de los pasos de `/verify-cluster` (`BASE=http://localhost:8080`).

> Las secciones 6.1–6.4 se capturaron con la versión 1.0, antes de añadir la autenticación. Con la
> versión 1.1 los mismos comandos necesitan `-H "Authorization: Bearer $TOKEN"` (token de admin) y el
> comportamiento es idéntico, como confirman las pruebas e2e de §5.3, que ya se ejecutan con sesión.

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

### 6.5 Actualización de una instalación existente (migración automática, v1.1)

Se actualizó el clúster local, cuyos volúmenes se habían creado con el esquema 1.0 (sin tabla `users`
ni columna `orders.owner_id`), **sin borrar los datos** (`docker compose up -d --build`):

```text
--- antes:
orders
0
--- logs de migración/arranque:
backend-node-2-1  | … INFO    [Nodo-Beta-02] pedidos.db: SCHEMA migrado en db-primary: orders.owner_id
backend-node-2-1  | … INFO    [Nodo-Beta-02] pedidos.db: SCHEMA migrado en db-replica: orders.owner_id
backend-node-2-1  | … INFO    [Nodo-Beta-02] pedidos.auth: ADMIN inicial 'admin' creado con la contraseña de ADMIN_PASSWORD
backend-node-2-1  | … INFO    [Nodo-Beta-02] pedidos: Nodo Nodo-Beta-02 listo (peers=['Nodo-Alpha-01'], sync cada 5.0s)
backend-node-1-1  | … INFO    [Nodo-Alpha-01] pedidos: Nodo Nodo-Alpha-01 listo (peers=['Nodo-Beta-02'], sync cada 5.0s)
--- después:
db-primary: orders,users | owner_id=1 | usuarios=1 | pedidos=3
db-replica: orders,users | owner_id=1 | usuarios=1 | pedidos=3
```

**Análisis:** los dos nodos arrancaron a la vez; el candado `pg_advisory_xact_lock` hizo que sólo uno
(Beta) aplicara la migración en ambas bases y el otro la encontrara hecha. El administrador inicial se
creó una sola vez y los 3 pedidos existentes se conservaron. Es el mismo proceso que se ejecuta al
redesplegar en Coolify sobre volúmenes existentes.

### 6.6 Cabeceras de seguridad del gateway (v1.1)

```text
$ curl -s -D - -o /dev/null http://localhost:8080/
Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'
X-Content-Type-Options: nosniff
X-Frame-Options: DENY
Referrer-Policy: strict-origin-when-cross-origin
```

`X-Upstream-Addr` (IPs internas de Docker) dejó de exponerse; `X-Upstream-Status` se mantiene como
evidencia del failover.

### 6.7 Monitoreo agregado a través del gateway (v1.2)

Consulta real a `GET /api/monitoring/cluster` (con sesión de admin) tras 8 peticiones, y prueba de que
el puerto interno del `stub_status` no está publicado:

```text
8081 desde el host: 000
no accesible (correcto)
summary: {'nodes_total': 2, 'nodes_up': 2, 'requests_per_minute': 9, 'window_requests': 9, 'window_server_errors': 0, 'error_rate': 0.0, 'latency_p95_worst_ms': 307.74, 'db_failovers': 0, 'window_replica_served': 0, 'in_sync': True}
gateway: {'configured': True, 'reachable': True, 'active': 2, 'accepts': 3, 'handled': 3, 'requests': 12, 'reading': 0, 'writing': 2, 'waiting': 0}
  Nodo-Alpha-01 reachable True | window_requests 4 | p95 282.2 | buckets 30
  Nodo-Beta-02 reachable True | window_requests 5 | p95 307.74 | buckets 30
```

**Análisis:** Round-Robin repartió 4/5 peticiones; cada nodo midió las suyas y el nodo que respondió las
combinó por REST. La p95 de unos 300 ms corresponde al inicio de sesión (PBKDF2 con 600 000 iteraciones),
que es lento a propósito.

---

## 7. Pruebas de interfaz (navegador real)

Script `e2e-ui/ui-test.mjs`: abre el dashboard en Chrome headless, **inicia sesión con el formulario
real**, hace clic en los botones y comprueba lo que se muestra; después de las caídas abre la pestaña
**Monitoreo** (tarjetas, indicadores, gráficos, tooltip, eventos y vista en tabla); al final cierra
sesión, **registra un cliente** y verifica que sólo vea lo suyo. También genera las capturas de
`docs/img/` usadas en el manual de usuario. Ejecutado sobre un clúster recién creado (`docker compose down -v && docker compose up -d --build`):

```text
PASS  Sin sesión se muestra la pantalla de inicio de sesión
PASS  Login con contraseña incorrecta muestra error genérico
PASS  Login de administrador abre el panel completo
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
PASS  Monitoreo: tarjeta por nodo, ambos operativos  -> 2 tarjetas
PASS  Monitoreo: indicadores del clúster (2 / 2 nodos)  -> Nodos operativos2 / 2✓ todos atienden tráfico
PASS  Monitoreo: gráficos con una línea por nodo  -> 4 trazos
PASS  Monitoreo: el tooltip de la cruz de lectura muestra ambos nodos
PASS  Monitoreo: eventos de caos y de failover de BD  -> 17 eventos
PASS  Monitoreo: el gateway reporta conexiones
PASS  Monitoreo: cada gráfico tiene vista de tabla  -> 30 filas
PASS  Filtro por estado 'En proceso'  -> Supermercado La Economía | Ferretería El Tornillo
PASS  Búsqueda por cliente ('clínica')  -> Clínica San Rafael
PASS  Eliminar pedidos con confirmación en dos pasos
PASS  Cerrar sesión vuelve a la pantalla de inicio
PASS  Registro de cliente inicia sesión con rol Cliente
PASS  El cliente no ve los pedidos de otros
PASS  El cliente no ve controles de caos ni de bases de datos
PASS  El formulario propone el nombre del cliente
PASS  El cliente crea un pedido y sólo ve el suyo  -> Camila Rojas
PASS  El cliente no puede avanzar estados (sin botón →)
PASS  El cliente puede cancelar su pedido pendiente
PASS  Sin errores de JavaScript en la consola

RESULTADO UI: 44/44 verificaciones OK
```

Evidencia visual del failover de nodo capturada durante esta prueba:

![Registro con reintento 503 → 200](img/05_registro_peticiones_failover.png)

Panel de monitoreo durante la misma ejecución (después de las caídas de nodo y de BD):

![Panel de monitoreo](img/11_monitoreo.png)

---

## 8. Resultados

| Suite | Ejecutados | Pasaron | Fallaron | Omitidos | Duración |
|-------|-----------:|--------:|---------:|---------:|---------:|
| pytest — Python 3.11 + SQLite | 42 | 36 | 0 | 6 (e2e, por diseño) | 6.45 s |
| pytest — Python 3.11 + PostgreSQL 16 | 36 | 36 | 0 | 0 | 15.46 s |
| pytest e2e — clúster Docker | 6 | 6 | 0 | 0 | 15.42 s |
| Verificaciones de clúster (simuladas + reales) | 8 | 8 | 0 | 0 | — |
| Actualización con migración (v1.0 → v1.1) | 1 | 1 | 0 | 0 | — |
| Interfaz (Chrome headless) | 44 | 44 | 0 | 0 | — |

**Tasa de éxito: 100 %** en todas las suites. Los 5 casos exigidos (REQ-1 a REQ-5) pasan tanto con
SQLite como con PostgreSQL real, igual que los 13 de seguridad y los 8 de monitoreo.

---

## 9. Defectos encontrados y corregidos

Las pruebas detectaron estos problemas, que se corrigieron antes de la versión final:

| # | Detectado por | Problema | Corrección |
|---|---------------|----------|------------|
| 1 | Verificación F-06 | Al restaurar la primaria, la respuesta informaba `copied_to_primary: 0` aunque sí se habían reconciliado filas: el nodo propagaba primero la orden al otro nodo, éste reconciliaba, y el receptor ya no encontraba diferencias. Correcto en datos, pero engañoso en la demo. | El nodo receptor reconcilia **antes** de propagar e incluye el resultado de cada par (`propagated_to[].sync`). Se añadió la aserción correspondiente a U-15. |
| 2 | Prueba de interfaz (captura) | A 1440 px de ancho la columna **Acciones** de la tabla quedaba oculta tras un desplazamiento horizontal. | Tabla reorganizada de 8 a 6 columnas (fecha bajo el ID, ítems bajo el cliente) y acciones que se ajustan en dos líneas. |
| 3 | Prueba de interfaz | En capturas de página completa la barra superior fija aparecía en medio de la imagen. | La barra deja de ser fija en móvil; el script de capturas la desfija y vuelve arriba antes de capturar. |
| 4 | Prueba de interfaz (re-ejecución) | El script fallaba al repetirse con datos existentes (nombres de cliente repetidos). | Espera a que aumente el número de filas en lugar de buscar el nombre. |
| 5 | Prueba de interfaz (v1.1) | Con la nueva Content-Security-Policy, el navegador bloqueó el `<style>` en línea que inyectaba el script de capturas (*"Applying inline style violates the following Content Security Policy directive"*). Confirma que la CSP se aplica; la aplicación en sí no generó ninguna violación. | El script ajusta la página mediante CSSOM (`element.style`), que la CSP permite. |
| 7 | Validador de paleta (v1.2) | Los colores de los nodos (azul `#2563eb` / violeta `#7c3aed`) tenían ΔE **0,4** en deuteranopía (indistinguibles para personas con daltonismo) y 12,4 en visión normal (bajo el umbral de 15). En los chips no era grave porque llevan el nombre escrito, pero en un gráfico de líneas sí. | Beta pasó a naranja (`#eb6834`, texto `#c2410c`) en toda la aplicación: ΔE 29,9 en deuteranopía y 38,5 en visión normal (todas las comprobaciones en PASS). |
| 8 | Revisión de capturas (v1.2) | El último error de `db-primary` seguía en rojo en la tarjeta del nodo aunque la primaria ya estaba restaurada. | El error se muestra con su hora; en rojo sólo mientras el problema sigue activo (caída simulada o circuit breaker abierto), si no, en gris como historial. |
| 9 | **CI de GitHub Actions** (v1.2) | La prueba de interfaz falló en el runner (43/44): tras crear un pedido, la interfaz lanza una recarga silenciosa de la lista; si se pulsa "→ En proceso" enseguida, el `PATCH` podía responder **antes** que esa recarga, que había leído la BD antes del cambio, y al llegar tarde **devolvía el pedido a "Pendiente" en pantalla** (el servidor estaba bien). En local no se daba por ser más rápido. Se reprodujo de forma determinista retrasando 1,5 s sólo la respuesta del `GET` de la lista con el protocolo de depuración de Chrome. | La interfaz versiona la lista: una recarga iniciada antes de un cambio local, o superada por otra más nueva, se descarta. Se añadió esa misma reproducción como **prueba de regresión permanente** ("Una recarga lenta de la lista no revierte un cambio de estado"), las esperas fijas se cambiaron por esperas al estado esperado y se validó 45/45 en Windows, en Linux/Chromium y con la CPU 4 veces más lenta. |
| 10 | Prueba de interfaz en Linux (v1.2) | Accediendo por `http://gateway` (HTTP sin TLS y fuera de `localhost`) Chrome registra que **ignora `Cross-Origin-Opener-Policy`**. No es un error de la aplicación: la cabecera se aplica con HTTPS (Coolify + Traefik) y en `localhost`. | Se mantiene la cabecera; la verificación de consola tolera sólo ese aviso del navegador. |
| 6 | Prueba de interfaz (v1.1) | El 401 del login fallido que la prueba provoca a propósito aparece en la consola del navegador como recurso fallido y la verificación "sin errores de consola" lo contaba. | Se tolera exactamente ese 401 esperado; cualquier otro error sigue haciendo fallar la prueba. |

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

6. **Seguridad**: las rutas exigen sesión; el token emitido por un nodo es válido en el otro, por lo
   que el balanceo y el failover no cierran la sesión; el login y el registro también funcionan con la
   primaria caída, y los usuarios se replican y reconcilian igual que los pedidos. Los permisos se
   aplican en el servidor (un cliente no puede ver pedidos ajenos, cambiar estados ni usar el caos).

7. **Monitoreo**: cada nodo mide su tráfico, latencia y eventos sin depender de la base de datos; la
   vista agregada combina ambos nodos por REST, informa un nodo caído en lugar de fallar y muestra en
   vivo los failovers y caídas provocados en la demo.

El sistema cumple los objetivos de **disponibilidad**, **replicación**, **recuperación**, **control de acceso** y **observabilidad** planteados.

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
- El límite de intentos de login vive en la memoria de cada nodo: con 2 nodos un atacante dispone del
  doble de intentos. Un límite global requeriría estado compartido (p. ej. Redis o `limit_req` en NGINX).
- Las métricas de monitoreo están en memoria (se pierden al reiniciar un nodo y cubren 15 min) y no hay
  alertas; para producción se exportarían a Prometheus/Grafana.
- El token se guarda en `localStorage`: es simple y evita CSRF, pero un XSS podría leerlo (mitigado con la
  CSP estricta). La alternativa es una cookie `HttpOnly` con protección CSRF.
