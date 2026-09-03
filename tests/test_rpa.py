"""
tests/test_rpa.py — Backoff exponencial con jitter y persistencia de
sesión del worker RPA (rpa/playwright_rpa.py), sin lanzar un navegador
real: ambas piezas son testeables de forma aislada.
"""

from __future__ import annotations

import os
import stat
import time

import pytest

from rpa.playwright_rpa import GestorSesiones, RpaIntranet


class ContextoFalso:
    """Doble mínimo de `playwright.sync_api.BrowserContext` para GestorSesiones."""

    def storage_state(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"cookies": [{"name": "sess", "value": "abc"}], "origins": []}')


class TestBackoffConJitter:
    @pytest.fixture
    def rpa(self, configuracion):
        cfg = configuracion.model_copy(update={"rpa_modo": "playwright"})
        return RpaIntranet(cfg)

    def test_backoff_crece_exponencialmente_dentro_del_jitter(self, rpa):
        for intento in range(1, 6):
            base_ms = rpa.config.rpa_reintento_base_ms * (2 ** (intento - 1))
            base_ms = min(base_ms, rpa.config.rpa_reintento_max_ms)
            jitter = base_ms * rpa.config.rpa_jitter_factor
            minimo, maximo = (base_ms - jitter) / 1000, (base_ms + jitter) / 1000
            valores = [rpa._calcular_espera_backoff(intento) for _ in range(200)]
            assert all(minimo - 0.01 <= v <= maximo + 0.01 for v in valores), (intento, min(valores), max(valores))

    def test_backoff_nunca_excede_el_techo_configurado(self, rpa):
        techo_con_jitter = (rpa.config.rpa_reintento_max_ms * (1 + rpa.config.rpa_jitter_factor)) / 1000
        valores = [rpa._calcular_espera_backoff(intento=10) for _ in range(200)]
        assert max(valores) <= techo_con_jitter

    def test_backoff_nunca_es_negativo_ni_cero(self, rpa):
        assert all(rpa._calcular_espera_backoff(1) > 0 for _ in range(50))


class TestClasificacionErrores:
    @pytest.fixture
    def rpa(self, configuracion):
        cfg = configuracion.model_copy(update={"rpa_modo": "playwright"})
        return RpaIntranet(cfg)

    @pytest.mark.parametrize("codigo", [
        "FORMULARIO_WEBIX_TIMEOUT", "INTRANET_NO_ALCANZABLE", "SESION_EXPIRADA",
        "OBJETIVO_CERRADO", "RPA_ERROR_INESPERADO",
    ])
    def test_errores_transitorios_se_reintentan(self, rpa, codigo):
        from rpa.playwright_rpa import ErrorRpa
        assert rpa._es_transitorio(ErrorRpa(codigo, "x")) is True

    def test_autenticacion_fallida_no_es_transitoria(self, rpa):
        from rpa.playwright_rpa import ErrorRpa
        assert rpa._es_transitorio(ErrorRpa("AUTENTICACION_INTRANET_FALLIDA", "x")) is False


class TestGestorSesiones:
    def test_sin_estado_previo(self, configuracion):
        gs = GestorSesiones(configuracion)
        assert gs.tiene_estado is False
        assert gs.opciones_contexto() == {}

    def test_guardar_y_reutilizar_mientras_este_fresco(self, configuracion):
        gs = GestorSesiones(configuracion)
        gs.guardar(ContextoFalso())
        assert gs.tiene_estado is True
        assert gs.estado_fresco is True
        assert "storage_state" in gs.opciones_contexto()

    def test_directorio_y_archivo_con_permisos_restringidos(self, configuracion):
        """El storage_state contiene cookies de sesión: no debe ser legible por otros usuarios."""
        gs = GestorSesiones(configuracion)
        gs.guardar(ContextoFalso())
        assert stat.S_IMODE(os.stat(gs._directorio).st_mode) == 0o700
        assert stat.S_IMODE(os.stat(gs._ruta).st_mode) == 0o600

    def test_ttl_vencido_deja_de_usarse(self, configuracion):
        cfg = configuracion.model_copy(update={"rpa_session_ttl_min": 1})
        gs = GestorSesiones(cfg)
        gs.guardar(ContextoFalso())
        os.utime(gs._ruta, (time.time() - 120, time.time() - 120))  # 2 min atrás, TTL=1 min
        assert gs.estado_fresco is False
        assert gs.opciones_contexto() == {}

    def test_invalidar_elimina_el_archivo(self, configuracion):
        gs = GestorSesiones(configuracion)
        gs.guardar(ContextoFalso())
        gs.invalidar()
        assert gs.tiene_estado is False

    def test_limpiar_expiradas_purga_solo_lo_viejo(self, configuracion):
        gs = GestorSesiones(configuracion)
        gs.guardar(ContextoFalso())
        os.utime(gs._ruta, (time.time() - 25 * 3600, time.time() - 25 * 3600))
        assert gs.limpiar_expiradas(ttl_horas=24) == 1
        assert gs.tiene_estado is False
