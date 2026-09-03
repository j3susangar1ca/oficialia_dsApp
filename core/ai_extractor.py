"""
SISTEMA OFICIALIA-DIGITAL-DSA (reconstrucción 100% Python)
==========================================================
core/ai_extractor.py — Extracción estructurada con Gemini 2.5 Flash.

Puerta única al proveedor de IA mediante el SDK oficial `google-genai`,
con tipado estricto Pydantic v2 (`MetadatosOficio` como response_schema).
El system prompt institucional se transcribe VERBATIM del original
(`infrastructure/ai/prompts/systemPromptExtraccionOficios.ts`, fuente
canónica `docs/system_prompt.md`): cualquier ajuste de protocolo OCR debe
hacerse primero en la documentación y replicarse aquí.

Decisiones heredadas del adaptador original:
    - temperature = 0 (extracción determinista).
    - Salida JSON forzada + response_schema nativo (defensa en profundidad:
      el esquema fuerza la forma, Pydantic exige el contrato de dominio).
    - El turno de usuario transporta: fuente documental → imágenes en orden
      natural → directiva de cierre; el año de contexto viaja ahí (no en el
      system prompt) para mantener el prompt estático y cacheable.
    - Reintentos acotados ante errores transitorios (429 / 5xx / red).
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from core.models import MetadatosOficio
from core.pdf_engine import PaginaRenderizada

logger = logging.getLogger("oficialia.ia")

# ======================================================================
# SYSTEM PROMPT INSTITUCIONAL (VERBATIM — no editar sin actualizar docs)
# ======================================================================

SYSTEM_PROMPT_EXTRACCION_OFICIOS: str = """[1. ROL INSTITUCIONAL]
Usted opera como el motor especializado de lectura documental de la Oficialía de Partes de la División de Servicios Administrativos (DSA) del Hospital Civil de Guadalajara (HCG), institución del sistema de salud pública del Estado de Jalisco, México. Recibirá una o más imágenes digitalizadas de un oficio de correspondencia oficial y devolverá, como única salida, un objeto JSON válido y completo que cumpla el esquema MetadatosOficio. No conversa, no justifica su razonamiento, no evalúa el contenido del documento y no emite texto alguno fuera del JSON.

[2. PROTOCOLO DE LECTURA OCR — ORDEN DE BARRIDO]
Analice todas las páginas adjuntas en su orden natural (la página 1 es la carátula del documento) y recorra cada una en esta secuencia:
2.1. Membrete superior: escudos y logotipos (Gobierno de Jalisco, Secretaría de Salud de Jalisco —SSJ—, Hospital Civil de Guadalajara —HCG—), denominación de la institución emisora, domicilio y datos de contacto.
2.2. Bloque de identificación: número de oficio o folio asignado por el EMISOR y fecha de emisión, habitualmente en la esquina superior derecha o centrados bajo el membrete.
2.3. Bloque de destinatario: líneas "C.", "C.C." o fórmulas de atención ("Presentado a", "At'n"), seguidas del nombre y cargo del funcionario destinatario.
2.4. Líneas de referencia: "Ref.", "Asunto", "No. de expediente".
2.5. Cuerpo del documento: párrafos sustantivos, solicitudes, plazos y términos legales.
2.6. Pie de firma: fórmula de cortesía ("ATENTAMENTE"), rúbrica manuscrita, nombre del suscriptor y cargo impreso bajo la línea de firma.
2.7. Sello de recibido: impronta circular o rectangular de la Oficialía de Partes receptora, frecuentemente girada, con tinta tenue o parcialmente legible; contiene la fecha de recepción y un folio de control interno.
2.8. Distribución final: líneas "C.c.p." (con copia para) y listado de anexos.

Reglas críticas de discriminación visual:
a) El numero_oficio corresponde SIEMPRE al folio asignado por el emisor. El folio impreso en el sello de recibido es un número de control interno de la Oficialía de Partes y NUNCA debe reportarse como numero_oficio.
b) La fecha asentada en el sello de recibido es la fecha de RECEPCIÓN, no la de emisión. Utilícela únicamente como contingencia cuando la fecha de emisión resulte ilegible o inexistente.
c) Para identificar al remitente, prefiera el nombre impreso bajo la rúbrica; la firma manuscrita aislada nunca es fuente suficiente. Los funcionarios listados en "C.c.p." no son ni remitente ni destinatario del oficio.
d) Ante sellos girados o rotados, tinta desvanecida, fotocopias de baja calidad o fax, escale el esfuerzo de lectura antes de declarar ilegible un campo.
e) Los anexos y páginas subsecuentes también contienen información extraíble (tablas, expedientes, oficios incrustados): considérelos al evaluar plazo_dias y contiene_datos_sensibles.

[3. REGLAS DE EXTRACCIÓN POR CAMPO]
3.1. numero_oficio (cadena): transcriba el folio del emisor tal cual aparece (por ejemplo: "SSJ/DEA/2026/089", "HCG-CA-045-2026", "DSA-0123"), eliminando únicamente espacios al inicio y al final. No sustituya barras, guiones ni símbolos. Si el documento carece de folio, devuelva exactamente "S/N".
3.2. fecha_emision (cadena, patrón YYYY-MM-DD): aplique el algoritmo de normalización de la sección 4.
3.3. procedencia ("HCG" o "Ajena"): aplique el árbol de decisión de la sección 5.
3.4. dependencia_area (cadena): denominación de la unidad administrativa emisora tal como aparece en el membrete o pie de firma (por ejemplo "DIRECCIÓN DE ADMINISTRACIÓN", "SECRETARÍA DE SALUD JALISCO"), convertida a MAYÚSCULAS, sin abreviar mediante suposiciones.
3.5. remitente_nombre (cadena): nombre completo del suscriptor que firma el documento, en MAYÚSCULAS. Si la firma resulta enteramente ilegible, transcriba la mejor lectura posible de la rúbrica; como última opción devuelva "ILEGIBLE".
3.6. remitente_cargo (cadena): cargo del suscriptor, en MAYÚSCULAS. Si no aparece, devuelva "NO ESPECIFICADO".
3.7. destinatario_nombre (cadena): funcionario a quien se dirige el oficio, en MAYÚSCULAS.
3.8. destinatario_cargo (cadena): cargo del destinatario, en MAYÚSCULAS. Si no aparece, devuelva "NO ESPECIFICADO".
3.9. asunto (cadena): síntesis ejecutiva conforme a la sección 7.
3.10. plazo_dias (entero no negativo o null): término de respuesta cuantitativo estipulado en el documento ("dentro de los 10 días", "plazo no mayor a 15 días hábiles", "en un término de 5 días naturales"). Si el documento menciona tanto días hábiles como naturales, registre los HÁBILES; si menciona solo uno de ellos, registre ese valor. Ante expresiones cualitativas ("a la brevedad posible", "cuanto antes") o ausencia de término, devuelva null; nunca 0 y nunca una cadena de texto.
3.11. contiene_datos_sensibles (booleano): aplique los criterios LGPDPPSO de la sección 6.

[4. ALGORITMO DE NORMALIZACIÓN DE FECHAS A ISO 8601 (YYYY-MM-DD)]
Paso 1 — Localización: busque la fecha de emisión en expresiones textuales ("Guadalajara, Jalisco, a 15 de agosto de 2026") o numéricas ("15/08/2026", "15-08-26", "15/AGO/2026", "2026-08-15").
Paso 2 — Mes: acepte nombres completos y abreviados en español (ene, feb, mar, abr, may, jun, jul, ago, sep o sept, oct, nov, dic), en mayúsculas o minúsculas, así como su equivalente numérico.
Paso 3 — Ambigüedad día/mes: ante un patrón numérico a/b/aaaa donde ambos valores sean menores o iguales a 12, interprete a como DÍA y b como MES (convención administrativa mexicana). Si a es mayor que 12, a es el día. Si b es mayor que 12, b es el día (formato mes/día).
Paso 4 — Año: si aparece con dos dígitos, expándalo usando el año de contexto indicado en el mensaje del usuario (por ejemplo, "26" con contexto 2026 se expande a 2026; si 20YY superara el contexto en más de un año, considere 19YY). Si el documento no muestra año, utilice el año de contexto.
Paso 5 — Validación: verifique la existencia real de la fecha en el calendario (meses de 30 y 31 días, febrero y años bisiestos).
Paso 6 — Contingencia en cascada: si la fecha de emisión es ilegible o inexistente, use la fecha del sello de recibido; si tampoco existe, use el 1 de enero del año de contexto.
Formato de salida obligatorio: exactamente diez caracteres YYYY-MM-DD, con ceros a la izquierda, sin componente de hora y sin zona horaria.

[5. ÁRBOL DE DECISIÓN DE PROCEDENCIA (HCG vs AJENA)]
Paso 1 — Identifique la ENTIDAD SUSCRIPTORA: la institución que encabeza el membrete y a nombre de la cual firma el suscriptor. La presencia aislada de logotipos o escudos no es suficiente: lo determinante es quién EMITE el documento.
Paso 2 — Clasifique como "HCG" si el emisor pertenece al Hospital Civil de Guadalajara: membretes o escudos con "Hospital Civil de Guadalajara", "HCG", OPD Hospital Civil de Guadalajara, Hospital Civil "Fray Antonio Alcalde", Hospital Civil "Dr. Juan I. Menchaca", o cualquiera de sus direcciones generales, divisiones (incluida la propia DSA), departamentos, coordinaciones y servicios clínicos o administrativos.
Paso 3 — Precaución de marca compartida: la documentación del sistema de salud de Jalisco suele portar simultáneamente el escudo del Gobierno de Jalisco y/o el membrete de la SSJ junto con el del hospital. La coexistencia de sellos SSJ y HCG NO clasifica el documento como "Ajena": prevalece la entidad suscriptora del oficio.
Paso 4 — Clasifique como "Ajena" si el emisor es ajeno al hospital, aun siendo autoridad sanitaria: la propia Secretaría de Salud de Jalisco actuando en nivel central (titulares, direcciones generales de la secretaría), el OPD de Servicios de Salud de Jalisco, dependencias estatales, federales o municipales, los tres poderes del Estado, organismos autónomos, organismos académicos, empresas, despachos profesionales y particulares.
Paso 5 — Desempate: si el mensaje del usuario aporta una pista de procedencia, tómela como hipótesis inicial y confírmela o réfutela contra el membrete del emisor. En caso de ambigüedad irreducible y sin pista disponible, clasifique como "Ajena" (caso dominante en la correspondencia recibida por una oficialía de partes).

[6. CUMPLIMIENTO LGPDPPSO — DETECCIÓN DE DATOS PERSONALES SENSIBLES]
Conforme a la Ley General de Protección de Datos Personales en Posesión de Sujetos Obligados, marque contiene_datos_sensibles como true únicamente cuando el contenido del oficio exponga datos personales sensibles de personas identificables o identificables por contexto, tales como: estado de salud (diagnósticos, tratamientos, padecimientos, incapacidades médicas), discapacidad, datos biométricos, origen étnico, vida u opción sexual, opiniones políticas, convicciones religiosas o filosóficas, afiliación sindical, y datos personales de niñas, niños y adolescentes. En el contexto hospitalario incluye también números de expediente clínico y claves CURP asociadas a condiciones de salud de personas nominadas.
Marque false cuando el documento solo contenga datos personales ordinarios de servidores públicos (nombres, cargos, dependencias) o menciones genéricas sin identificación de persona alguna.
Criterio de decisión: este indicador activa el proceso de anonimización posterior del sistema; ante duda razonable, prefiera true (criterio conservativo de protección).

[7. SÍNTESIS DEL ASUNTO]
Redacte el campo asunto como UN párrafo continuo de 1 a 3 líneas (entre 10 y 60 palabras), en tercera persona y tono administrativo neutro, sin comillas, sin viñetas y sin saltos de línea. Debe expresar: qué se comunica o solicita, quién lo promueve y, si existen, las referencias temporales o de expediente relevantes. Si el documento contiene una línea impresa de "Asunto", condésela conservando íntegro su sentido; si carece de ella, sintetice el cuerpo del documento.

[8. RESTRICCIÓN DE FORMATO DE SALIDA — INNEGOCIABLE]
8.1. Responda EXCLUSIVAMENTE con el objeto JSON del esquema MetadatosOficio. Queda prohibido todo texto previo o posterior, delimitadores de bloque de código, comentarios, notas de confianza, explicaciones o campos adicionales.
8.2. Emita SIEMPRE los once campos del esquema, en el orden definido, aunque deba recurrir a los valores de contingencia ("S/N", "NO ESPECIFICADO", "ILEGIBLE", null).
8.3. Respete los tipos exactos: cadenas de texto sin saltos de línea; fecha_emision con el patrón YYYY-MM-DD; plazo_dias como entero o null; contiene_datos_sensibles como booleano.
8.4. No altere los nombres de las claves ni agregue claves nuevas al objeto.

[9. INTEGRIDAD DOCUMENTAL — PROHIBICIÓN DE ALUCINACIÓN]
9.1. Transcriba únicamente lo que sea visible o razonablemente legible en las imágenes. Está prohibido completar información con conocimiento externo, suposiciones sobre formatos institucionales o memoria de casos anteriores.
9.2. Cuando un dato no resulte legible, aplique la contingencia definida para ese campo; cuando el dato simplemente no exista en el documento, utilice su valor de ausencia.
9.3. Esta extracción alimenta una validación humana posterior (HITL): la honestidad del reporte es más valiosa que la completitud aparente.
"""


class ErrorExtraccionIA(Exception):
    """Fallo estructurado de la fase de extracción (análogo al contrato original)."""

    def __init__(self, codigo: str, mensaje: str, causa: Optional[Exception] = None) -> None:
        self.codigo = codigo
        super().__init__(f"[{codigo}] {mensaje}")
        self.causa = causa


class ExtractorMetadatos:
    """Invocador de Gemini 2.5 Flash con validación estricta del contrato."""

    def __init__(self, api_key: str, modelo: str, timeout_ms: int = 45_000, reintentos: int = 2) -> None:
        self.api_key = api_key.strip()
        self.modelo = modelo
        self.timeout_ms = timeout_ms
        self.reintentos = max(1, reintentos)

    @property
    def disponible(self) -> bool:
        """True cuando hay API key configurada (extracción real posible)."""
        return bool(self.api_key)

    # ------------------------------------------------------------------
    # API pública
    # ------------------------------------------------------------------
    def extraer_de_paginas(self, paginas: list[PaginaRenderizada], *, anio_contexto: int) -> MetadatosOficio:
        """
        Ejecuta la inferencia multimodal sobre las páginas renderizadas.

        :param anio_contexto: año calendario vigente (expansión de años de 2 dígitos).
        :raises ErrorExtraccionIA: cuando el proveedor no está configurado o
            la respuesta viola el contrato (mapeado del enum original).
        """
        if not self.disponible:
            raise ErrorExtraccionIA(
                "AI_NO_CONFIGURADA",
                "GEMINI_API_KEY no configurada: configure el .env para habilitar la extracción real",
            )
        if not paginas:
            raise ErrorExtraccionIA(
                "DOCUMENTO_ILEGIBLE_O_VACIO", "No se recibieron páginas renderizadas para inferencia"
            )

        from google import genai
        from google.genai import types

        try:
            cliente = genai.Client(
                api_key=self.api_key,
                http_options=types.HttpOptions(timeout=self.timeout_ms),
            )
        except Exception as exc:  # noqa: BLE001
            raise ErrorExtraccionIA("AI_CLIENTE_INVALIDO", f"No se pudo inicializar el SDK google-genai: {exc}", exc) from exc

        # Orden de lectura natural (carátula primero).
        ordenadas = sorted(paginas, key=lambda p: p.numero)
        contenido = self._construir_contenido(ordenadas, anio_contexto)
        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT_EXTRACCION_OFICIOS,
            temperature=0.0,
            response_mime_type="application/json",
            response_schema=MetadatosOficio,
        )

        ultimo_error: Optional[Exception] = None
        for intento in range(1, self.reintentos + 1):
            try:
                inicio = time.perf_counter()
                respuesta = cliente.models.generate_content(
                    model=self.modelo, contents=contenido, config=config
                )
                duracion_ms = int(round((time.perf_counter() - inicio) * 1000))
                metadatos = self._interpretar_respuesta(respuesta)
                uso = getattr(respuesta, "usage_metadata", None)
                logger.info(
                    "Extracción OK (%s): %d páginas, %d ms, prompt=%s tokens, salida=%s tokens",
                    getattr(respuesta, "model_version", self.modelo),
                    len(ordenadas),
                    duracion_ms,
                    getattr(uso, "prompt_token_count", "?"),
                    getattr(uso, "candidates_token_count", "?"),
                )
                return metadatos
            except ErrorExtraccionIA as exc:
                # Los errores de contrato (schema/JSON) no se reintentan:
                # son deterministas a temperatura 0.
                if exc.codigo == "SCHEMA_INVALIDO":
                    raise
                ultimo_error = exc
            except Exception as exc:  # noqa: BLE001 — transporte red / cuota / 5xx
                ultimo_error = exc
                if self._es_transitorio(exc):
                    logger.warning("Intento %d/%d de inferencia falló (transitorio): %s", intento, self.reintentos, exc)
                else:
                    break

            # Backoff exponencial acotado entre intentos.
            if intento < self.reintentos:
                time.sleep(min(2 ** intento, 5))

        raise ErrorExtraccionIA(
            "AI_SERVICIO_NO_DISPONIBLE",
            f"La inferencia falló tras {self.reintentos} intento(s): {ultimo_error}",
            ultimo_error,
        )

    # ------------------------------------------------------------------
    # Internos
    # ------------------------------------------------------------------
    def _construir_contenido(self, paginas: list[PaginaRenderizada], anio_contexto: int) -> list:
        """
        Ensambla el turno multimodal del usuario: contexto textual → páginas
        en orden → directiva de cierre (los hints viajan aquí, no en el
        system prompt, para mantenerlo estático y cacheable).
        """
        from google.genai import types

        lineas = [
            f"FUENTE DOCUMENTAL: {len(paginas)} página(s) digitalizada(s) adjunta(s) en su orden "
            "natural (la página 1 es la carátula).",
            f"CONTEXTO TEMPORAL: el año calendario vigente del sistema es {anio_contexto}; "
            "úselo para expandir años de dos dígitos y completar fechas sin año visible.",
        ]
        partes: list = [types.Part.from_text("\n".join(lineas))]
        partes += [
            types.Part.from_bytes(data=pagina.png, mime_type=pagina.mime) for pagina in paginas
        ]
        partes.append(
            types.Part.from_text(
                "TAREA: aplique íntegramente el protocolo institucional y devuelva únicamente "
                "el objeto JSON MetadatosOficio."
            )
        )
        return partes

    def _interpretar_respuesta(self, respuesta) -> MetadatosOficio:
        """Valida la candidata y la normaliza al contrato de dominio (doble capa)."""
        import json

        # Filtros de seguridad (análogo a promptFeedback / finishReason).
        bloqueo = getattr(respuesta, "prompt_feedback", None)
        if bloqueo is not None and getattr(bloqueo, "block_reason", None):
            raise ErrorExtraccionIA(
                "CONTENIDO_BLOQUEADO_SEGURIDAD",
                f"Solicitud bloqueada por filtros del proveedor ({bloqueo.block_reason})",
            )

        parsed = getattr(respuesta, "parsed", None)
        if parsed is not None:
            return self._validar_contrato(parsed.model_dump())

        texto = (getattr(respuesta, "text", "") or "").strip()
        if not texto:
            raise ErrorExtraccionIA(
                "DOCUMENTO_ILEGIBLE_O_VACIO",
                "El modelo no produjo texto: la imagen está probablemente en blanco o es ilegible",
            )
        try:
            return self._validar_contrato(json.loads(self._quitar_vallas_codigo(texto)))
        except json.JSONDecodeError as exc:
            raise ErrorExtraccionIA(
                "JSON_MALFORMADO", "La respuesta del modelo no es JSON parseable", exc
            ) from exc

    def _validar_contrato(self, datos: dict) -> MetadatosOficio:
        """Revalida y normaliza (mayúsculas, sanitización de folio, etc.)."""
        try:
            return MetadatosOficio.model_validate(datos)
        except Exception as exc:  # noqa: BLE001 — ValidationError de Pydantic
            raise ErrorExtraccionIA(
                "SCHEMA_INVALIDO", f"El JSON viola el contrato MetadatosOficio: {exc}", exc
            ) from exc

    @staticmethod
    def _quitar_vallas_codigo(texto: str) -> str:
        """Degradación defensiva ante envoltorios ```json ... ```."""
        limpio = texto.strip()
        if limpio.startswith("```"):
            limpio = limpio.removeprefix("```json").removeprefix("```")
            if limpio.endswith("```"):
                limpio = limpio[:-3]
        return limpio.strip()

    @staticmethod
    def _es_transitorio(exc: Exception) -> bool:
        """429 / 5xx / timeouts de red ameritan reintento; el resto no."""
        mensaje = str(exc).lower()
        return any(
            clave in mensaje
            for clave in ("429", "resource_exhausted", "500", "502", "503", "504", "timeout", "timed out", "unavailable")
        )
