"""
tests/test_config.py — Configuración: validadores y resolución de rutas
del ejecutable empaquetado (ver packaging/, config.py::EMPAQUETADO).
"""

from __future__ import annotations

import importlib
import sys

import pytest

import config as config_module
from config import Configuracion


class TestValidadores:
    def test_rpa_modo_invalido_se_rechaza(self, configuracion):
        with pytest.raises(Exception):
            Configuracion(
                storage_root=configuracion.storage_root,
                database_path=configuracion.database_path,
                rpa_modo="no-existe",
            )

    def test_rpa_modo_se_normaliza_a_minusculas(self, configuracion):
        cfg = Configuracion(
            storage_root=configuracion.storage_root,
            database_path=configuracion.database_path,
            rpa_modo="SIMULACION",
        )
        assert cfg.rpa_modo == "simulacion"

    @pytest.mark.parametrize("valor", [-0.1, 1.1, 5.0])
    def test_jitter_fuera_de_rango_se_rechaza(self, configuracion, valor):
        with pytest.raises(Exception):
            Configuracion(
                storage_root=configuracion.storage_root,
                database_path=configuracion.database_path,
                rpa_jitter_factor=valor,
            )

    def test_jitter_en_rango_se_acepta(self, configuracion):
        cfg = Configuracion(
            storage_root=configuracion.storage_root,
            database_path=configuracion.database_path,
            rpa_jitter_factor=0.5,
        )
        assert cfg.rpa_jitter_factor == 0.5


class TestModoDesarrollo:
    def test_no_empaquetado_por_defecto(self):
        assert config_module.EMPAQUETADO is False

    def test_datos_dir_coincide_con_base_dir_en_desarrollo(self):
        assert config_module.DATOS_DIR == config_module.BASE_DIR


class TestRutasEmpaquetado:
    """
    Simula `sys.frozen` (como si el proceso fuera el .exe de PyInstaller)
    para validar, sin un entorno Windows real, que:
      1. La carpeta de datos se redirige fuera de la carpeta de instalación
         (de solo lectura para un usuario estándar en Archivos de programa).
      2. Se siembra un `.env` desde la plantilla empaquetada en el primer
         arranque, sin sobrescribir uno ya existente.
    Ver config.py::_directorio_datos_predeterminado / _sembrar_env_inicial.
    """

    @pytest.fixture
    def config_recargado(self, tmp_path, monkeypatch):
        """Reimporta config.py con sys.frozen=True y PROGRAMDATA apuntando a tmp_path."""
        meipass = tmp_path / "bundle"
        meipass.mkdir()
        (meipass / ".env.example").write_text("RPA_MODO=simulacion\n", encoding="utf-8")

        programdata = tmp_path / "programdata"
        programdata.mkdir()

        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "_MEIPASS", str(meipass), raising=False)
        monkeypatch.setenv("PROGRAMDATA", str(programdata))
        monkeypatch.delenv("LOCALAPPDATA", raising=False)

        modulo = importlib.reload(config_module)
        yield modulo, programdata

        # Deshacer para no filtrar el estado "empaquetado" a otras pruebas.
        monkeypatch.delattr(sys, "frozen", raising=False)
        monkeypatch.delattr(sys, "_MEIPASS", raising=False)
        importlib.reload(config_module)

    def test_datos_dir_se_redirige_a_programdata(self, config_recargado):
        modulo, programdata = config_recargado
        assert modulo.EMPAQUETADO is True
        assert modulo.DATOS_DIR == programdata / "OficialiaDigitalDSA"
        assert modulo.DATOS_DIR.is_dir()

    def test_env_se_siembra_desde_la_plantilla(self, config_recargado):
        modulo, programdata = config_recargado
        env = programdata / "OficialiaDigitalDSA" / ".env"
        assert env.is_file()
        assert "RPA_MODO=simulacion" in env.read_text(encoding="utf-8")

    def test_env_existente_no_se_sobrescribe(self, tmp_path, monkeypatch):
        meipass = tmp_path / "bundle"
        meipass.mkdir()
        (meipass / ".env.example").write_text("RPA_MODO=simulacion\n", encoding="utf-8")
        programdata = tmp_path / "programdata"
        datos = programdata / "OficialiaDigitalDSA"
        datos.mkdir(parents=True)
        (datos / ".env").write_text("GEMINI_API_KEY=ya-configurada\n", encoding="utf-8")

        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "_MEIPASS", str(meipass), raising=False)
        monkeypatch.setenv("PROGRAMDATA", str(programdata))
        monkeypatch.delenv("LOCALAPPDATA", raising=False)

        importlib.reload(config_module)
        try:
            contenido = (datos / ".env").read_text(encoding="utf-8")
            assert "GEMINI_API_KEY=ya-configurada" in contenido
            assert "RPA_MODO" not in contenido
        finally:
            monkeypatch.delattr(sys, "frozen", raising=False)
            monkeypatch.delattr(sys, "_MEIPASS", raising=False)
            importlib.reload(config_module)
