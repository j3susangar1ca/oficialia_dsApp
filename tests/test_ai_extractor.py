"""
tests/test_ai_extractor.py — Ensamblado del turno de usuario (prompt),
sin red: `_construir_contenido` es puro dado un extractor ya instanciado
(no requiere GEMINI_API_KEY real ni llamar al proveedor).

Cubre específicamente el bloque de PISTAS HEURÍSTICAS DE PREPROCESAMIENTO
(core.heuristic_extractor.extraer_pistas → core.pipeline →
_construir_contenido): que aparezca solo cuando hay al menos un candidato,
que respete el criterio "no autoritativo" del prompt y que cada campo se
omita individualmente cuando no hay candidato para él.
"""

from __future__ import annotations

from core.ai_extractor import ExtractorMetadatos
from core.heuristic_extractor import PistaHeuristica
from core.pdf_engine import PaginaRenderizada


def _extractor() -> ExtractorMetadatos:
    # api_key/modelo no se usan en _construir_contenido (no hay red aquí).
    return ExtractorMetadatos(api_key="dummy", modelo="gemini-2.5-flash")


def _pagina() -> PaginaRenderizada:
    return PaginaRenderizada(numero=1, png=b"\x89PNG\r\n\x1a\n-fake-", mime="image/png")


def _texto_turno(contenido: list) -> str:
    """La primera parte del turno es siempre el bloque de texto de contexto."""
    return contenido[0].text


class TestBloquePistasHeuristicas:
    def test_sin_pistas_no_agrega_bloque(self):
        contenido = _extractor()._construir_contenido([_pagina()], 2026, None, None)
        texto = _texto_turno(contenido)
        assert "PISTAS HEURÍSTICAS" not in texto

    def test_pistas_vacias_no_agrega_bloque(self):
        """PistaHeuristica() sin ningún campo (hay_pistas=False) tampoco
        debe generar un bloque vacío en el prompt."""
        contenido = _extractor()._construir_contenido([_pagina()], 2026, None, PistaHeuristica())
        texto = _texto_turno(contenido)
        assert "PISTAS HEURÍSTICAS" not in texto

    def test_ambas_pistas_presentes(self):
        pistas = PistaHeuristica(numero_oficio="DSA-2026-089-OF", fecha_emision="2026-08-15")
        contenido = _extractor()._construir_contenido([_pagina()], 2026, None, pistas)
        texto = _texto_turno(contenido)
        assert "PISTAS HEURÍSTICAS" in texto
        assert "'DSA-2026-089-OF'" in texto
        assert "'2026-08-15'" in texto

    def test_solo_folio_omite_linea_de_fecha(self):
        pistas = PistaHeuristica(numero_oficio="DSA-2026-089-OF", fecha_emision=None)
        contenido = _extractor()._construir_contenido([_pagina()], 2026, None, pistas)
        texto = _texto_turno(contenido)
        assert "numero_oficio candidato" in texto
        assert "fecha_emision candidata" not in texto

    def test_solo_fecha_omite_linea_de_folio(self):
        pistas = PistaHeuristica(numero_oficio=None, fecha_emision="2026-08-15")
        contenido = _extractor()._construir_contenido([_pagina()], 2026, None, pistas)
        texto = _texto_turno(contenido)
        assert "fecha_emision candidata" in texto
        assert "numero_oficio candidato" not in texto

    def test_pistas_se_marcan_explicitamente_no_autoritativas(self):
        """El refuerzo anti-alucinación del prompt no debe relajarse: las
        pistas deben presentarse como candidatos a confirmar, no como dato."""
        pistas = PistaHeuristica(numero_oficio="DSA-2026-089-OF")
        contenido = _extractor()._construir_contenido([_pagina()], 2026, None, pistas)
        texto = _texto_turno(contenido)
        assert "NO autoritativos" in texto

    def test_convive_con_bloque_de_ocr(self):
        pistas = PistaHeuristica(numero_oficio="DSA-2026-089-OF")
        textos_ocr = {1: "texto ocr de referencia"}
        contenido = _extractor()._construir_contenido([_pagina()], 2026, textos_ocr, pistas)
        texto = _texto_turno(contenido)
        assert "PISTAS HEURÍSTICAS" in texto
        assert "TEXTO OCR AUXILIAR" in texto
        # Orden: pistas antes que el texto OCR extenso.
        assert texto.index("PISTAS HEURÍSTICAS") < texto.index("TEXTO OCR AUXILIAR")

    def test_paginas_e_imagenes_siguen_presentes(self):
        """El bloque nuevo no debe desplazar ni perder las partes de imagen
        ni la directiva de cierre."""
        pistas = PistaHeuristica(numero_oficio="DSA-2026-089-OF")
        contenido = _extractor()._construir_contenido([_pagina()], 2026, None, pistas)
        # texto de contexto + 1 imagen + directiva de cierre
        assert len(contenido) == 3
        assert contenido[-1].text.startswith("TAREA:")
