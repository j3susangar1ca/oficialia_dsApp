"""
tests/test_heuristic_extractor.py — Extractor de respaldo (regex, sin IA).
"""

from __future__ import annotations

import unicodedata

from core.heuristic_extractor import extraer_heuristico, extraer_pistas
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


class TestExtraerPistas:
    """`extraer_pistas` — fase previa de preprocesamiento para core.ai_extractor.

    A diferencia de `extraer_heuristico`, NUNCA rellena con valores de
    contingencia ("S/N", etc.): cada campo es `None` cuando no hay
    candidato, porque estas pistas viajan al prompt de Gemini como
    candidatos a confirmar, no como resultado final.
    """

    def test_encuentra_folio_y_fecha(self):
        pistas = extraer_pistas(
            {1: "OFICIO SSJ/DEA/2026/089\n15 de agosto de 2026"}, anio_contexto=2026
        )
        assert pistas.numero_oficio == "SSJ/DEA/2026/089"
        assert pistas.fecha_emision == "2026-08-15"
        assert pistas.hay_pistas is True

    def test_sin_texto_no_hay_pistas_ninguna_es_contingencia(self):
        pistas = extraer_pistas({}, anio_contexto=2026)
        assert pistas.numero_oficio is None
        assert pistas.fecha_emision is None
        assert pistas.hay_pistas is False

    def test_solo_folio_sin_fecha(self):
        pistas = extraer_pistas({1: "HCG-CA-045-2026"}, anio_contexto=2026)
        assert pistas.numero_oficio == "HCG-CA-045-2026"
        assert pistas.fecha_emision is None
        assert pistas.hay_pistas is True

    def test_extraer_heuristico_reutiliza_extraer_pistas(self):
        """Mismo motor de patrones: los valores encontrados deben coincidir
        (la única diferencia es la contingencia cuando no hay candidato)."""
        texto = {1: "REF DSA-0123\n03/04/2026"}
        pistas = extraer_pistas(texto, anio_contexto=2026)
        metadatos = extraer_heuristico(texto, anio_contexto=2026)
        assert metadatos.numero_oficio == pistas.numero_oficio
        assert metadatos.fecha_emision == pistas.fecha_emision


class TestNormalizacionUnicodeYPrioridadSemantica:
    """Cobertura de las mejoras v3 adoptadas: NFC previo, continuación tras
    candidato inválido, aislamiento por página y prioridad textual>numérica
    en TODO el documento (no intercalada por página)."""

    def test_folio_con_diacritico_descompuesto_nfd_se_reconoce(self):
        # "Á" descompuesta (A + U+0301, artefacto típico de OCR/PDF): sin
        # normalización NFC previa, \b se activa tras la marca combinante y
        # la "Á" se pierde en silencio.
        folio_nfd = unicodedata.normalize("NFD", "ÁBC/DEA/2026/001")
        pistas = extraer_pistas({1: f"OFICIO {folio_nfd}"}, anio_contexto=2026)
        assert pistas.numero_oficio == "ÁBC/DEA/2026/001"

    def test_fecha_textual_continua_tras_candidato_invalido(self):
        """'31 de febrero' no existe: la búsqueda debe seguir con el
        siguiente candidato de la misma página, no abortar la rama textual."""
        pistas = extraer_pistas(
            {1: "31 de febrero de 2026; se expide 2 de abril de 2026"}, anio_contexto=2026
        )
        assert pistas.fecha_emision == "2026-04-02"

    def test_mes_con_ortografia_falsa_se_rechaza(self):
        """'diciémbre' (artefacto de OCR) no es un mes real: se descarta."""
        pistas = extraer_pistas({1: "15 de diciémbre de 2026"}, anio_contexto=2026)
        assert pistas.fecha_emision is None

    def test_no_fabrica_fecha_entre_paginas(self):
        """Unir páginas con '\\n' podría fabricar '15 de\\nagosto de 2026' a
        partir del final de una página y el inicio de la siguiente; con
        páginas aisladas eso no debe ocurrir."""
        paginas = {1: "Texto largo 15 de", 2: "agosto de 2026 mas texto"}
        assert extraer_pistas(paginas, anio_contexto=2026).fecha_emision is None
        # La fecha completa DENTRO de una misma página sí se reconoce:
        paginas_ok = {1: "Texto largo", 2: "15 de agosto de 2026"}
        assert extraer_pistas(paginas_ok, anio_contexto=2026).fecha_emision == "2026-08-15"

    def test_prioridad_textual_sobre_numerica_en_todo_el_documento(self):
        """Numérica en la página 1 y textual en la página 2: gana la
        textual en TODO el documento (no se intercala por página)."""
        paginas = {1: "Ref: 15/08/2026", 2: "10 de enero de 2025"}
        assert extraer_pistas(paginas, anio_contexto=2026).fecha_emision == "2025-01-10"
