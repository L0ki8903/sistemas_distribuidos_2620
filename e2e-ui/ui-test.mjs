// Prueba E2E de la interfaz contra el clúster real + capturas para el manual de usuario.
//
// Uso (con el clúster arriba, idealmente recién creado con `docker compose down -v && docker compose up -d --build`):
//   cd e2e-ui && npm install && node ui-test.mjs [carpeta-de-capturas]
// Variables: BASE (por defecto http://localhost:8080) y CHROME_PATH (ruta del ejecutable de Chrome/Edge).
import puppeteer from "puppeteer-core";
import fs from "node:fs";
import path from "node:path";

const BASE = process.env.BASE || "http://localhost:8080";
const OUT = process.argv[2] || path.resolve("../docs/img");
const CHROME =
  process.env.CHROME_PATH ||
  [
    "C:/Program Files/Google/Chrome/Application/chrome.exe",
    "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
  ].find((candidate) => fs.existsSync(candidate));
if (!CHROME) throw new Error("No se encontró Chrome/Edge: define CHROME_PATH");
fs.mkdirSync(OUT, { recursive: true });
const SEED_CUSTOMERS = ["Distribuidora Andina S.A.S.", "Ferretería El Tornillo", "María Fernanda López"];
const ADMIN_USER = process.env.UI_ADMIN_USER || "admin";
const ADMIN_PASSWORD = process.env.UI_ADMIN_PASSWORD || "Admin12345";

const results = [];
const check = (name, ok, extra = "") => {
  results.push({ name, ok: Boolean(ok) });
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${extra ? `  -> ${extra}` : ""}`);
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const browser = await puppeteer.launch({
  executablePath: CHROME,
  headless: true,
  args: ["--no-sandbox", "--disable-dev-shm-usage", "--hide-scrollbars"],
  defaultViewport: { width: 1440, height: 1000 },
});
const page = await browser.newPage();
const pageErrors = [];
page.on("pageerror", (e) => pageErrors.push(e.message));
// El navegador registra en consola el 401 del login fallido que la prueba provoca a propósito;
// se tolera exactamente ese (expectedUnauthorized) y cualquier otro error cuenta como fallo.
let expectedUnauthorized = 0;
page.on("console", (m) => {
  if (m.type() !== "error") return;
  if (expectedUnauthorized > 0 && /status of 401/.test(m.text())) {
    expectedUnauthorized -= 1;
    return;
  }
  pageErrors.push(m.text());
});

const shot = async (name, selector, { keepToasts = false } = {}) => {
  const file = path.join(OUT, name);
  // Captura limpia: barra superior no fija, sin foco en inputs y (opcional) sin toasts.
  // (Vía CSSOM y no con <style>: la Content-Security-Policy del sitio bloquea estilos en línea.)
  await page.evaluate((keepToasts) => {
    const topbar = document.querySelector(".topbar");
    if (topbar) topbar.style.position = "static";
    document.activeElement?.blur();
    if (!keepToasts) document.querySelectorAll(".toast .icon-btn").forEach((b) => b.click());
    window.scrollTo(0, 0);
  }, keepToasts);
  await sleep(250);
  if (selector) await (await page.$(selector)).screenshot({ path: file });
  else await page.screenshot({ path: file, fullPage: true });
  console.log(`      📸 ${name}`);
};
const text = (selector) => page.$eval(selector, (el) => el.textContent.trim()).catch(() => "");
const customers = () => page.$$eval("tbody tr .customer", (els) => els.map((e) => e.textContent));
const waitText = (selector, needle, timeout = 10000) =>
  page.waitForFunction(
    (s, n) => [...document.querySelectorAll(s)].some((el) => el.textContent.includes(n)),
    { timeout },
    selector,
    needle,
  );
const clickButton = async (label, scope = "body") => {
  const ok = await page.evaluate(
    (label, scope) => {
      const btn = [...document.querySelector(scope).querySelectorAll("button")].find(
        (b) => b.textContent.trim().includes(label) && !b.disabled,
      );
      btn?.click();
      return Boolean(btn);
    },
    label,
    scope,
  );
  if (!ok) throw new Error(`Botón no encontrado: "${label}" en ${scope}`);
};
const clickInTile = (tileText, label) =>
  page.evaluate(
    (tileText, label) => {
      const tile = [...document.querySelectorAll(".tile")].find((t) => t.textContent.includes(tileText));
      const btn = tile && [...tile.querySelectorAll("button")].find((b) => b.textContent.includes(label));
      btn?.click();
      return Boolean(btn);
    },
    tileText,
    label,
  );
const clickInRow = (customer, label) =>
  page.evaluate(
    (customer, label) => {
      const row = [...document.querySelectorAll("tbody tr")].find(
        (r) => r.querySelector(".customer")?.textContent === customer,
      );
      const btn = row && [...row.querySelectorAll("button")].find((b) => b.textContent.includes(label));
      btn?.click();
      return Boolean(btn);
    },
    customer,
    label,
  );
const rowStatus = (customer) =>
  page.evaluate((customer) => {
    const row = [...document.querySelectorAll("tbody tr")].find((r) => r.querySelector(".customer")?.textContent === customer);
    return row?.querySelector(".status")?.textContent ?? null;
  }, customer);
const waitIdle = (label) =>
  page.waitForFunction(
    (label) => [...document.querySelectorAll("button")].some((b) => b.textContent.includes(label) && !b.disabled),
    { timeout: 20000 },
    label,
  );

async function fillOrder(customer, items) {
  await page.click(".order-form .field input", { clickCount: 3 });
  await page.type(".order-form .field input", customer);
  for (let i = 0; i < items.length; i += 1) {
    if (i > 0) await clickButton("+ Agregar ítem", ".order-form");
    const rows = await page.$$(".order-form .item-row");
    const [product, qty, price] = await rows[i].$$("input");
    await product.type(items[i][0]);
    await qty.click({ clickCount: 3 });
    await qty.type(String(items[i][1]));
    await price.click({ clickCount: 3 });
    await price.type(String(items[i][2]));
  }
}
// Envía el formulario y espera a que la tabla tenga una fila más (robusto ante nombres repetidos).
async function submitNewOrder() {
  const before = (await customers()).length;
  await clickButton("Crear pedido", ".order-form");
  await page.waitForFunction((n) => document.querySelectorAll("tbody .customer").length > n, { timeout: 10000 }, before);
  await waitIdle("Crear pedido");
}
async function createOrder(customer, items) {
  await fillOrder(customer, items);
  await submitNewOrder();
}

async function fillLogin(username, password) {
  const inputs = await page.$$(".login-card input");
  await inputs[0].click({ clickCount: 3 });
  await inputs[0].type(username);
  const passwordInput = await page.$('.login-card input[type="password"]');
  await passwordInput.click({ clickCount: 3 });
  await passwordInput.type(password);
}

try {
  // ------------------------------------------------------------------ inicio de sesión
  await page.goto(BASE, { waitUntil: "networkidle0" });
  await page.evaluate(() => localStorage.clear());
  await page.reload({ waitUntil: "networkidle0" });
  await page.waitForSelector(".login-card");
  check("Sin sesión se muestra la pantalla de inicio de sesión", Boolean(await page.$(".login-card")));
  await shot("00_inicio_sesion.png");
  expectedUnauthorized = 1;
  await fillLogin(ADMIN_USER, "contraseña-incorrecta-1");
  await clickButton("Entrar", ".login-card");
  await waitText(".login-card .form-error", "incorrectos");
  check("Login con contraseña incorrecta muestra error genérico", true);
  await fillLogin(ADMIN_USER, ADMIN_PASSWORD);
  await clickButton("Entrar", ".login-card");
  await page.waitForSelector(".role-badge");
  check("Login de administrador abre el panel completo", (await text(".role-badge")) === "Administrador");

  // ------------------------------------------------------------------ carga inicial
  await page.waitForSelector("tbody .customer");
  await sleep(3500); // primer ciclo de polling del clúster
  const initial = await customers();
  check("Carga los 3 pedidos de ejemplo de init.sql", SEED_CUSTOMERS.every((c) => initial.includes(c)), initial.join(" | "));
  const tiles = await page.$$eval(".tile", (els) => els.map((e) => e.textContent));
  check(
    "Panel muestra ambos nodos operativos",
    tiles.some((t) => t.includes("Nodo-Alpha-01") && t.includes("Operativo")) &&
      tiles.some((t) => t.includes("Nodo-Beta-02") && t.includes("Operativo")),
  );
  check("Panel muestra db-primary y db-replica operativas", tiles.filter((t) => t.includes("db-") && t.includes("Operativa")).length === 2);

  // ------------------------------------------------------------------ crear pedidos con el formulario
  await fillOrder("Logística Express S.A.S.", [
    ["Caja corrugada 60x40", 20, 4.75],
    ["Film stretch 50 cm", 5, 12.9],
  ]);
  check("Formulario calcula el total estimado (20×4,75 + 5×12,90)", (await text(".form-total strong")) === "$ 159,50", await text(".form-total strong"));
  await shot("02_formulario_nuevo_pedido.png", ".order-form");
  await submitNewOrder();
  check("Crear pedido: aparece en la tabla como Pendiente", (await rowStatus("Logística Express S.A.S.")) === "Pendiente");
  const lastNode = await text(".last-request .node-chip");
  check("Panel 'Última petición' muestra el nodo que atendió", /Nodo-(Alpha-01|Beta-02)/.test(lastNode), lastNode);
  check("Panel 'Última petición' muestra BD Primary", (await text(".last-request .db-chip")) === "Primary");

  await createOrder("Supermercado La Economía", [["Arroz 5 kg", 12, 8.4]]);
  await createOrder("Clínica San Rafael", [["Guantes de nitrilo (caja)", 30, 6.2]]);

  // ------------------------------------------------------------------ cambio de estado
  await clickInRow("Supermercado La Economía", "→ En proceso");
  await page.waitForFunction(() => true, { timeout: 100 });
  await sleep(700);
  check("Avanzar estado: pendiente → en proceso", (await rowStatus("Supermercado La Economía")) === "En proceso");
  await clickInRow("Logística Express S.A.S.", "→ En proceso");
  await sleep(700);
  await clickInRow("Logística Express S.A.S.", "→ Enviado");
  await sleep(700);
  check("Avanzar estado: en proceso → enviado", (await rowStatus("Logística Express S.A.S.")) === "Enviado");
  await clickInRow("María Fernanda López", "→ Entregado");
  await sleep(700);
  check("Avanzar estado: enviado → entregado (fin del ciclo)", (await rowStatus("María Fernanda López")) === "Entregado");

  // ------------------------------------------------------------------ editar
  await clickInRow("Clínica San Rafael", "Editar");
  await waitText(".order-form h3", "Editar pedido");
  const qty = (await page.$$(".order-form .item-row input"))[1];
  await qty.click({ clickCount: 3 });
  await qty.type("40");
  await clickButton("Guardar cambios", ".order-form");
  await waitText(".order-form h3", "Nuevo pedido");
  await sleep(500);
  const editedTotal = await page.evaluate(() => {
    const row = [...document.querySelectorAll("tbody tr")].find((r) => r.textContent.includes("Clínica San Rafael"));
    return row?.querySelector("td.num")?.textContent;
  });
  check("Editar pedido pendiente: total recalculado (40×6,20)", editedTotal === "$ 248,00", editedTotal);

  // ------------------------------------------------------------------ balanceo
  await clickButton("Probar balanceo");
  await waitIdle("Probar balanceo");
  const cells = await page.$$eval(".balance-cell", (els) => els.map((e) => e.textContent));
  check("Probar balanceo: 6 respuestas repartidas entre α y β", cells.length === 6 && cells.includes("α") && cells.includes("β"), cells.join(""));
  await sleep(3200);
  await shot("03_estado_cluster_normal.png", ".cluster");
  await shot("01_dashboard.png");

  // ------------------------------------------------------------------ caída de nodo
  check("Clic en 'Simular Caída de Nodo' (Alpha)", await clickInTile("Nodo-Alpha-01", "Simular Caída de Nodo"));
  await waitText(".tile", "Caída simulada (503)");
  await clickButton("Probar balanceo");
  await waitIdle("Probar balanceo");
  const cellsDown = await page.$$eval(".balance-cell", (els) => els.map((e) => e.textContent));
  check("Con Alpha caído, las 6 peticiones las atiende Beta (sin errores)", cellsDown.length === 6 && cellsDown.every((c) => c === "β"), cellsDown.join(""));
  const retries = await page.$$eval(".log-table .retry", (els) => els.map((e) => e.textContent));
  check("El registro evidencia el reintento de NGINX (503 → 200)", retries.some((r) => r.includes("503") && r.includes("200")), retries.join(" | "));
  await createOrder("Pedido con nodo caído", [["Etiqueta térmica", 100, 0.15]]);
  check("Crear pedido con un nodo caído funciona (lo atiende Beta)", (await text(".last-request .node-chip")) === "Nodo-Beta-02");
  await sleep(3200);
  await shot("04_caida_de_nodo.png", ".cluster");
  await shot("05_registro_peticiones_failover.png", ".log");
  check("Clic en 'Restaurar nodo'", await clickInTile("Nodo-Alpha-01", "Restaurar nodo"));
  await page.waitForFunction(() => [...document.querySelectorAll(".tile")].filter((t) => t.textContent.includes("Operativo")).length === 2, { timeout: 10000 });
  check("Nodo Alpha vuelve a Operativo", true);

  // ------------------------------------------------------------------ caída de BD primaria
  await clickButton("Simular Caída de BD Primaria");
  await waitText(".tile", "Caída simulada");
  await createOrder("Pedido con BD primaria caída", [["Pallet de madera", 4, 18]]);
  check("Con db-primary caída, el pedido se guarda en 'Replica Failover'", (await text(".last-request .db-chip")) === "Replica Failover");
  check("Se muestra el aviso de failover de BD", (await text(".last-request .callout-amber")).includes("db-replica"));
  await clickButton("Refrescar");
  await sleep(800);
  await sleep(3000);
  await shot("06_failover_bd_primaria.png");

  await clickButton("Restaurar BD Primaria");
  await waitText(".toast", "reconciliación");
  const toast = await page.$$eval(".toast", (els) => els.map((e) => e.textContent).join(" | "));
  check("Al restaurar, la reconciliación copia 1 fila réplica → primaria", toast.includes("1 fila(s) copiadas"), toast);
  await waitText(".sync-line", "Réplica en sincronía", 12000);
  check("Panel indica 'Réplica en sincronía'", true);
  await shot("07_reconciliacion.png", ".cluster");

  // ------------------------------------------------------------------ filtros, búsqueda y eliminación
  await clickButton("En proceso", ".chips");
  await sleep(300);
  const filtered = await customers();
  check("Filtro por estado 'En proceso'", filtered.length > 0 && (await page.$$eval("tbody .status", (els) => els.every((e) => e.textContent === "En proceso"))), filtered.join(" | "));
  await shot("08_filtros.png", ".orders");
  await clickButton("Todos", ".chips");
  await page.type(".search", "clínica");
  await sleep(300);
  const searched = await customers();
  check("Búsqueda por cliente ('clínica')", searched.length >= 1 && searched.every((c) => c === "Clínica San Rafael"), searched.join(" | "));
  await page.click(".search", { clickCount: 3 });
  await page.keyboard.press("Backspace");

  for (const name of ["Pedido con nodo caído", "Pedido con BD primaria caída"]) {
    await clickInRow(name, "Eliminar");
    await clickInRow(name, "¿Confirmar?");
    await page.waitForFunction((n) => ![...document.querySelectorAll("tbody .customer")].some((e) => e.textContent === n), { timeout: 8000 }, name);
  }
  check("Eliminar pedidos con confirmación en dos pasos", !(await customers()).some((c) => c.startsWith("Pedido con")));

  // ------------------------------------------------------------------ cliente (rol user)
  await clickButton("Cerrar sesión");
  await page.waitForSelector(".login-card");
  check("Cerrar sesión vuelve a la pantalla de inicio", true);
  const clientUser = `cliente.${Date.now().toString(36)}`;
  await clickButton("Crear cuenta", ".login-tabs");
  const registerInputs = await page.$$(".login-card input");
  await registerInputs[0].type(clientUser);
  await registerInputs[1].type("Camila Rojas");
  await registerInputs[2].type("Cliente2026");
  await registerInputs[3].type("Cliente2026");
  await page.click(".login-card button[type=submit]");
  await page.waitForSelector(".role-badge");
  check("Registro de cliente inicia sesión con rol Cliente", (await text(".role-badge")) === "Cliente");
  await page.waitForFunction(() => document.querySelector("#orders-title")?.textContent === "Mis pedidos");
  check("El cliente no ve los pedidos de otros", (await customers()).length === 0);
  check("El cliente no ve controles de caos ni de bases de datos", !(await page.$$eval("button", (bs) => bs.some((b) => /Simular|Restaurar|Sincronizar/.test(b.textContent)))));
  check("El formulario propone el nombre del cliente", (await page.$eval(".order-form .field input", (i) => i.value)) === "Camila Rojas");
  await fillOrder("", [["Sillas plegables", 6, 22.5]]);
  await submitNewOrder();
  const clientRows = await customers();
  check("El cliente crea un pedido y sólo ve el suyo", clientRows.length === 1 && clientRows[0] === "Camila Rojas", clientRows.join(" | "));
  check("El cliente no puede avanzar estados (sin botón →)", !(await page.$$eval("tbody button", (bs) => bs.some((b) => b.textContent.startsWith("→")))));
  check("El cliente puede cancelar su pedido pendiente", await page.$$eval("tbody button", (bs) => bs.some((b) => b.textContent === "Cancelar pedido")));
  await sleep(500);
  await shot("10_vista_cliente.png");

  // ------------------------------------------------------------------ vista móvil (cliente)
  await page.setViewport({ width: 390, height: 844, isMobile: true, deviceScaleFactor: 2 });
  await sleep(1500);
  await page.evaluate(() => {
    document.querySelectorAll(".toast .icon-btn").forEach((b) => b.click());
    window.scrollTo(0, 0);
  });
  await sleep(300);
  await page.screenshot({ path: path.join(OUT, "09_vista_movil.png") });
  console.log("      📸 09_vista_movil.png");
} catch (error) {
  check(`Ejecución sin excepciones`, false, error.message);
  await page.screenshot({ path: path.join(OUT, "_error.png"), fullPage: true });
} finally {
  check("Sin errores de JavaScript en la consola", pageErrors.length === 0, pageErrors.join(" | "));
  await browser.close();
  const failed = results.filter((r) => !r.ok).length;
  console.log(`\nRESULTADO UI: ${results.length - failed}/${results.length} verificaciones OK`);
  process.exitCode = failed ? 1 : 0;
}
