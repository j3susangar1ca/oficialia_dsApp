"""
SISTEMA OFICIALIA-DIGITAL-DSA (reconstrucción 100% Python)
==========================================================
core/file_manager.py — Gestión del ciclo de vida físico de los archivos.

Administra el watchfolder `storage/{01_entrada, 02_en_proceso, 03_procesados,
04_errores}` con las mismas garantías que el `LocalFileStorageAdapter` original:

    - Todas las rutas persistidas son RELATIVAS a la raíz de storage
      (portabilidad entre montajes locales y SMB).
    - Movimientos atómicos (os.replace) con degradación a copia+borrado
      cuando se cruza de dispositivo (EXDEV, típico en volúmenes de red).
    - Escritura de ingesta exclusiva (flag 'x') con prefijo de epoch-ms para
      desambiguar el canal WEB del canal SCANNER ante el vigilante.
    - Trazabilidad de cuarentena: junto a cada archivo aislado en
      `04_errores/` se anexa `<archivo>.error.txt` con `ISO :: motivo`.
"""

from __future__ import annotations

import errno
import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import Optional

from config import Configuracion, get_settings
from core.models import MetadatosOficio, nombre_archivo_canonico

logger = logging.getLogger("oficialia.archivos")

#: Prefijo que identifica las copias escritas por la propia app (canal WEB).
PREFIJO_UPLOAD_PROPIO = re.compile(r"^\d{10,}_")

#: Caracteres prohibidos en nombres de archivo de Windows (destino real del
#: recurso SMB): \ / : * ? " < > |
_CARACTERES_PROHIBIDOS_SMB_RE = re.compile(r'[\\/:*?"<>|]')

#: Longitud máxima del campo "asunto" dentro del nombre de archivo exportado
#: (evita rutas ilegibles o cercanas al límite de 260 caracteres de Windows).
LONGITUD_MAXIMA_ASUNTO_SMB = 60


def _sanear_campo_smb(valor: object, longitud_maxima: Optional[int] = None) -> str:
    """
    Limpia un campo para usarlo como componente del nombre de archivo SMB:
    quita caracteres reservados de Windows, colapsa espacios a '_' y trunca
    si excede `longitud_maxima`. Nunca devuelve una cadena vacía.
    """
    limpio = _CARACTERES_PROHIBIDOS_SMB_RE.sub("-", str(valor or "").strip())
    limpio = re.sub(r"\s+", "_", limpio)
    if longitud_maxima is not None and len(limpio) > longitud_maxima:
        limpio = limpio[:longitud_maxima].rstrip("_-")
    return limpio or "SIN_DATO"


def exportar_a_red_smb(
    ruta_pdf: Path, metadatos: dict, configuracion: Optional[Configuracion] = None
) -> Optional[Path]:
    """
    Copia (best-effort) el PDF ya procesado a la carpeta compartida de red
    (SMB_EXPORT_DIR) con nombre dinámico `{folio}_{fecha}_{remitente}_{asunto}.pdf`.

    `metadatos` admite indistintamente las claves cortas (`folio`, `fecha`,
    `remitente`, `asunto`) o los nombres de campo de `MetadatosOficio`
    (`numero_oficio`, `fecha_emision`, `remitente_nombre`, `asunto`).

    Nunca lanza excepción: un problema de red/permisos sobre el recurso
    compartido (carpeta no montada, sin permisos de escritura, servidor
    caído) se registra en el log y se devuelve `None` — la copia a SMB es
    un efecto secundario, jamás debe interrumpir el flujo principal.

    :returns: ruta absoluta del archivo copiado, o `None` si la exportación
        está desactivada (`SMB_EXPORT_DIR` vacío) o falló.
    """
    config = configuracion or get_settings()
    destino_dir_str = config.smb_export_dir.strip()
    if not destino_dir_str:
        logger.debug("Exportación a red SMB desactivada (SMB_EXPORT_DIR vacío)")
        return None

    destino_dir = Path(destino_dir_str)
    try:
        destino_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning(
            "[SMB] No se pudo asegurar la carpeta de red %s (¿montada? ¿permisos?): %s",
            destino_dir, exc,
        )
        return None

    folio = _sanear_campo_smb(metadatos.get("folio") or metadatos.get("numero_oficio"))
    fecha = _sanear_campo_smb(metadatos.get("fecha") or metadatos.get("fecha_emision"))
    remitente = _sanear_campo_smb(metadatos.get("remitente") or metadatos.get("remitente_nombre"))
    asunto = _sanear_campo_smb(metadatos.get("asunto"), longitud_maxima=LONGITUD_MAXIMA_ASUNTO_SMB)

    nombre_base = f"{folio}_{fecha}_{remitente}_{asunto}"
    destino = destino_dir / f"{nombre_base}.pdf"

    # Colisión de nombre (dos oficios que sanean al mismo nombre): sufijo
    # numérico incremental, nunca se sobrescribe un archivo ya exportado.
    contador = 1
    while destino.exists():
        destino = destino_dir / f"{nombre_base}_{contador}.pdf"
        contador += 1

    try:
        shutil.copy2(ruta_pdf, destino)
    except OSError as exc:
        logger.warning(
            "[SMB] Fallo al copiar %s → %s (posible problema de permisos de red): %s",
            ruta_pdf, destino, exc,
        )
        return None

    logger.info("[SMB] Documento exportado a la carpeta de red: %s", destino)
    return destino


class ErrorAlmacenamiento(Exception):
    """Fallo de E/S sobre el ciclo de vida físico de un documento."""

    def __init__(self, codigo: str, mensaje: str, ruta: str = "") -> None:
        self.codigo = codigo
        self.ruta = ruta
        super().__init__(f"[{codigo}] {mensaje}")


class GestorArchivos:
    """Custodio del árbol de storage y de la nomenclatura canónica."""

    def __init__(self, raiz: Path) -> None:
        self.raiz = Path(raiz)

    # ------------------------------------------------------------------
    # Estructura y resolución de rutas
    # ------------------------------------------------------------------
    def asegurar_estructura(self) -> None:
        """Crea el árbol completo de storage si aún no existe."""
        for carpeta in ("01_entrada", "02_en_proceso", "03_procesados", "04_errores"):
            (self.raiz / carpeta).mkdir(parents=True, exist_ok=True)

    def absoluta(self, ruta_relativa: str) -> Path:
        """Resuelve una ruta relativa (persistida en BD) contra la raíz."""
        return (self.raiz / ruta_relativa).resolve()

    @staticmethod
    def _nombre_seguro(nombre: str) -> str:
        """Sanea un nombre de archivo para el sistema de archivos destino."""
        base = Path(nombre).name.strip()
        limpio = re.sub(r"[/\\:*?\"<>|]", "-", base)
        return limpio or "documento.pdf"

    # ------------------------------------------------------------------
    # Ingesta y transiciones físicas
    # ------------------------------------------------------------------
    def guardar_entrada(self, nombre: str, contenido: bytes) -> str:
        """
        Escribe la copia de ingesta en `01_entrada/` con prefijo epoch-ms.

        La escritura usa el modo exclusivo 'xb': si colisiona el nombre
        (dos archivos en el mismo milisegundo) reintenta con el siguiente
        milisegundo — máx. 5 intentos.
        """
        if not contenido:
            raise ErrorAlmacenamiento("BUFFER_VACIO", "El archivo recibido está vacío")

        seguro = self._nombre_seguro(nombre)
        for intento in range(5):
            relativo = Path("01_entrada") / f"{int(time.time() * 1000) + intento}_{seguro}"
            destino = self.absoluta(str(relativo))
            destino.parent.mkdir(parents=True, exist_ok=True)
            try:
                with open(destino, "xb") as handle:
                    handle.write(contenido)
                return str(relativo)
            except FileExistsError:
                continue
        raise ErrorAlmacenamiento("COLISION_DE_NOMBRE", f"No se pudo escribir la ingesta de {nombre}")

    def mover_a_en_proceso(self, ruta_relativa: str, identificador: str) -> str:
        """Bloquea el archivo en `02_en_proceso/{id}.pdf`."""
        destino_rel = f"02_en_proceso/{identificador}.pdf"
        self._mover(ruta_relativa, destino_rel)
        return destino_rel

    def mover_a_canonico(
        self, ruta_relativa: str, metadatos: MetadatosOficio
    ) -> tuple[str, str, str]:
        """
        Consolida el documento validado por HITL:

            1. Mueve el PDF a `03_procesados/YYYY/MM/{nombre_canonico}.pdf`.
            2. Genera el respaldo espejo `.json` junto al PDF.
            3. Recalcula el SHA-256 del archivo YA ESCRITO (verificación
               post-escritura, no reutilización del hash de preproceso).

        :returns: (ruta_pdf_relativa, ruta_json_relativa, sha256_final)
        """
        anio, mes = metadatos.fecha_emision.split("-")[0:2]
        directorio = Path("03_procesados") / anio / mes
        nombre = nombre_archivo_canonico(metadatos)

        pdf_relativo = str(directorio / nombre)
        json_relativo = str(directorio / (nombre.removesuffix(".pdf") + ".json"))

        self._mover(ruta_relativa, pdf_relativo)

        ruta_json = self.absoluta(json_relativo)
        ruta_json.parent.mkdir(parents=True, exist_ok=True)
        try:
            ruta_json.write_text(
                metadatos.model_dump_json(indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
        except OSError as exc:
            raise ErrorAlmacenamiento(
                "MIRROR_JSON_WRITE_FAILED", f"No se pudo escribir el JSON espejo: {json_relativo}", json_relativo
            ) from exc

        sha_final = self._hash_archivo(pdf_relativo)
        return pdf_relativo, json_relativo, sha_final

    def mover_a_error(self, ruta_relativa: str, motivo: str) -> str:
        """
        Aísla el archivo en `04_errores/` y anexa el motivo a
        `<archivo>.error.txt` (una línea por incidente: `ISO :: motivo`).
        """
        base = Path(ruta_relativa).name
        destino_rel = f"04_errores/{base}"
        destino = self.absoluta(destino_rel)

        # Evita pisar una cuarentena previa con el mismo nombre.
        if destino.exists():
            destino_rel = f"04_errores/{int(time.time() * 1000)}_{base}"
            destino = self.absoluta(destino_rel)

        try:
            self._mover(ruta_relativa, destino_rel, permitir_origen_ausente=True)
            destino.parent.mkdir(parents=True, exist_ok=True)
            with open(destino.with_suffix(destino.suffix + ".error.txt"), "a", encoding="utf-8") as handle:
                handle.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} :: {motivo}\n")
        except OSError as exc:
            raise ErrorAlmacenamiento(
                "CUARENTENA_FALLIDA", f"No se pudo aislar {ruta_relativa}: {exc}", ruta_relativa
            ) from exc
        return destino_rel

    # ------------------------------------------------------------------
    # Lectura
    # ------------------------------------------------------------------
    def leer(self, ruta_relativa: str) -> bytes:
        try:
            return self.absoluta(ruta_relativa).read_bytes()
        except FileNotFoundError as exc:
            raise ErrorAlmacenamiento(
                "FILE_NOT_FOUND", f"Archivo no encontrado: {ruta_relativa}", ruta_relativa
            ) from exc

    def existe(self, ruta_relativa: str) -> bool:
        return self.absoluta(ruta_relativa).is_file()

    # ------------------------------------------------------------------
    # Internos
    # ------------------------------------------------------------------
    def _hash_archivo(self, ruta_relativa: str) -> str:
        import hashlib

        digesto = hashlib.sha256()
        with open(self.absoluta(ruta_relativa), "rb") as handle:
            for bloque in iter(lambda: handle.read(1024 * 1024), b""):
                digesto.update(bloque)
        return digesto.hexdigest()

    def _mover(self, origen_relativo: str, destino_relativo: str, *, permitir_origen_ausente: bool = False) -> None:
        """Movimiento atómico con degradación EXDEV → copia + borrado."""
        origen = self.absoluta(origen_relativo)
        destino = self.absoluta(destino_relativo)
        destino.parent.mkdir(parents=True, exist_ok=True)

        if not origen.exists():
            if permitir_origen_ausente:
                return
            raise ErrorAlmacenamiento(
                "FILE_NOT_FOUND", f"Archivo origen no encontrado: {origen_relativo}", origen_relativo
            )

        try:
            os.replace(origen, destino)
        except OSError as exc:
            if exc.errno == errno.EXDEV:  # cruce de dispositivos (SMB)
                shutil.copy2(origen, destino)
                origen.unlink()
                return
            raise ErrorAlmacenamiento(
                "MOVIMIENTO_FALLIDO", f"No se pudo mover a {destino_relativo}: {exc}", destino_relativo
            ) from exc
