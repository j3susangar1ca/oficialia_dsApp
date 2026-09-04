"""
SISTEMA OFICIALIA-DIGITAL-DSA (reconstrucción 100% Python)
==========================================================
core/heuristic_extractor.py — Regex sobre texto plano: pistas de
preprocesamiento para la IA + respaldo de último recurso.

Dos consumidores, un solo motor de patrones (fuente única de verdad):

    1. `extraer_pistas()` — FASE PREVIA de preprocesamiento (nueva).
       `core.pipeline` la invoca ANTES de llamar a Gemini, sobre la capa
       de texto embebida del PDF (o el OCR auxiliar si esa capa está
       vacía). Sus candidatos (folio, fecha) viajan al turno de usuario
       de `core.ai_extractor` como pistas explícitamente NO autoritativas
       — reducen el trabajo de localización del modelo sin sustituir su
       lectura de la imagen ni relajar las reglas anti-alucinación del
       prompt (sección [2.9]). Nunca se usan para completar el contrato:
       solo un `Optional[str]` por campo, `None` si no hubo coincidencia.

    2. `extraer_heuristico()` — respaldo de ÚLTIMO RECURSO (preexistente,
       comportamiento sin cambios). Se invoca ÚNICAMENTE cuando
       `core.ai_extractor.ExtractorMetadatos` falla (sin GEMINI_API_KEY,
       cuota agotada, timeout de red tras agotar reintentos, respuesta
       malformada, etc. — ver `core.pipeline.FlujoDocumental`). Nunca
       reemplaza a la IA: es deliberadamente más débil y solo intenta
       rescatar número de oficio y fecha de emisión.

Filosofía de diseño (continúa la de `core.ai_extractor`): honestidad ante
completitud aparente. Todo lo que no se pudo inferir con razonable
confianza se deja en su valor de contingencia del contrato
(`MetadatosOficio`: "S/N", "NO ESPECIFICADO", "ILEGIBLE") — nunca se
inventa un dato. El documento que pasa por `extraer_heuristico()` SIEMPRE
se marca con `MetodoExtraccion.HEURISTICA_FALLBACK` (ver core/models.py y
database.py::_migracion_0_a_1) para que la bandeja y la pantalla HITL
adviertan al revisor que debe completar/verificar TODOS los campos. Un
documento que solo pasó por `extraer_pistas()` en cambio se extrajo por
IA de la forma normal (`MetodoExtraccion.IA`): las pistas son un insumo
del prompt, no un método de extracción aparte.

Motor de patrones (auditoría formal — ver detalle en cada patrón/función):
    - Normalización NFC previa (UAX #15) con vía rápida sin copia
      (`unicodedata.is_normalized`): los diacríticos descompuestos por
      OCR/PDF (NFD: "A" + U+0301) ya no truncan folios ("Á" perdida tras
      el primer `\\b`) ni ocultan fechas.
    - Alternancia explícita de meses derivada de `MESES_ES` (fuente única
      de verdad: patrón y tabla no pueden divergir) en vez de la clase
      abierta `[a-záéíóúñ]+` — solo empareja meses reales.
    - Búsqueda POR PÁGINA aislada, no sobre un `"\\n".join()` del
      documento completo: elimina el falso positivo de una fecha
      fabricada a caballo entre el final de una página y el inicio de la
      siguiente.
    - La búsqueda continúa tras un candidato de calendario inválido
      ("31 de febrero") en vez de abortar la rama completa.
    - Se preserva la prioridad semántica original: la fecha TEXTUAL se
      busca en TODO el documento antes de intentar la NUMÉRICA (dos
      pasadas independientes) — intercalar ambas por página invertiría
      esta prioridad.
    - Todos los cuantificadores son acotados y los tokens adyacentes usan
      clases disjuntas o literales ancla ⇒ sin ReDoS.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from typing import Optional

from core.models import MetadatosOficio, Procedencia

logger = logging.getLogger("oficialia.heuristica")

# ======================================================================
# Normalización canónica (UAX #15) — prerrequisito de UTS #18 RL3.8
# ======================================================================

_FORMA_NFC = "NFC"


def _nfc(texto: str) -> str:
    """`texto` en NFC sin copiar cuando ya está normalizado (caso dominante
    en PDF con capa de texto moderna): `unicodedata.is_normalized` es el
    quickcheck O(L) sin asignaciones de UAX #15."""
    if unicodedata.is_normalized(_FORMA_NFC, texto):
        return texto
    return unicodedata.normalize(_FORMA_NFC, texto)


# ======================================================================
# Patrones precompilados
# ======================================================================

#: Folio de oficio: 2-10 letras mayúsculas españolas (incluye Ü, presente
#: en siglas institucionales) + 1-5 grupos "/letra-num" o "-letra-num"
#: (cubre "SSJ/DEA/2026/089", "HCG-CA-045-2026", "DSA-0123"). Sin
#: alternativas NFD en el propio patrón: la normalización NFC previa ya
#: compone los diacríticos a forma precompuesta.
REGEX_NUMERO_OFICIO = re.compile(
    r"\b([A-ZÁÉÍÓÚÜÑ]{2,10}(?:[/-][A-Z0-9]{1,10}){1,5})\b", re.UNICODE
)

MESES_ES: dict[str, int] = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}

#: Alternancia derivada de MESES_ES (imposible que patrón y tabla
#: diverjan), ordenada por longitud descendente para que ningún nombre
#: quede eclipsado por el prefijo de otro. Sustituye la clase abierta
#: "[a-záéíóúñ]+": solo empareja meses reales, sin lookup fallido por
#: palabra arbitraria (rechaza ortografías falsas de OCR como "diciémbre").
_MESES_ALTERNANCIA = "|".join(sorted(MESES_ES, key=len, reverse=True))

#: Fecha textual en español: "15 de agosto de 2026". "\s+" admite saltos
#: de línea DENTRO de una página (fechas partidas por ajuste de línea); el
#: riesgo de falso positivo ENTRE páginas se elimina por arquitectura
#: (aislamiento por página en `_iter_paginas_nfc`), no recortando "\s".
REGEX_FECHA_TEXTUAL = re.compile(
    rf"\b(?P<dia>\d{{1,2}})\s+de\s+(?P<mes>{_MESES_ALTERNANCIA})\s+de\s+(?P<anio>\d{{4}})\b",
    re.IGNORECASE | re.UNICODE,
)

#: Fecha numérica: 15/08/2026, 15-08-26, etc. Separadores "[/-]"
#: independientes entre sí (15-08/2026 también empareja).
REGEX_FECHA_NUMERICA = re.compile(
    r"\b(?P<a>\d{1,2})[/-](?P<b>\d{1,2})[/-](?P<anio>\d{2,4})\b", re.UNICODE
)

#: Texto de "asunto" — deliberadamente visible/alarmante: es lo primero
#: que el revisor lee en la pantalla HITL para este documento.
ASUNTO_MARCADOR = (
    "EXTRACCIÓN HEURÍSTICA DE RESPALDO — REQUIERE REVISIÓN COMPLETA: "
    "VERIFIQUE Y COMPLETE MANUALMENTE TODOS LOS CAMPOS DE ESTE FORMULARIO "
    "ANTES DE CONFIRMAR."
)


# ======================================================================
# Normalización de fecha y candidatos válidos
# (subconjunto determinista del algoritmo del prompt institucional —
# sección 4 de core/ai_extractor.py)
# ======================================================================

def _expandir_anio(anio: int, anio_contexto: int) -> int:
    """Expande años de dos dígitos con `anio_contexto` como pivote.

    Convención idéntica a la del prompt institucional (sección 4, paso 3):
    2000 + anio, retrocediendo un siglo si el candidato excede
    anio_contexto + 1 (admite fechas del año entrante).
    """
    if anio >= 100:
        return anio
    candidato = 2000 + anio
    if candidato > anio_contexto + 1:
        candidato -= 100
    return candidato


def _iter_fechas_textuales(texto_nfc: str) -> Iterator[str]:
    """Genera (lazy) las fechas textuales VÁLIDAS de una página en NFC.

    Cada match se valida contra el calendario gregoriano; los inválidos
    (p. ej. "31 de febrero") se descartan y la búsqueda continúa con el
    siguiente candidato de la misma página, en vez de abortar la rama
    textual completa.
    """
    for m in REGEX_FECHA_TEXTUAL.finditer(texto_nfc):
        mes = MESES_ES.get(m.group("mes").lower())
        if mes is None:  # inalcanzable con la alternancia; defensivo
            continue
        try:
            yield date(int(m.group("anio")), mes, int(m.group("dia"))).isoformat()
        except ValueError:
            continue  # fecha de calendario inválida: siguiente candidato


def _iter_fechas_numericas(texto_nfc: str, anio_contexto: int) -> Iterator[str]:
    """Genera (lazy) las fechas numéricas VÁLIDAS de una página en NFC.

    Ambigüedad día/mes: misma convención que el prompt institucional
    (sección 4, paso 3) — por defecto a=día, b=mes; SOLO si b>12 (b no
    puede ser mes) se invierte a formato mes/día: a=mes, b=día.
    """
    for m in REGEX_FECHA_NUMERICA.finditer(texto_nfc):
        a, b = int(m.group("a")), int(m.group("b"))
        anio = _expandir_anio(int(m.group("anio")), anio_contexto)
        dia, mes = (b, a) if b > 12 else (a, b)
        if not 1 <= mes <= 12:
            continue
        try:
            yield date(anio, mes, dia).isoformat()
        except ValueError:
            continue  # p. ej. 29 de febrero no bisiesto: siguiente candidato


def _iter_paginas_nfc(texto_por_pagina: dict[int, str]) -> Iterator[str]:
    """Genera cada página en NFC, en orden ascendente de número de página."""
    for clave in sorted(texto_por_pagina):
        yield _nfc(texto_por_pagina[clave])


def _buscar_numero_oficio(texto_primera_pagina: str) -> Optional[str]:
    """Folio del emisor, buscado SOLO en la primera página.

    El membrete y el bloque de identificación siempre están ahí;
    restringir la búsqueda a la primera página reduce falsos positivos de
    referencias sueltas en el cuerpo o pie del documento.
    """
    m = REGEX_NUMERO_OFICIO.search(_nfc(texto_primera_pagina))
    return m.group(1) if m else None


def _buscar_fecha_emision(
    texto_por_pagina: dict[int, str], anio_contexto: int
) -> Optional[str]:
    """Busca una fecha de emisión plausible en todo el documento.

    Dos pasadas independientes que preservan la prioridad semántica del
    diseño original (la fecha textual es más confiable que la numérica,
    que suele corresponder a referencias o folios):

        1. Fecha textual en cada página, en orden ascendente de página.
        2. Solo si NINGUNA página tuvo fecha textual válida: numérica.
    """
    for pagina_nfc in _iter_paginas_nfc(texto_por_pagina):
        for fecha in _iter_fechas_textuales(pagina_nfc):
            return fecha
    for pagina_nfc in _iter_paginas_nfc(texto_por_pagina):
        for fecha in _iter_fechas_numericas(pagina_nfc, anio_contexto):
            return fecha
    return None


# ======================================================================
# API pública — 1) fase previa de preprocesamiento (pistas para Gemini)
# ======================================================================

@dataclass(frozen=True)
class PistaHeuristica:
    """Candidatos de preprocesamiento para `core.ai_extractor` — NO un
    `MetadatosOficio`: cada campo es `None` cuando no hubo coincidencia
    (nunca se rellena con un valor de contingencia como "S/N"), porque
    estas pistas viajan al prompt como candidatos a confirmar, no como
    respuesta final."""

    numero_oficio: Optional[str] = None
    fecha_emision: Optional[str] = None

    @property
    def hay_pistas(self) -> bool:
        """True si al menos un campo tiene un candidato — evita agregar
        al prompt un bloque de pistas vacío."""
        return self.numero_oficio is not None or self.fecha_emision is not None


def extraer_pistas(
    texto_por_pagina: dict[int, str], *, anio_contexto: int
) -> PistaHeuristica:
    """
    Preprocesamiento determinista PREVIO a la llamada a Gemini: localiza
    por regex, sobre texto plano (capa embebida del PDF u OCR auxiliar),
    candidatos de número de oficio y fecha de emisión para reducir el
    trabajo de localización del modelo — nunca para sustituir su lectura
    de la imagen. `core.ai_extractor` decide cómo presentarlas (siempre
    marcadas como no autoritativas) y el modelo sigue obligado a
    confirmarlas o refutarlas contra la página.

    Nunca lanza: ante cualquier fallo de parseo interno, ese campo queda
    en `None` (sin candidato) — igual de inofensivo que no ejecutar el
    preprocesamiento.

    :param texto_por_pagina: {número de página: texto}, formato de
        `pdf_engine.extraer_texto_capa` / `pdf_engine.extraer_texto_ocr`.
        Puede estar vacío (documento sin capa de texto y sin OCR
        disponible): en ese caso no hay pistas, Gemini trabaja solo con
        las imágenes, como si el preprocesamiento no se hubiera invocado.
    :param anio_contexto: año calendario vigente (expansión de años de 2
        dígitos), igual criterio que el prompt institucional.
    """
    numero_oficio: Optional[str] = None
    fecha_emision: Optional[str] = None

    try:
        if texto_por_pagina:
            primera_pagina = texto_por_pagina[min(texto_por_pagina)]
            numero_oficio = _buscar_numero_oficio(primera_pagina)
    except Exception:  # noqa: BLE001 — el preprocesamiento nunca debe fallar
        logger.warning("Preprocesamiento heurístico de número de oficio falló", exc_info=True)

    try:
        fecha_emision = _buscar_fecha_emision(texto_por_pagina, anio_contexto)
    except Exception:  # noqa: BLE001
        logger.warning("Preprocesamiento heurístico de fecha falló", exc_info=True)

    pistas = PistaHeuristica(numero_oficio=numero_oficio, fecha_emision=fecha_emision)
    logger.info(
        "Pistas heurísticas de preprocesamiento: numero_oficio=%r fecha_emision=%r (%d página(s))",
        pistas.numero_oficio, pistas.fecha_emision, len(texto_por_pagina),
    )
    return pistas


# ======================================================================
# API pública — 2) respaldo de último recurso (comportamiento sin cambios)
# ======================================================================

def extraer_heuristico(
    texto_por_pagina: dict[int, str], *, anio_contexto: int
) -> MetadatosOficio:
    """
    Construye un `MetadatosOficio` de mejor esfuerzo a partir de texto
    plano, para cuando la IA falló por completo (ver `core.pipeline`).
    Nunca lanza: ante cualquier fallo de parseo interno, degrada al valor
    de contingencia de ese campo.

    Reutiliza el mismo motor de patrones que `extraer_pistas` (fuente
    única de verdad); la diferencia es contractual: aquí SIEMPRE se
    devuelve un `MetadatosOficio` completo con placeholders explícitos
    ("S/N", "NO ESPECIFICADO", "ILEGIBLE") en los campos sin evidencia,
    porque este es el resultado final que verá el revisor en HITL — no un
    candidato a confirmar contra la imagen.

    :param texto_por_pagina: {número de página: texto}. Puede estar vacío
        (documento sin capa de texto y sin OCR disponible) — en ese caso
        se devuelven únicamente valores de contingencia, pero el
        documento igual pasa a PENDIENTE_REVISION en vez de perderse en
        la cuarentena de errores.
    :param anio_contexto: año calendario vigente (expansión de años de 2 dígitos).
    """
    pistas = extraer_pistas(texto_por_pagina, anio_contexto=anio_contexto)
    numero_oficio = pistas.numero_oficio or "S/N"
    fecha_emision = pistas.fecha_emision or f"{anio_contexto}-01-01"

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
