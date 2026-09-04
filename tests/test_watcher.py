"""
tests/test_watcher.py — core.watcher.VigilanteCarpetas (canal SCANNER_ADF).

Sin cobertura previa. Se enfoca en `_procesar_archivo` directamente (sin
pasar por `_barrer_carpeta`/watchdog real): es donde vive la lógica que
importa probar y evita depender de temporización real de filesystem
(`watchfolder_estabilidad_ms`), que sería lenta y potencialmente
inestable en CI. La detección de estabilidad en sí (`_barrer_carpeta`) no
se toca en este cambio y no se cubre aquí.

Casos cubiertos — los dos puntos ciegos corregidos en este cambio:
    1. Archivo bloqueado por otro proceso (OSError en read_bytes, p. ej.
       WinError 32): antes reintentaba para siempre sin nunca aislar: el
       reintento debe estar acotado por WATCHFOLDER_MAX_REINTENTOS.
    2. Archivo que excede MAX_UPLOAD_BYTES: antes se leía entero a
       memoria sin ningún límite (a diferencia del canal WEB); debe
       aislarse sin reintentar y sin leer el contenido.
Además, casos de referencia (éxito y duplicado) para no dejar el módulo
sin ninguna cobertura de su camino normal.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from config import Configuracion
from core.models import DocumentoRegistro, EstadoDocumento, OrigenIngesta
from core.pipeline import DocumentoDuplicado
from core.watcher import VigilanteCarpetas, _ArchivoRastreado


class _PipelineFalso:
    """Doble mínimo del contrato _Pipeline (Protocol) de core.watcher."""

    def __init__(self, resultado=None, excepcion: Exception | None = None):
        self.resultado = resultado
        self.excepcion = excepcion
        self.llamadas: list[tuple[str, OrigenIngesta, bytes]] = []

    def ingestar_y_procesar(self, nombre: str, origen: OrigenIngesta, contenido: bytes):
        self.llamadas.append((nombre, origen, contenido))
        if self.excepcion is not None:
            raise self.excepcion
        return self.resultado


@pytest.fixture
def config_watcher(configuracion: Configuracion) -> Configuracion:
    """`configuracion` con las carpetas del watchfolder ya creadas."""
    configuracion.dir_entrada.mkdir(parents=True, exist_ok=True)
    configuracion.dir_errores.mkdir(parents=True, exist_ok=True)
    return configuracion


def _rastrear(vigilante: VigilanteCarpetas, ruta: Path, *, tamano: int | None = None) -> None:
    """Registra `ruta` como ya visto/estable, como haría _barrer_carpeta
    justo antes de invocar _procesar_archivo."""
    estadisticas = ruta.stat()
    vigilante._rastreados[ruta.name] = _ArchivoRastreado(
        tamano=estadisticas.st_size if tamano is None else tamano,
        mtime_ms=estadisticas.st_mtime * 1000,
    )


class TestArchivoBloqueado:
    def test_reintenta_acotado_y_luego_aisla(self, config_watcher, monkeypatch):
        """OSError persistente en read_bytes (bloqueo de archivo): debe
        reintentar hasta WATCHFOLDER_MAX_REINTENTOS y solo entonces aislar
        — antes no había tope y el archivo nunca se aislaba."""
        ruta = config_watcher.dir_entrada / "bloqueado.pdf"
        ruta.write_bytes(b"%PDF-1.4 contenido de prueba")

        original_read_bytes = Path.read_bytes

        def _read_bloqueado(self: Path):
            if self.name == "bloqueado.pdf":
                raise OSError(32, "The process cannot access the file")
            return original_read_bytes(self)

        monkeypatch.setattr(Path, "read_bytes", _read_bloqueado)

        vigilante = VigilanteCarpetas(configuracion=config_watcher, pipeline=_PipelineFalso())
        _rastrear(vigilante, ruta)

        for intento in range(1, config_watcher.watchfolder_max_reintentos):
            vigilante._procesar_archivo(ruta)
            assert ruta.exists(), f"no debía aislarse todavía (intento {intento})"
            assert not (config_watcher.dir_errores / "bloqueado.pdf").exists()

        # Último intento: agota el tope configurado y aísla.
        vigilante._procesar_archivo(ruta)
        assert not ruta.exists()
        destino = config_watcher.dir_errores / "bloqueado.pdf"
        assert destino.exists()
        motivo = Path(f"{destino}.error.txt").read_text(encoding="utf-8")
        assert "WATCHFOLDER_FILE_LOCKED" in motivo

    def test_libera_el_bloqueo_antes_de_agotar_reintentos_se_ingiere(self, config_watcher, monkeypatch):
        """Si el bloqueo se libera antes de agotar el tope, el siguiente
        intento debe leer e ingerir normalmente (no queda 'contaminado'
        por los fallos previos)."""
        ruta = config_watcher.dir_entrada / "temporal.pdf"
        ruta.write_bytes(b"%PDF-1.4 contenido")

        original_read_bytes = Path.read_bytes
        estado = {"fallos_restantes": 2}

        def _read_intermitente(self: Path):
            if self.name == "temporal.pdf" and estado["fallos_restantes"] > 0:
                estado["fallos_restantes"] -= 1
                raise OSError(32, "The process cannot access the file")
            return original_read_bytes(self)

        monkeypatch.setattr(Path, "read_bytes", _read_intermitente)

        pipeline = _PipelineFalso(resultado=object())
        vigilante = VigilanteCarpetas(configuracion=config_watcher, pipeline=pipeline)
        _rastrear(vigilante, ruta)

        vigilante._procesar_archivo(ruta)  # falla (1/2)
        vigilante._procesar_archivo(ruta)  # falla (2/2)
        assert len(pipeline.llamadas) == 0
        vigilante._procesar_archivo(ruta)  # se liberó: ingiere

        assert len(pipeline.llamadas) == 1
        assert not ruta.exists()  # original consumido tras ingesta persistida


class TestArchivoDemasiadoPesado:
    def test_se_aisla_sin_reintentar_ni_leer_contenido(self, config_watcher, monkeypatch):
        """Debe aislarse de inmediato usando el tamaño ya conocido por
        _barrer_carpeta, SIN llamar a read_bytes (evita cargar el archivo
        completo a memoria solo para rechazarlo)."""
        ruta = config_watcher.dir_entrada / "pesado.pdf"
        ruta.write_bytes(b"%PDF-1.4 contenido pequeno en disco")  # tamaño real irrelevante aquí

        def _read_bytes_no_deberia_llamarse(self: Path):
            raise AssertionError("no debía leerse un archivo que excede el límite de tamaño")

        monkeypatch.setattr(Path, "read_bytes", _read_bytes_no_deberia_llamarse)

        vigilante = VigilanteCarpetas(configuracion=config_watcher, pipeline=_PipelineFalso())
        _rastrear(vigilante, ruta, tamano=config_watcher.max_upload_bytes + 1)

        vigilante._procesar_archivo(ruta)

        assert not ruta.exists()
        destino = config_watcher.dir_errores / "pesado.pdf"
        assert destino.exists()
        motivo = Path(f"{destino}.error.txt").read_text(encoding="utf-8")
        assert "FILE_TOO_LARGE" in motivo

    def test_archivo_dentro_del_limite_no_se_afecta(self, config_watcher):
        """Un archivo dentro del límite no debe verse afectado por el
        nuevo chequeo (control negativo)."""
        ruta = config_watcher.dir_entrada / "normal.pdf"
        ruta.write_bytes(b"%PDF-1.4 contenido")

        pipeline = _PipelineFalso(resultado=object())
        vigilante = VigilanteCarpetas(configuracion=config_watcher, pipeline=pipeline)
        _rastrear(vigilante, ruta, tamano=config_watcher.max_upload_bytes - 1)

        vigilante._procesar_archivo(ruta)

        assert len(pipeline.llamadas) == 1
        assert not (config_watcher.dir_errores / "normal.pdf").exists()


class TestCaminosDeReferencia:
    """Éxito y duplicado — cobertura mínima del camino normal, no tocado
    por este cambio pero sin ninguna prueba previa."""

    def test_ingesta_exitosa_consume_el_original(self, config_watcher):
        ruta = config_watcher.dir_entrada / "oficio.pdf"
        contenido = b"%PDF-1.4 contenido de un oficio"
        ruta.write_bytes(contenido)

        pipeline = _PipelineFalso(resultado=object())
        vigilante = VigilanteCarpetas(configuracion=config_watcher, pipeline=pipeline)
        _rastrear(vigilante, ruta)

        vigilante._procesar_archivo(ruta)

        assert pipeline.llamadas == [("oficio.pdf", OrigenIngesta.SCANNER_ADF, contenido)]
        assert not ruta.exists()

    def test_duplicado_consume_el_original_sin_aislar_como_error(self, config_watcher):
        """El pipeline ya aisló SU propia copia (ver core.pipeline); el
        original del escáner simplemente sobra y se descarta, no debe
        además crearse una cuarentena en 04_errores para él."""
        ruta = config_watcher.dir_entrada / "repetido.pdf"
        ruta.write_bytes(b"%PDF-1.4 contenido repetido")

        existente = DocumentoRegistro(
            id="doc-existente",
            nombre_archivo_original="repetido.pdf",
            ruta_archivo_actual="03_procesados/2026/01/repetido.pdf",
            origen=OrigenIngesta.WEB_DRAG_DROP,
            estado=EstadoDocumento.COMPLETADO,
            sha256="a" * 64,
        )
        pipeline = _PipelineFalso(excepcion=DocumentoDuplicado(existente, "a" * 64))
        vigilante = VigilanteCarpetas(configuracion=config_watcher, pipeline=pipeline)
        _rastrear(vigilante, ruta)

        vigilante._procesar_archivo(ruta)

        assert not ruta.exists()
        assert not (config_watcher.dir_errores / "repetido.pdf").exists()
