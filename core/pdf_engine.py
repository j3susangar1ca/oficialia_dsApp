"""
SISTEMA OFICIALIA-DIGITAL-DSA (reconstrucción 100% Python)
==========================================================
core/pdf_engine.py — Motor de preprocesamiento documental (PyMuPDF / fitz).

Unifica en memoria lo que el original repartía entre Node y un subproceso
CLI (`scripts/pdf_worker.py`): inspección de integridad, sanitización de
estructura, cálculo de SHA-256, conteo/dimensiones de páginas y renderizado
a PNG de alta resolución para la inferencia multimodal.

Reglas heredadas 1:1 del worker original:
    - Cabecera '%PDF-' obligatoria.
    - Rechazo de PDFs con contraseña (needs_pass) y de documentos sin páginas.
    - Sanitización: regeneración del árbol xref (garbage=3, deflate, clean).
    - Render: matriz dpi/72, sin canal alfa, PNG nativo.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass

import pymupdf

from core.models import DimensionPagina, InfoPreproceso


class ErrorPdf(Exception):
    """Fallo controlado del preprocesamiento (código + mensaje legible)."""

    def __init__(self, codigo: str, mensaje: str) -> None:
        self.codigo = codigo
        self.mensaje = mensaje
        super().__init__(f"[{codigo}] {mensaje}")


@dataclass(frozen=True)
class PaginaRenderizada:
    """Página convertida a imagen PNG en memoria, lista para Gemini."""
    numero: int          # 1-indexado (orden natural de lectura)
    png: bytes
    mime: str = "image/png"


def calcular_sha256(buffer: bytes) -> str:
    """Hash SHA-256 (hex) del buffer — deduplicación atómica de documentos."""
    return hashlib.sha256(buffer).hexdigest()


def _abrir_pdf(buffer: bytes) -> pymupdf.Document:
    """Abre el PDF en memoria validando cabecera, contraseña y estructura."""
    if len(buffer) < 5 or buffer[:5] != b"%PDF-":
        raise ErrorPdf("INVALID_PDF_HEADER", "El archivo no tiene cabecera PDF válida")

    try:
        doc = pymupdf.open(stream=buffer, filetype="pdf")
    except Exception as exc:  # noqa: BLE001 — mapeo del parser al dominio
        mensaje = str(exc).lower()
        if "password" in mensaje or "encrypted" in mensaje:
            raise ErrorPdf("PASSWORD_PROTECTED_FILE", "PDF protegido con contraseña") from exc
        raise ErrorPdf("CORRUPTED_PDF_STRUCTURE", f"Parser falló: {exc}") from exc

    if doc.needs_pass:
        doc.close()
        raise ErrorPdf("PASSWORD_PROTECTED_FILE", "PDF protegido con contraseña")
    if doc.page_count == 0:
        doc.close()
        raise ErrorPdf("CORRUPTED_PDF_STRUCTURE", "PDF sin páginas")
    return doc


def sanitizar_pdf(doc: pymupdf.Document) -> bytes:
    """Regenera el PDF limpiando el árbol xref y objetos no referenciados."""
    return doc.tobytes(garbage=3, deflate=True, clean=True)


def inspeccionar_y_sanitizar(
    buffer: bytes, *, dpi: int = 300
) -> tuple[bytes, InfoPreproceso]:
    """
    Valida el documento, calcula su hash y produce el buffer sanitizado.

    :returns: (pdf_sanitizado, métricas de preproceso para auditoría en BD)
    :raises ErrorPdf: con el código operativo del fallo.
    """
    inicio = time.perf_counter()
    doc = _abrir_pdf(buffer)
    try:
        sanitizado = sanitizar_pdf(doc)
        paginas = [
            DimensionPagina(
                numero=indice + 1,
                ancho_px=int(round(pagina.rect.width * dpi / 72)),
                alto_px=int(round(pagina.rect.height * dpi / 72)),
                dpi=dpi,
            )
            for indice, pagina in enumerate(doc)
        ]
        info = InfoPreproceso(
            num_paginas=doc.page_count,
            tamano_bytes=len(buffer),
            sha256=calcular_sha256(buffer),
            paginas=paginas,
            duracion_ms=int(round((time.perf_counter() - inicio) * 1000)),
            sanitizado=True,
        )
        return sanitizado, info
    finally:
        doc.close()


def renderizar_paginas(
    buffer: bytes, *, dpi: int = 300, max_paginas: int = 10
) -> list[PaginaRenderizada]:
    """
    Renderiza hasta `max_paginas` páginas a PNG de alta resolución.

    El límite de páginas reproduce el presupuesto del pipeline original
    (10 páginas por inferencia) para acotar el costo de tokens de la IA.
    """
    doc = _abrir_pdf(buffer)
    try:
        matriz = pymupdf.Matrix(dpi / 72, dpi / 72)
        limite = min(doc.page_count, max_paginas)
        return [
            PaginaRenderizada(
                numero=indice + 1,
                png=doc.load_page(indice).get_pixmap(matrix=matriz, alpha=False).tobytes("png"),
            )
            for indice in range(limite)
        ]
    finally:
        doc.close()
