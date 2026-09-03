"""
SISTEMA OFICIALIA-DIGITAL-DSA (reconstrucción 100% Python)
==========================================================
database.py — Conexión SQLite (modo WAL) y repositorio CRUD de documentos.

Consolidación deliberada respecto del original: las 4 tablas
(`documentos`, `preproceso_metadata`, `rpa_ejecuciones`, `google_sheets_sync`)
se unifican en UNA sola tabla `documentos` con columnas JSON embebidas
(preproceso/rpa/sheets), porque todas comparten relación 1:1 con el documento.
Se conservan íntegras las garantías: WAL, busy_timeout, índices de bandeja,
hash UNIQUE para deduplicación y control de concurrencia optimista por `version`.

Cada operación abre su propia conexión corta (seguro entre hilos del watcher,
del pipeline y de la UI) y cierra con commit automático.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional, Sequence

from core.models import (
    DocumentoRegistro,
    EstadoDocumento,
    EstadoSheets,
    InfoPreproceso,
    MetadatosOficio,
    MetodoExtraccion,
    OrigenIngesta,
    ResultadoRpa,
    ahora_utc_iso,
)
from config import Configuracion, get_settings

logger = logging.getLogger("oficialia.db")

#: Centinela: "no modificar esta columna" en actualizar_estado().
SIN_CAMBIO = object()

#: SQL de creación del esquema (idempotente).
ESQUEMA_SQL = """
CREATE TABLE IF NOT EXISTS documentos (
    id                       TEXT PRIMARY KEY,
    nombre_archivo_original  TEXT NOT NULL,
    nombre_archivo_canonico  TEXT,
    ruta_archivo_actual      TEXT NOT NULL,
    ruta_espejo_json         TEXT,
    origen                   TEXT NOT NULL
        CHECK (origen IN ('SCANNER_ADF', 'WEB_DRAG_DROP')),
    estado                   TEXT NOT NULL
        CHECK (estado IN ('INGESTADO', 'EN_PREPROCESO', 'EXTRAYENDO',
                          'PENDIENTE_REVISION', 'EJECUTANDO_RPA', 'ERROR_RPA',
                          'COMPLETADO', 'DESCARTADO')),
    sha256_hash              TEXT NOT NULL UNIQUE,
    numero_oficio            TEXT,
    metadatos_extraidos      TEXT,
    metadatos_validados      TEXT,
    preproceso_json          TEXT,
    rpa_json                 TEXT,
    sheets_json              TEXT,
    error_msg                TEXT,
    revisor_usuario_id       TEXT,
    fecha_ingesta            TEXT NOT NULL,
    fecha_validacion_hitl    TEXT,
    fecha_finalizacion       TEXT,
    updated_at               TEXT NOT NULL,
    version                  INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    extraccion_metodo        TEXT NOT NULL DEFAULT 'IA'
        CHECK (extraccion_metodo IN ('IA', 'HEURISTICA_FALLBACK', 'HITL'))
);

CREATE INDEX IF NOT EXISTS idx_documentos_numero_oficio ON documentos(numero_oficio);
CREATE INDEX IF NOT EXISTS idx_documentos_estado        ON documentos(estado);
CREATE INDEX IF NOT EXISTS idx_documentos_estado_fecha  ON documentos(estado, fecha_ingesta);
CREATE INDEX IF NOT EXISTS idx_documentos_fecha_ingesta ON documentos(fecha_ingesta);
"""

# ======================================================================
# Versionado de esquema (PRAGMA user_version) — migraciones incrementales
# ======================================================================
#
# `ESQUEMA_SQL` de arriba crea la tabla desde cero para una BD nueva (todas
# las columnas de la versión más reciente). `_MIGRACIONES` es la ruta que
# sigue una BD YA EXISTENTE de una versión anterior: cada entrada agrega lo
# que le falte a esa versión concreta. Ambos caminos deben converger al
# mismo esquema final — de lo contrario, actualizar la app en la máquina de
# un usuario con una BD antigua fallaría en vez de migrar silenciosamente.
#
# Índice de la lista = versión ORIGEN (0-based); cada función deja la BD en
# `version + 1`. Agregar una migración nueva NUNCA debe reescribir una ya
# publicada (el historial de versiones ya entregadas a clientes es inmutable).


def _migracion_0_a_1(conn: sqlite3.Connection) -> None:
    """
    v0 → v1: agrega `extraccion_metodo` (IA | HEURISTICA_FALLBACK | HITL),
    usada por el extractor heurístico de respaldo (core/heuristic_extractor.py)
    para que la revisión HITL sepa distinguir una extracción de Gemini de
    una de solo-regex cuando la IA no está disponible.
    """
    columnas = {fila["name"] for fila in conn.execute("PRAGMA table_info(documentos)")}
    if "extraccion_metodo" not in columnas:
        conn.execute(
            "ALTER TABLE documentos ADD COLUMN extraccion_metodo TEXT NOT NULL DEFAULT 'IA' "
            "CHECK (extraccion_metodo IN ('IA', 'HEURISTICA_FALLBACK', 'HITL'))"
        )


#: Migraciones en orden: `_MIGRACIONES[N]` lleva de la versión N a la N+1.
#: `VERSION_ESQUEMA` (= len(_MIGRACIONES)) es la versión objetivo actual.
_MIGRACIONES: list = [_migracion_0_a_1]
VERSION_ESQUEMA: int = len(_MIGRACIONES)


class ErrorConcurrencia(Exception):
    """La versión esperada no coincide: otro proceso mutó el registro antes."""


class RepositorioDocumentos:
    """CRUD simple y tipado sobre la tabla `documentos`."""

    def __init__(self, configuracion: Optional[Configuracion] = None) -> None:
        self.config = configuracion or get_settings()
        self.config.database_path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Conexión corta con pragmas operativos (WAL, timeout, FK)
    # ------------------------------------------------------------------
    @contextmanager
    def _conexion(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.config.database_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.execute("PRAGMA foreign_keys = ON")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Inicialización del esquema + migraciones incrementales
    # ------------------------------------------------------------------
    def inicializar(self) -> None:
        """
        Crea el esquema si no existe (BD nueva: ya nace en VERSION_ESQUEMA) y
        aplica las migraciones pendientes si es una BD de una versión
        anterior (actualización de la app sobre una instalación existente).
        Idempotente y segura de llamar en cada arranque.
        """
        with self._conexion() as conn:
            conn.executescript(ESQUEMA_SQL)

            version_actual = conn.execute("PRAGMA user_version").fetchone()[0]
            if version_actual < VERSION_ESQUEMA:
                logger.info(
                    "Migrando esquema SQLite: v%d → v%d", version_actual, VERSION_ESQUEMA
                )
            for version_origen in range(version_actual, VERSION_ESQUEMA):
                _MIGRACIONES[version_origen](conn)
                conn.execute(f"PRAGMA user_version = {version_origen + 1}")
        logger.info(
            "SQLite listo (WAL, esquema v%d) en %s", VERSION_ESQUEMA, self.config.database_path
        )

    # ------------------------------------------------------------------
    # Mapeo fila ⇄ modelo
    # ------------------------------------------------------------------
    @staticmethod
    def _a_modelo(fila: sqlite3.Row) -> DocumentoRegistro:
        def _json_metadatos(crudo: Optional[str]) -> Optional[MetadatosOficio]:
            return MetadatosOficio.model_validate_json(crudo) if crudo else None

        return DocumentoRegistro(
            id=fila["id"],
            nombre_archivo_original=fila["nombre_archivo_original"],
            nombre_archivo_canonico=fila["nombre_archivo_canonico"],
            ruta_archivo_actual=fila["ruta_archivo_actual"],
            ruta_espejo_json=fila["ruta_espejo_json"],
            origen=OrigenIngesta(fila["origen"]),
            estado=EstadoDocumento(fila["estado"]),
            sha256=fila["sha256_hash"],
            numero_oficio=fila["numero_oficio"],
            metadatos_extraidos=_json_metadatos(fila["metadatos_extraidos"]),
            metadatos_validados=_json_metadatos(fila["metadatos_validados"]),
            preproceso=InfoPreproceso.model_validate_json(fila["preproceso_json"]) if fila["preproceso_json"] else None,
            rpa=ResultadoRpa.model_validate_json(fila["rpa_json"]) if fila["rpa_json"] else None,
            sheets=EstadoSheets.model_validate_json(fila["sheets_json"]) if fila["sheets_json"] else EstadoSheets(),
            error_msg=fila["error_msg"],
            revisor_usuario_id=fila["revisor_usuario_id"],
            fecha_ingesta=fila["fecha_ingesta"],
            fecha_validacion_hitl=fila["fecha_validacion_hitl"],
            fecha_finalizacion=fila["fecha_finalizacion"],
            updated_at=fila["updated_at"],
            version=fila["version"],
            extraccion_metodo=MetodoExtraccion(fila["extraccion_metodo"]),
        )

    # ------------------------------------------------------------------
    # Escrituras
    # ------------------------------------------------------------------
    def crear(self, registro: DocumentoRegistro) -> DocumentoRegistro:
        """Inserta el registro raíz (estado INGESTADO al ingerir)."""
        with self._conexion() as conn:
            conn.execute(
                """
                INSERT INTO documentos (
                    id, nombre_archivo_original, ruta_archivo_actual, origen, estado,
                    sha256_hash, numero_oficio, metadatos_extraidos, metadatos_validados,
                    preproceso_json, rpa_json, sheets_json, error_msg, revisor_usuario_id,
                    fecha_ingesta, fecha_validacion_hitl, fecha_finalizacion, updated_at, version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    registro.id,
                    registro.nombre_archivo_original,
                    registro.ruta_archivo_actual,
                    registro.origen.value,
                    registro.estado.value,
                    registro.sha256,
                    registro.numero_oficio,
                    registro.metadatos_extraidos.model_dump_json() if registro.metadatos_extraidos else None,
                    registro.metadatos_validados.model_dump_json() if registro.metadatos_validados else None,
                    registro.preproceso.model_dump_json() if registro.preproceso else None,
                    registro.rpa.model_dump_json() if registro.rpa else None,
                    registro.sheets.model_dump_json(),
                    registro.error_msg,
                    registro.revisor_usuario_id,
                    registro.fecha_ingesta,
                    registro.fecha_validacion_hitl,
                    registro.fecha_finalizacion,
                    ahora_utc_iso(),
                    registro.version,
                ),
            )
        return registro

    def actualizar_estado(
        self,
        doc_id: str,
        estado: EstadoDocumento,
        *,
        version_esperada: int,
        nueva_ruta: Optional[str] = None,
        error_msg: object = SIN_CAMBIO,
        finalizado: bool = False,
    ) -> DocumentoRegistro:
        """
        Transición de estado con concurrencia optimista.

        :param error_msg: `None` limpia el error; ``SIN_CAMBIO`` (default) lo conserva.
        :param finalizado: marca `fecha_finalizacion` (COMPLETADO / DESCARTADO).
        """
        asignaciones = ["estado = ?", "updated_at = ?", "version = version + 1"]
        parametros: list[object] = [estado.value, ahora_utc_iso()]
        if nueva_ruta is not None:
            asignaciones.append("ruta_archivo_actual = ?")
            parametros.append(nueva_ruta)
        if error_msg is not SIN_CAMBIO:
            asignaciones.append("error_msg = ?")
            parametros.append(error_msg)
        if finalizado:
            asignaciones.append("fecha_finalizacion = ?")
            parametros.append(ahora_utc_iso())

        parametros.extend([doc_id, version_esperada])
        with self._conexion() as conn:
            cursor = conn.execute(
                f"UPDATE documentos SET {', '.join(asignaciones)} WHERE id = ? AND version = ?",
                parametros,
            )
            if cursor.rowcount == 0:
                raise ErrorConcurrencia(
                    f"Conflicto de versión al actualizar {doc_id} (esperada {version_esperada})"
                )
        registro = self.obtener(doc_id)
        assert registro is not None
        return registro

    def guardar_metadatos_extraidos(
        self,
        doc_id: str,
        metadatos: MetadatosOficio,
        estado: EstadoDocumento,
        *,
        version_esperada: int,
        extraccion_metodo: MetodoExtraccion = MetodoExtraccion.IA,
    ) -> DocumentoRegistro:
        """
        Persiste los metadatos extraídos y el estado resultante.

        :param extraccion_metodo: IA (default, Gemini) o HEURISTICA_FALLBACK
            cuando el extractor de respaldo (core/heuristic_extractor.py)
            produjo estos metadatos porque la IA no estaba disponible —
            queda visible en la bandeja/HITL para que el revisor sepa que
            debe verificar/completar TODOS los campos, no solo confirmar.
        """
        with self._conexion() as conn:
            cursor = conn.execute(
                """
                UPDATE documentos
                   SET metadatos_extraidos = ?, numero_oficio = ?, estado = ?,
                       extraccion_metodo = ?, error_msg = NULL, updated_at = ?,
                       version = version + 1
                 WHERE id = ? AND version = ?
                """,
                (
                    metadatos.model_dump_json(),
                    metadatos.numero_oficio,
                    estado.value,
                    extraccion_metodo.value,
                    ahora_utc_iso(),
                    doc_id,
                    version_esperada,
                ),
            )
            if cursor.rowcount == 0:
                raise ErrorConcurrencia(f"Conflicto de versión al guardar metadatos de {doc_id}")
        registro = self.obtener(doc_id)
        assert registro is not None
        return registro

    def guardar_preproceso(
        self, doc_id: str, preproceso: InfoPreproceso, *, version_esperada: int
    ) -> DocumentoRegistro:
        with self._conexion() as conn:
            cursor = conn.execute(
                """
                UPDATE documentos
                   SET preproceso_json = ?, updated_at = ?, version = version + 1
                 WHERE id = ? AND version = ?
                """,
                (preproceso.model_dump_json(), ahora_utc_iso(), doc_id, version_esperada),
            )
            if cursor.rowcount == 0:
                raise ErrorConcurrencia(f"Conflicto de versión al guardar preproceso de {doc_id}")
        registro = self.obtener(doc_id)
        assert registro is not None
        return registro

    def guardar_confirmacion_hitl(
        self,
        doc_id: str,
        *,
        metadatos: MetadatosOficio,
        nombre_canonico: str,
        ruta_pdf: str,
        ruta_json: str,
        revisor: str,
        version_esperada: int,
    ) -> DocumentoRegistro:
        """Persiste la validación humana + nomenclatura canónica + rutas finales."""
        with self._conexion() as conn:
            cursor = conn.execute(
                """
                UPDATE documentos
                   SET metadatos_validados = ?, numero_oficio = ?, nombre_archivo_canonico = ?,
                       ruta_archivo_actual = ?, ruta_espejo_json = ?, revisor_usuario_id = ?,
                       fecha_validacion_hitl = ?, error_msg = NULL, updated_at = ?,
                       version = version + 1
                 WHERE id = ? AND version = ?
                """,
                (
                    metadatos.model_dump_json(),
                    metadatos.numero_oficio,
                    nombre_canonico,
                    ruta_pdf,
                    ruta_json,
                    revisor,
                    ahora_utc_iso(),
                    ahora_utc_iso(),
                    doc_id,
                    version_esperada,
                ),
            )
            if cursor.rowcount == 0:
                raise ErrorConcurrencia(f"Conflicto de versión al confirmar {doc_id}")
        registro = self.obtener(doc_id)
        assert registro is not None
        return registro

    def guardar_resultado_rpa(
        self,
        doc_id: str,
        resultado: ResultadoRpa,
        estado_final: EstadoDocumento,
        *,
        version_esperada: int,
    ) -> DocumentoRegistro:
        """Persiste la ejecución RPA y su estado terminal (COMPLETADO / ERROR_RPA)."""
        finalizado = estado_final == EstadoDocumento.COMPLETADO or estado_final == EstadoDocumento.DESCARTADO
        with self._conexion() as conn:
            cursor = conn.execute(
                """
                UPDATE documentos
                   SET rpa_json = ?, estado = ?, error_msg = ?,
                       fecha_finalizacion = CASE WHEN ? THEN ? ELSE fecha_finalizacion END,
                       updated_at = ?, version = version + 1
                 WHERE id = ? AND version = ?
                """,
                (
                    resultado.model_dump_json(),
                    estado_final.value,
                    resultado.mensaje_error if not resultado.exitoso else None,
                    1 if finalizado else 0,
                    ahora_utc_iso(),
                    ahora_utc_iso(),
                    doc_id,
                    version_esperada,
                ),
            )
            if cursor.rowcount == 0:
                raise ErrorConcurrencia(f"Conflicto de versión al guardar RPA de {doc_id}")
        registro = self.obtener(doc_id)
        assert registro is not None
        return registro

    def guardar_sheets(self, doc_id: str, estado: EstadoSheets, *, version_esperada: int) -> DocumentoRegistro:
        with self._conexion() as conn:
            cursor = conn.execute(
                """
                UPDATE documentos
                   SET sheets_json = ?, updated_at = ?, version = version + 1
                 WHERE id = ? AND version = ?
                """,
                (estado.model_dump_json(), ahora_utc_iso(), doc_id, version_esperada),
            )
            if cursor.rowcount == 0:
                raise ErrorConcurrencia(f"Conflicto de versión al guardar sincronización de {doc_id}")
        registro = self.obtener(doc_id)
        assert registro is not None
        return registro

    # ------------------------------------------------------------------
    # Lecturas
    # ------------------------------------------------------------------
    def obtener(self, doc_id: str) -> Optional[DocumentoRegistro]:
        with self._conexion() as conn:
            fila = conn.execute("SELECT * FROM documentos WHERE id = ?", (doc_id,)).fetchone()
        return self._a_modelo(fila) if fila else None

    def obtener_por_hash(self, sha256: str) -> Optional[DocumentoRegistro]:
        with self._conexion() as conn:
            fila = conn.execute("SELECT * FROM documentos WHERE sha256_hash = ?", (sha256,)).fetchone()
        return self._a_modelo(fila) if fila else None

    def listar(
        self,
        *,
        estados: Optional[Sequence[EstadoDocumento]] = None,
        texto_busqueda: str = "",
        limite: int = 200,
    ) -> list[DocumentoRegistro]:
        """Bandeja: filtro por estados + buscador en vivo (archivo/folio/remitente/asunto)."""
        condiciones: list[str] = []
        parametros: list[object] = []

        if estados:
            marcadores = ", ".join("?" for _ in estados)
            condiciones.append(f"estado IN ({marcadores})")
            parametros.extend(e.value for e in estados)

        texto = texto_busqueda.strip().lower()
        if texto:
            condiciones.append(
                "(LOWER(nombre_archivo_original) LIKE ? OR LOWER(IFNULL(numero_oficio, '')) LIKE ? "
                "OR LOWER(IFNULL(metadatos_extraidos, '') || IFNULL(metadatos_validados, '')) LIKE ?)"
            )
            like = f"%{texto}%"
            parametros.extend([like, like, like])

        where = f"WHERE {' AND '.join(condiciones)}" if condiciones else ""
        sql = f"SELECT * FROM documentos {where} ORDER BY fecha_ingesta DESC LIMIT ?"
        parametros.append(limite)

        with self._conexion() as conn:
            filas = conn.execute(sql, parametros).fetchall()
        return [self._a_modelo(fila) for fila in filas]

    def contadores_kpi(self) -> dict[str, int]:
        """KPIs de la bandeja: pendientes, en proceso, errores, completados y total."""
        with self._conexion() as conn:
            filas = conn.execute("SELECT estado, COUNT(*) AS total FROM documentos GROUP BY estado").fetchall()
        conteo = {fila["estado"]: fila["total"] for fila in filas}

        en_proceso = sum(conteo.get(e.value, 0) for e in (
            EstadoDocumento.INGESTADO,
            EstadoDocumento.EN_PREPROCESO,
            EstadoDocumento.EXTRAYENDO,
            EstadoDocumento.EJECUTANDO_RPA,
        ))
        return {
            "pendientes": conteo.get(EstadoDocumento.PENDIENTE_REVISION.value, 0),
            "en_proceso": en_proceso,
            "errores": conteo.get(EstadoDocumento.ERROR_RPA.value, 0)
            + conteo.get(EstadoDocumento.DESCARTADO.value, 0),
            "completados": conteo.get(EstadoDocumento.COMPLETADO.value, 0),
            "total": sum(conteo.values()),
        }



def iniciar_bd() -> RepositorioDocumentos:
    """Crea el esquema si no existe y devuelve el repositorio listo."""
    repo = RepositorioDocumentos()
    repo.inicializar()
    return repo
