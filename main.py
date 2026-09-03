"""
SISTEMA OFICIALIA-DIGITAL-DSA (reconstrucción 100% Python)
==========================================================
main.py — Punto de entrada único del sistema.

    python main.py

Arranca en un solo proceso (sin npm, sin Docker, sin terminales paralelas):

    1. Configuración central (config.py + .env) y logging.
    2. Estructura de storage + SQLite (WAL) inicializados.
    3. Composición del pipeline: repositorio, archivos, extractor Gemini,
       RPA (simulación/playwright) y sincronizador Sheets.
    4. Vigilante de carpeta (watchdog sobre storage/01_entrada).
    5. Rutas de archivos para el visor HITL (/pdf/{id}, /evidencia/{id}).
    6. Interfaz NiceGUI (bandeja + split-screen) y servidor uvicorn.

Todos los servicios de fondo corren como hilos demonio: cerrar la consola
detiene el sistema completo de forma limpia.
"""

from __future__ import annotations

import logging
from pathlib import Path

from nicegui import app, ui

# ----------------------------------------------------------------------
# 1) Logging y configuración
# ----------------------------------------------------------------------
from config import DATOS_DIR, EMPAQUETADO, get_settings  # noqa: E402
from core.logging_setup import configurar_logging  # noqa: E402

configurar_logging(DATOS_DIR)
logger = logging.getLogger("oficialia.main")

from core.ai_extractor import ExtractorMetadatos  # noqa: E402
from core.file_manager import GestorArchivos  # noqa: E402
from core.models import DocumentoRegistro, EstadoDocumento, ResultadoRpa  # noqa: E402
from core.pipeline import FlujoDocumental  # noqa: E402
from core.sheets_sync import SincronizadorSheets  # noqa: E402
from core.watcher import VigilanteCarpetas  # noqa: E402
from database import iniciar_bd  # noqa: E402
from rpa.playwright_rpa import crear_rpa  # noqa: E402
from ui.layout import montar_contexto  # noqa: E402

configuracion = get_settings()


def _componer_pipeline() -> FlujoDocumental:
    """Composition root: instancia los colaboradores y ensambla el flujo."""
    configuracion.storage_root.mkdir(parents=True, exist_ok=True)

    repositorio = iniciar_bd()
    archivos = GestorArchivos(configuracion.storage_root)
    archivos.asegurar_estructura()

    extractor = ExtractorMetadatos(
        api_key=configuracion.gemini_api_key,
        modelo=configuracion.gemini_modelo,
        timeout_ms=configuracion.gemini_timeout_ms,
        reintentos=configuracion.gemini_reintentos,
    )
    worker_rpa = crear_rpa(configuracion)
    sincronizador = SincronizadorSheets(configuracion)

    return FlujoDocumental(
        repositorio=repositorio,
        archivos=archivos,
        extractor=extractor,
        rpa=worker_rpa,
        sincronizador_sheets=sincronizador,
        configuracion=configuracion,
    )


pipeline = _componer_pipeline()

# ----------------------------------------------------------------------
# 2) Vigilante de carpetas (canal SCANNER_ADF)
# ----------------------------------------------------------------------
vigilante = VigilanteCarpetas(configuracion, pipeline)


# ----------------------------------------------------------------------
# 3) Rutas de archivos para el visor HITL
# ----------------------------------------------------------------------
def _registrar_rutas_archivos() -> None:
    """Sirve el PDF vigente del documento y la evidencia del acuse."""
    from fastapi.responses import FileResponse
    from fastapi import HTTPException

    @app.get("/pdf/{doc_id}")
    def servir_pdf(doc_id: str):
        documento = pipeline.repo.obtener(doc_id)
        if documento is None:
            raise HTTPException(status_code=404, detail="Documento no encontrado")
        ruta = (configuracion.storage_root / documento.ruta_archivo_actual).resolve()
        if not ruta.is_file():
            raise HTTPException(status_code=404, detail="Archivo físico no disponible")
        return FileResponse(
            ruta, media_type="application/pdf", filename=documento.nombre_archivo_original
        )

    @app.get("/evidencia/{doc_id}")
    def servir_evidencia(doc_id: str):
        documento = pipeline.repo.obtener(doc_id)
        if documento is None or documento.rpa is None or not documento.rpa.captura_acuse_path:
            raise HTTPException(status_code=404, detail="Evidencia no disponible")
        ruta = (configuracion.storage_root / documento.rpa.captura_acuse_path).resolve()
        if not ruta.is_file():
            raise HTTPException(status_code=404, detail="Evidencia no disponible")
        return FileResponse(ruta, media_type="image/png")


_registrar_rutas_archivos()

# ----------------------------------------------------------------------
# 4) Interfaz (el registro de páginas ocurre al importar los módulos)
# ----------------------------------------------------------------------
montar_contexto(pipeline, configuracion)

from ui import views_dashboard, views_hitl  # noqa: E402,F401 — registran las páginas

# ----------------------------------------------------------------------
# 5) Arranque
# ----------------------------------------------------------------------
@app.on_startup
async def _arrancar_servicios_fondo() -> None:
    vigilante.iniciar()
    for linea in configuracion.resumen_arranque():
        logger.info("%s", linea)
    logger.info("Interfaz lista en http://%s:%d", "localhost", configuracion.app_port)

    if EMPAQUETADO:
        # Ejecutable instalado (ver packaging/): no hay consola visible para
        # el usuario final, así que se abre el navegador automáticamente en
        # vez de esperar a que alguien escriba la URL a mano.
        import webbrowser

        webbrowser.open(f"http://127.0.0.1:{configuracion.app_port}/")


@app.on_shutdown
async def _detener_servicios_fondo() -> None:
    vigilante.detener()
    pipeline.cerrar()
    logger.info("Servicios de fondo detenidos; hasta pronto.")


ui.run(
    host=configuracion.app_host,
    port=configuracion.app_port,
    title=configuracion.app_titulo,
    reload=False,          # imprescindible con hilos de fondo
    show=False,
    favicon="🏥",
    dark=False,
    # Localiza los textos incorporados de Quasar (paginación de ui.table,
    # selectores de fecha, diálogos…) al español — antes el paginador de la
    # bandeja mostraba "Records per page:" / "1-3 of 3" pese a que el resto
    # de la interfaz está en español.
    language="es",
)
