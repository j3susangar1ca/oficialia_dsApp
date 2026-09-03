"""
SISTEMA OFICIALIA-DIGITAL-DSA (reconstrucción 100% Python)
==========================================================
core/models.py — Esquemas de dominio (Pydantic v2).

Espejo 1:1 del contrato original (`contracts/types.ts` +
`contracts/schemas/metadatosOficio.schema.ts`), con las reglas de
normalización heredadas del esquema Zod:

    - numero_oficio: trim, obligatorio, caracteres reservados de
      sistema de archivos ( / \\ : * ? " < > | ) sustituidos por '-'.
    - fecha_emision: patrón YYYY-MM-DD y fecha calendario real válida.
    - procedencia: enum cerrado 'HCG' | 'Ajena'.
    - Textos de personas/áreas: trim + MAYÚSCULAS.
    - cargos: default 'NO ESPECIFICADO' si vienen vacíos.
    - asunto: mínimo 5 caracteres, saltos de línea colapsados a espacio.
    - plazo_dias: entero >= 0 o None (nunca 0 "por defecto").
    - contiene_datos_sensibles: booleano (criterio LGPDPPSO).
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: Caracteres prohibidos en folios/nombres de archivo (igual que el original).
CARACTERES_RESERVADOS_RE = re.compile(r"[/\\:*?\"<>|]")
PATRON_FECHA_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def ahora_utc_iso() -> str:
    """Marca de tiempo ISO 8601 UTC con milisegundos (formato original)."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# ======================================================================
# 1. ENUMERACIONES DE ESTADO
# ======================================================================

class OrigenIngesta(str, Enum):
    """Canal por el cual ingresó el documento al sistema."""
    SCANNER_ADF = "SCANNER_ADF"
    WEB_DRAG_DROP = "WEB_DRAG_DROP"


class Procedencia(str, Enum):
    """Procedencia institucional del oficio."""
    HCG = "HCG"
    AJENA = "Ajena"


class EstadoDocumento(str, Enum):
    """
    Máquina de estados del ciclo de vida (versión consolidada):

        INGESTADO → EN_PREPROCESO → EXTRAYENDO → PENDIENTE_REVISION
                                          │
                                          ├─ [Confirmar] → EJECUTANDO_RPA → COMPLETADO
                                          │                     └→ ERROR_RPA (reintento)
                                          └─ [Descartar]  → DESCARTADO

    Nota de consolidación: el sistema original distinguía 12 estados
    (PENDIENTE_EXTRACCION, EN_EXTRACCION, APROBADO_HITL, EN_RPA y tres
    variantes de ERROR_*). Esta versión respeta el ciclo de vida requerido
    de 8 estados: los fallos de preprocesamiento/extracción se registran
    como DESCARTADO con `error_msg` detallado y el archivo aislado en
    `storage/04_errores/`, preservando la trazabilidad completa.
    """
    INGESTADO = "INGESTADO"
    EN_PREPROCESO = "EN_PREPROCESO"
    EXTRAYENDO = "EXTRAYENDO"
    PENDIENTE_REVISION = "PENDIENTE_REVISION"
    EJECUTANDO_RPA = "EJECUTANDO_RPA"
    ERROR_RPA = "ERROR_RPA"
    COMPLETADO = "COMPLETADO"
    DESCARTADO = "DESCARTADO"


#: Estados que representan un fallo (para filtros de bandeja y KPIs).
ESTADOS_ERROR: frozenset[EstadoDocumento] = frozenset({EstadoDocumento.ERROR_RPA})

#: Estados que representan trabajo en curso (visibles en la pestaña "En proceso").
ESTADOS_EN_PROCESO: frozenset[EstadoDocumento] = frozenset(
    {
        EstadoDocumento.INGESTADO,
        EstadoDocumento.EN_PREPROCESO,
        EstadoDocumento.EXTRAYENDO,
        EstadoDocumento.EJECUTANDO_RPA,
    }
)

#: Estados que alimentan la pestaña "Pendientes" de la bandeja HITL.
ESTADOS_PENDIENTES: frozenset[EstadoDocumento] = frozenset({EstadoDocumento.PENDIENTE_REVISION})


# ======================================================================
# 2. METADATOS DEL OFICIO (contrato de inferencia y de formulario HITL)
# ======================================================================

class MetadatosOficio(BaseModel):
    """
    Metadatos estructurados extraídos del oficio (contrato de la IA y del
    formulario de revisión asistida). Los 11 campos son obligatorios en la
    salida; los validadores aplican la normalización de dominio original.
    """

    model_config = ConfigDict(str_strip_whitespace=False)  # el trim se hace explícito

    #: Folio asignado por el EMISOR, sanitizado ("S/N" si carece de folio).
    numero_oficio: str = Field(
        ...,
        description="Número de oficio o folio oficial (ej. 'DSA-2026-089-OF' o 'S/N')",
    )
    #: Fecha de emisión en formato ISO 8601 calendario.
    fecha_emision: str = Field(..., description="Fecha de emisión (YYYY-MM-DD)")
    #: Origen institucional del documento.
    procedencia: Procedencia = Field(..., description="'HCG' o 'Ajena'")
    #: Dependencia, departamento o secretaría emisora (MAYÚSCULAS).
    dependencia_area: str = Field(..., description="Área emisora en mayúsculas")
    #: Nombre completo del firmante (MAYÚSCULAS).
    remitente_nombre: str = Field(..., description="Nombre del suscriptor/firmante")
    #: Cargo del firmante (MAYÚSCULAS, 'NO ESPECIFICADO' por omisión).
    remitente_cargo: str = Field(default="NO ESPECIFICADO", description="Cargo del firmante")
    #: Nombre del destinatario (MAYÚSCULAS).
    destinatario_nombre: str = Field(..., description="Nombre del funcionario destinatario")
    #: Cargo del destinatario (MAYÚSCULAS, 'NO ESPECIFICADO' por omisión).
    destinatario_cargo: str = Field(default="NO ESPECIFICADO", description="Cargo del destinatario")
    #: Síntesis ejecutiva (1 a 3 oraciones continuas, sin saltos de línea).
    asunto: str = Field(..., description="Síntesis del asunto (10-60 palabras)")
    #: Plazo legal de respuesta en días; None si el documento no estipula término.
    plazo_dias: Optional[int] = Field(default=None, ge=0, description="Plazo de respuesta en días o null")
    #: Bandera LGPDPPSO: el documento expone datos personales sensibles.
    contiene_datos_sensibles: bool = Field(default=False, description="Contiene datos sensibles")

    # ------------------------------------------------------------------
    # Normalizaciones (equivalentes a los .transform() del esquema Zod)
    # ------------------------------------------------------------------
    @field_validator("numero_oficio")
    @classmethod
    def _normalizar_folio(cls, valor: str) -> str:
        valor = valor.strip()
        if not valor:
            raise ValueError("El número de oficio es obligatorio (use 'S/N' si carece de folio)")
        return CARACTERES_RESERVADOS_RE.sub("-", valor)

    @field_validator("fecha_emision")
    @classmethod
    def _validar_fecha(cls, valor: str) -> str:
        valor = valor.strip()
        if not PATRON_FECHA_ISO.match(valor):
            raise ValueError("Formato de fecha requerido: YYYY-MM-DD")
        try:
            date.fromisoformat(valor)
        except ValueError as exc:
            raise ValueError("La fecha de emisión no es una fecha calendario válida") from exc
        return valor

    @field_validator("dependencia_area", "remitente_nombre", "destinatario_nombre")
    @classmethod
    def _mayusculas_obligatorio(cls, valor: str) -> str:
        valor = valor.strip()
        if not valor:
            raise ValueError("Campo obligatorio")
        return valor.upper()

    @field_validator("remitente_cargo", "destinatario_cargo")
    @classmethod
    def _mayusculas_default(cls, valor: str) -> str:
        valor = valor.strip()
        return valor.upper() if valor else "NO ESPECIFICADO"

    @field_validator("asunto")
    @classmethod
    def _asunto_continuo(cls, valor: str) -> str:
        valor = re.sub(r"[\r\n]+", " ", valor).strip()
        if len(valor) < 5:
            raise ValueError("Síntesis del oficio demasiado corta (1 a 3 oraciones)")
        return valor

    @field_validator("plazo_dias", mode="before")
    @classmethod
    def _plazo_vacio_none(cls, valor: Any) -> Any:
        """'' y 0 'por descarte' se interpretan como término NO estipulado."""
        if valor is None or (isinstance(valor, str) and valor.strip() == ""):
            return None
        if isinstance(valor, str):
            return int(valor.strip())
        return valor


# ======================================================================
# 3. SUB-MODELOS DE AUDITORÍA TÉCNICA
# ======================================================================

class DimensionPagina(BaseModel):
    """Dimensiones de renderizado de una página del documento."""
    numero: int
    ancho_px: int
    alto_px: int
    dpi: int


class InfoPreproceso(BaseModel):
    """Métricas del preprocesamiento PyMuPDF (auditoría técnica)."""
    num_paginas: int
    tamano_bytes: int
    sha256: str
    paginas: list[DimensionPagina] = Field(default_factory=list)
    duracion_ms: int = 0
    sanitizado: bool = True


class ResultadoRpa(BaseModel):
    """Trazabilidad de la ejecución del RPA en la Intranet Webix."""
    id_ejecucion: str
    folio_acuse: Optional[str] = None
    fecha_ejecucion: str = Field(default_factory=ahora_utc_iso)
    duracion_ms: int = 0
    captura_acuse_path: Optional[str] = None
    intentos: int = 1
    mensaje_error: Optional[str] = None
    exitoso: bool = False
    simulado: bool = False


class EstadoSheets(BaseModel):
    """Estado de sincronización hacia el tablero externo (Google Sheets)."""
    sincronizado: bool = False
    modo: str = "no_configurado"      # 'google' | 'stub_local' | 'no_configurado'
    fila_index: Optional[int] = None
    timestamp: Optional[str] = None
    error: Optional[str] = None


# ======================================================================
# 4. REGISTRO RAÍZ PERSISTIDO EN SQLITE
# ======================================================================

class DocumentoRegistro(BaseModel):
    """Fila de la tabla `documentos`: ciclo de vida completo de un oficio."""

    id: str
    nombre_archivo_original: str
    nombre_archivo_canonico: Optional[str] = None
    ruta_archivo_actual: str            # relativa a storage/ (portabilidad)
    ruta_espejo_json: Optional[str] = None
    origen: OrigenIngesta
    estado: EstadoDocumento
    sha256: str
    numero_oficio: Optional[str] = None
    metadatos_extraidos: Optional[MetadatosOficio] = None
    metadatos_validados: Optional[MetadatosOficio] = None
    preproceso: Optional[InfoPreproceso] = None
    rpa: Optional[ResultadoRpa] = None
    sheets: EstadoSheets = Field(default_factory=EstadoSheets)
    error_msg: Optional[str] = None
    revisor_usuario_id: Optional[str] = None
    fecha_ingesta: str = Field(default_factory=ahora_utc_iso)
    fecha_validacion_hitl: Optional[str] = None
    fecha_finalizacion: Optional[str] = None
    updated_at: str = Field(default_factory=ahora_utc_iso)
    version: int = 1


# ======================================================================
# 5. METADATOS VISUALES DE ESTADO (badges / etiquetas de la interfaz)
# ======================================================================

class MetaEstado(BaseModel):
    """Etiqueta y color de badge para cada estado (fuente única de la UI)."""
    etiqueta: str
    color: str      # color de Quasar para ui.badge
    punto: str      # color sólido para indicadores


META_ESTADOS: dict[EstadoDocumento, MetaEstado] = {
    EstadoDocumento.INGESTADO: MetaEstado(etiqueta="En cola", color="grey-3", punto="slate"),
    EstadoDocumento.EN_PREPROCESO: MetaEstado(etiqueta="Preprocesando", color="info", punto="sky"),
    EstadoDocumento.EXTRAYENDO: MetaEstado(etiqueta="Extrayendo datos", color="info", punto="sky"),
    EstadoDocumento.PENDIENTE_REVISION: MetaEstado(etiqueta="Por revisar", color="warning", punto="amber"),
    EstadoDocumento.EJECUTANDO_RPA: MetaEstado(etiqueta="Registrando en Intranet", color="primary", punto="brand"),
    EstadoDocumento.ERROR_RPA: MetaEstado(etiqueta="Error al registrar", color="negative", punto="rose"),
    EstadoDocumento.COMPLETADO: MetaEstado(etiqueta="Completado", color="positive", punto="emerald"),
    EstadoDocumento.DESCARTADO: MetaEstado(etiqueta="Descartado", color="grey-6", punto="slate"),
}


def meta_estado(estado: EstadoDocumento) -> MetaEstado:
    """Acceso seguro a la metadatura visual de un estado."""
    return META_ESTADOS.get(estado, MetaEstado(etiqueta=estado.value, color="grey-6", punto="slate"))


#: Grupos de la bandeja (filtros por pestaña, igual que el frontend original).
class GrupoBandeja(BaseModel):
    id: str
    etiqueta: str
    estados: Optional[frozenset[EstadoDocumento]] = None  # None ⇒ "Todos"


GRUPOS_BANDEJA: list[GrupoBandeja] = [
    GrupoBandeja(id="todos", etiqueta="Todos", estados=None),
    GrupoBandeja(id="pendientes", etiqueta="Pendientes", estados=ESTADOS_PENDIENTES),
    GrupoBandeja(id="en_proceso", etiqueta="En proceso", estados=ESTADOS_EN_PROCESO),
    GrupoBandeja(id="errores", etiqueta="Errores RPA", estados=frozenset(ESTADOS_ERROR | {EstadoDocumento.DESCARTADO})),
    GrupoBandeja(id="completados", etiqueta="Completados", estados=frozenset({EstadoDocumento.COMPLETADO})),
]


def nombre_archivo_canonico(metadatos: MetadatosOficio) -> str:
    """
    Construye la nomenclatura canónica obligatoria:
        YYYY-MM-DD__[FOLIO]__[REMITENTE].pdf

    Reglas heredadas del orquestador original:
        - folio: ya viene sanitizado por MetadatosOficio (reservados → '-').
        - remitente: primeros 30 caracteres, espacios colapsados a '_'.
    """
    folio = metadatos.numero_oficio.strip() or "SIN_FOLIO"
    remitente = CARACTERES_RESERVADOS_RE.sub(
        "-", metadatos.remitente_nombre[:30].strip()
    ).replace(" ", "_")
    remitente = remitente or "SIN_REMITENTE"
    return f"{metadatos.fecha_emision}__{folio}__{remitente}.pdf"
