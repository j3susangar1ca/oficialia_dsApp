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

Post-procesado de imagen (siempre activo, sin variables de configuración):
    - `_mejorar_imagen` aplica autocontraste y un realce de nitidez suave a
      cada render antes de enviarlo al modelo multimodal. Se preserva el
      color deliberadamente: el prompt institucional distingue elementos
      por color (sello de recibido, tinta de firma frente a texto impreso)
      y una conversión a escala de grises degradaría esa señal en vez de
      mejorarla. Cualquier fallo del post-procesado se absorbe devolviendo
      el PNG original sin modificar — nunca debe abortar la ingesta.
    - `extraer_texto_ocr` produce una referencia textual auxiliar vía
      Tesseract (si está disponible) para complementar la lectura del
      modelo; se degrada con gracia página por página si el binario o la
      dependencia no están instalados, sin interrumpir el pipeline.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from io import BytesIO

import pymupdf

from core.models import DimensionPagina, InfoPreproceso

logger = logging.getLogger("oficialia.pdf")


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


def _mejorar_imagen(png_bytes: bytes) -> bytes:
    """
    Aumenta el contraste local (autocontraste, recortando el 1% de outliers
    por canal) y aplica un realce de nitidez moderado (Unsharp Mask) para
    favorecer la lectura OCR del modelo multimodal ante sellos tenues,
    fotocopias o digitalizaciones de fax de baja calidad.

    Preserva el modo de color original (nunca convierte a escala de grises):
    el protocolo institucional discrimina campos por color (p. ej. sello de
    recibido frente a tinta de firma), y perder esa señal sería contrario
    al objetivo de mejorar la extracción. Ante cualquier fallo del
    post-procesado (Pillow ausente, imagen corrupta) se degrada con gracia
    devolviendo el PNG original — nunca debe abortar la ingesta.
    """
    try:
        from PIL import Image, ImageFilter, ImageOps
    except ImportError:
        logger.warning("Pillow no está instalado: se omite el realce de imagen")
        return png_bytes

    try:
        imagen = Image.open(BytesIO(png_bytes))
        modo_original = imagen.mode
        realzada = ImageOps.autocontrast(imagen.convert("RGB"), cutoff=1)
        realzada = realzada.filter(ImageFilter.UnsharpMask(radius=1.5, percent=60, threshold=3))
        if modo_original != "RGB":
            realzada = realzada.convert(modo_original)
        salida = BytesIO()
        realzada.save(salida, format="PNG")
        return salida.getvalue()
    except Exception as exc:  # noqa: BLE001 — el realce es una mejora, nunca un requisito
        logger.warning("Realce de imagen omitido por error de post-procesado: %s", exc)
        return png_bytes


def renderizar_paginas(
    buffer: bytes, *, dpi: int = 300, max_paginas: int = 10
) -> list[PaginaRenderizada]:
    """
    Renderiza hasta `max_paginas` páginas a PNG de alta resolución y aplica
    siempre el realce de `_mejorar_imagen` antes de entregarlas a la IA.

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
                png=_mejorar_imagen(
                    doc.load_page(indice).get_pixmap(matrix=matriz, alpha=False).tobytes("png")
                ),
            )
            for indice in range(limite)
        ]
    finally:
        doc.close()


def extraer_texto_capa(buffer: bytes, *, max_paginas: int = 10) -> dict[int, str]:
    """
    Extrae la capa de texto EMBEBIDA del PDF (sin OCR, sin dependencias
    adicionales): instantáneo y gratis para documentos "nacidos digitales"
    (ej. un oficio exportado desde Word), pero vacío para PDFs de solo
    imagen (fax, escaneo del ADF sin capa de texto) — en ese caso el
    llamador debe recurrir a `extraer_texto_ocr` si necesita texto igual.
    Nunca lanza: una página cuya extracción falle se omite del resultado.
    """
    doc = _abrir_pdf(buffer)
    try:
        textos: dict[int, str] = {}
        for indice in range(min(doc.page_count, max_paginas)):
            try:
                texto = doc.load_page(indice).get_text().strip()
                if texto:
                    textos[indice + 1] = texto
            except Exception as exc:  # noqa: BLE001 — página corrupta puntual
                logger.warning("Extracción de texto embebido omitida en página %d: %s", indice + 1, exc)
        return textos
    finally:
        doc.close()


def extraer_texto_ocr(
    buffer: bytes, *, dpi: int = 200, max_paginas: int = 10, idioma: str = "spa"
) -> dict[int, str]:
    """
    Extrae texto por página mediante OCR (Tesseract) como referencia textual
    auxiliar para el modelo multimodal. Nunca interrumpe el pipeline: ante
    cualquier fallo (dependencia `pytesseract` ausente, binario `tesseract`
    no instalado, página ilegible) registra advertencia y continúa,
    devolviendo únicamente lo que pudo leerse ({} si no pudo leer nada).

    :param dpi: resolución del render intermedio para el OCR (menor que la
        de `renderizar_paginas`: Tesseract no necesita 300 dpi para operar
        y esto evita duplicar el costo de render en cada ingesta).
    """
    try:
        import pytesseract
    except ImportError:
        logger.warning("pytesseract no está instalado: se omite el OCR auxiliar")
        return {}

    try:
        from PIL import Image
    except ImportError:
        logger.warning("Pillow no está instalado: se omite el OCR auxiliar")
        return {}

    doc = _abrir_pdf(buffer)
    try:
        matriz = pymupdf.Matrix(dpi / 72, dpi / 72)
        textos: dict[int, str] = {}
        for indice in range(min(doc.page_count, max_paginas)):
            try:
                pix = doc.load_page(indice).get_pixmap(matrix=matriz, alpha=False)
                imagen = Image.open(BytesIO(pix.tobytes("png")))
                texto = pytesseract.image_to_string(imagen, lang=idioma).strip()
                if texto:
                    textos[indice + 1] = texto
            except Exception as exc:  # noqa: BLE001 — binario ausente / página corrupta
                logger.warning("OCR omitido en página %d: %s", indice + 1, exc)
        return textos
    finally:
        doc.close()
