"""
tests/test_models.py — Contrato Pydantic (MetadatosOficio) y utilidades
de nomenclatura canónica.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from core.models import MetadatosOficio, Procedencia, nombre_archivo_canonico

CAMPOS_BASE = dict(
    fecha_emision="2026-08-15",
    procedencia=Procedencia.AJENA,
    dependencia_area="dirección de administración",
    remitente_nombre="juan pérez lópez",
    destinatario_nombre="maría gómez",
    asunto="Se solicita la validación de documentos administrativos pendientes de revisión.",
)


def _metadatos(**overrides) -> MetadatosOficio:
    datos = {**CAMPOS_BASE, "numero_oficio": "DSA-2026-089-OF", **overrides}
    return MetadatosOficio(**datos)


class TestNumeroOficio:
    def test_centinela_sn_se_preserva_intacto(self):
        """
        Regresión: el validador de folios sustituye caracteres reservados
        de sistema de archivos ('/', etc.) por '-', pero NO debe tocar el
        centinela documentado "S/N" (contrato de la IA y placeholder visible
        en la UI de revisión) — antes se corrompía silenciosamente a "S-N".
        """
        m = _metadatos(numero_oficio="s/n")
        assert m.numero_oficio == "S/N"

    def test_folio_real_con_barra_si_se_sanea(self):
        m = _metadatos(numero_oficio="SSJ/DEA/2026/089")
        assert m.numero_oficio == "SSJ-DEA-2026-089"

    def test_folio_vacio_es_invalido(self):
        with pytest.raises(ValidationError):
            _metadatos(numero_oficio="   ")

    def test_folio_se_recorta(self):
        m = _metadatos(numero_oficio="  DSA-0123  ")
        assert m.numero_oficio == "DSA-0123"


class TestFechaEmision:
    @pytest.mark.parametrize("valor", ["2026-13-01", "15/08/2026", "2026-2-30", "no-es-fecha"])
    def test_formatos_invalidos_se_rechazan(self, valor):
        with pytest.raises(ValidationError):
            _metadatos(fecha_emision=valor)

    def test_fecha_iso_valida_pasa(self):
        m = _metadatos(fecha_emision="2026-02-28")
        assert m.fecha_emision == "2026-02-28"


class TestNormalizacionMayusculas:
    def test_dependencia_remitente_destinatario_a_mayusculas(self):
        m = _metadatos()
        assert m.dependencia_area == "DIRECCIÓN DE ADMINISTRACIÓN"
        assert m.remitente_nombre == "JUAN PÉREZ LÓPEZ"
        assert m.destinatario_nombre == "MARÍA GÓMEZ"

    def test_cargos_vacios_usan_no_especificado(self):
        m = _metadatos(remitente_cargo="", destinatario_cargo="   ")
        assert m.remitente_cargo == "NO ESPECIFICADO"
        assert m.destinatario_cargo == "NO ESPECIFICADO"


class TestAsunto:
    def test_saltos_de_linea_se_colapsan(self):
        m = _metadatos(asunto="Primera línea.\nSegunda línea.\r\nTercera.")
        assert "\n" not in m.asunto and "\r" not in m.asunto

    def test_muy_corto_es_invalido(self):
        with pytest.raises(ValidationError):
            _metadatos(asunto="hola")


class TestPlazoDias:
    def test_vacio_se_interpreta_como_none(self):
        assert _metadatos(plazo_dias="").plazo_dias is None
        assert _metadatos(plazo_dias=None).plazo_dias is None

    def test_cadena_numerica_se_castea(self):
        assert _metadatos(plazo_dias="10").plazo_dias == 10

    def test_negativo_es_invalido(self):
        with pytest.raises(ValidationError):
            _metadatos(plazo_dias=-1)


class TestNombreArchivoCanonico:
    def test_formato_general(self):
        m = _metadatos(numero_oficio="DSA-2026-089-OF", remitente_nombre="Juan Pérez López")
        nombre = nombre_archivo_canonico(m)
        assert nombre == "2026-08-15__DSA-2026-089-OF__JUAN_PÉREZ_LÓPEZ.pdf"

    def test_centinela_sn_no_rompe_el_nombre_de_archivo(self):
        """
        Regresión conjunta con TestNumeroOficio: aunque el modelo preserva
        "S/N" intacto, el nombre de archivo NUNCA debe contener una barra
        (crearía/apuntaría a un subdirectorio inexistente al escribir en disco).
        """
        m = _metadatos(numero_oficio="S/N")
        nombre = nombre_archivo_canonico(m)
        assert "/" not in nombre.removesuffix(".pdf")
        assert "S-N" in nombre

    def test_remitente_se_trunca_a_30_caracteres(self):
        m = _metadatos(remitente_nombre="X" * 50)
        nombre = nombre_archivo_canonico(m)
        segmento_remitente = nombre.split("__")[2].removesuffix(".pdf")
        assert len(segmento_remitente) <= 30
