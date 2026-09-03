# Oficialía Digital DSA — 100% Python

> **Middleware de Ingesta, Extracción IA y RPA para Gestión Documental**
> Reconstrucción unificada del sistema original (Node.js/Fastify/TypeScript/Svelte 5/WebSockets)
> en **Python puro**, sin sobreingeniería: un proceso, un comando (`python main.py`), cero Node.

**Institución:** División de Servicios Administrativos (DSA) — Hospital Civil de Guadalajara (HCG).

---

## 1. Qué hace el sistema

1. **Ingesta dual de PDFs**: vigilancia automática de `storage/01_entrada/` (escáner ADF, vía
   `watchdog`) y carga manual por la web (arrastrar y soltar). Deduplicación **atómica** por
   SHA-256: un duplicado jamás crea un segundo registro.
2. **Preprocesamiento** con **PyMuPDF** en memoria: validación de cabecera/contraseña/estructura,
   sanitización del árbol xref, conteo de páginas y renderizado a **PNG @300 dpi** (máx. 10
   páginas por inferencia).
3. **Extracción estructurada** con **Gemini 2.5 Flash** (SDK oficial `google-genai`):
   system prompt institucional (protocolo OCR de oficios, 9 secciones), salida JSON forzada y
   validación estricta con **Pydantic v2** (`MetadatosOficio`, 11 campos). Si la IA no está
   disponible (sin API key, cuota agotada, timeout de red), un **extractor heurístico de
   respaldo** (`core/heuristic_extractor.py`, solo regex, sin red) rescata al menos el número
   de oficio y la fecha del texto plano del PDF, para que el documento llegue de todos modos a
   `PENDIENTE_REVISION` — marcado como `HEURISTICA_FALLBACK` — en vez de perderse en cuarentena.
4. **Ciclo de vida persistido en SQLite (WAL)**:
   `INGESTADO → EN_PREPROCESO → EXTRAYENDO → PENDIENTE_REVISION → EJECUTANDO_RPA → COMPLETADO`
   (con `ERROR_RPA` reinteligible y `DESCARTADO` terminal).
5. **Revisión asistida (HITL)** en la web: bandeja con filtros/KPIs/buscador en vivo y
   **split-screen 50/50** — visor de PDF a la izquierda, formulario precargado con la IA a la
   derecha. Acciones: **[Confirmar y Registrar]**, **[Descartar]**, **[Reintentar RPA]**.
6. **Al confirmar**: renombrado canónico `YYYY-MM-DD__[FOLIO]__[REMITENTE].pdf` en
   `storage/03_procesados/YYYY/MM/` + **respaldo espejo `.json`** + verificación de hash
   post-escritura.
7. **RPA con Playwright**: inyección del oficio en la Intranet Webix (`op_cucs.fwx` → iframe
   `op_ningr.fwx`), subida del PDF canónico, captura del **folio de acuse** y screenshot de
   evidencia. Modo dual `RPA_MODO=simulacion|playwright` y `RPA_HEADLESS=false` para ver el
   navegador.
8. **Sincronización opcional a Google Sheets** (cuenta de servicio) con el layout A:M del tablero
   de control; sin credenciales funciona en **stub local** (`data/tablero_local.csv`).
9. **Operación diagnosticable**: log rotativo a archivo (`logs/app.log`, 10 MB × 5 respaldos,
   `core/logging_setup.py`) además de la consola, y esquema SQLite versionado
   (`PRAGMA user_version` + migraciones incrementales en `database.py`) para que actualizar la
   app sobre una instalación existente no corrompa ni pierda la base de datos de un cliente.

---

## 2. Instalación en Windows (usuario final — sin Python, sin nada que instalar a mano)

Para el personal de la DSA que solo va a **usar** el sistema en un equipo Windows 10/11,
no hace falta clonar el repositorio, instalar Python ni ejecutar `pip install`:

1. Vaya a la pestaña **[Releases](../../releases)** de este repositorio (o a la pestaña
   **Actions → Instalador de Windows → última ejecución → Artifacts**, si aún no hay una
   versión etiquetada) y descargue **`OficialiaDigitalDSA-Setup.exe`**.
2. Ejecútelo y siga el asistente (pide permisos de administrador **solo durante la
   instalación**, para escribir en `Archivos de programa` y crear la carpeta de datos
   compartida). Puede omitir el componente **"Automatización RPA"** (~300 MB, el
   navegador Chromium) si de momento solo va a usar el modo simulación/HITL.
3. Al terminar, el propio instalador ofrece abrir la aplicación — el navegador se abre
   solo en `http://127.0.0.1:8080`. También queda un acceso directo en el Escritorio y
   en el menú Inicio.
4. Para extracción real con Gemini (o RPA/Sheets reales), abra
   **Inicio → Oficialía Digital DSA → Configuración (.env)**, capture las claves/credenciales
   necesarias y reinicie la aplicación. **Esto es lo único que el instalador no puede
   resolver por usted**: la API key de Gemini y las credenciales institucionales son
   secretos propios de cada instalación, no dependencias de software.
5. Sin tocar nada, el sistema arranca igualmente en modo seguro: RPA simulado
   (acuses sintéticos `HCG-OP-SIM-*`) y Google Sheets en stub local — sirve para
   explorar la bandeja y el flujo HITL antes de configurar credenciales reales.

Todo queda instalado en `Archivos de programa\OficialiaDigitalDSA\` (código, de solo
lectura) y los datos (`oficialia.db`, PDFs procesados, `.env`) en
`%ProgramData%\OficialiaDigitalDSA\` (con permisos de escritura para el usuario estándar
que ejecuta la app — no requiere privilegios de administrador en el uso diario).
Desinstalar desde *Agregar o quitar programas* **no borra** esa carpeta de datos: la BD y
los PDFs institucionales quedan a salvo.

> **¿Cómo se genera ese instalador?** `packaging/oficialia.spec` (PyInstaller) +
> `packaging/oficialia.iss` (Inno Setup) + `packaging/build_windows.ps1` los ensamblan en
> un único `.exe` que ya trae Python, todas las dependencias de `requirements.txt` y
> (opcionalmente) el navegador Chromium de Playwright — el usuario final nunca instala
> nada de eso por separado. El workflow `.github/workflows/build-windows-installer.yml`
> construye este instalador automáticamente en un runner de Windows de GitHub Actions
> (PyInstaller no compila de forma cruzada) cada vez que se publica un tag `v*`, y lo deja
> tanto como artefacto de la ejecución como adjunto de la Release — nadie necesita un
> equipo Windows propio para publicar una nueva versión. Vea el detalle en
> `packaging/build_windows.ps1`. **No se incluye Tesseract/OCR** (dependencia opcional y
> auxiliar de `core/pdf_engine.py`: el sistema funciona igual sin ella, la extracción
> corre por Gemini) — si el IT institucional lo requiere, puede instalarse aparte.

---

## 3. Arquitectura (monolito modular, un solo proceso)

```text
                 ┌────────────────────────────────────────────────────────┐
                 │                     python main.py                     │
                 ├────────────────────────────────────────────────────────┤
   01_entrada/ ─▶│ core/watcher.py   (watchdog + poll de respaldo)        │
   Web (upload)─▶│ core/pipeline.py  (orquestador del flujo)              │
                 │   ├─ core/pdf_engine.py    (PyMuPDF: hash/render)      │
                 │   ├─ core/ai_extractor.py  (Gemini 2.5 Flash)          │
                 │   ├─ core/file_manager.py  (storage + canónicos)       │
                 │   ├─ database.py           (SQLite WAL + CRUD)         │
                 │   ├─ rpa/playwright_rpa.py  (Intranet Webix)           │
                 │   └─ core/sheets_sync.py   (Sheets / stub local)       │
                 │ ui/  (NiceGUI: bandeja + HITL split-screen)            │
                 └────────────────────────────────────────────────────────┘
```

### Estructura de carpetas

```text
oficialia_dsa/
├── config.py             # Configuración central (pydantic-settings + .env)
├── database.py           # SQLite WAL, esquema y repositorio CRUD
├── core/                 # Lógica de negocio
│   ├── models.py         # Esquemas Pydantic (MetadatosOficio, estados, badges)
│   ├── pdf_engine.py     # PyMuPDF: hash, sanitización y render
│   ├── ai_extractor.py   # Gemini 2.5 Flash + prompt institucional (verbatim)
│   ├── file_manager.py   # Gestión de storage y nomenclatura canónica
│   ├── pipeline.py       # Orquestador (ingesta → HITL → RPA → Sheets)
│   ├── heuristic_extractor.py # Extractor de respaldo por regex (sin IA)
│   ├── logging_setup.py  # Logging a consola + archivo rotativo
│   ├── watcher.py        # Vigilancia en tiempo real (watchdog)
│   └── sheets_sync.py    # Google Sheets vía Service Account (+ stub local)
├── rpa/
│   └── playwright_rpa.py # Worker RPA (real + simulación) para op_cucs.fwx
├── ui/
│   ├── layout.py         # Encabezado, KPIs y contexto compartido
│   ├── views_dashboard.py# Bandeja: filtros, buscador, tabla y dropzone
│   └── views_hitl.py     # Split-screen: visor PDF + formulario reactivo
├── tests/                # pytest — ver sección 5.6 (sin red, sin credenciales)
├── storage/              # 01_entrada · 02_en_proceso · 03_procesados · 04_errores
├── data/                 # oficialia.db (SQLite) y tablero_local.csv (stub)
├── main.py               # Punto de entrada único
├── requirements.txt      # Dependencias exactas
├── requirements-dev.txt  # + pytest (desarrollo/pruebas)
├── pytest.ini
├── .env.example          # Plantilla de variables de entorno
├── packaging/            # Instalador Windows (ver sección 2)
│   ├── oficialia.spec    # Bundle PyInstaller (onedir)
│   ├── oficialia.iss     # Instalador Inno Setup
│   ├── build_windows.ps1 # Orquesta todo el proceso de build
│   └── requirements-build.txt
├── .github/workflows/
│   └── build-windows-installer.yml  # Construye el instalador en CI (runner Windows)
└── README.md
```

> **Adiciones justificadas** respecto de la estructura base solicitada:
> `core/pipeline.py` (el orquestador que en el original era `DocumentWorkflowOrchestrator`;
> el watcher, la UI y el RPA lo comparten) y `core/sheets_sync.py` (regla 6 de
> sincronización externa). No existen scripts secundarios sueltos: todo el sistema
> se ejecuta con `python main.py`.

---

## 4. Máquina de estados y ciclo de vida físico

```text
 INGESTADO ─▶ EN_PREPROCESO ─▶ EXTRAYENDO ─▶ PENDIENTE_REVISION
                                              │
                 [Confirmar y Registrar] ──────┤──▶ EJECUTANDO_RPA ─▶ COMPLETADO
                 [Descartar] ──▶ DESCARTADO    │         └▶ ERROR_RPA ─(Reintentar)─┘
```

| Estado | Archivo físico | Observaciones |
| --- | --- | --- |
| `INGESTADO` | `01_entrada/{epoch_ms}_{nombre}` | Registro creado tras deduplicar hash |
| `EN_PREPROCESO` | `02_en_proceso/{uuid}.pdf` | Validación + sanitización PyMuPDF |
| `EXTRAYENDO` | `02_en_proceso/{uuid}.pdf` | Render 300 dpi + Gemini 2.5 Flash |
| `PENDIENTE_REVISION` | `02_en_proceso/{uuid}.pdf` | Cola de la bandeja HITL |
| `EJECUTANDO_RPA` | `03_procesados/YYYY/MM/{canónico}.pdf` | + `.json` espejo |
| `COMPLETADO` | ídem | Acuse + screenshot + Sheets (no bloqueante) |
| `ERROR_RPA` | ídem | Reinteligible sin reextraer ([Reintentar RPA]) |
| `DESCARTADO` | `04_errores/{nombre}` + `.error.txt` | Descarte humano o fallo temprano |

**Fallos de preproceso/extracción**: el original distinguía `ERROR_PREPROCESO` y
`ERROR_EXTRACCION`; esta versión consolidada los registra como `DESCARTADO` con el
motivo completo en `error_msg` y el archivo aislado en `04_errores/` con su
`.error.txt` — misma trazabilidad, menos estados. El duplicado por hash también se
aísla en `04_errores` con motivo `DUPLICATE_HASH_DETECTED`.

---

## 5. Instalación desde código fuente (desarrollo, o cualquier plataforma sin el instalador)

> Si solo va a **usar** el sistema en Windows 10/11, no necesita nada de esta sección:
> vea la sección 2. Esta ruta es para desarrollo, contribución al código, o para
> ejecutar el sistema en Linux/macOS (el instalador empaquetado es exclusivo de Windows).

### 5.1 Requisitos previos

- **Python 3.11+** (probado con 3.12) con `venv` y `pip`.
- (Solo para RPA real) acceso LAN/VPN a la Intranet institucional y credenciales.
- (Solo para IA real) una API key de Google AI Studio (Gemini).

### 5.2 Crear el entorno virtual e instalar dependencias

```bash
# 1) Ubicarse en la raíz del proyecto
cd oficialia_dsa

# 2) Crear y activar el entorno virtual
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# 3) Instalar las dependencias exactas
pip install -r requirements.txt
```

### 5.3 Instalar el navegador de Playwright (solo para RPA real)

```bash
# Descarga el Chromium gestionado por Playwright (~150 MB, una sola vez)
playwright install chromium
```

> En modo `RPA_MODO=simulacion` (default) **no** es necesario instalar el navegador.

### 5.4 Configurar variables de entorno

```bash
cp .env.example .env
# Edite .env: como mínimo GEMINI_API_KEY para extracción real.
# Sin configurar nada, el sistema arranca seguro: RPA simulado + Sheets stub.
chmod 600 .env     # recomendado en el servidor institucional
```

### 5.5 Ejecutar el sistema (comando único)

```bash
python main.py
```

Abrir en el navegador: **http://localhost:8080** (o `APP_PORT` del `.env`).

- Deje PDFs en `storage/01_entrada/` (el escáner departamental puede apuntar ahí por SMB)
  o súbalos desde la bandeja web.
- Revise cada documento en `Pendientes` → verifique el formulario → **[Confirmar y Registrar]**.

### 5.6 Ejecutar la suite de pruebas

```bash
pip install -r requirements-dev.txt
pytest -q
```

No requiere `GEMINI_API_KEY`, Playwright ni Tesseract: cada prueba usa su propia
carpeta temporal (BD SQLite + storage aislados, `tests/conftest.py`) y dobles
(fakes) explícitos donde haría falta un servicio externo — nunca golpea Gemini,
la Intranet real ni internet. Cobertura actual: contrato `MetadatosOficio`
(normalización, centinela `S/N`, nomenclatura canónica), repositorio SQLite
(CRUD, concurrencia optimista, **migraciones de esquema**, el buscador de la
bandeja), preprocesamiento PyMuPDF, el extractor heurístico de respaldo
(sección 1) y la integración completa del pipeline de ingesta con un extractor
de IA simulado. `pytest.ini` fija `testpaths = tests`.

---

## 6. Modos de operación

| Módulo | Variable | Valores | Sin configurar |
| --- | --- | --- | --- |
| Extracción IA | `GEMINI_API_KEY` | clave real | **Falla honestamente**: documento en `DESCARTADO` con `AI_NO_CONFIGURADA` y archivo en cuarentena (no hay stub de IA para no falsear datos) |
| RPA | `RPA_MODO` | `simulacion` \| `playwright` | `simulacion`: acuse sintético `HCG-OP-SIM-*`, sin navegador |
| RPA navegador | `RPA_HEADLESS` | `false` (visible) \| `true` | `false` — ver el navegador al inyectar |
| Forzar fallo RPA simulado | `RPA_SIMULACION_FALLAR` | `true` \| `false` | `false` — útiles para ejercitar `ERROR_RPA` + reintento |
| Google Sheets | `GOOGLE_SHEETS_SPREADSHEET_ID` + credenciales | Service Account (JSON en una línea o `GOOGLE_APPLICATION_CREDENTIALS`) | **Stub local**: `data/tablero_local.csv` |
| Watchfolder | `WATCHFOLDER_ENABLED` | `true` \| `false` | `true` |

Layout del tablero de Sheets (fila 1 = encabezados, gestionados por usted):

```text
A: Fecha registro | B: ID documento | C: Folio oficio    | D: Fecha emisión
E: Procedencia    | F: Dependencia  | G: Remitente        | H: Asunto
I: Plazo (días)   | J: Datos sensibles | K: Archivo canónico
L: Folio acuse RPA | M: RPA exitoso
```

---

## 7. Mapeo de campos del RPA (Intranet Webix `op_ningr.fwx`)

| Campo Webix (view id) | Origen del dato |
| --- | --- |
| `cve` | CVE de oficialía (config `RPA_OFICIALIA_CVE` o primer elemento del combo) |
| `anio_ingr`, `fech_rece`, `hora_rece` | Fecha/hora local del registro |
| `nume_cont`, `nume_ofic` | `numero_oficio` (folio del emisor) |
| `fech_ofic` | `fecha_emision` en DD/MM/AAAA |
| `info_sens` | `contiene_datos_sensibles` → `'1'`/`'0'` |
| `rbDepe` | `procedencia` → `'1'` (HCG) / `'2'` (Ajena) |
| `dependen` / `txtDepen` | HCG: CVE o búsqueda del combo; Ajena: texto libre |
| `remi_nomb`, `remi_carg` | Remitente (nombre/cargo) |
| `dest_nomb`, `dest_carg` | Destinatario (nombre/cargo) |
| `tipo_ofic` | `'5'` (CON TÉRMINO) si `plazo_dias > 0`, si no `'1'` (ORIGINAL) |
| `fech_term`, `txtFech_term` | `fecha_emision + plazo_dias` |
| `clase` | `'5'` si el asunto menciona INVITACIÓN, si no `'4'` |
| `asunto`, `nota` | Síntesis y `PLAZO ESTIPULADO: N DÍA(S)` |
| PDF canónico | `input[type=file]` del formulario (si la pantalla lo expone) |

Acuse: folio detectado por regex `HCG-OP-\d{4}-\d{4,}…` en diálogos nativos, texto de la
página (todos los frames) o la URL; screenshot completo en
`03_procesados/YYYY/MM/acuse_{uuid}.png` (fallos: `04_errores/YYYY/MM/error_{uuid}.png`).

---

## 8. Decisiones de consolidación (original → Python)

| Original (Node/TS) | Reconstrucción (Python) | Motivo |
| --- | --- | --- |
| 4 tablas SQLite (1 raíz + 3 de detalle 1:1) | 1 tabla `documentos` con JSON embebido | Menos JOINs, misma información, CRUD simple |
| 12 estados del ciclo de vida | 8 estados requeridos | Consolidación pedida; errores tempranos → `DESCARTADO` + `error_msg` + cuarentena |
| Fastify + rutas HTTP + WebSocket + cliente Svelte 5 | NiceGUI en el mismo proceso | Un solo lenguaje/proceso; el refresco "en vivo" se logra con `ui.timer` + SQLite local |
| Subproceso CLI `pdf_worker.py` (spawn por archivo) | `core/pdf_engine.py` en memoria | Sin IPC JSON/stdin, sin coste de arranque por documento |
| Clean Architecture (8 interfaces/puertos + DI manual) | Módulos concretos + 1 orquestador | Legible y mantenible por un solo desarrollador |
| Vigilancia por polling puro (SMB) | **watchdog** + poll de respaldo | Requisito explícito de `watchdog`, conservando la robustez ante volúmenes de red |
| Búsqueda semántica local (Puerto 7, P1 opcional: `@xenova/transformers`, modelo `bge-m3` de cientos de MB) | **No migrada** | Fase complementaria opcional del PRD; su peso contradice el objetivo de ligereza. El match exacto por folio/hash y el buscador en vivo cubren el caso principal. Puede re-añadirse como módulo sin tocar el pipeline |

**Garantías operativas conservadas**: deduplicación atómica por hash (única restricción
SQLite), cuarentena con `.error.txt`, verificación de hash post-escritura del canónico,
concurrencia optimista por `version`, Sheets jamás bloquea el `COMPLETADO`, reintentos RPA
con backoff y clasificación de errores transitorios, prefijo `{epoch_ms}_` para distinguir
canal WEB del escáner ante el vigilante, estabilidad de archivo (tamaño/mtime) antes de ingerir.

---

## 9. Solución de problemas frecuentes

| Síntoma | Causa y remedio |
| --- | --- |
| Documentos caen en `DESCARTADO` con `AI_NO_CONFIGURADA` | Falta `GEMINI_API_KEY` en `.env` (comportamiento honesto: no hay stub de IA) |
| `El registro en la Intranet falló (HTTP 401)` | Credenciales `INTRANET_HTTP_USERNAME/PASSWORD` inválidas; si la Intranet usa NTLM, habilite Negotiate para Chromium |
| `FORMULARIO_WEBIX_TIMEOUT` | La Intranet no expone `op_ningr.fwx` en el iframe o los `view id` cambiaron — revise selectores |
| El visor PDF no muestra el documento | El navegador debe tener visor PDF nativo (Chrome/Edge/Firefox modernos lo traen); use «Abrir en pestaña nueva» |
| Watcher no detecta archivos sobre montaje SMB | Confirme `WATCHFOLDER_ENABLED=true`; el poll de respaldo (cada 5 s) barre el directorio de todos modos |
| Puerto ocupado | Cambie `APP_PORT` en `.env` (o en `%ProgramData%\OficialiaDigitalDSA\.env` si usa el instalador Windows) |
| (Instalador Windows) RPA falla con "Executable doesn't exist" | Se omitió el componente "Automatización RPA" al instalar — reinstale marcándolo, o cambie `RPA_MODO=simulacion` |
| (Instalador Windows) No abre el navegador solo | Ábralo manualmente en `http://127.0.0.1:8080`; revise la ventana de consola de la app por errores |

---

## 10. Licencia y mantenimiento

Propiedad intelectual de la División de Servicios Administrativos (DSA) del Hospital Civil de
Guadalajara. Uso interno restringido — prohibida su divulgación o implementación externa.
