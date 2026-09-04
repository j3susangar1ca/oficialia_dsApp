"""
SISTEMA OFICIALIA-DIGITAL-DSA (reconstrucción 100% Python)
==========================================================
core/watcher.py — Vigilancia en tiempo real de `storage/01_entrada/`.

Implementa la "Ingesta Dual" (canal SCANNER_ADF): un escáner departamental
deposita PDFs en el watchfolder (volumen local o montaje SMB) y este
servicio dispara la ingesta automáticamente, sin intervención humana.

Diseño (hibrido, por fiabilidad):
    - **watchdog** (requisito del sistema): detecta nuevos PDFs al instante
      y despierta el bucle de procesamiento (eventos created/moved).
    - **Poll de respaldo** (cada `WATCHFOLDER_INTERVALO_MS`): red barre el
      directorio. El original usaba SOLO polling porque `fs.watch`/inotify
      pierde eventos sobre SMB; el poll de respaldo conserva esa garantía
      mientras watchdog aporta la latencia baja en disco local.

"Estabilidad" de archivo (heredada del original): un PDF se considera listo
solo cuando su tamaño y mtime no cambian entre dos pasadas Y su mtime es
más antiguo que `WATCHFOLDER_ESTABILIDAD_MS` — evita ingerir a medias un
archivo que el escáner aún está escribiendo.

Deduplicación con el canal WEB: las copias que la propia aplicación escribe
en 01_entrada llevan prefijo `{epoch_ms}_` y se ignoran aquí. La
deduplicación por contenido (SHA-256, contra CUALQUIER documento ya
registrado, no solo los procesados) la hace `core.pipeline.
FlujoDocumental.ingestar_y_procesar` — este módulo no la duplica.

Después de ingerir un archivo, el ORIGINAL del escáner se consume (borra):
el pipeline ya persistió su propia copia o aisló una cuarentena con motivo.

Endurecido contra dos puntos ciegos del watchfolder (a diferencia del canal
WEB, aquí no hay un formulario que valide nada antes de que el archivo
llegue a disco):
    - Tamaño máximo (`MAX_UPLOAD_BYTES`, misma paridad que el canal WEB):
      se aísla sin reintentar, sin releer el archivo completo a memoria.
    - Bloqueo de archivo persistente (antivirus, un share SMB que no
      soltó el descriptor — WinError 32 en Windows): reintenta acotado
      por `WATCHFOLDER_MAX_REINTENTOS` (mismo contador que ya protegía
      los fallos de ingesta) en vez de reintentar para siempre en
      silencio sin aislar nunca el archivo.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol

from config import Configuracion, get_settings
from core.models import OrigenIngesta
from core.pipeline import DocumentoDuplicado, FlujoDocumental

logger = logging.getLogger("oficialia.watcher")

#: Prefijo de las copias escritas por la propia app (canal WEB).
PREFIJO_UPLOAD_PROPIO = re.compile(r"^\d{10,}_")


class _Pipeline(Protocol):
    """Contrato mínimo que el vigilante exige del orquestador."""

    def ingestar_y_procesar(self, nombre: str, origen: OrigenIngesta, contenido: bytes): ...


@dataclass
class _ArchivoRastreado:
    tamano: int
    mtime_ms: float
    intentos_fallo: int = 0


class VigilanteCarpetas:
    """Servicio de fondo: watchdog + poll de respaldo sobre 01_entrada/."""

    def __init__(
        self,
        configuracion: Optional[Configuracion] = None,
        pipeline: Optional[_Pipeline] = None,
    ) -> None:
        self.config = configuracion or get_settings()
        self.pipeline = pipeline
        self.dir_entrada = self.config.dir_entrada
        self.dir_errores = self.config.dir_errores

        self._rastreados: dict[str, _ArchivoRastreado] = {}
        self._en_vuelo: set[str] = set()
        self._despertar = threading.Event()
        self._hilo: Optional[threading.Thread] = None
        self._detener = threading.Event()
        self._observador = None

    # ------------------------------------------------------------------
    # Ciclo de vida
    # ------------------------------------------------------------------
    def iniciar(self) -> None:
        """Arranca el observador watchdog y el hilo trabajador."""
        if self._hilo is not None:
            return
        if not self.config.watchfolder_enabled:
            logger.info("Vigilante de carpeta DESACTIVADO (WATCHFOLDER_ENABLED=false)")
            return

        self.dir_entrada.mkdir(parents=True, exist_ok=True)

        from watchdog.events import PatternMatchingEventHandler
        from watchdog.observers import Observer

        vigilante = self

        class _Handler(PatternMatchingEventHandler):
            """Despierta el bucle ante cualquier evento de PDF en la carpeta."""

            def __init__(self) -> None:
                super().__init__(patterns=["*.pdf"], ignore_directories=True, case_sensitive=False)

            def on_created(self, event) -> None:  # noqa: D102
                vigilante._despertar.set()

            def on_moved(self, event) -> None:  # noqa: D102 — move-in desde otra carpeta
                vigilante._despertar.set()

        self._observador = Observer()
        self._observador.schedule(_Handler(), str(self.dir_entrada), recursive=False)
        self._observador.start()

        self._hilo = threading.Thread(
            target=self._bucle, name="oficialia-watcher", daemon=True
        )
        self._hilo.start()
        logger.info(
            "Vigilando %s (watchdog + respaldo cada %d ms, estabilidad %d ms)",
            self.dir_entrada,
            self.config.watchfolder_intervalo_ms,
            self.config.watchfolder_estabilidad_ms,
        )

    def detener(self) -> None:
        """Detención ordenada (observador + hilo trabajador)."""
        self._detener.set()
        self._despertar.set()
        if self._observador is not None:
            self._observador.stop()
            self._observador.join(timeout=3)
        if self._hilo is not None:
            self._hilo.join(timeout=5)
            self._hilo = None

    # ------------------------------------------------------------------
    # Bucle principal
    # ------------------------------------------------------------------
    def _bucle(self) -> None:
        """Worker: duerme hasta evento de watchdog o timeout del respaldo."""
        while not self._detener.is_set():
            self._despertar.wait(timeout=self.config.watchfolder_intervalo_ms / 1000)
            self._despertar.clear()
            try:
                self._barrer_carpeta()
            except Exception:  # noqa: BLE001 — el vigilante nunca muere
                logger.exception("Fallo del ciclo de vigilancia; se reintenta en el siguiente tick")

    def _barrer_carpeta(self) -> None:
        """Una pasada: detecta archivos estables y los ingiere."""
        if self.pipeline is None:
            return
        vistos_ahora: set[str] = set()

        for entrada in sorted(self.dir_entrada.iterdir()):
            if not entrada.is_file():
                continue
            if entrada.suffix.lower() != ".pdf":
                continue
            if PREFIJO_UPLOAD_PROPIO.match(entrada.name):
                continue  # copia del canal WEB: ya la procesa el upload
            if entrada.name in self._en_vuelo:
                continue

            vistos_ahora.add(entrada.name)
            estadisticas = entrada.stat()

            previo = self._rastreados.get(entrada.name)
            if (
                previo is None
                or previo.tamano != estadisticas.st_size
                or abs(previo.mtime_ms - estadisticas.st_mtime * 1000) > 1
            ):
                # Nuevo o todavía "caliente": registrar y esperar otra pasada.
                self._rastreados[entrada.name] = _ArchivoRastreado(
                    tamano=estadisticas.st_size,
                    mtime_ms=estadisticas.st_mtime * 1000,
                    intentos_fallo=previo.intentos_fallo if previo else 0,
                )
                continue

            if time.time() * 1000 - estadisticas.st_mtime * 1000 < self.config.watchfolder_estabilidad_ms:
                continue  # sin cambios pero aún muy reciente

            self._en_vuelo.add(entrada.name)
            try:
                self._procesar_archivo(entrada)
            finally:
                self._en_vuelo.discard(entrada.name)

        # Deja de rastrear archivos que ya no están (ingeridos o removidos).
        for nombre in list(self._rastreados):
            if nombre not in vistos_ahora:
                self._rastreados.pop(nombre, None)

    # ------------------------------------------------------------------
    # Ingesta de un archivo estable (con reintentos y cuarentena)
    # ------------------------------------------------------------------
    def _procesar_archivo(self, ruta: Path) -> None:
        rastreado = self._rastreados.get(ruta.name)
        if rastreado is None:
            rastreado = _ArchivoRastreado(0, 0.0)

        # Límite de tamaño (paridad con el canal WEB, ver
        # ui.views_dashboard._manejar_carga): antes solo el upload web lo
        # aplicaba — un PDF pesado dejado en el watchfolder se leía entero
        # a memoria y avanzaba sin control. Se comprueba con el tamaño ya
        # confirmado estable por _barrer_carpeta, sin releer el archivo.
        if rastreado.tamano > self.config.max_upload_bytes:
            limite_mb = self.config.max_upload_bytes // (1024 * 1024)
            logger.warning(
                "%s (%.1f MB) excede el límite de %d MB, se aísla sin reintentar",
                ruta.name, rastreado.tamano / (1024 * 1024), limite_mb,
            )
            self._aislar(ruta, f"FILE_TOO_LARGE :: {rastreado.tamano} bytes (máx. {self.config.max_upload_bytes})")
            return

        try:
            contenido = ruta.read_bytes()
        except OSError as exc:
            # Archivo bloqueado por otro proceso (antivirus, un share SMB
            # que aún no soltó el descriptor, etc.): antes se reintentaba
            # para siempre, sin tope ni cuarentena — un bloqueo permanente
            # (permisos, un proceso que nunca libera el archivo) quedaba
            # dando vueltas en el log sin que nadie se enterara nunca. Se
            # reutiliza el mismo contador/tope acotado que ya protege el
            # camino de fallo de ingesta, más abajo.
            self._reintentar_o_aislar(ruta, rastreado, "WATCHFOLDER_FILE_LOCKED", exc)
            return

        if not contenido:
            logger.warning("%s está vacío, se aísla sin reintentar", ruta.name)
            self._aislar(ruta, "EMPTY_FILE_FROM_WATCHFOLDER")
            return

        try:
            self.pipeline.ingestar_y_procesar(ruta.name, OrigenIngesta.SCANNER_ADF, contenido)
            logger.info("Ingerido %s desde el watchfolder", ruta.name)
            self._consumir_original(ruta)
        except DocumentoDuplicado as exc:
            # El pipeline ya aisló SU copia; el original del escáner sobra.
            logger.warning("%s: %s", ruta.name, exc)
            self._consumir_original(ruta)
        except Exception as exc:  # noqa: BLE001 — fallo inesperado
            self._reintentar_o_aislar(ruta, rastreado, "WATCHFOLDER_MAX_RETRIES", exc)

    def _reintentar_o_aislar(
        self, ruta: Path, rastreado: "_ArchivoRastreado", codigo: str, exc: Exception
    ) -> None:
        """Cuenta un intento fallido (lectura bloqueada o ingesta) y, al
        agotar `watchfolder_max_reintentos`, aísla en cuarentena — antes
        SOLO el camino de fallo de ingesta tenía este tope; un bloqueo de
        lectura persistente reintentaba indefinidamente sin nunca avisar."""
        intentos = rastreado.intentos_fallo + 1
        logger.error(
            "Fallo procesando %s (intento %d/%d): %s",
            ruta.name, intentos, self.config.watchfolder_max_reintentos, exc,
        )
        if intentos >= self.config.watchfolder_max_reintentos:
            self._aislar(ruta, f"{codigo} :: {exc}")
            return
        self._rastreados[ruta.name] = _ArchivoRastreado(
            tamano=rastreado.tamano, mtime_ms=rastreado.mtime_ms, intentos_fallo=intentos
        )

    def _consumir_original(self, ruta: Path) -> None:
        """Borra el original del escáner tras una ingesta persistida."""
        try:
            ruta.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("No se pudo borrar el original %s: %s", ruta.name, exc)
        finally:
            self._rastreados.pop(ruta.name, None)

    def _aislar(self, ruta: Path, motivo: str) -> None:
        """Último recurso: cuarentena en 04_errores/ con motivo anexo."""
        try:
            self.dir_errores.mkdir(parents=True, exist_ok=True)
            destino = self.dir_errores / ruta.name
            ruta.replace(destino)
            with open(f"{destino}.error.txt", "a", encoding="utf-8") as handle:
                handle.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} :: {motivo}\n")
        except OSError:
            logger.exception("No se pudo aislar %s", ruta.name)
        finally:
            self._rastreados.pop(ruta.name, None)
