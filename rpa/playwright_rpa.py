"""
SISTEMA OFICIALIA-DIGITAL-DSA (reconstrucción 100% Python)
==========================================================
rpa/playwright_rpa.py — Automatización de la Intranet institucional Webix.

Puerta de entrada de la Intranet SII HCG (`op_cucs.fwx` con el formulario
embebido en el iframe `op_ningr.fwx`). Migración 1:1 del
`PlaywrightRpaAdapter` original: selectores mapeados desde
`docs/rpa/webix_dump_for_qwen.json`, relleno de los controles Webix por
`view id` vía `webix.$$().setValue()`, subida del PDF canónico, captura del
acuse institucional y screenshot de evidencia.

Modo dual (config.RPA_MODO):
    - 'playwright' (default de esta instalación): automatización REAL con
      Chromium (requiere `playwright install chromium` y credenciales/CVEs
      institucionales — ver RPA_USUARIO/RPA_PASSWORD).
    - 'simulacion': modo seguro para pruebas locales — NO lanza navegador,
      produce un acuse sintético y permite recorrer el ciclo completo hasta
      COMPLETADO (o ERROR_RPA si RPA_SIMULACION_FALLAR=true).

Notas de ejecución: el worker corre en su propio hilo (ejecutor serializado
del pipeline) con la API sincrónica de Playwright; nunca en el event loop
de la interfaz.

MEJORAS v2 sobre la migración original (config.py trae los parámetros
nuevos con defaults seguros: rpa_selector_timeout_ms, rpa_webix_init_
timeout_ms, rpa_reintento_base_ms, rpa_reintento_max_ms, rpa_session_ttl_
min, rpa_jitter_factor):
    1. Selectores tolerantes a demoras de red: espera en dos fases del
       framework Webix (motor → formulario/controles), reintentos internos
       con backoff lineal sobre `wait_for_function`, verificación de que
       cada control expone `setValue` antes de invocarlo, resolución del
       iframe con reintentos y pausas para cascadas de eventos onChange
       (rbDepe/tipo_ofic recargando combos dependientes).
    2. Reintentos exponenciales con jitter (evita reintentos sincronizados
       tipo thundering-herd) y captura de screenshot en CADA intento
       fallido (no solo al final), con las excepciones no clasificadas del
       stack de Playwright envueltas en ErrorRpa para que el bucle de
       reintentos las trate como transitorias.
    3. Persistencia de sesión: el `storage_state` de Playwright (cookies +
       localStorage) se guarda en disco de forma atómica y se reutiliza
       mientras esté fresco (TTL configurable), evitando reinicializar la
       sesión Webix en cada envío. Se invalida automáticamente ante 401/403
       o un redirect a login, y el archivo se guarda con permisos 0600 en
       un directorio 0700 (contiene material de sesión, no debe ser legible
       por otros usuarios del sistema).
"""

from __future__ import annotations

import hashlib
import logging
import os
import random
import re
import tempfile
import time
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from config import Configuracion, get_settings
from core.models import DocumentoRegistro, ResultadoRpa

logger = logging.getLogger("oficialia.rpa")

#: URL institucional por omisión (op_cucs.fwx).
URL_INTRANET_POR_DEFECTO = "https://sii.hcg.gob.mx/intranet/op_cucs.fwx"

#: Selector del iframe que contiene el formulario de ingreso.
SELECTOR_IFRAME = 'iframe[src*="op_ningr.fwx"]'

#: Regex de folios institucionales de confirmación (ej. HCG-OP-2026-009821).
REGEX_FOLIO = re.compile(r"(HCG-OP-\d{4}-\d{4,}|[A-Z]{2,}(?:[-/][A-Z0-9]+)*-\d{4}-\d{3,})")


class ErrorRpa(Exception):
    """Fallo clasificado del worker RPA (código operativo + mensaje accionable)."""

    def __init__(self, codigo: str, mensaje: str, captura_error: Optional[str] = None) -> None:
        self.codigo = codigo
        self.captura_error = captura_error
        super().__init__(f"[{codigo}] {mensaje}")


# ======================================================================
# Utilidades compartidas
# ======================================================================

def _formatear_fecha(valor: date) -> str:
    """Formato de fecha que exige la pantalla Webix: DD/MM/AAAA."""
    return valor.strftime("%d/%m/%Y")


def _formatear_hora(valor: datetime) -> str:
    return valor.strftime("%H:%M")


def _limpiar_texto(valor: str) -> str:
    """Mayúsculas + espacios colapsados (misma limpieza que el original)."""
    return re.sub(r"\s+", " ", valor.upper()).strip()


# ======================================================================
# Gestor de sesiones / cookies
# ======================================================================

class GestorSesiones:
    """
    Persiste y restaura el `storage_state` de Playwright entre ejecuciones.

    Guarda cookies + localStorage en `<storage_root>/.rpa_sessions/
    state_<hash>.json`, con el hash derivado de la URL de la Intranet y el
    usuario HTTP configurado (cada cuenta tiene su propio archivo). La
    escritura es atómica (write-to-temp + os.replace) para no corromper el
    estado si el proceso cae a mitad de escritura, y tanto el directorio
    como el archivo quedan con permisos restringidos (0700/0600): contienen
    material de sesión y no deben ser legibles por otros usuarios locales.
    """

    def __init__(self, config: Configuracion) -> None:
        self._config = config
        self._directorio = config.storage_root / ".rpa_sessions"
        self._directorio.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self._directorio, 0o700)
        except OSError:
            logger.debug("No se pudieron restringir permisos de %s", self._directorio, exc_info=True)
        self._ruta = self._directorio / f"state_{self._hash()}.json"

    # -- Propiedades ----------------------------------------------------

    @property
    def tiene_estado(self) -> bool:
        """¿Existe un archivo de estado en disco?"""
        return self._ruta.is_file() and self._ruta.stat().st_size > 10

    @property
    def estado_fresco(self) -> bool:
        """¿El archivo de estado es más reciente que el TTL configurado?"""
        if not self.tiene_estado:
            return False
        ttl_s = self._config.rpa_session_ttl_min * 60
        edad = time.time() - self._ruta.stat().st_mtime
        return edad < ttl_s

    # -- Operaciones ------------------------------------------------------

    def opciones_contexto(self) -> dict[str, Any]:
        """Kwargs para `navegador.new_context(...)` si hay estado fresco."""
        if self.estado_fresco:
            logger.debug("[Sesión] Restaurando storage_state desde %s", self._ruta)
            return {"storage_state": str(self._ruta)}
        return {}

    def guardar(self, contexto) -> None:
        """Persiste el storage_state del contexto (escritura atómica)."""
        tmp: Optional[str] = None
        try:
            fd, tmp = tempfile.mkstemp(suffix=".json", dir=str(self._directorio))
            os.close(fd)  # mkstemp ya crea el archivo con permisos 0600
            contexto.storage_state(path=tmp)
            os.replace(tmp, str(self._ruta))
            logger.info("[Sesión] storage_state guardado (%s)", self._ruta.name)
        except Exception:  # noqa: BLE001 — persistir la sesión es una mejora, no un requisito
            logger.debug("[Sesión] No se pudo guardar storage_state", exc_info=True)
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    def invalidar(self) -> None:
        """Elimina el archivo de estado (fuerza reautenticación en el próximo intento)."""
        try:
            if self._ruta.is_file():
                self._ruta.unlink()
                logger.info("[Sesión] Estado de sesión invalidado")
        except Exception:  # noqa: BLE001
            logger.debug("[Sesión] Error al invalidar sesión", exc_info=True)

    def limpiar_expiradas(self, ttl_horas: int = 24) -> int:
        """Elimina archivos de sesión (de cualquier cuenta) más viejos que `ttl_horas`."""
        eliminados = 0
        for archivo in self._directorio.glob("state_*.json"):
            try:
                if time.time() - archivo.stat().st_mtime > ttl_horas * 3600:
                    archivo.unlink()
                    eliminados += 1
            except OSError:
                pass
        return eliminados

    # -- Internos -----------------------------------------------------------

    def _hash(self) -> str:
        clave = f"{self._config.intranet_base_url}|{self._config.intranet_http_username}"
        return hashlib.sha256(clave.encode()).hexdigest()[:12]


# ======================================================================
# Modo SIMULACIÓN (pruebas locales seguras)
# ======================================================================

class RpaSimulado:
    """
    Stub honesto para desarrollo: no abre navegador ni toca la red.
    Produce un acuse sintético (HCG-OP-SIM-…) para poder recorrer el flujo
    completo INGESTADO → COMPLETADO sin credenciales. Con
    RPA_SIMULACION_FALLAR=true reproduce un ERROR_RPA controlado.
    """

    def __init__(self, config: Configuracion) -> None:
        self.config = config

    @property
    def disponible(self) -> bool:
        return False  # no hay Intranet real detrás

    def inyectar_documento(self, documento: DocumentoRegistro) -> ResultadoRpa:
        inicio = time.perf_counter()
        id_ejecucion = str(uuid.uuid4())
        metadatos = documento.metadatos_validados
        assert metadatos is not None

        logger.warning(
            "[RPA-SIMULACIÓN] Inyectando documento %s SIN navegador real "
            "(configure RPA_MODO=playwright para producción)",
            documento.id,
        )
        time.sleep(1.5)  # latencia simulada de sesión

        if self.config.rpa_simulacion_fallar:
            raise ErrorRpa(
                "RPA_SIMULADO_CONFIGURADO_PARA_FALLAR",
                "Fallo inducido deliberadamente (RPA_SIMULACION_FALLAR=true) para probar la ruta ERROR_RPA",
            )

        anio = datetime.now().year
        consecutivo = random.randint(100000, 999999)
        return ResultadoRpa(
            id_ejecucion=id_ejecucion,
            folio_acuse=f"HCG-OP-SIM-{anio}-{consecutivo}",
            fecha_ejecucion=datetime.now().isoformat(timespec="milliseconds"),
            duracion_ms=int((time.perf_counter() - inicio) * 1000),
            captura_acuse_path=None,
            intentos=1,
            mensaje_error=None,
            exitoso=True,
            simulado=True,
        )


# ======================================================================
# Modo REAL (Playwright → Intranet Webix)
# ======================================================================

class RpaIntranet:
    """Automatización real contra op_cucs.fwx (iframe op_ningr.fwx)."""

    def __init__(self, config: Configuracion) -> None:
        self.config = config
        self._sesion = GestorSesiones(config)
        eliminadas = self._sesion.limpiar_expiradas()
        if eliminadas:
            logger.info("[Sesión] %d archivo(s) de sesión expirados eliminados", eliminadas)

    @property
    def disponible(self) -> bool:
        return True

    # ------------------------------------------------------------------
    # API pública (contrato común con el simulado)
    # ------------------------------------------------------------------
    def inyectar_documento(self, documento: DocumentoRegistro) -> ResultadoRpa:
        """
        Ejecuta la inyección con reintentos exponenciales + jitter.

        En cada fallo: captura screenshot de evidencia, invalida la sesión
        si el error es de autenticación/expiración, y calcula la espera de
        backoff antes de reintentar (salvo que el error no sea transitorio
        o se haya agotado el presupuesto de intentos).
        """
        id_ejecucion = str(uuid.uuid4())
        reintentos = max(1, self.config.rpa_reintentos)
        ultimo_error: Optional[ErrorRpa] = None

        for intento in range(1, reintentos + 1):
            try:
                return self._ejecutar_intento(documento, id_ejecucion, intento)
            except ErrorRpa as exc:
                ultimo_error = exc

                if exc.codigo in {"SESION_EXPIRADA", "AUTENTICACION_INTRANET_FALLIDA"}:
                    self._sesion.invalidar()

                if not self._es_transitorio(exc) or intento == reintentos:
                    raise

                espera_s = self._calcular_espera_backoff(intento)
                logger.warning(
                    "[RPA] Intento %d/%d falló [%s]: %s — reintento en %.1fs",
                    intento, reintentos, exc.codigo, exc, espera_s,
                )
                time.sleep(espera_s)

        raise ultimo_error or ErrorRpa("RPA_FALLIDO", "Fallo RPA desconocido")  # pragma: no cover

    # ------------------------------------------------------------------
    # Orquestación de un intento
    # ------------------------------------------------------------------
    def _ejecutar_intento(self, documento: DocumentoRegistro, id_ejecucion: str, intento: int) -> ResultadoRpa:
        desde = time.monotonic()
        metadatos = documento.metadatos_validados
        assert metadatos is not None, "La salida solo se programa tras validar metadatos"

        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            navegador = self._lanzar_navegador(p)
            try:
                contexto = self._crear_contexto_con_sesion(navegador)
                pagina = contexto.new_page()
                pagina.set_default_timeout(self.config.rpa_timeout_ms)

                dialogo = self._registrar_manejador_dialogos(pagina)
                try:
                    try:
                        respuesta = pagina.goto(
                            self.config.intranet_base_url,
                            wait_until="domcontentloaded",
                            timeout=self.config.rpa_timeout_ms,
                        )
                    except Exception as exc:  # noqa: BLE001 — red/DNS/timeout de navegación
                        raise ErrorRpa(
                            "INTRANET_NO_ALCANZABLE", f"Error de conexión a la Intranet: {exc}",
                        ) from exc
                    self._validar_respuesta_navegacion(respuesta)

                    if self._detectar_sesion_expirada(pagina):
                        self._sesion.invalidar()
                        raise ErrorRpa(
                            "SESION_EXPIRADA",
                            "La sesión de la Intranet ha expirado (redirect a login).",
                        )

                    marco = self._resolver_marco_op_ningr(pagina)
                    self._esperar_webix_listo(marco)

                    self._rellenar_formulario_webix(marco, metadatos)
                    self._adjuntar_pdf_si_hay_control(pagina, marco, documento)

                    self._enviar_formulario(marco)
                    self._estabilizar_tras_envio(pagina, dialogo)

                    folio = self._extraer_folio_confirmacion(pagina, dialogo)
                    captura = self._guardar_evidencia(pagina, "03_procesados", "acuse", id_ejecucion)

                    # Sesión válida hasta el final: se persiste para el próximo envío.
                    self._sesion.guardar(contexto)

                    return ResultadoRpa(
                        id_ejecucion=id_ejecucion,
                        folio_acuse=folio,
                        fecha_ejecucion=datetime.now().isoformat(timespec="milliseconds"),
                        duracion_ms=int((time.monotonic() - desde) * 1000),
                        captura_acuse_path=captura,
                        intentos=intento,
                        mensaje_error=None,
                        exitoso=True,
                        simulado=False,
                    )
                finally:
                    pagina.remove_listener("dialog", dialogo["manejador"])
            except ErrorRpa as exc:
                self._capturar_fallo_intento(navegador, id_ejecucion, intento, exc.codigo)
                raise
            except Exception as exc:  # noqa: BLE001 — cualquier fallo no clasificado del stack
                # Se envuelve en ErrorRpa para que el bucle de reintentos de
                # inyectar_documento() pueda tratarlo como transitorio (p. ej.
                # "Target closed" de Playwright a mitad de una interacción).
                self._capturar_fallo_intento(navegador, id_ejecucion, intento, "INESPERADO")
                raise ErrorRpa(
                    "RPA_ERROR_INESPERADO", f"Error inesperado durante la ejecución: {exc}",
                ) from exc
            finally:
                navegador.close()

    # ------------------------------------------------------------------
    # Selección de navegador
    # ------------------------------------------------------------------
    def _lanzar_navegador(self, p):
        """
        Lanza el navegador según `RPA_NAVEGADOR`:

          - 'auto' (default): intenta primero el Microsoft Edge YA instalado
            en el sistema (preinstalado de fábrica en Windows 10 1809+ y en
            todo Windows 11 — Playwright lo localiza vía el argumento
            `channel="msedge"`, SIN descargar ni empaquetar nada) y, si no
            está disponible, cae al Chromium empaquetado por el instalador
            en `pw-browsers/` (componente opcional, ver packaging/oficialia.iss).
          - 'msedge': solo Edge del sistema — falla explícito si no está.
          - 'chromium': solo el Chromium empaquetado — omite Edge por completo.

        Preferir Edge reduce la dependencia del componente de ~300 MB del
        instalador a un respaldo, no un requisito: la automatización real
        funciona de fábrica en cualquier Windows 10/11 sin marcar esa casilla.
        """
        preferencia = self.config.rpa_navegador
        intentos: list[tuple[str, dict]] = []
        if preferencia in ("auto", "msedge"):
            intentos.append(("Microsoft Edge (sistema)", {"channel": "msedge"}))
        if preferencia in ("auto", "chromium"):
            intentos.append(("Chromium (empaquetado)", {}))

        ultimo_error: Optional[Exception] = None
        for nombre, kwargs in intentos:
            try:
                navegador = p.chromium.launch(headless=self.config.rpa_headless, **kwargs)
                logger.info("[RPA] Navegador en uso: %s", nombre)
                return navegador
            except Exception as exc:  # noqa: BLE001 — se prueba el siguiente candidato
                logger.warning("[RPA] No se pudo lanzar %s: %s", nombre, exc)
                ultimo_error = exc

        raise ErrorRpa(
            "NAVEGADOR_NO_DISPONIBLE",
            f"Ningún navegador disponible para RPA_NAVEGADOR={preferencia!r}: {ultimo_error}",
        )

    # ------------------------------------------------------------------
    # Ciclo de vida de sesión
    # ------------------------------------------------------------------
    def _crear_contexto_con_sesion(self, navegador):
        """
        Crea un contexto restaurando cookies/localStorage si hay un
        storage_state fresco. Si el archivo está corrupto o Playwright lo
        rechaza, invalida la sesión y reintenta sin ella (nunca bloquea el
        intento por un problema de la capa de conveniencia de sesión).
        """
        opciones = self._opciones_contexto()
        opciones.update(self._sesion.opciones_contexto())
        try:
            return navegador.new_context(**opciones)
        except Exception as exc:  # noqa: BLE001
            if "storage_state" in opciones:
                logger.warning("[Sesión] Error al restaurar estado, reintentando sin sesión: %s", exc)
                self._sesion.invalidar()
                opciones.pop("storage_state", None)
                return navegador.new_context(**opciones)
            raise

    def _detectar_sesion_expirada(self, pagina) -> bool:
        """Heurística: ¿fuimos redirigidos a una página de login?"""
        url = pagina.url.lower()
        if any(p in url for p in ("login", "signin", "acceso", "autenticacion", "logon")):
            return True
        try:
            campo_password = pagina.locator('input[type="password"]')
            if campo_password.count() > 0 and campo_password.first.is_visible(timeout=1000):
                return True
        except Exception:  # noqa: BLE001 — heurística de mejor esfuerzo
            pass
        return False

    # ------------------------------------------------------------------
    # Sesión y navegación (opciones de contexto / validación HTTP)
    # ------------------------------------------------------------------
    def _opciones_contexto(self) -> dict[str, Any]:
        opciones: dict[str, Any] = {
            "ignore_https_errors": True,
            "viewport": {"width": 1920, "height": 1080},
            "locale": "es-MX",
            "timezone_id": "America/Mexico_City",
        }
        # INTRANET_HTTP_USERNAME/PASSWORD tienen prioridad (credenciales HTTP
        # dedicadas del recurso); si se omiten, se usa la cuenta institucional
        # RPA_USUARIO/RPA_PASSWORD configurada para el login SII/Webix.
        usuario = (self.config.intranet_http_username.strip() or self.config.rpa_usuario.strip())
        contrasena = (self.config.intranet_http_password.strip() or self.config.rpa_password.strip())
        if usuario and contrasena:
            # HTTP Basic/Digest (para NTLM se requiere habilitar Negotiate en Chromium).
            opciones["http_credentials"] = {"username": usuario, "password": contrasena}
        return opciones

    def _validar_respuesta_navegacion(self, respuesta) -> None:
        if respuesta is None:
            raise ErrorRpa("INTRANET_NO_ALCANZABLE", "No se recibió respuesta HTTP de la Intranet.")
        estado = respuesta.status
        if estado == 401:
            raise ErrorRpa(
                "AUTENTICACION_INTRANET_FALLIDA",
                f"Credenciales de la Intranet rechazadas (HTTP {estado}). Verifique "
                "INTRANET_HTTP_USERNAME / INTRANET_HTTP_PASSWORD.",
            )
        if estado == 403:
            raise ErrorRpa("SESION_EXPIRADA", f"Acceso denegado o sesión expirada (HTTP {estado}).")
        if estado >= 400:
            raise ErrorRpa("INTRANET_NO_ALCANZABLE", f"Respuesta inesperada de la Intranet (HTTP {estado}).")

    # ------------------------------------------------------------------
    # Selectores tolerantes a demoras de red
    # ------------------------------------------------------------------
    def _esperar_con_reintentos(
        self,
        marco,
        expresion_js: str,
        *,
        descripcion: str,
        timeout_ms: Optional[int] = None,
        reintentos: int = 3,
        arg: Any = None,
    ) -> None:
        """
        `wait_for_function` con reintentos internos y backoff lineal.

        Útil cuando el framework Webix o un control tarda más de lo
        esperado en estar disponible (latencia del CDN de Webix, combos que
        cargan datos de forma diferida, etc.).
        """
        timeout = timeout_ms or self.config.rpa_selector_timeout_ms
        ultimo_error: Optional[Exception] = None

        for intento in range(1, reintentos + 1):
            try:
                marco.wait_for_function(expresion_js, arg=arg, timeout=timeout)
                return
            except Exception as exc:  # noqa: BLE001
                ultimo_error = exc
                if intento < reintentos:
                    espera = min(0.5 * intento, 2.0)
                    logger.debug(
                        "[Selector] %s no disponible (intento %d/%d), reintentando en %.1fs",
                        descripcion, intento, reintentos, espera,
                    )
                    time.sleep(espera)

        raise ErrorRpa(
            "FORMULARIO_WEBIX_TIMEOUT",
            f"{descripcion} no disponible tras {reintentos} intentos ({timeout}ms c/u): {ultimo_error}",
        )

    def _resolver_marco_op_ningr(self, pagina):
        """Resuelve el iframe op_ningr.fwx con reintentos y verificación de contentFrame."""
        timeout = self.config.rpa_selector_timeout_ms
        for intento in range(1, 4):
            try:
                handle = pagina.wait_for_selector(SELECTOR_IFRAME, state="attached", timeout=timeout)
                marco = handle.content_frame() if handle else None
                if marco is not None:
                    return marco
            except Exception as exc:  # noqa: BLE001
                if intento == 3:
                    raise ErrorRpa(
                        "FORMULARIO_WEBIX_TIMEOUT",
                        f"iframe op_ningr.fwx no resolvió contentFrame tras 3 intentos: {exc}",
                    ) from exc
                time.sleep(1.0 * intento)
        raise ErrorRpa("FORMULARIO_WEBIX_TIMEOUT", "iframe op_ningr.fwx no encontrado.")

    def _esperar_webix_listo(self, marco) -> None:
        """
        Espera en dos fases: framework Webix cargado → formulario y
        controles principales renderizados. Separar ambas fases da un
        diagnóstico más preciso que un único wait_for_function monolítico.
        """
        # Fase 1 — Framework Webix cargado (window.webix.$$ disponible).
        self._esperar_con_reintentos(
            marco,
            "() => !!(window.webix && typeof window.webix.$$ === 'function')",
            descripcion="framework Webix",
            timeout_ms=min(self.config.rpa_webix_init_timeout_ms, 10_000),
        )
        # Fase 2 — Formulario y controles principales (frm1, btnGuardar, cve).
        self._esperar_con_reintentos(
            marco,
            "() => {"
            "  const w = window.webix;"
            "  return !!(w && w.$$('frm1') && w.$$('btnGuardar') && w.$$('cve'));"
            "}",
            descripcion="formulario op_ningr (frm1, btnGuardar, cve)",
            timeout_ms=self.config.rpa_selector_timeout_ms,
        )

    @staticmethod
    def _pausa_cascada_webix(ms: int = 150) -> None:
        """Pausa para que Webix procese handlers en cascada (onChange → reload combos)."""
        time.sleep(ms / 1000)

    # ------------------------------------------------------------------
    # Inyección de datos en los controles Webix (mapeo por view id)
    # ------------------------------------------------------------------
    def _rellenar_formulario_webix(self, marco, metadatos) -> None:
        ahora = datetime.now()

        cve_oficialia = self._resolver_cve_oficialia(marco)
        if not cve_oficialia:
            raise ErrorRpa("FORMULARIO_WEBIX_TIMEOUT", "No fue posible resolver la oficialía (campo Webix 'cve').")

        # Campos base del formulario op_ningr.fwx (mismo orden que el original).
        self._asignar_webix(marco, "cve", cve_oficialia)
        self._asignar_webix_opcional(marco, "oficio_bis", False)

        self._asignar_webix(marco, "anio_ingr", str(ahora.year))
        self._asignar_webix(marco, "nume_cont", metadatos.numero_oficio)
        self._asignar_webix(marco, "fech_ofic", _formatear_fecha(self._parsear_fecha(metadatos.fecha_emision)))
        self._asignar_webix(marco, "info_sens", "1" if metadatos.contiene_datos_sensibles else "0")
        self._asignar_webix(marco, "tipo_info", "0")
        self._asignar_webix(marco, "fech_rece", _formatear_fecha(ahora))
        self._asignar_webix(marco, "hora_rece", _formatear_hora(ahora))
        self._asignar_webix(marco, "nume_ofic", metadatos.numero_oficio)

        # Limpieza de ligados por si la sesión arrastra valores previos.
        self._asignar_webix_opcional(marco, "cmbLiga_ofic", "")
        self._asignar_webix_opcional(marco, "liga_sali", [])
        self._asignar_webix_opcional(marco, "liga_entr", [])

        # Procedencia / dependencia.
        self._asignar_webix(marco, "rbDepe", "1" if metadatos.procedencia.value == "HCG" else "2")
        self._pausa_cascada_webix()  # onChange de rbDepe recarga combos dependientes
        self._aplicar_dependencia(marco, metadatos)

        # Remitente / destinatario.
        self._asignar_webix(marco, "remi_nomb", _limpiar_texto(metadatos.remitente_nombre))
        self._asignar_webix(marco, "remi_carg", _limpiar_texto(metadatos.remitente_cargo))
        self._asignar_webix(marco, "dest_nomb", _limpiar_texto(metadatos.destinatario_nombre))
        self._asignar_webix(marco, "dest_carg", _limpiar_texto(metadatos.destinatario_cargo))

        # Tipo de oficio: plazo estipulado ⇒ '5' (CON TÉRMINO); resto ⇒ '1' (ORIGINAL).
        con_plazo = metadatos.plazo_dias is not None and metadatos.plazo_dias > 0
        self._asignar_webix(marco, "tipo_ofic", "5" if con_plazo else "1")
        self._pausa_cascada_webix()  # onChange puede mostrar/ocultar los campos de término
        if con_plazo:
            fecha_emision = self._parsear_fecha(metadatos.fecha_emision)
            fecha_termino = _formatear_fecha(fecha_emision + timedelta(days=metadatos.plazo_dias or 0))
            self._asignar_webix_opcional(marco, "fech_term", fecha_termino)
            self._asignar_webix_opcional(marco, "txtFech_term", fecha_termino)

        # Clase: INVITACIÓN ⇒ '5'; resto ⇒ '4'.
        self._asignar_webix(marco, "clase", "5" if re.search(r"INVITACI[ÓO]N", metadatos.asunto) else "4")

        self._asignar_webix(marco, "tipo_ingr", "0")
        self._asignar_webix(marco, "asunto", _limpiar_texto(metadatos.asunto))

        if self.config.rpa_seccion_cve:
            self._asignar_webix_opcional(marco, "seccion", self.config.rpa_seccion_cve)

        self._asignar_webix_opcional(
            marco,
            "nota",
            f"PLAZO ESTIPULADO: {metadatos.plazo_dias} DÍA(S)" if con_plazo else "",
        )
        self._asignar_webix_opcional(marco, "ligado_a", "")

    def _aplicar_dependencia(self, marco, metadatos) -> None:
        """HCG: CVE institucional o búsqueda del combo por texto; Ajena: campo libre."""
        dependencia = _limpiar_texto(metadatos.dependencia_area)
        if metadatos.procedencia.value == "HCG":
            if self.config.rpa_hcg_dependencia_cve:
                self._asignar_webix(marco, "dependen", self.config.rpa_hcg_dependencia_cve)
            else:
                self._seleccionar_combo_por_texto(marco, "dependen", dependencia)
        else:
            self._asignar_webix_opcional(marco, "txtDepen", dependencia)
            self._asignar_webix_opcional(marco, "dependen", dependencia)

    def _resolver_cve_oficialia(self, marco) -> str:
        """CVE configurada, o la primera opción del combo 'cve'."""
        if self.config.rpa_oficialia_cve:
            return self.config.rpa_oficialia_cve
        try:
            self._esperar_con_reintentos(
                marco,
                "() => {"
                "  const c = window.webix?.$$('cve');"
                "  return !!(c?.getList?.()?.getFirstData?.());"
                "}",
                descripcion="combo 'cve' con datos",
                timeout_ms=5000,
                reintentos=2,
            )
        except ErrorRpa:
            pass  # Se intenta leer de todas formas.
        return marco.evaluate(
            "() => { const c = window.webix?.$$('cve'); return c?.getList?.()?.getFirstData?.()?.id ?? ''; }"
        ) or ""

    def _asignar_webix(self, marco, view_id: str, valor: Any, *, verificar: bool = False) -> None:
        """
        Asigna valor a un control Webix con espera tolerante:

        1. Sondea (wait_for_function) hasta que el control exista y exponga
           `setValue` — evita la carrera con controles que Webix aún no ha
           terminado de renderizar.
        2. Ejecuta `control.setValue(valor)`.
        3. Si `verificar` es True, relee el valor y solo deja constancia en
           el log ante discrepancias (nunca aborta el flujo por esto: el
           read-back es diagnóstico, no un requisito de éxito).
        """
        timeout_control = min(self.config.rpa_selector_timeout_ms, 3_000)

        try:
            marco.wait_for_function(
                "([id]) => {"
                "  const w = window.webix;"
                "  if (!w || typeof w.$$ !== 'function') return false;"
                "  const c = w.$$(id);"
                "  return !!(c && typeof c.setValue === 'function');"
                "}",
                arg=[view_id],
                timeout=timeout_control,
            )
        except Exception as exc:  # noqa: BLE001
            raise ErrorRpa(
                "FORMULARIO_WEBIX_TIMEOUT",
                f"Control Webix '{view_id}' no disponible tras {timeout_control}ms: {exc}",
            ) from exc

        try:
            marco.evaluate("([id, val]) => window.webix.$$(id).setValue(val)", [view_id, valor])
        except Exception as exc:  # noqa: BLE001
            raise ErrorRpa(
                "FORMULARIO_WEBIX_TIMEOUT", f"No se pudo asignar el campo Webix '{view_id}': {exc}",
            ) from exc

        if verificar:
            self._pausa_cascada_webix(50)
            try:
                actual = marco.evaluate(
                    "([id]) => window.webix?.$$(id)?.getValue?.() ?? null", [view_id],
                )
                if str(actual) != str(valor):
                    logger.warning(
                        "[Webix] Verificación falló para '%s': esperado=%s, actual=%s",
                        view_id, valor, actual,
                    )
            except Exception:  # noqa: BLE001 — el read-back es solo diagnóstico
                pass

    def _asignar_webix_opcional(self, marco, view_id: str, valor: Any) -> None:
        """Campo opcional/oculto: un fallo no bloquea el flujo."""
        try:
            self._asignar_webix(marco, view_id, valor)
        except ErrorRpa:
            pass

    def _seleccionar_combo_por_texto(self, marco, view_id: str, texto: str) -> None:
        """Búsqueda difusa por value/label en el combo y selección por id."""
        try:
            marco.evaluate(
                "([id, searchText]) => {"
                "  const w = window.webix;"
                "  if (!w || typeof w.$$ !== 'function') return;"
                "  const control = w.$$(id);"
                "  if (!control) return;"
                "  const normalize = (v) => String(v ?? '').toUpperCase();"
                "  const target = normalize(searchText);"
                "  const list = control.getList?.();"
                "  if (list && typeof list.find === 'function') {"
                "    const match = list.find((item) => {"
                "      const value = normalize(item?.value); const label = normalize(item?.label);"
                "      return value.includes(target) || label.includes(target);"
                "    });"
                "    if (match?.id) { control.setValue(match.id); return; }"
                "  }"
                "  control.setValue(searchText);"
                "}",
                [view_id, texto],
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Combo Webix '%s' no pudo fijarse por texto: %s", view_id, exc)

    # ------------------------------------------------------------------
    # Subida del PDF canónico
    # ------------------------------------------------------------------
    def _adjuntar_pdf_si_hay_control(self, pagina, marco, documento: DocumentoRegistro) -> None:
        """
        Adjunta el PDF canónico probando varios selectores de file input.

        Distingue dos casos: si NINGÚN selector encuentra un control (la
        pantalla no lo requiere), se omite en silencio — igual que el
        comportamiento original. Pero si un control SÍ existe y la subida
        falla, se propaga el error: adjuntar el PDF es parte del contrato
        institucional y una falla silenciosa dejaría un oficio incompleto
        registrado en la Intranet sin que nadie se entere.
        """
        ruta_absoluta = (self.config.storage_root / documento.ruta_archivo_actual).resolve()
        if not ruta_absoluta.is_file():
            logger.warning("PDF canónico ausente, se omite el adjunto: %s", ruta_absoluta)
            return

        candidatos = [
            marco.locator('input[type="file"]'),
            pagina.locator('input[type="file"]'),
            marco.locator('input[accept*="pdf"]'),
            marco.locator('.webix_upload_file input[type="file"]'),
        ]

        control_encontrado = False
        for locator in candidatos:
            try:
                if locator.count() == 0:
                    continue
                control_encontrado = True
                locator.first.set_input_files(str(ruta_absoluta), timeout=5000)
                logger.info("PDF adjuntado exitosamente: %s", ruta_absoluta.name)
                return
            except Exception as exc:  # noqa: BLE001 — control presente pero la subida falló: es un error real
                raise ErrorRpa(
                    "SUBIDA_ARCHIVO_FALLIDA", f"No fue posible adjuntar el PDF canónico: {exc}",
                ) from exc

        if not control_encontrado:
            logger.info("Sin control de archivo visible; adjunto omitido (la pantalla no lo requiere).")

    # ------------------------------------------------------------------
    # Envío, diálogos nativos y folio de acuse
    # ------------------------------------------------------------------
    def _enviar_formulario(self, marco) -> None:
        """Clic en el botón físico 'Ingresar oficio' o en btnGuardar vía API Webix."""
        try:
            boton = marco.locator('button:has-text("Ingresar oficio")').first
            boton.wait_for(state="visible", timeout=min(self.config.rpa_timeout_ms, 5000))
            boton.click(timeout=self.config.rpa_timeout_ms)
        except Exception:  # noqa: BLE001 — fallback por API Webix
            try:
                marco.evaluate(
                    "() => {"
                    "  const control = window.webix?.$$('btnGuardar');"
                    "  if (!control) throw new Error('Webix control no encontrado: btnGuardar');"
                    "  if (typeof control.callEvent === 'function') control.callEvent('onItemClick', []);"
                    "  else if (typeof control.click === 'function') control.click();"
                    "  else throw new Error('btnGuardar no expone mecanismo de click.');"
                    "}"
                )
            except Exception as exc:  # noqa: BLE001
                raise ErrorRpa("FORMULARIO_WEBIX_TIMEOUT", f"No se pudo enviar el formulario: {exc}") from exc

    def _registrar_manejador_dialogos(self, pagina) -> dict[str, Any]:
        """Captura el folio del alert() nativo y lo acepta siempre."""
        estado: dict[str, Any] = {"folio": None, "manejador": None}

        def manejador(dialogo) -> None:
            try:
                folio = self._parsear_folio(dialogo.message)
                if folio:
                    estado["folio"] = folio
            finally:
                try:
                    dialogo.accept()
                except Exception:  # noqa: BLE001
                    pass

        estado["manejador"] = manejador
        pagina.on("dialog", manejador)
        return estado

    def _estabilizar_tras_envio(self, pagina, dialogo: dict[str, Any]) -> None:
        """Espera breve: diálogo nativo, networkidle o timeout acotado."""
        espera_ms = min(self.config.rpa_timeout_ms, 2500)
        try:
            pagina.wait_for_load_state("networkidle", timeout=espera_ms)
        except Exception:  # noqa: BLE001
            time.sleep(espera_ms / 1000)

    def _extraer_folio_confirmacion(self, pagina, dialogo: dict[str, Any]) -> str:
        """Sondea diálogo, texto de página/frames y URL hasta dar con el folio."""
        plazo = time.monotonic() + self.config.rpa_timeout_ms / 1000
        while time.monotonic() < plazo:
            if dialogo.get("folio"):
                return str(dialogo["folio"])
            folio = self._leer_folio_de_pagina(pagina)
            if folio:
                return folio
            time.sleep(0.25)
        if dialogo.get("folio"):
            return str(dialogo["folio"])
        raise ErrorRpa(
            "FOLIO_CONFIRMACION_NO_ENCONTRADO",
            "No fue posible extraer el folio institucional de confirmación.",
        )

    def _leer_folio_de_pagina(self, pagina) -> Optional[str]:
        for candidato in [pagina, *pagina.frames]:
            try:
                texto = candidato.evaluate("() => document.body?.innerText ?? ''")
                folio = self._parsear_folio(texto or "")
                if folio:
                    return folio
                folio = self._parsear_folio(candidato.url)
                if folio:
                    return folio
            except Exception:  # noqa: BLE001
                continue
        return None

    def _parsear_folio(self, texto: str) -> Optional[str]:
        if not texto:
            return None
        normalizado = re.sub(r"\s+", " ", texto.upper())
        match = REGEX_FOLIO.search(normalizado)
        return match.group(0) if match else None

    # ------------------------------------------------------------------
    # Evidencia (screenshots de acuse / error)
    # ------------------------------------------------------------------
    def _guardar_evidencia(self, pagina, escenario: str, prefijo: str, id_ejecucion: str) -> str:
        ahora = datetime.now()
        relativa = Path(escenario) / f"{ahora:%Y}" / f"{ahora.month:02d}"
        absoluta = self.config.storage_root / relativa
        absoluta.mkdir(parents=True, exist_ok=True)
        ruta = absoluta / f"{prefijo}_{id_ejecucion}.png"
        try:
            pagina.wait_for_load_state("networkidle", timeout=2500)
        except Exception:  # noqa: BLE001
            pass
        pagina.screenshot(path=str(ruta), full_page=True)
        return str(relativa / ruta.name)

    def _capturar_fallo_intento(self, navegador, id_ejecucion: str, intento: int, codigo: str) -> None:
        """
        Screenshot de evidencia por intento fallido (mejor esfuerzo), en
        `04_errores/<YYYY>/<MM>/error_<id>_int<N>_<CODIGO>.png` — cada
        intento de una misma inyección queda documentado por separado, en
        vez de solo capturar el último fallo como en la versión anterior.
        """
        try:
            ctx = navegador.contexts[0] if navegador.contexts else None
            pagina = ctx.pages[0] if ctx and ctx.pages else None
            if pagina is None:
                return
            ahora = datetime.now()
            relativa = Path("04_errores") / f"{ahora:%Y}" / f"{ahora.month:02d}"
            absoluta = self.config.storage_root / relativa
            absoluta.mkdir(parents=True, exist_ok=True)
            ruta = absoluta / f"error_{id_ejecucion}_int{intento}_{codigo}.png"
            pagina.screenshot(path=str(ruta), full_page=True)
            logger.info("[RPA] Screenshot de fallo (intento %d): %s", intento, ruta)
        except Exception:  # noqa: BLE001 — la evidencia es mejor esfuerzo, nunca bloqueante
            logger.debug("No se pudo capturar screenshot de fallo (intento %d)", intento, exc_info=True)

    # ------------------------------------------------------------------
    # Clasificación de errores y utilidades
    # ------------------------------------------------------------------
    @staticmethod
    def _parsear_fecha(iso: str) -> date:
        try:
            return date.fromisoformat(iso)
        except ValueError as exc:
            raise ErrorRpa("FORMULARIO_WEBIX_TIMEOUT", f"Fecha inválida: {iso}") from exc

    def _es_transitorio(self, exc: ErrorRpa) -> bool:
        return exc.codigo in {
            "FORMULARIO_WEBIX_TIMEOUT",
            "INTRANET_NO_ALCANZABLE",
            "SESION_EXPIRADA",
            "OBJETIVO_CERRADO",
            "RPA_ERROR_INESPERADO",
        }

    def _calcular_espera_backoff(self, intento: int) -> float:
        """
        Backoff exponencial con jitter: `min(base × 2^(n-1), max) ± jitter`.

        El jitter evita que reintentos de múltiples documentos en cola
        converjan en el mismo instante (thundering herd) contra la
        Intranet. Con los defaults (base=1s, max=30s, jitter=±25%):
        intento 1 → ~1s, intento 2 → ~2s, intento 3 → ~4s, … hasta el techo.
        """
        base_ms = self.config.rpa_reintento_base_ms
        max_ms = self.config.rpa_reintento_max_ms
        jitter_factor = self.config.rpa_jitter_factor
        delay_ms = min(base_ms * (2 ** (intento - 1)), max_ms)
        jitter = delay_ms * jitter_factor * (2 * random.random() - 1)
        return max(0.1, (delay_ms + jitter) / 1000)


# ======================================================================
# Fábrica (composition root del modo RPA)
# ======================================================================

def crear_rpa(config: Optional[Configuracion] = None) -> Any:
    """Resuelve el worker RPA según RPA_MODO ('simulacion' | 'playwright')."""
    cfg = config or get_settings()
    if cfg.rpa_es_simulacion:
        return RpaSimulado(cfg)
    return RpaIntranet(cfg)
