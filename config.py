"""
SISTEMA OFICIALIA-DIGITAL-DSA (reconstrucción 100% Python)
==========================================================
config.py — Configuración centralizada de toda la aplicación.

Carga las variables desde un archivo `.env` mediante `pydantic-settings`.
Ningún otro módulo lee variables de entorno directamente: todos importan
`get_settings()`.

Diseño:
    - Todos los valores tienen un default seguro para desarrollo local
      (RPA y Google Sheets arrancan en modo simulación/stub sin credenciales).
    - `extra='ignore'` tolera variables ajenas en el .env (p. ej. las del
      entorno institucional del servidor).

Ejecutable empaquetado (instalador Windows, ver packaging/):
    Cuando el proceso corre congelado por PyInstaller (`sys.frozen`), el
    código vive en una carpeta de solo lectura (típicamente
    `Archivos de programa\OficialiaDigitalDSA`, instalada por el usuario
    estándar sin privilegios de escritura). En ese caso las rutas de datos
    (.env, storage/, data/) se redirigen a `%ProgramData%\OficialiaDigitalDSA`
    — carpeta de datos por máquina que el instalador crea con permisos de
    escritura para usuarios estándar — en vez de junto al ejecutable. En
    modo desarrollo (`python main.py`) el comportamiento no cambia: todo
    sigue relativo a la carpeta del proyecto, como antes.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger("oficialia.config")

#: Raíz del código y recursos empaquetados (solo lectura una vez instalado).
BASE_DIR: Path = Path(__file__).resolve().parent

#: True dentro del ejecutable generado por PyInstaller (ver packaging/oficialia.spec).
EMPAQUETADO: bool = bool(getattr(sys, "frozen", False))

#: Carpeta de recursos de solo lectura empaquetados junto al código
#: (con PyInstaller onedir, `sys._MEIPASS` apunta a esa carpeta; en
#: desarrollo coincide con BASE_DIR). Aquí vive `.env.example`.
_RECURSOS_DIR: Path = Path(getattr(sys, "_MEIPASS", BASE_DIR))

#: Carpeta del ejecutable instalado (donde el instalador coloca `pw-browsers/`).
_DIR_EJECUTABLE: Path = Path(sys.executable).resolve().parent if EMPAQUETADO else BASE_DIR


def _directorio_datos_predeterminado() -> Path:
    """
    Raíz de datos (.env, storage/, data/): junto al proyecto en modo
    desarrollo; en el ejecutable empaquetado se usa una carpeta de datos
    por máquina en %ProgramData% (creada con permisos de escritura por el
    instalador), con reserva a %LOCALAPPDATA% si no fuese escribible.
    """
    if not EMPAQUETADO:
        return BASE_DIR
    for variable in ("PROGRAMDATA", "LOCALAPPDATA"):
        base = os.environ.get(variable)
        if not base:
            continue
        candidato = Path(base) / "OficialiaDigitalDSA"
        try:
            candidato.mkdir(parents=True, exist_ok=True)
            sonda = candidato / ".escritura_ok"
            sonda.write_text("ok", encoding="utf-8")
            sonda.unlink()
            return candidato
        except OSError:
            continue
    # Último recurso (no debería alcanzarse en una instalación Windows normal).
    return _DIR_EJECUTABLE


#: Carpeta de datos de lectura/escritura (.env, storage/, data/).
DATOS_DIR: Path = _directorio_datos_predeterminado()


def _sembrar_env_inicial() -> None:
    """
    Primera ejecución del empaquetado: si no existe un `.env` en la carpeta
    de datos, se copia la plantilla `.env.example` empaquetada para que el
    usuario tenga un archivo a la mano donde capturar GEMINI_API_KEY y
    credenciales de RPA/Sheets — la aplicación funciona igual sin él (modo
    simulación, sin IA), pero así queda un archivo descubrible en vez de
    exigir que el usuario lo cree a mano. Nunca sobrescribe uno existente.
    """
    if not EMPAQUETADO:
        return
    destino = DATOS_DIR / ".env"
    origen = _RECURSOS_DIR / ".env.example"
    if destino.exists() or not origen.is_file():
        return
    try:
        shutil.copyfile(origen, destino)
        logger.info("Plantilla de configuración creada en %s", destino)
    except OSError:
        logger.warning("No se pudo crear %s a partir de la plantilla empaquetada", destino, exc_info=True)


def _preparar_navegador_playwright_empaquetado() -> None:
    """
    El instalador coloca el Chromium de Playwright en `pw-browsers/`, junto
    al ejecutable (fuera del bundle de PyInstaller, para no duplicar ~300 MB
    en cada rebuild). Si existe, se apunta PLAYWRIGHT_BROWSERS_PATH ahí antes
    de que rpa/playwright_rpa.py importe `playwright.sync_api` — así el modo
    RPA_MODO=playwright funciona sin que el usuario ejecute
    `playwright install`. Si el componente no fue instalado (opcional en el
    instalador), Playwright simplemente reportará el navegador ausente al
    intentar usarlo — el resto de la aplicación no se ve afectado.
    """
    if not EMPAQUETADO:
        return
    navegadores = _DIR_EJECUTABLE / "pw-browsers"
    if navegadores.is_dir():
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(navegadores))


_sembrar_env_inicial()
_preparar_navegador_playwright_empaquetado()


class Configuracion(BaseSettings):
    """Parámetros operativos del sistema (fuente única de verdad)."""

    model_config = SettingsConfigDict(
        env_file=DATOS_DIR / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ------------------------------------------------------------------
    # Aplicación / Interfaz NiceGUI
    # ------------------------------------------------------------------
    app_host: str = "0.0.0.0"          # LAN hospitalaria (prd: estrictamente local)
    app_port: int = 8080                # Puerto de la interfaz web
    app_titulo: str = "Oficialía Digital DSA"
    max_upload_bytes: int = 25 * 1024 * 1024   # 25 MB por oficio (límite original)

    # ------------------------------------------------------------------
    # Persistencia (SQLite en modo WAL + rutas de storage)
    # ------------------------------------------------------------------
    database_path: Path = DATOS_DIR / "data" / "oficialia.db"
    storage_root: Path = DATOS_DIR / "storage"

    # ------------------------------------------------------------------
    # Extracción IA — Gemini 2.5 Flash (SDK oficial google-genai)
    # ------------------------------------------------------------------
    gemini_api_key: str = ""            # Vacío ⇒ la extracción fallará de forma honesta
    gemini_modelo: str = "gemini-2.5-flash"
    gemini_timeout_ms: int = 45_000     # Límite de espera de la inferencia
    gemini_reintentos: int = 2          # Reintentos ante 429/5xx/timeout de red
    render_dpi: int = 300               # Resolución de renderizado de páginas
    render_max_paginas: int = 10        # Máximo de páginas enviadas al modelo

    # ------------------------------------------------------------------
    # Vigilancia de carpetas (watchdog sobre storage/01_entrada)
    # ------------------------------------------------------------------
    watchfolder_enabled: bool = True
    watchfolder_intervalo_ms: int = 5_000     # Poll de respaldo (red de seguridad)
    watchfolder_estabilidad_ms: int = 4_000   # Tiempo sin cambios para dar por "estable" un archivo
    watchfolder_max_reintentos: int = 5       # Reintentos ante fallos inesperados

    # ------------------------------------------------------------------
    # RPA — Intranet Webix (op_cucs.fwx / op_ningr.fwx)
    # ------------------------------------------------------------------
    rpa_modo: str = "simulacion"        # 'simulacion' (default seguro) | 'playwright' (real)
    rpa_headless: bool = False          # false = navegador VISIBLE para depuración
    rpa_timeout_ms: int = 90_000        # Timeout por acción de Playwright
    rpa_reintentos: int = 3             # Reintentos ante errores transitorios
    rpa_simulacion_fallar: bool = False # true = el RPA simulado falla (probar ERROR_RPA)
    intranet_base_url: str = "https://sii.hcg.gob.mx/intranet/op_cucs.fwx"
    intranet_http_username: str = ""    # Credenciales HTTP Basic de la Intranet
    intranet_http_password: str = ""
    rpa_oficialia_cve: str = ""         # CVE de oficialía precargada (combo Webix 'cve')
    rpa_hcg_dependencia_cve: str = ""   # CVE de dependencia cuando procedencia = HCG
    rpa_seccion_cve: str = ""           # CVE de sección (campo opcional 'seccion')
    rpa_navegador: str = "auto"         # 'auto' (Edge del sistema, respaldo Chromium empaquetado)
                                         # | 'msedge' | 'chromium' — ver rpa/playwright_rpa.py

    # -- Tolerancia a demoras de red / reintentos / sesión (worker Playwright) --
    rpa_selector_timeout_ms: int = 15_000   # Timeout individual por selector/control Webix
    rpa_webix_init_timeout_ms: int = 20_000 # Timeout de carga del framework Webix en el iframe
    rpa_reintento_base_ms: int = 1_000      # Base del backoff exponencial entre intentos
    rpa_reintento_max_ms: int = 30_000      # Techo del backoff exponencial
    rpa_session_ttl_min: int = 30           # Minutos de vida útil del storage_state persistido
    rpa_jitter_factor: float = 0.25         # Amplitud del jitter del backoff (±25% por defecto)

    # ------------------------------------------------------------------
    # Sincronización externa — Google Sheets (cuenta de servicio)
    # ------------------------------------------------------------------
    google_sheets_spreadsheet_id: str = ""   # Vacío ⇒ stub local (CSV espejo)
    google_sheets_sheet_name: str = "Hoja1"  # Nombre de la pestaña destino
    google_service_account_json: str = ""    # JSON de la Service Account en UNA línea

    # ------------------------------------------------------------------
    # Validaciones
    # ------------------------------------------------------------------
    @field_validator("rpa_modo")
    @classmethod
    def _validar_rpa_modo(cls, valor: str) -> str:
        valor = valor.strip().lower()
        if valor not in {"simulacion", "playwright"}:
            raise ValueError("RPA_MODO debe ser 'simulacion' o 'playwright'")
        return valor

    @field_validator("rpa_jitter_factor")
    @classmethod
    def _validar_jitter(cls, valor: float) -> float:
        if not 0.0 <= valor <= 1.0:
            raise ValueError("RPA_JITTER_FACTOR debe estar entre 0.0 y 1.0")
        return valor

    @field_validator("rpa_navegador")
    @classmethod
    def _validar_rpa_navegador(cls, valor: str) -> str:
        valor = valor.strip().lower()
        if valor not in {"auto", "msedge", "chromium"}:
            raise ValueError("RPA_NAVEGADOR debe ser 'auto', 'msedge' o 'chromium'")
        return valor

    # ------------------------------------------------------------------
    # Propiedades derivadas (rutas de trabajo del ciclo de vida físico)
    # ------------------------------------------------------------------
    @property
    def dir_entrada(self) -> Path:
        """Watchfolder del escáner ADF / arrastrar-y-soltar."""
        return self.storage_root / "01_entrada"

    @property
    def dir_en_proceso(self) -> Path:
        """Documentos bloqueados durante el preprocesamiento."""
        return self.storage_root / "02_en_proceso"

    @property
    def dir_procesados(self) -> Path:
        """PDFs canónicos + JSON espejo + capturas de acuse."""
        return self.storage_root / "03_procesados"

    @property
    def dir_errores(self) -> Path:
        """Archivos en cuarentena con su correspondiente .error.txt."""
        return self.storage_root / "04_errores"

    @property
    def rpa_es_simulacion(self) -> bool:
        """True cuando NO se lanzará un navegador real."""
        return self.rpa_modo == "simulacion"

    @property
    def sheets_configurado(self) -> bool:
        """True cuando hay hoja destino Y credenciales de Service Account."""
        return bool(self.google_sheets_spreadsheet_id.strip())

    def resumen_arranque(self) -> list[str]:
        """Líneas de diagnóstico que se imprimen al iniciar (transparencia operativa)."""
        lineas = [
            f"Modo           : {'Ejecutable instalado (Windows)' if EMPAQUETADO else 'Fuente (python main.py)'}",
            f"Logs           : {DATOS_DIR / 'logs' / 'app.log'} (rotativo, 10 MB x 5 respaldos)",
            f"Almacenamiento : {self.storage_root}",
            f"Base de datos  : {self.database_path}",
            f"Extracción IA  : {'Gemini ' + self.gemini_modelo if self.gemini_api_key else 'SIN GEMINI_API_KEY (la extracción fallará y quedará registrado el error)'}",
            f"RPA            : {'SIMULACIÓN (sin navegador real)' if self.rpa_es_simulacion else 'Playwright real → ' + self.intranet_base_url}",
        ]
        if self.rpa_es_simulacion:
            lineas.append("                 ⚠ modo seguro para pruebas locales — configure RPA_MODO=playwright para producción")
        if not self.rpa_headless and not self.rpa_es_simulacion:
            lineas.append("                 (navegador VISIBLE: RPA_HEADLESS=false)")
        if not self.rpa_es_simulacion:
            lineas.append(
                f"                 sesión persistida hasta {self.rpa_session_ttl_min} min "
                f"en {self.storage_root / '.rpa_sessions'}"
            )
        lineas.append(
            f"Google Sheets  : {'Service Account activa' if self.sheets_configurado else 'STUB LOCAL (CSV espejo en data/) — sin credenciales'}"
        )
        lineas.append(f"Vigilante      : {'activo sobre ' + str(self.dir_entrada) if self.watchfolder_enabled else 'desactivado'}")
        return lineas


@lru_cache
def get_settings() -> Configuracion:
    """Devuelve la configuración única (singleton) del proceso."""
    return Configuracion()  # type: ignore[call-arg]
