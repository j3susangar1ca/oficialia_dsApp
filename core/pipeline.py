"""
SISTEMA OFICIALIA-DIGITAL-DSA (reconstrucción 100% Python)
==========================================================
core/pipeline.py — Orquestador del flujo de trabajo documental.

Equivalente funcional del `DocumentWorkflowOrchestrator` original, sin la
ceremonia de inyección de dependencias hexagonal: los colaboradores concretos
(repositorio, gestor de archivos, extractor IA, RPA y Sheets) se componen
directamente. Coordina el ciclo de vida completo:

    INGESTADO → EN_PREPROCESO → EXTRAYENDO → PENDIENTE_REVISION
        ├─ [Confirmar y Registrar] → EJECUTANDO_RPA → COMPLETADO / ERROR_RPA
        └─ [Descartar]             → DESCARTADO (archivo aislado en 04_errores)

Garantías operativas heredadas:
    - Deduplicación atómica por SHA-256 antes de crear el registro.
    - Fallos de preproceso/extracción: registro persistido con error,
      archivo aislado en 04_errores y motivo anexado (.error.txt).
    - Confirmación HITL: nomenclatura canónica, JSON espejo en
      03_procesados/YYYY/MM/ y verificación de integridad post-escritura.
    - RPA y Google Sheets se ejecutan en segundo plano y NUNCA bloquean
      la confirmación; un fallo de Sheets no revierte el COMPLETADO.
    - 'Reintentar RPA' reinyecta el documento en ERROR_RPA sin reextraer.

Ejecución asíncrona:
    - `ejecutor_ingesta` (2 hilos): preproceso + extracción IA.
    - `ejecutor_salida` (1 hilo): sesiones RPA serializadas (un navegador
      a la vez, como en el worker Playwright original).
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Optional

from config import Configuracion, get_settings
from core.ai_extractor import ErrorExtraccionIA, ExtractorMetadatos
from core.file_manager import GestorArchivos
from core.models import (
    DocumentoRegistro,
    EstadoDocumento,
    EstadoSheets,
    MetadatosOficio,
    OrigenIngesta,
    ResultadoRpa,
)
from core.pdf_engine import ErrorPdf, calcular_sha256, inspeccionar_y_sanitizar, renderizar_paginas
from database import RepositorioDocumentos

logger = logging.getLogger("oficialia.pipeline")


class DocumentoDuplicado(Exception):
    """El SHA-256 ya existe en la base (deduplicación atómica)."""

    def __init__(self, existente: DocumentoRegistro, sha256: str) -> None:
        self.existente = existente
        self.sha256 = sha256
        super().__init__(f"Documento duplicado detectado con hash {sha256} (id {existente.id})")


class FlujoDocumental:
    """Casos de uso del sistema: ingesta, confirmación, descarte y reintentos."""

    def __init__(
        self,
        repositorio: RepositorioDocumentos,
        archivos: GestorArchivos,
        extractor: ExtractorMetadatos,
        rpa,
        sincronizador_sheets,
        configuracion: Optional[Configuracion] = None,
    ) -> None:
        self.repo = repositorio
        self.archivos = archivos
        self.extractor = extractor
        self.rpa = rpa
        self.sheets = sincronizador_sheets
        self.config = configuracion or get_settings()

        self.ejecutor_ingesta = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="oficialia-ingesta"
        )
        self.ejecutor_salida = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="oficialia-salida"
        )

    # ------------------------------------------------------------------
    # Flujo 1 — Ingesta, preprocesamiento y extracción IA
    # ------------------------------------------------------------------
    def programar_ingesta(self, nombre: str, origen: OrigenIngesta, contenido: bytes) -> None:
        """Encola la ingesta completa en segundo plano (fire-and-forget)."""
        self.ejecutor_ingesta.submit(self.ingestar_y_procesar, nombre, origen, contenido)

    def ingestar_y_procesar(self, nombre: str, origen: OrigenIngesta, contenido: bytes) -> DocumentoRegistro:
        """
        Ejecuta el tramo INGESTADO → PENDIENTE_REVISION de forma síncrona.
        Corre SIEMPRE en un hilo de fondo (watcher o ejecutor de ingesta).
        """
        # 1) Copia de trabajo en 01_entrada/ (prefijo epoch-ms) y registro raíz.
        ruta_entrada = self.archivos.guardar_entrada(nombre, contenido)
        sha256 = calcular_sha256(contenido)

        duplicado = self.repo.obtener_por_hash(sha256)
        if duplicado is not None:
            self.archivos.mover_a_error(ruta_entrada, "DUPLICATE_HASH_DETECTED")
            logger.warning("Duplicado descartado: %s (hash %s…)", nombre, sha256[:12])
            raise DocumentoDuplicado(duplicado, sha256)

        try:
            registro = self.repo.crear(
                DocumentoRegistro(
                    id=str(uuid.uuid4()),
                    nombre_archivo_original=nombre,
                    ruta_archivo_actual=ruta_entrada,
                    origen=origen,
                    estado=EstadoDocumento.INGESTADO,
                    sha256=sha256,
                )
            )
        except sqlite3.IntegrityError:
            # Carrera de ingesta concurrente con el mismo hash (canal WEB + escáner):
            # la restricción UNIQUE actuó de barrera atómica — tratamos como duplicado.
            existente = self.repo.obtener_por_hash(sha256)
            assert existente is not None
            self.archivos.mover_a_error(ruta_entrada, "DUPLICATE_HASH_DETECTED")
            raise DocumentoDuplicado(existente, sha256) from None
        logger.info("Ingesta %s → documento %s (origen %s)", nombre, registro.id, origen.value)

        # 2) Bloqueo físico en 02_en_proceso/ + EN_PREPROCESO.
        try:
            ruta_proceso = self.archivos.mover_a_en_proceso(ruta_entrada, registro.id)
            registro = self.repo.actualizar_estado(
                registro.id, EstadoDocumento.EN_PREPROCESO, version_esperada=registro.version, nueva_ruta=ruta_proceso
            )

            # 3) Inspección de integridad y sanitización (PyMuPDF en memoria).
            buffer = self.archivos.leer(ruta_proceso)
            sanitizado, info = inspeccionar_y_sanitizar(buffer, dpi=self.config.render_dpi)
            registro = self.repo.guardar_preproceso(registro.id, info, version_esperada=registro.version)

            # 4) Render + extracción IA.
            registro = self.repo.actualizar_estado(
                registro.id, EstadoDocumento.EXTRAYENDO, version_esperada=registro.version
            )
            paginas = renderizar_paginas(
                sanitizado, dpi=self.config.render_dpi, max_paginas=self.config.render_max_paginas
            )
            metadatos = self.extractor.extraer_de_paginas(paginas, anio_contexto=datetime.now().year)

            registro = self.repo.guardar_metadatos_extraidos(
                registro.id, metadatos, EstadoDocumento.PENDIENTE_REVISION, version_esperada=registro.version
            )
            logger.info(
                "Documento %s listo para revisión (oficio %s, %d páginas)",
                registro.id, metadatos.numero_oficio, info.num_paginas,
            )
            return registro

        except (ErrorPdf, ErrorExtraccionIA) as exc:
            return self._aislar_por_error(registro, f"{exc}")

        except Exception as exc:  # noqa: BLE001 — fallo inesperado del pipeline
            logger.exception("Fallo inesperado procesando %s", registro.id)
            return self._aislar_por_error(registro, f"ERROR_INESPERADO :: {exc}")

    def _aislar_por_error(self, registro: DocumentoRegistro, motivo: str) -> DocumentoRegistro:
        """Fallo de preproceso/extracción: DESCARTADO + cuarentena + trazabilidad."""
        try:
            ruta_error = self.archivos.mover_a_error(registro.ruta_archivo_actual, motivo)
            return self.repo.actualizar_estado(
                registro.id,
                EstadoDocumento.DESCARTADO,
                version_esperada=registro.version,
                nueva_ruta=ruta_error,
                error_msg=motivo,
                finalizado=True,
            )
        except Exception:  # noqa: BLE001 — último recurso: dejar constancia en BD
            logger.exception("No se pudo aislar el archivo del documento %s", registro.id)
            return self.repo.actualizar_estado(
                registro.id,
                EstadoDocumento.DESCARTADO,
                version_esperada=registro.version,
                error_msg=f"{motivo} (además: falló el aislamiento físico)",
                finalizado=True,
            )

    # ------------------------------------------------------------------
    # Flujo 2 — Confirmación HITL + archivo canónico
    # ------------------------------------------------------------------
    def confirmar_hitl(self, doc_id: str, metadatos: MetadatosOficio, revisor: str) -> DocumentoRegistro:
        """
        Valida la confirmación humana, consolida el archivo canónico con su
        JSON espejo y encola la salida (RPA + Sheets) en segundo plano.

        :raises ValueError: si el documento no está en PENDIENTE_REVISION.
        """
        documento = self.repo.obtener(doc_id)
        if documento is None:
            raise ValueError(f"Documento no encontrado: {doc_id}")
        if documento.estado != EstadoDocumento.PENDIENTE_REVISION:
            raise ValueError(
                f"El documento no está en PENDIENTE_REVISION (estado actual: {documento.estado.value})"
            )

        # 1) Nomenclatura canónica + 03_procesados/YYYY/MM/ + JSON espejo + re-hash.
        ruta_pdf, ruta_json, sha_final = self.archivos.mover_a_canonico(
            documento.ruta_archivo_actual, metadatos
        )
        logger.info("Archivo canónico consolidado: %s (sha %s…)", ruta_pdf, sha_final[:12])

        # 2) Persistencia de la validación + transición a EJECUTANDO_RPA.
        documento = self.repo.guardar_confirmacion_hitl(
            doc_id,
            metadatos=metadatos,
            nombre_canonico=ruta_pdf.rsplit("/", 1)[-1],
            ruta_pdf=ruta_pdf,
            ruta_json=ruta_json,
            revisor=revisor,
            version_esperada=documento.version,
        )
        documento = self.repo.actualizar_estado(
            doc_id, EstadoDocumento.EJECUTANDO_RPA, version_esperada=documento.version
        )

        # 3) Pipeline de salida asíncrono (RPA serializado + Sheets no bloqueante).
        self.ejecutor_salida.submit(self._ejecutar_salida, doc_id)
        return documento

    def descartar(self, doc_id: str, motivo: str, revisor: str) -> DocumentoRegistro:
        """
        Acción [Descartar]: terminal para el ciclo del documento. El archivo
        se aísla en 04_errores con motivo auditable (nada se pierde).
        """
        documento = self.repo.obtener(doc_id)
        if documento is None:
            raise ValueError(f"Documento no encontrado: {doc_id}")
        if documento.estado in {EstadoDocumento.COMPLETADO, EstadoDocumento.DESCARTADO}:
            raise ValueError(f"El documento ya es terminal ({documento.estado.value})")

        motivo_completo = f"DESCARTADO_POR_REVISOR::{revisor} :: {motivo}".strip(" :")
        ruta_error = self.archivos.mover_a_error(documento.ruta_archivo_actual, motivo_completo)
        return self.repo.actualizar_estado(
            doc_id,
            EstadoDocumento.DESCARTADO,
            version_esperada=documento.version,
            nueva_ruta=ruta_error,
            error_msg=motivo_completo,
            finalizado=True,
        )

    # ------------------------------------------------------------------
    # Flujo 3 — Salida (RPA + Google Sheets) y reintento
    # ------------------------------------------------------------------
    def reintentar_rpa(self, doc_id: str) -> DocumentoRegistro:
        """Reinyecta en la Intranet un documento en ERROR_RPA (sin reextraer)."""
        documento = self.repo.obtener(doc_id)
        if documento is None:
            raise ValueError(f"Documento no encontrado: {doc_id}")
        if documento.estado != EstadoDocumento.ERROR_RPA:
            raise ValueError(f"El documento no está en ERROR_RPA (estado actual: {documento.estado.value})")

        documento = self.repo.actualizar_estado(
            doc_id, EstadoDocumento.EJECUTANDO_RPA, version_esperada=documento.version
        )
        self.ejecutor_salida.submit(self._ejecutar_salida, doc_id)
        return documento

    def _ejecutar_salida(self, doc_id: str) -> None:
        """Worker de salida: RPA → COMPLETADO/ERROR_RPA → Sheets (no bloqueante)."""
        documento = self.repo.obtener(doc_id)
        if documento is None or documento.metadatos_validados is None:
            logger.error("Salida omitida: documento %s sin metadatos validados", doc_id)
            return

        # A. Inyección RPA en la Intranet Webix.
        try:
            resultado = self.rpa.inyectar_documento(documento)
            estado_final = EstadoDocumento.COMPLETADO if resultado.exitoso else EstadoDocumento.ERROR_RPA
        except Exception as exc:  # noqa: BLE001 — cualquier fallo del worker
            logger.exception("RPA falló para %s", doc_id)
            resultado = ResultadoRpa(
                id_ejecucion=str(uuid.uuid4()),
                mensaje_error=f"RPA_FALLIDO :: {exc}",
                exitoso=False,
            )
            estado_final = EstadoDocumento.ERROR_RPA

        try:
            self.repo.guardar_resultado_rpa(
                doc_id, resultado, estado_final, version_esperada=documento.version
            )
        except Exception:  # noqa: BLE001 — conflicto de versión u otra incidencia de BD
            logger.exception("No se pudo persistir el resultado RPA de %s", doc_id)
            return

        if resultado.exitoso:
            logger.info("Documento %s COMPLETADO (acuse %s)", doc_id, resultado.folio_acuse)
        else:
            logger.error("Documento %s en ERROR_RPA: %s", doc_id, resultado.mensaje_error)
            return

        # B. Google Sheets: fallo controlado, jamás revierte el COMPLETADO.
        try:
            estado_sheets = self.sheets.registrar_documento(documento, resultado)
        except Exception as exc:  # noqa: BLE001
            estado_sheets = EstadoSheets(sincronizado=False, error=f"Fallo al tabular en Google Sheets: {exc}")
        try:
            self.repo.guardar_sheets(
                doc_id, estado_sheets, version_esperada=self.repo.obtener(doc_id).version  # type: ignore[union-attr]
            )
        except Exception:  # noqa: BLE001
            logger.exception("No se pudo persistir el estado Sheets de %s", doc_id)

    # ------------------------------------------------------------------
    # Ciclo de vida del proceso
    # ------------------------------------------------------------------
    def cerrar(self) -> None:
        """Apaga ordenadamente los ejecutores de fondo."""
        self.ejecutor_ingesta.shutdown(wait=False, cancel_futures=True)
        self.ejecutor_salida.shutdown(wait=False, cancel_futures=True)
