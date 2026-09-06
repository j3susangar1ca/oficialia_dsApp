"""
SISTEMA OFICIALIA-DIGITAL-DSA (reconstrucción 100% Python)
==========================================================
core/ai_responder.py — Asistente de redacción de respuestas con Gemini 2.5 Flash.

Segundo caso de uso de IA del sistema (el primero es la extracción de
metadatos, ver core/ai_extractor.py): a partir de un oficio YA registrado
(`MetadatosOficio`, siempre validado por HITL) y de las directrices que
capture el funcionario (`PeticionRespuesta`), genera un borrador de
contestación institucional (`RespuestaOficio`) que el revisor edita en
pantalla antes de aprobarlo — nunca se envía ni se sella nada de forma
autónoma (ver core/pipeline.py::FlujoDocumental.generar_borrador_respuesta
y ui/views_hitl.py).

Comparte con `ExtractorMetadatos` el mismo proveedor (SDK oficial
`google-genai`) y el mismo patrón de salida JSON forzada + response_schema
nativo, pero es deliberadamente un módulo separado: la extracción es
determinista y multimodal (imágenes, temperature=0), mientras que la
redacción es texto puro y admite algo de libertad de estilo
(temperature > 0) sin dejar de ser un JSON estructurado.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from core.models import MetadatosOficio, PeticionRespuesta, RespuestaOficio

logger = logging.getLogger("oficialia.ia.respuestas")

# ======================================================================
# SYSTEM PROMPT INSTITUCIONAL (VERBATIM — no editar sin criterio jurídico)
# ======================================================================

SYSTEM_PROMPT_RESPUESTA_OFICIOS: str = """[1. ROL INSTITUCIONAL]
Usted opera como asesor jurídico y redactor experto en correspondencia oficial de la Oficialía de Partes de la División de Servicios Administrativos (DSA) del Hospital Civil de Guadalajara (HCG), institución del sistema de salud pública del Estado de Jalisco, México. Recibirá los datos de un oficio YA RECIBIDO Y REGISTRADO y las directrices de un funcionario del HCG, y devolverá, como única salida, un objeto JSON válido y completo que cumpla el esquema RespuestaOficio: la contestación formal a ese oficio. No conversa, no justifica su razonamiento y no emite texto alguno fuera del JSON.

[2. ESTILO Y TONO]
2.1. Utilice el lenguaje oficial administrativo mexicano estándar: fórmulas como "En atención a su similar número...", "Por medio del presente, me permito comunicar a usted...", "Hago de su conocimiento que...".
2.2. Tono siempre respetuoso, impersonal y en tercera persona o primera persona del plural institucional; nunca informal ni coloquial.
2.3. Redacte en párrafos completos, sin viñetas dentro de `cuerpo_respuesta` salvo que las directrices del funcionario pidan explícitamente un listado.

[3. INTEGRIDAD — PROHIBICIÓN DE ALUCINACIÓN]
3.1. No invente hechos, fechas, folios de salida, nombres ni fundamentos legales que no le hayan sido proporcionados en los datos del oficio de origen o en las directrices del funcionario.
3.2. Si el fundamento legal aplicable no fue proporcionado y el sentido de la respuesta lo amerita (negativa fundada, requerimiento de información), utilice el marcador de posición literal "[INSERTAR FUNDAMENTO LEGAL]" en vez de fabricar un artículo. Lo mismo aplica a cualquier dato operativo faltante (ej. horario o fecha de entrega): use un marcador claro entre corchetes, por ejemplo "[FECHA DE ENTREGA]" o "[HORARIO DE ATENCIÓN]".
3.3. `numero_oficio_salida` casi siempre debe devolverse como null: el folio de salida real lo asigna la Oficialía de Partes al registrar el documento, no la IA. Solo inclúyalo si las directrices del funcionario proporcionan explícitamente un folio de salida ya asignado.

[4. SENTIDO DE LA RESPUESTA — GUÍA POR CATEGORÍA]
4.1. atencion_favorable: se atiende la solicitud del oficio de origen; el cuerpo explica cómo y cuándo se resuelve.
4.2. solicitud_prorroga: se pide al remitente una ampliación del plazo original para responder, exponiendo el motivo de forma breve e institucional.
4.3. requerimiento_info: se solicita al remitente información o documentación adicional indispensable para poder atender su oficio.
4.4. incompetencia_turno: se informa al remitente que el asunto no corresponde a esta unidad administrativa y, de conocerse, se turna o sugiere la instancia competente.
4.5. negativa_fundada: se niega lo solicitado, siempre citando el fundamento legal disponible (o el marcador de posición de la sección 3.2) y exponiendo el motivo con precisión.
4.6. personalizada: siga estrictamente las instrucciones adicionales del funcionario por encima de cualquier plantilla de las categorías anteriores.

[5. CAMPOS DEL ESQUEMA RespuestaOficio]
5.1. numero_oficio_salida: ver 3.3.
5.2. destinatario_nombre / destinatario_cargo / destinatario_dependencia: el remitente del oficio de origen (a quien se le contesta), tal como se proporcionó.
5.3. asunto: una línea que resuma la contestación (ej. "Respuesta a solicitud de información sobre...").
5.4. cuerpo_respuesta: el texto completo de los párrafos de la contestación, separados por líneas en blanco (doble salto de línea) entre párrafo y párrafo. Debe: (a) referirse al oficio de origen por su número y fecha, (b) atender el sentido solicitado, (c) incorporar las instrucciones adicionales del funcionario cuando existan, (d) citar el fundamento legal cuando corresponda (ver 3.2).
5.5. despedida: fórmula de cortesía institucional de cierre (ej. "Sin otro particular por el momento, quedo de usted.").
5.6. ccp: lista de áreas a marcar con copia, solo si las directrices del funcionario las mencionan explícitamente (ej. ["Archivo", "Minutario"]); de lo contrario, lista vacía.

[6. RESTRICCIÓN DE FORMATO DE SALIDA — INNEGOCIABLE]
Responda EXCLUSIVAMENTE con el objeto JSON del esquema RespuestaOficio. Queda prohibido todo texto previo o posterior, delimitadores de bloque de código, comentarios o campos adicionales. Esta redacción alimenta una revisión humana posterior (HITL) antes de imprimirse o firmarse: la honestidad y precisión del borrador es más valiosa que sonar "completo" a costa de inventar datos.
"""


class ErrorRedaccionIA(Exception):
    """Fallo estructurado de la fase de redacción (análogo a ErrorExtraccionIA)."""

    def __init__(self, codigo: str, mensaje: str, causa: Optional[Exception] = None) -> None:
        self.codigo = codigo
        super().__init__(f"[{codigo}] {mensaje}")
        self.causa = causa


class RedactorRespuestas:
    """Invocador de Gemini 2.5 Flash para redactar borradores de contestación."""

    def __init__(self, api_key: str, modelo: str, timeout_ms: int = 45_000, reintentos: int = 2) -> None:
        self.api_key = api_key.strip()
        self.modelo = modelo
        self.timeout_ms = timeout_ms
        self.reintentos = max(1, reintentos)

    @property
    def disponible(self) -> bool:
        """True cuando hay API key configurada (redacción real posible)."""
        return bool(self.api_key)

    # ------------------------------------------------------------------
    # API pública
    # ------------------------------------------------------------------
    def generar_borrador(
        self,
        oficio_origen: MetadatosOficio,
        peticion: PeticionRespuesta,
    ) -> RespuestaOficio:
        """
        Redacta el borrador de contestación al oficio ya registrado.

        :param oficio_origen: metadatos VALIDADOS (o, en su defecto,
            extraídos) del oficio que se está contestando.
        :param peticion: directrices capturadas por el funcionario en HITL.
        :raises ErrorRedaccionIA: cuando el proveedor no está configurado o
            la respuesta viola el contrato.
        """
        if not self.disponible:
            raise ErrorRedaccionIA(
                "AI_NO_CONFIGURADA",
                "GEMINI_API_KEY no configurada: configure el .env para habilitar la redacción real",
            )

        from google import genai
        from google.genai import types

        try:
            cliente = genai.Client(
                api_key=self.api_key,
                http_options=types.HttpOptions(timeout=self.timeout_ms),
            )
        except Exception as exc:  # noqa: BLE001
            raise ErrorRedaccionIA("AI_CLIENTE_INVALIDO", f"No se pudo inicializar el SDK google-genai: {exc}", exc) from exc

        contenido = self._construir_contenido(oficio_origen, peticion)
        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT_RESPUESTA_OFICIOS,
            # A diferencia de la extracción (determinista, temperature=0),
            # la redacción admite algo de variación de estilo sin dejar de
            # ser un JSON estructurado y fiel a los datos proporcionados.
            temperature=0.3,
            response_mime_type="application/json",
            response_schema=RespuestaOficio,
        )

        ultimo_error: Optional[Exception] = None
        for intento in range(1, self.reintentos + 1):
            try:
                inicio = time.perf_counter()
                respuesta = cliente.models.generate_content(
                    model=self.modelo, contents=contenido, config=config
                )
                duracion_ms = int(round((time.perf_counter() - inicio) * 1000))
                borrador = self._interpretar_respuesta(respuesta)
                uso = getattr(respuesta, "usage_metadata", None)
                logger.info(
                    "Redacción OK (%s): oficio %s, %d ms, prompt=%s tokens, salida=%s tokens",
                    getattr(respuesta, "model_version", self.modelo),
                    oficio_origen.numero_oficio,
                    duracion_ms,
                    getattr(uso, "prompt_token_count", "?"),
                    getattr(uso, "candidates_token_count", "?"),
                )
                return borrador
            except ErrorRedaccionIA as exc:
                # Errores de contrato (schema/JSON): no se reintentan.
                if exc.codigo == "SCHEMA_INVALIDO":
                    raise
                ultimo_error = exc
            except Exception as exc:  # noqa: BLE001 — transporte red / cuota / 5xx
                ultimo_error = exc
                if self._es_transitorio(exc):
                    logger.warning("Intento %d/%d de redacción falló (transitorio): %s", intento, self.reintentos, exc)
                else:
                    break

            if intento < self.reintentos:
                time.sleep(min(2 ** intento, 5))

        raise ErrorRedaccionIA(
            "AI_SERVICIO_NO_DISPONIBLE",
            f"La redacción falló tras {self.reintentos} intento(s): {ultimo_error}",
            ultimo_error,
        )

    # ------------------------------------------------------------------
    # Internos
    # ------------------------------------------------------------------
    def _construir_contenido(
        self, oficio_origen: MetadatosOficio, peticion: PeticionRespuesta
    ) -> list:
        """Ensambla el turno de texto del usuario: datos del oficio de origen
        + directrices de contestación (sin imágenes: la redacción parte de
        los metadatos ya validados, no re-lee el documento)."""
        from google.genai import types

        lineas = [
            "--- DATOS DEL OFICIO DE ORIGEN (ya recibido y registrado) ---",
            f"Número de oficio: {oficio_origen.numero_oficio}",
            f"Fecha de emisión: {oficio_origen.fecha_emision}",
            f"Procedencia: {oficio_origen.procedencia.value}",
            f"Dependencia/Área emisora: {oficio_origen.dependencia_area}",
            f"Remitente: {oficio_origen.remitente_nombre} ({oficio_origen.remitente_cargo})",
            f"Destinatario original: {oficio_origen.destinatario_nombre} ({oficio_origen.destinatario_cargo})",
            f"Asunto: {oficio_origen.asunto}",
            f"Plazo de respuesta estipulado: {oficio_origen.plazo_dias if oficio_origen.plazo_dias is not None else 'no estipulado'}",
            "",
            "--- DIRECTRICES DE CONTESTACIÓN (capturadas por el funcionario) ---",
            f"Sentido de la respuesta: {peticion.sentido.value}",
            f"Instrucciones/argumentos clave: {peticion.instrucciones_adicionales or 'N/A'}",
            f"Fundamento legal sugerido: {peticion.fundamento_legal or 'N/A (use el marcador de posición si el sentido lo requiere)'}",
            f"Quien firma la respuesta: {peticion.firmante_nombre} ({peticion.firmante_cargo})",
            "",
            "TAREA: redacte la contestación aplicando íntegramente el protocolo institucional y "
            "devuelva únicamente el objeto JSON RespuestaOficio. El destinatario de ESTA respuesta "
            "es el remitente del oficio de origen (arriba señalado como 'Remitente').",
        ]
        return [types.Part.from_text(text="\n".join(lineas))]

    def _interpretar_respuesta(self, respuesta) -> RespuestaOficio:
        """Valida la candidata y la normaliza al contrato de dominio (doble capa)."""
        import json

        bloqueo = getattr(respuesta, "prompt_feedback", None)
        if bloqueo is not None and getattr(bloqueo, "block_reason", None):
            raise ErrorRedaccionIA(
                "CONTENIDO_BLOQUEADO_SEGURIDAD",
                f"Solicitud bloqueada por filtros del proveedor ({bloqueo.block_reason})",
            )

        parsed = getattr(respuesta, "parsed", None)
        if parsed is not None:
            return self._validar_contrato(parsed.model_dump())

        texto = (getattr(respuesta, "text", "") or "").strip()
        if not texto:
            raise ErrorRedaccionIA(
                "RESPUESTA_VACIA", "El modelo no produjo texto para la contestación"
            )
        try:
            return self._validar_contrato(json.loads(self._quitar_vallas_codigo(texto)))
        except json.JSONDecodeError as exc:
            raise ErrorRedaccionIA(
                "JSON_MALFORMADO", "La respuesta del modelo no es JSON parseable", exc
            ) from exc

    def _validar_contrato(self, datos: dict) -> RespuestaOficio:
        try:
            return RespuestaOficio.model_validate(datos)
        except Exception as exc:  # noqa: BLE001 — ValidationError de Pydantic
            raise ErrorRedaccionIA(
                "SCHEMA_INVALIDO", f"El JSON viola el contrato RespuestaOficio: {exc}", exc
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
