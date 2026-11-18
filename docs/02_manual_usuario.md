# Manual de Usuario — Sistema Distribuido de Gestión de Pedidos

> Guía para instalar, ejecutar y usar el panel logístico, y para **demostrar en vivo** la tolerancia a
> fallos. No requiere conocimientos de programación.

## Contenido

1. [¿Qué es el sistema?](#1-qué-es-el-sistema)
2. [Requisitos](#2-requisitos)
3. [Instalación y ejecución](#3-instalación-y-ejecución)
4. [Recorrido por la interfaz](#4-recorrido-por-la-interfaz)
5. [Gestión de pedidos](#5-gestión-de-pedidos)
6. [Cómo demostrar la tolerancia a fallos](#6-cómo-demostrar-la-tolerancia-a-fallos)
7. [Uso desde el celular](#7-uso-desde-el-celular)
8. [Preguntas frecuentes y solución de problemas](#8-preguntas-frecuentes-y-solución-de-problemas)
9. [Glosario](#9-glosario)

---

## 1. ¿Qué es el sistema?

Es una aplicación web para **registrar y dar seguimiento a pedidos** (crear, consultar, cambiar de
estado, editar y eliminar). Por dentro funciona como un sistema distribuido: hay **dos servidores**
(nodos) que atienden las peticiones y **dos bases de datos** con la misma información. Si uno de ellos
falla, el otro toma su lugar y el usuario puede seguir trabajando.

El panel muestra en todo momento **qué servidor** y **qué base de datos** atendió cada operación, y
tiene botones para **simular caídas** y comprobar que el sistema sigue funcionando.

---

## 2. Requisitos

| Requisito | Detalle |
|-----------|---------|
| Docker Desktop (Windows/macOS) o Docker Engine + Compose (Linux) | Versión con `docker compose` (v2 o superior) |
| Navegador moderno | Chrome, Edge, Firefox o Safari |
| Puerto libre | `8080` en el equipo (se puede cambiar, ver §8) |
| Memoria | ~1 GB libre para los 5 contenedores |

Opcional, sólo para desarrollo: Python 3.11 y Node.js 20+.

---

## 3. Instalación y ejecución

1. **Abrir Docker Desktop** y esperar a que indique que está en ejecución.
2. **Obtener el proyecto** (clonar el repositorio o descomprimir la carpeta entregada).
3. Abrir una terminal **en la carpeta del proyecto** (donde está `docker-compose.yml`) y ejecutar:

   ```bash
   docker compose up -d --build
   ```

   La primera vez tarda unos minutos (descarga imágenes y compila el frontend).

4. Comprobar que los 5 servicios estén en estado `healthy`:

   ```bash
   docker compose ps
   ```

   ```text
   SERVICE          STATUS                    PORTS
   backend-node-1   Up 8 seconds (healthy)    8000/tcp
   backend-node-2   Up 7 seconds (healthy)    8000/tcp
   db-primary       Up 14 seconds (healthy)   5432/tcp
   db-replica       Up 14 seconds (healthy)   5432/tcp
   gateway          Up 7 seconds (healthy)    0.0.0.0:8080->80/tcp
   ```

5. Abrir **<http://localhost:8080>** en el navegador.

| Dirección | Qué es |
|-----------|--------|
| <http://localhost:8080> | Panel logístico (dashboard) |
| <http://localhost:8080/api/docs> | Documentación interactiva de la API (Swagger) |
| <http://localhost:8080/api/health> | Estado del nodo que responde |

**Detener el sistema:** `docker compose down` (los datos se conservan).
**Detener y borrar todos los datos** (vuelve a los 3 pedidos de ejemplo): `docker compose down -v`.

---

## 4. Recorrido por la interfaz

![Vista general del panel](img/01_dashboard.png)

La pantalla tiene cuatro zonas:

| Zona | Contenido |
|------|-----------|
| **Barra superior** | Nombre del sistema, indicador **"Gateway NGINX conectado"** (verde) o **"sin backends"** (rojo) y enlace a la documentación de la API |
| **Estado del clúster** | Última petición, estado de los nodos y de las bases de datos, botones de simulación de fallos |
| **Pedidos** | Formulario "Nuevo pedido" (izquierda) y tabla de pedidos con filtros (derecha) |
| **Registro de peticiones** | Historial de las últimas peticiones con el nodo y la base que las atendió |

### 4.1 Panel "Estado del clúster"

![Panel de estado del clúster en condiciones normales](img/03_estado_cluster_normal.png)

- **Última petición:** muestra el **nodo que respondió** (`Nodo-Alpha-01` en azul o `Nodo-Beta-02` en
  violeta), la **base de datos usada** (`Primary` en verde o `Replica Failover` en ámbar), la operación,
  el código HTTP y el tiempo de respuesta. Si hubo un fallo y el sistema lo resolvió, aparece un aviso
  explicativo.
- **Nodos backend:** una tarjeta por servidor con su estado (`Operativo`, `Caída simulada (503)` o
  `Inalcanzable`), cuántas peticiones ha atendido en esta sesión y el botón **Simular Caída de Nodo**.
  El botón **Probar balanceo (6 peticiones)** envía seis consultas seguidas y muestra quién respondió
  cada una (α = Alpha, β = Beta).
- **Bases de datos:** tarjetas de `db-primary` y `db-replica` con su estado, latencia y número de filas,
  el botón **Simular Caída de BD Primaria**, el botón **Sincronizar ahora** y una franja que indica si la
  réplica está **en sincronía** con la primaria.

El panel se actualiza solo cada 3 segundos.

### 4.2 Registro de peticiones

![Registro de peticiones con un reintento de NGINX](img/05_registro_peticiones_failover.png)

Cada fila es una petición hecha desde la interfaz: hora, operación, código HTTP, nodo, base de datos,
**intentos NGINX** y duración. Si aparece **`503 → 200`** en rojo significa que el primer servidor falló
y el gateway reintentó automáticamente en el otro: **el usuario no vio ningún error**.

---

## 5. Gestión de pedidos

### 5.1 Crear un pedido

![Formulario de nuevo pedido](img/02_formulario_nuevo_pedido.png)

1. Escribir el **nombre del cliente** (mínimo 2 caracteres).
2. Completar el primer ítem: **producto**, **cantidad** (entero ≥ 1) y **precio unitario** (≥ 0).
3. Para más productos, pulsar **+ Agregar ítem**; para quitar uno, pulsar **×**.
4. Revisar el **Total estimado** (el total definitivo lo calcula el servidor).
5. Pulsar **Crear pedido**. El pedido aparece al inicio de la tabla en estado **Pendiente** y una
   notificación indica qué nodo y qué base lo guardaron.

Si falta algún dato, el formulario muestra el error en rojo y no envía nada.

### 5.2 Ver, filtrar y buscar

![Tabla filtrada por estado](img/08_filtros.png)

- Los **chips de estado** (Todos, Pendiente, En proceso, Enviado, Entregado) filtran la tabla y muestran
  cuántos pedidos hay en cada estado.
- El cuadro **Buscar cliente o ID…** filtra por nombre de cliente o por el inicio del identificador.
- **Refrescar** vuelve a leer los pedidos del servidor. Junto al botón se indica de qué base se leyó.
- Cada fila muestra: identificador corto y fecha, cliente y resumen de ítems, total, estado con una barra
  de progreso de 4 pasos y el nodo que hizo el último cambio (`init.sql` para los pedidos de ejemplo).

### 5.3 Cambiar el estado

Los pedidos avanzan siempre en este orden:

**Pendiente → En proceso → Enviado → Entregado**

Pulsar el botón verde **→ (siguiente estado)** de la fila. Cuando el pedido llega a **Entregado** se
muestra **✓ Completado**. No se puede saltar ni retroceder estados (el sistema lo impide).

### 5.4 Editar un pedido

Sólo los pedidos **Pendiente** muestran el botón **Editar**. Al pulsarlo, el formulario de la izquierda
se carga con los datos del pedido; modificar lo necesario y pulsar **Guardar cambios** (o **Cancelar**).
El total se recalcula automáticamente.

### 5.5 Eliminar un pedido

Pulsar **Eliminar**; el botón cambia a **¿Confirmar?** durante 3 segundos. Pulsarlo de nuevo para
eliminar definitivamente. Si no se confirma, vuelve a su estado normal.

---

## 6. Cómo demostrar la tolerancia a fallos

Guion sugerido para una presentación (≈ 5 minutos).

### 6.1 Balanceo de carga

1. Pulsar **Probar balanceo (6 peticiones)**.
2. **Qué observar:** la franja muestra **β α β α β α**: las peticiones se reparten por turnos entre
   los dos servidores (Round-Robin).

### 6.2 Caída de un nodo

![Nodo Alpha caído: todas las peticiones las atiende Beta](img/04_caida_de_nodo.png)

1. En la tarjeta de **Nodo-Alpha-01**, pulsar **Simular Caída de Nodo**. La tarjeta se pone roja con
   **Caída simulada (503)**.
2. Pulsar **Probar balanceo**: ahora la franja muestra **β β β β β β**: las seis peticiones tuvieron
   éxito y todas las atendió Beta (una petición fallida aparecería como **×** en rojo).
3. Crear un pedido o avanzar un estado: **funciona normalmente** y lo atiende `Nodo-Beta-02`.
4. En el **Registro de peticiones** aparece **`503 → 200`**: el gateway detectó el fallo y reintentó
   en el otro nodo de forma transparente (ver captura de §4.2).
5. Pulsar **Restaurar nodo**. En unos segundos Alpha vuelve a recibir peticiones.

### 6.3 Caída de la base de datos primaria

![Base primaria caída: los datos se sirven desde la réplica](img/06_failover_bd_primaria.png)

1. Pulsar **Simular Caída de BD Primaria**. La tarjeta `db-primary` muestra **Caída simulada** y la
   franja de sincronía pasa a ámbar.
2. Crear un pedido. En **Última petición** la base usada es **Replica Failover** y aparece el aviso
   *"Failover de BD: db-primary no respondió; la operación se atendió en db-replica…"*.
3. La tabla indica **Leído desde Replica Failover** y sigue mostrando todos los pedidos.

### 6.4 Recuperación y reconciliación

![Primaria restaurada y réplica en sincronía](img/07_reconciliacion.png)

1. Pulsar **Restaurar BD Primaria**.
2. Aparece la notificación *"db-primary restaurada · reconciliación: 1 fila(s) copiadas réplica →
   primaria"*: el pedido creado durante la caída se copió a la primaria.
3. La franja vuelve a verde: **✓ Réplica en sincronía**. Las siguientes operaciones usan **Primary**.

### 6.5 Fallos reales (opcional, desde la terminal)

Además de la simulación, se pueden apagar contenedores de verdad:

```bash
docker compose stop backend-node-1   # el panel marca Nodo-Alpha-01 como "Inalcanzable"; todo sigue funcionando
docker compose start backend-node-1

docker compose stop db-primary       # las operaciones pasan a "Replica Failover"
docker compose start db-primary      # en ~5-10 s la réplica y la primaria se reconcilian solas
```

---

## 7. Uso desde el celular

La interfaz se adapta a pantallas pequeñas: las tarjetas se apilan y la tabla permite desplazamiento
horizontal.

<img src="img/09_vista_movil.png" alt="Vista en un teléfono" width="320">

---

## 8. Preguntas frecuentes y solución de problemas

| Problema | Solución |
|----------|----------|
| `docker: command not found` o *"cannot connect to the Docker daemon"* | Abrir Docker Desktop y esperar a que termine de iniciar |
| *"port is already allocated"* al levantar | Otro programa usa el 8080. Crear un archivo `.env` con `GATEWAY_PORT=8081` y abrir <http://localhost:8081> |
| La página carga pero dice **"Gateway NGINX sin backends"** | Los dos nodos están caídos. Restaurar con los botones o `docker compose start backend-node-1 backend-node-2` |
| Un pedido muestra error *"Transición inválida"* | Se intentó saltar o retroceder un estado; sólo se avanza al siguiente |
| No aparece el botón **Editar** | Sólo los pedidos en estado Pendiente se pueden editar |
| Quiero volver a los datos de ejemplo | `docker compose down -v && docker compose up -d --build` |
| Ver lo que hace cada servidor | `docker compose logs -f backend-node-1 backend-node-2` (busca `FAILOVER`, `REPLICATION`, `SYNC`) |
| La franja de sincronía sigue en ámbar tras restaurar | Pulsar **Sincronizar ahora** o esperar ~5 s a la reconciliación automática |

---

## 9. Glosario

| Término | Significado |
|---------|-------------|
| **Nodo** | Cada uno de los dos servidores de aplicación (`Nodo-Alpha-01`, `Nodo-Beta-02`) |
| **Gateway (NGINX)** | Puerta de entrada: entrega la página y reparte las peticiones entre los nodos |
| **Round-Robin** | Reparto por turnos: una petición a cada nodo, alternando |
| **Failover** | Cambio automático a un componente de respaldo cuando el principal falla |
| **db-primary / db-replica** | Base de datos principal y su copia de respaldo |
| **Replica Failover** | La operación se atendió desde la réplica porque la primaria no estaba disponible |
| **Reconciliación** | Proceso que iguala ambas bases copiando los cambios que faltan |
| **Caída simulada** | Fallo provocado a propósito desde el panel para demostrar la tolerancia a fallos |
