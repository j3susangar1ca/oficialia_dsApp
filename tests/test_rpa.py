"""
tests/test_rpa.py — Backoff exponencial con jitter, persistencia de sesión
y navegación al submódulo de captura del worker RPA (rpa/playwright_rpa.py),
sin lanzar un navegador real: todo es testeable de forma aislada con dobles
mínimos de la API sync de Playwright (Page/Locator).
"""

from __future__ import annotations

import os
import stat
import time

import pytest

from rpa.playwright_rpa import ErrorRpa, GestorSesiones, RpaIntranet, RUTA_SUBMODULO_INGRESO


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


class _LocatorFalso:
    """Doble mínimo de `playwright.sync_api.Locator` (solo lo que usa _abrir_submodulo_ingreso)."""

    def __init__(self, visible: bool = True):
        self.visible = visible
        self.click_llamado = False

    @property
    def first(self) -> "_LocatorFalso":
        return self

    def wait_for(self, state: str = "visible", timeout: float = 0) -> None:
        if not self.visible:
            raise TimeoutError("Elemento no visible dentro del timeout")

    def click(self) -> None:
        self.click_llamado = True


class _PaginaFalsa:
    """Doble mínimo de `playwright.sync_api.Page` para _abrir_submodulo_ingreso."""

    def __init__(self, *, resultado_evaluate: bool = True, lanza_evaluate: bool = False, boton_visible: bool = True):
        self.resultado_evaluate = resultado_evaluate
        self.lanza_evaluate = lanza_evaluate
        self.llamadas_evaluate: list[tuple[str, object]] = []
        self.locator_falso = _LocatorFalso(visible=boton_visible)

    def evaluate(self, script: str, arg: object = None):
        self.llamadas_evaluate.append((script, arg))
        if self.lanza_evaluate:
            raise RuntimeError("window.myTabbar no definido en esta versión de la interfaz")
        return self.resultado_evaluate

    def locator(self, selector: str) -> _LocatorFalso:
        return self.locator_falso


class TestAbrirSubmoduloIngreso:
    """
    Navegación al submódulo op_ningr.fwx dentro del tabbar DHTMLX de
    op_cucs.fwx (ver docs/rpa/webix_dump_for_qwen.json): tras el login la
    pestaña activa 'a1' carga por omisión OTRO submódulo, así que sin este
    paso _resolver_marco_op_ningr esperaría indefinidamente un iframe que
    nunca llega a crearse.
    """

    @pytest.fixture
    def rpa(self, configuracion):
        cfg = configuracion.model_copy(update={"rpa_modo": "playwright"})
        return RpaIntranet(cfg)

    def test_usa_la_api_dhtmlx_cuando_myTabbar_esta_disponible(self, rpa):
        pagina = _PaginaFalsa(resultado_evaluate=True)
        rpa._abrir_submodulo_ingreso(pagina)

        assert len(pagina.llamadas_evaluate) == 1
        _script, ruta = pagina.llamadas_evaluate[0]
        assert ruta == RUTA_SUBMODULO_INGRESO
        assert pagina.locator_falso.click_llamado is False  # nunca cayó al respaldo visual

    def test_cae_al_clic_visual_si_dhtmlx_no_respondio(self, rpa):
        pagina = _PaginaFalsa(resultado_evaluate=False)
        rpa._abrir_submodulo_ingreso(pagina)

        assert pagina.locator_falso.click_llamado is True

    def test_cae_al_clic_visual_si_evaluate_lanza_excepcion(self, rpa):
        pagina = _PaginaFalsa(lanza_evaluate=True)
        rpa._abrir_submodulo_ingreso(pagina)

        assert pagina.locator_falso.click_llamado is True

    def test_error_formulario_webix_timeout_si_ni_api_ni_clic_funcionan(self, rpa):
        pagina = _PaginaFalsa(resultado_evaluate=False, boton_visible=False)

        with pytest.raises(ErrorRpa) as info:
            rpa._abrir_submodulo_ingreso(pagina)
        assert info.value.codigo == "FORMULARIO_WEBIX_TIMEOUT"
