"""
tests/test_pdf_engine.py — Preprocesamiento PyMuPDF: validación, hash,
sanitización, render y extracción de texto embebido.
"""

from __future__ import annotations

import pymupdf
import pytest

from core.pdf_engine import (
    ErrorPdf,
    calcular_sha256,
    extraer_texto_capa,
    inspeccionar_y_sanitizar,
    renderizar_paginas,
)


def _pdf_con_texto(*lineas: str) -> bytes:
    doc = pymupdf.open()
    pagina = doc.new_page()
    for i, linea in enumerate(lineas):
        pagina.insert_text((72, 72 + i * 24), linea, fontsize=12)
    buffer = doc.tobytes()
    doc.close()
    return buffer


def _pdf_en_blanco(paginas: int = 1) -> bytes:
    doc = pymupdf.open()
    for _ in range(paginas):
        doc.new_page()
    buffer = doc.tobytes()
    doc.close()
    return buffer


class TestValidacionEstructura:
    def test_cabecera_invalida_se_rechaza(self):
        with pytest.raises(ErrorPdf) as exc:
            inspeccionar_y_sanitizar(b"esto no es un PDF")
        assert exc.value.codigo == "INVALID_PDF_HEADER"

    def test_pdf_valido_se_acepta(self):
        buffer = _pdf_en_blanco()
        _, info = inspeccionar_y_sanitizar(buffer)
        assert info.num_paginas == 1
        assert info.sanitizado is True

    def test_pdf_truncado_se_rechaza(self):
        """
        PyMuPDF se niega a serializar un documento de 0 páginas (no se puede
        construir ese buffer "legítimamente"), así que este caso simula el
        escenario real más probable de estructura corrupta: una descarga o
        escritura a disco cortada a la mitad.
        """
        buffer = _pdf_con_texto("contenido")
        with pytest.raises(ErrorPdf) as exc:
            inspeccionar_y_sanitizar(buffer[: len(buffer) // 2])
        assert exc.value.codigo == "CORRUPTED_PDF_STRUCTURE"


class TestHash:
    def test_hash_es_determinista(self):
        buffer = _pdf_en_blanco()
        assert calcular_sha256(buffer) == calcular_sha256(buffer)

    def test_hash_cambia_con_el_contenido(self):
        assert calcular_sha256(_pdf_en_blanco(1)) != calcular_sha256(_pdf_en_blanco(2))


class TestRenderizado:
    def test_render_produce_png_por_pagina(self):
        buffer = _pdf_con_texto("Hola mundo")
        paginas = renderizar_paginas(buffer, dpi=100, max_paginas=5)
        assert len(paginas) == 1
        assert paginas[0].png[:8] == b"\x89PNG\r\n\x1a\n"
        assert paginas[0].mime == "image/png"

    def test_render_respeta_max_paginas(self):
        buffer = _pdf_en_blanco(paginas=5)
        paginas = renderizar_paginas(buffer, dpi=72, max_paginas=2)
        assert len(paginas) == 2


class TestTextoCapa:
    def test_extrae_texto_de_pdf_nacido_digital(self):
        buffer = _pdf_con_texto("OFICIO No. DSA-2026-089-OF", "15 de agosto de 2026")
        textos = extraer_texto_capa(buffer)
        assert 1 in textos
        assert "DSA-2026-089-OF" in textos[1]

    def test_pdf_sin_capa_de_texto_devuelve_vacio(self):
        buffer = _pdf_en_blanco()
        assert extraer_texto_capa(buffer) == {}
