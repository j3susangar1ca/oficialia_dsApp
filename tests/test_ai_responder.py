"""
tests/test_ai_responder.py — Ensamblado del turno de usuario (prompt) del
asistente de respuesta, sin red: `_construir_contenido` es puro dado un
redactor ya instanciado (no requiere GEMINI_API_KEY real ni llamar al
proveedor). Mirror de tests/test_ai_extractor.py para el segundo caso de
uso de IA del sistema.
"""

from __future__ import annotations

from core.ai_responder import RedactorRespuestas
from core.models import MetadatosOficio, PeticionRespuesta, Procedencia, SentidoRespuesta


def _redactor() -> RedactorRespuestas:
    # api_key/modelo no se usan en _construir_contenido (no hay red aquí).
    return RedactorRespuestas(api_key="dummy", modelo="gemini-2.5-flash")


def _oficio_origen(**overrides) -> MetadatosOficio:
    datos = dict(
        numero_oficio="DSA-2026-089-OF",
        fecha_emision="2026-08-15",
        procedencia=Procedencia.AJENA,
        dependencia_area="DIRECCIÓN DE ADMINISTRACIÓN",
        remitente_nombre="JUAN PÉREZ LÓPEZ",
        destinatario_nombre="MARÍA GÓMEZ",
        asunto="Se solicita la validación de documentos administrativos pendientes de revisión.",
        plazo_dias=10,
    )
    datos.update(overrides)
    return MetadatosOficio(**datos)


def _texto_turno(contenido: list) -> str:
    return contenido[0].text


class TestDisponible:
    def test_sin_api_key_no_disponible(self):
        assert RedactorRespuestas(api_key="", modelo="gemini-2.5-flash").disponible is False

    def test_con_api_key_disponible(self):
        assert _redactor().disponible is True


class TestConstruccionDelTurno:
    def test_incluye_datos_del_oficio_de_origen(self):
        peticion = PeticionRespuesta(sentido=SentidoRespuesta.ATENCION_FAVORABLE)
        contenido = _redactor()._construir_contenido(_oficio_origen(), peticion)
        texto = _texto_turno(contenido)
        assert "DSA-2026-089-OF" in texto
        assert "JUAN PÉREZ LÓPEZ" in texto
        assert "2026-08-15" in texto

    def test_incluye_directrices_de_la_peticion(self):
        peticion = PeticionRespuesta(
            sentido=SentidoRespuesta.NEGATIVA_FUNDADA,
            instrucciones_adicionales="Citar falta de competencia territorial",
            fundamento_legal="Art. 8 Constitucional",
            firmante_nombre="ANA TORRES",
            firmante_cargo="Jefa de Departamento",
        )
        contenido = _redactor()._construir_contenido(_oficio_origen(), peticion)
        texto = _texto_turno(contenido)
        assert "negativa_fundada" in texto
        assert "Citar falta de competencia territorial" in texto
        assert "Art. 8 Constitucional" in texto
        assert "ANA TORRES" in texto

    def test_instrucciones_ausentes_se_marcan_no_aplica(self):
        peticion = PeticionRespuesta(sentido=SentidoRespuesta.ATENCION_FAVORABLE)
        contenido = _redactor()._construir_contenido(_oficio_origen(), peticion)
        texto = _texto_turno(contenido)
        assert "Instrucciones/argumentos clave: N/A" in texto

    def test_plazo_no_estipulado_se_reporta_explicitamente(self):
        peticion = PeticionRespuesta()
        contenido = _redactor()._construir_contenido(_oficio_origen(plazo_dias=None), peticion)
        texto = _texto_turno(contenido)
        assert "no estipulado" in texto

    def test_una_sola_parte_de_texto_sin_imagenes(self):
        """A diferencia de la extracción (multimodal), la redacción parte
        solo de metadatos ya validados: un único turno de texto."""
        peticion = PeticionRespuesta()
        contenido = _redactor()._construir_contenido(_oficio_origen(), peticion)
        assert len(contenido) == 1


class TestPeticionRespuesta:
    def test_cadenas_vacias_se_normalizan_a_none(self):
        peticion = PeticionRespuesta(instrucciones_adicionales="   ", fundamento_legal="")
        assert peticion.instrucciones_adicionales is None
        assert peticion.fundamento_legal is None

    def test_valores_por_defecto_del_firmante(self):
        peticion = PeticionRespuesta()
        assert peticion.firmante_nombre == "Titular de la Unidad Administrativa"
        assert peticion.sentido == SentidoRespuesta.ATENCION_FAVORABLE
