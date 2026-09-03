"""
SISTEMA OFICIALIA-DIGITAL-DSA (reconstrucción 100% Python)
==========================================================
core/heuristic_extractor.py — Extractor de respaldo (regex, sin IA).

Se invoca ÚNICAMENTE cuando `core.ai_extractor.ExtractorMetadatos` falla
(sin GEMINI_API_KEY, cuota agotada, timeout de red tras agotar reintentos,
respuesta malformada, etc.) — ver `core.pipeline.FlujoDocumental`. Nunca
reemplaza a la IA: es deliberadamente más débil y solo intenta rescatar
número de oficio y fecha de emisión mediante expresiones regulares sobre
el texto plano del PDF (capa de texto embebida de PyMuPDF, o el OCR
auxiliar de `core.pdf_engine.extraer_texto_ocr` si esa capa está vacía).

Filosofía de diseño (continúa la de `core.ai_extractor`): honestidad ante
completitud aparente. Todo lo que no se pudo inferir con razonable
confianza se deja en su valor de contingencia del contrato
(`MetadatosOficio`: "S/N", "NO ESPECIFICADO", "ILEGIBLE") — nunca se
inventa un dato. El documento resultante SIEMPRE se marca con
`MetodoExtraccion.HEURISTICA_FALLBACK` (ver core/models.py y
database.py::_migracion_0_a_1) para que la bandeja y la pantalla HITL
adviertan al revisor que debe completar/verificar TODOS los campos, no
solo confirmar lo precargado.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from typing import Optional

from core.models import MetadatosOficio, Procedencia

logger = logging.getLogger("oficialia.heuristica")

# ======================================================================
# Patrones
# ======================================================================

#: Folio de oficio: 2-10 letras mayúsculas + 1-5 grupos "/letra-num" o
#: "-letra-num" (cubre "SSJ/DEA/2026/089", "HCG-CA-045-2026", "DSA-0123").
REGEX_NUMERO_OFICIO = re.compile(r"\b([A-ZÁÉÍÓÚÑ]{2,10}(?:[/-][A-Z0-9]{1,10}){1,5})\b")

#: Fecha textual en español: "15 de agosto de 2026".
REGEX_FECHA_TEXTUAL = re.compile(
    r"\b(\d{1,2})\s+de\s+([a-záéíóúñ]+)\s+de\s+(\d{4})\b", re.IGNORECASE
)

#: Fecha numérica: 15/08/2026, 15-08-26, etc.
REGEX_FECHA_NUMERICA = re.compile(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\b")

MESES_ES: dict[str, int] = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}

#: Texto de "asunto" — deliberadamente visible/alarmante: es lo primero
#: que el revisor lee en la pantalla HITL para este documento.
ASUNTO_MARCADOR = (
    "EXTRACCIÓN HEURÍSTICA DE RESPALDO — REQUIERE REVISIÓN COMPLETA: "
    "VERIFIQUE Y COMPLETE MANUALMENTE TODOS LOS CAMPOS DE ESTE FORMULARIO "
    "ANTES DE CONFIRMAR."
)


# ======================================================================
# Normalización de fecha (subconjunto determinista del algoritmo del
# prompt institucional — sección 4 de core/ai_extractor.py)
# ======================================================================

def _expandir_anio(anio: int, anio_contexto: int) -> int:
    if anio >= 100:
        return anio
    candidato = 2000 + anio
    if candidato > anio_contexto + 1:
        candidato -= 100
    return candidato


def _normalizar_fecha_heuristica(texto: str, anio_contexto: int) -> Optional[str]:
    """Busca una fecha de emisión plausible en `texto`; None si no hay ninguna."""
    m = REGEX_FECHA_TEXTUAL.search(texto)
    if m:
        dia, mes_texto, anio = m.groups()
        mes = MESES_ES.get(mes_texto.lower())
        if mes:
            try:
                return date(int(anio), mes, int(dia)).isoformat()
            except ValueError:
                pass  # fecha de calendario inválida (ej. 31 de febrero): se descarta

    for m in REGEX_FECHA_NUMERICA.finditer(texto):
        a, b, anio_crudo = (int(g) for g in m.groups())
        anio = _expandir_anio(anio_crudo, anio_contexto)
        # Ambigüedad día/mes: igual convención que el prompt institucional
        # (sección 4, paso 3) — por defecto a=día, b=mes (ambos ≤12, o a>12
        # y por tanto forzosamente día); SOLO si b>12 (b no puede ser mes)
        # se invierte a formato mes/día: a=mes, b=día.
        dia, mes = (b, a) if b > 12 else (a, b)
        if not (1 <= mes <= 12):
            continue
        try:
            return date(anio, mes, dia).isoformat()
        except ValueError:
            continue  # sigue intentando con la siguiente coincidencia

    return None


def _buscar_numero_oficio(texto_primera_pagina: str) -> Optional[str]:
    """
    Folio del emisor: se busca SOLO en la primera página (el membrete y el
    bloque de identificación siempre están ahí) para reducir falsos
    positivos de referencias sueltas en el cuerpo o pie del documento.
    """
    m = REGEX_NUMERO_OFICIO.search(texto_primera_pagina)
    return m.group(1) if m else None


# ======================================================================
# API pública
# ======================================================================

def extraer_heuristico(
    texto_por_pagina: dict[int, str], *, anio_contexto: int
) -> MetadatosOficio:
    """
    Construye un `MetadatosOficio` de mejor esfuerzo a partir de texto
    plano (capa de texto embebida o OCR — ver `pdf_engine.extraer_texto`
    y `pdf_engine.extraer_texto_ocr`). Nunca lanza: ante cualquier fallo
    de parseo interno, degrada al valor de contingencia de ese campo.

    :param texto_por_pagina: {número de página: texto}, en el mismo
        formato que produce `pdf_engine`. Puede estar vacío (documento sin
        capa de texto y sin OCR disponible) — en ese caso se devuelven
        únicamente valores de contingencia, pero el documento igual pasa a
        PENDIENTE_REVISION en vez de perderse en la cuarentena de errores.
    :param anio_contexto: año calendario vigente (expansión de años de 2 dígitos).
    """
    texto_completo = "\n".join(texto_por_pagina[p] for p in sorted(texto_por_pagina))
    texto_primera_pagina = texto_por_pagina.get(min(texto_por_pagina, default=0), "")

    numero_oficio = "S/N"
    fecha_emision = f"{anio_contexto}-01-01"
    try:
        numero_oficio = _buscar_numero_oficio(texto_primera_pagina) or "S/N"
    except Exception:  # noqa: BLE001 — el fallback nunca debe fallar
        logger.warning("Heurística de número de oficio falló", exc_info=True)
    try:
        fecha_emision = _normalizar_fecha_heuristica(texto_completo, anio_contexto) or fecha_emision
    except Exception:  # noqa: BLE001
        logger.warning("Heurística de fecha falló", exc_info=True)

    logger.info(
        "Extracción heurística de respaldo: numero_oficio=%r fecha_emision=%r (%d página(s) de texto)",
        numero_oficio, fecha_emision, len(texto_por_pagina),
    )

    return MetadatosOficio(
        numero_oficio=numero_oficio,
        fecha_emision=fecha_emision,
        # Sin evidencia visual del membrete, "Ajena" es el valor por
        # defecto (mismo criterio que el árbol de decisión del prompt
        # institucional, sección 5, paso 5: caso dominante en la
        # correspondencia recibida por una oficialía de partes).
        procedencia=Procedencia.AJENA,
        dependencia_area="NO ESPECIFICADO",
        remitente_nombre="ILEGIBLE",
        remitente_cargo="NO ESPECIFICADO",
        destinatario_nombre="NO ESPECIFICADO",
        destinatario_cargo="NO ESPECIFICADO",
        asunto=ASUNTO_MARCADOR,
        plazo_dias=None,
        contiene_datos_sensibles=False,
    )
