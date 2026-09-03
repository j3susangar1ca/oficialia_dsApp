"""
tests/test_pipeline.py — Integración: ingesta completa con extractor IA
con doble (fake), sin red ni credenciales reales. Cubre el camino de
respaldo heurístico (core.heuristic_extractor) que core.pipeline invoca
cuando la IA falla.
"""

from __future__ import annotations

import pymupdf
import pytest

from core.ai_extractor import ErrorExtraccionIA
from core.models import EstadoDocumento, MetodoExtraccion, OrigenIngesta
from core.pipeline import FlujoDocumental


def _pdf_con_oficio() -> bytes:
    doc = pymupdf.open()
    pagina = doc.new_page()
    pagina.insert_text((72, 72), "OFICIO No. DSA-2026-777-OF", fontsize=14)
    pagina.insert_text((72, 100), "15 de agosto de 2026", fontsize=11)
    buffer = doc.tobytes()
    doc.close()
    return buffer


def _pdf_en_blanco() -> bytes:
    doc = pymupdf.open()
    doc.new_page()
    buffer = doc.tobytes()
    doc.close()
    return buffer


class _ExtractorFalso:
    """Doble de ExtractorMetadatos: simula éxito o un código de error dado."""

    def __init__(self, codigo_error: str | None = None):
        self.codigo_error = codigo_error

    def extraer_de_paginas(self, *args, **kwargs):
        if self.codigo_error:
            raise ErrorExtraccionIA(self.codigo_error, f"Fallo simulado ({self.codigo_error})")
        raise AssertionError("Este doble no simula el camino de éxito de la IA")


@pytest.fixture
def flujo(repositorio, gestor_archivos, configuracion):
    def _crear(extractor) -> FlujoDocumental:
        return FlujoDocumental(
            repositorio=repositorio,
            archivos=gestor_archivos,
            extractor=extractor,
            rpa=None,
            sincronizador_sheets=None,
            configuracion=configuracion,
        )
    return _crear


class TestRespaldoHeuristico:
    @pytest.mark.parametrize("codigo", [
        "AI_NO_CONFIGURADA", "AI_SERVICIO_NO_DISPONIBLE", "AI_CLIENTE_INVALIDO",
        "JSON_MALFORMADO", "SCHEMA_INVALIDO", "DOCUMENTO_ILEGIBLE_O_VACIO",
    ])
    def test_falla_de_ia_activa_la_heuristica_y_llega_a_revision(self, flujo, codigo):
        """
        Documento con texto reconocible: si la IA falla por cualquier causa
        "de disponibilidad/contrato" (no un bloqueo de seguridad), el
        pipeline debe rescatar folio/fecha por regex y dejar el documento
        en PENDIENTE_REVISION — nunca perderlo en la cuarentena de errores.
        """
        pipeline = flujo(_ExtractorFalso(codigo))
        registro = pipeline.ingestar_y_procesar("oficio.pdf", OrigenIngesta.WEB_DRAG_DROP, _pdf_con_oficio())

        assert registro.estado == EstadoDocumento.PENDIENTE_REVISION
        assert registro.extraccion_metodo == MetodoExtraccion.HEURISTICA_FALLBACK
        assert registro.numero_oficio == "DSA-2026-777-OF"
        assert registro.metadatos_extraidos.fecha_emision == "2026-08-15"

    def test_contenido_bloqueado_no_intenta_heuristica(self, flujo):
        """Un bloqueo de seguridad del proveedor NO se sortea: va directo a DESCARTADO."""
        pipeline = flujo(_ExtractorFalso("CONTENIDO_BLOQUEADO_SEGURIDAD"))
        registro = pipeline.ingestar_y_procesar("oficio.pdf", OrigenIngesta.WEB_DRAG_DROP, _pdf_con_oficio())

        assert registro.estado == EstadoDocumento.DESCARTADO
        assert "CONTENIDO_BLOQUEADO_SEGURIDAD" in registro.error_msg

    def test_sin_texto_disponible_igual_llega_a_revision_con_placeholders(self, flujo):
        """
        Documento sin capa de texto ni OCR (fax/escaneo puro, Tesseract no
        instalado): la heurística no encuentra nada, pero AÚN ASÍ produce
        un MetadatosOficio con placeholders — el documento sigue llegando a
        PENDIENTE_REVISION en vez de perderse en 04_errores.
        """
        pipeline = flujo(_ExtractorFalso("AI_NO_CONFIGURADA"))
        registro = pipeline.ingestar_y_procesar("oficio.pdf", OrigenIngesta.WEB_DRAG_DROP, _pdf_en_blanco())

        assert registro.estado == EstadoDocumento.PENDIENTE_REVISION
        assert registro.extraccion_metodo == MetodoExtraccion.HEURISTICA_FALLBACK
        assert registro.numero_oficio == "S/N"
