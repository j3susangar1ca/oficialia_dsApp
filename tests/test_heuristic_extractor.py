"""
tests/test_heuristic_extractor.py — Extractor de respaldo (regex, sin IA).
"""

from __future__ import annotations

from core.heuristic_extractor import extraer_heuristico
from core.models import MetodoExtraccion, Procedencia


class TestFolio:
    def test_encuentra_folio_con_barras(self):
        m = extraer_heuristico({1: "SECRETARÍA DE SALUD\nOFICIO No. SSJ/DEA/2026/089"}, anio_contexto=2026)
        # El folio crudo trae '/', pero MetadatosOficio los sanea a '-' para
        # ser seguro en nombres de archivo (igual que cualquier folio real
        # extraído por la IA) — ver TestNumeroOficio en test_models.py.
        assert m.numero_oficio == "SSJ-DEA-2026-089"

    def test_encuentra_folio_con_guiones(self):
        m = extraer_heuristico({1: "HCG-CA-045-2026\nAsunto: ..."}, anio_contexto=2026)
        assert m.numero_oficio == "HCG-CA-045-2026"

    def test_sin_folio_visible_usa_centinela(self):
        m = extraer_heuristico({1: "Estimado señor, por medio de la presente..."}, anio_contexto=2026)
        assert m.numero_oficio == "S/N"

    def test_solo_busca_en_la_primera_pagina(self):
        """Un folio en la página 2 (ej. una referencia en el cuerpo) no debe confundirse con el folio del emisor."""
        m = extraer_heuristico(
            {1: "Estimado señor.", 2: "Referencia: ABC-2020-999"}, anio_contexto=2026
        )
        assert m.numero_oficio == "S/N"


class TestFecha:
    def test_fecha_textual_en_espanol(self):
        m = extraer_heuristico({1: "Guadalajara, Jalisco, a 15 de agosto de 2026"}, anio_contexto=2026)
        assert m.fecha_emision == "2026-08-15"

    def test_fecha_numerica_dd_mm_aaaa(self):
        m = extraer_heuristico({1: "Fecha: 15/08/2026"}, anio_contexto=2026)
        assert m.fecha_emision == "2026-08-15"

    def test_fecha_numerica_ambigua_usa_convencion_dia_mes(self):
        """a/b/aaaa con ambos <=12: a es DÍA, b es MES (igual que el prompt de la IA)."""
        m = extraer_heuristico({1: "05/08/2026"}, anio_contexto=2026)
        assert m.fecha_emision == "2026-08-05"

    def test_fecha_numerica_mes_mayor_a_12_se_reinterpreta(self):
        """Si el primer número >12, es el día (formato mes/día descartado)."""
        m = extraer_heuristico({1: "25/03/2026"}, anio_contexto=2026)
        assert m.fecha_emision == "2026-03-25"

    def test_anio_de_dos_digitos_se_expande_con_contexto(self):
        m = extraer_heuristico({1: "15/08/26"}, anio_contexto=2026)
        assert m.fecha_emision == "2026-08-15"

    def test_sin_fecha_visible_usa_contingencia(self):
        m = extraer_heuristico({1: "Sin ninguna fecha en este texto."}, anio_contexto=2026)
        assert m.fecha_emision == "2026-01-01"

    def test_fecha_de_calendario_invalida_se_descarta(self):
        """31 de febrero no existe: debe ignorarse y caer en la contingencia."""
        m = extraer_heuristico({1: "31 de febrero de 2026"}, anio_contexto=2026)
        assert m.fecha_emision == "2026-01-01"


class TestValoresPorDefecto:
    def test_documento_sin_texto_produce_metadatos_validos_no_none(self):
        """
        El extractor SIEMPRE devuelve un MetadatosOficio válido (nunca None):
        un documento sin ninguna evidencia de texto (fax/escaneo sin OCR)
        debe seguir llegando a PENDIENTE_REVISION con placeholders claros,
        en vez de perderse en la cuarentena de errores.
        """
        m = extraer_heuristico({}, anio_contexto=2026)
        assert m.numero_oficio == "S/N"
        assert m.remitente_nombre == "ILEGIBLE"
        assert m.destinatario_nombre == "NO ESPECIFICADO"
        assert m.plazo_dias is None
        assert m.contiene_datos_sensibles is False
        assert len(m.asunto) >= 10  # pasa la validación mínima del contrato

    def test_procedencia_por_defecto_es_ajena(self):
        assert extraer_heuristico({}, anio_contexto=2026).procedencia == Procedencia.AJENA

    def test_asunto_advierte_extraccion_heuristica(self):
        """El asunto debe delatar de inmediato al revisor que esto NO vino de la IA."""
        asunto = extraer_heuristico({}, anio_contexto=2026).asunto.upper()
        assert "HEURÍSTICA" in asunto or "HEURISTICA" in asunto
