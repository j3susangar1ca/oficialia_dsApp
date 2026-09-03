"""
tests/test_file_manager.py — Exportación best-effort a carpeta compartida de
red (SMB): core.file_manager.exportar_a_red_smb. No requiere ningún recurso
de red real: SMB_EXPORT_DIR apunta a una carpeta temporal aislada (tmp_path),
que en producción sería el punto de montaje UNC — el código no distingue
entre ambos, solo copia sobre la ruta configurada.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.file_manager import exportar_a_red_smb


def _crear_pdf_falso(ruta: Path, contenido: bytes = b"%PDF-1.4 contenido de prueba") -> Path:
    ruta.write_bytes(contenido)
    return ruta


class TestExportarARedSmb:
    def test_sin_smb_export_dir_no_exporta(self, configuracion, tmp_path):
        cfg = configuracion.model_copy(update={"smb_export_dir": ""})
        pdf = _crear_pdf_falso(tmp_path / "origen.pdf")

        resultado = exportar_a_red_smb(
            pdf,
            {"folio": "DSA-1", "fecha": "2026-08-15", "remitente": "Juan Pérez", "asunto": "Solicitud"},
            configuracion=cfg,
        )

        assert resultado is None

    def test_copia_con_nombre_dinamico(self, configuracion, tmp_path):
        destino_dir = tmp_path / "smb"
        cfg = configuracion.model_copy(update={"smb_export_dir": str(destino_dir)})
        pdf = _crear_pdf_falso(tmp_path / "origen.pdf")

        resultado = exportar_a_red_smb(
            pdf,
            {
                "folio": "DSA-2026-777-OF",
                "fecha": "2026-08-15",
                "remitente": "Juan Pérez",
                "asunto": "Solicitud de información",
            },
            configuracion=cfg,
        )

        assert resultado is not None
        assert resultado.exists()
        assert resultado.read_bytes() == pdf.read_bytes()
        assert resultado.name == "DSA-2026-777-OF_2026-08-15_Juan_Pérez_Solicitud_de_información.pdf"

    def test_crea_la_carpeta_destino_si_no_existe(self, configuracion, tmp_path):
        destino_dir = tmp_path / "smb" / "2026"
        cfg = configuracion.model_copy(update={"smb_export_dir": str(destino_dir)})
        pdf = _crear_pdf_falso(tmp_path / "origen.pdf")

        resultado = exportar_a_red_smb(pdf, {"folio": "X", "fecha": "2026-01-01", "remitente": "A", "asunto": "B"}, configuracion=cfg)

        assert resultado is not None
        assert destino_dir.is_dir()

    def test_sanea_caracteres_prohibidos_de_windows(self, configuracion, tmp_path):
        destino_dir = tmp_path / "smb"
        cfg = configuracion.model_copy(update={"smb_export_dir": str(destino_dir)})
        pdf = _crear_pdf_falso(tmp_path / "origen.pdf")

        resultado = exportar_a_red_smb(
            pdf,
            {
                "folio": 'DSA/2026\\777:OF*?"<>|',
                "fecha": "2026-08-15",
                "remitente": "Juan Pérez",
                "asunto": "Asunto",
            },
            configuracion=cfg,
        )

        assert resultado is not None
        for prohibido in '\\/:*?"<>|':
            assert prohibido not in resultado.name

    def test_trunca_asunto_muy_extenso(self, configuracion, tmp_path):
        destino_dir = tmp_path / "smb"
        cfg = configuracion.model_copy(update={"smb_export_dir": str(destino_dir)})
        pdf = _crear_pdf_falso(tmp_path / "origen.pdf")
        asunto_largo = "palabra " * 30

        resultado = exportar_a_red_smb(
            pdf,
            {"folio": "F", "fecha": "2026-01-01", "remitente": "R", "asunto": asunto_largo},
            configuracion=cfg,
        )

        assert resultado is not None
        campo_asunto = resultado.stem.split("_", 3)[-1]
        assert len(campo_asunto) <= 60

    def test_resuelve_colision_con_sufijo_numerico(self, configuracion, tmp_path):
        destino_dir = tmp_path / "smb"
        cfg = configuracion.model_copy(update={"smb_export_dir": str(destino_dir)})
        metadatos = {"folio": "F", "fecha": "2026-01-01", "remitente": "R", "asunto": "Igual"}

        pdf1 = _crear_pdf_falso(tmp_path / "origen1.pdf", b"contenido-1")
        resultado1 = exportar_a_red_smb(pdf1, metadatos, configuracion=cfg)

        pdf2 = _crear_pdf_falso(tmp_path / "origen2.pdf", b"contenido-2")
        resultado2 = exportar_a_red_smb(pdf2, metadatos, configuracion=cfg)

        assert resultado1 is not None and resultado2 is not None
        assert resultado1 != resultado2
        assert resultado1.name == "F_2026-01-01_R_Igual.pdf"
        assert resultado2.name == "F_2026-01-01_R_Igual_1.pdf"
        assert resultado1.read_bytes() == b"contenido-1"
        assert resultado2.read_bytes() == b"contenido-2"

    def test_carpeta_de_red_inalcanzable_no_lanza_excepcion(self, configuracion, tmp_path):
        """
        Simula un recurso SMB inalcanzable (p. ej. carpeta no montada o sin
        permisos): forzamos el mismo tipo de error que produciría el SO —
        `mkdir` falla porque un componente de la ruta ya existe como archivo
        regular, no como carpeta — y verificamos que la función degrada a
        `None` en vez de propagar la excepción.
        """
        bloqueador = tmp_path / "no_es_una_carpeta"
        bloqueador.write_text("esto es un archivo, no una carpeta")
        destino_dir = bloqueador / "sub"
        cfg = configuracion.model_copy(update={"smb_export_dir": str(destino_dir)})
        pdf = _crear_pdf_falso(tmp_path / "origen.pdf")

        resultado = exportar_a_red_smb(
            pdf, {"folio": "F", "fecha": "2026-01-01", "remitente": "R", "asunto": "A"}, configuracion=cfg
        )
        assert resultado is None

    def test_acepta_claves_de_metadatosoficio(self, configuracion, tmp_path):
        """metadatos también puede traer las claves largas de MetadatosOficio."""
        destino_dir = tmp_path / "smb"
        cfg = configuracion.model_copy(update={"smb_export_dir": str(destino_dir)})
        pdf = _crear_pdf_falso(tmp_path / "origen.pdf")

        resultado = exportar_a_red_smb(
            pdf,
            {
                "numero_oficio": "DSA-1",
                "fecha_emision": "2026-08-15",
                "remitente_nombre": "Juan Pérez",
                "asunto": "Solicitud",
            },
            configuracion=cfg,
        )

        assert resultado is not None
        assert resultado.name == "DSA-1_2026-08-15_Juan_Pérez_Solicitud.pdf"
