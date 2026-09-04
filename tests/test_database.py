"""
tests/test_database.py — Repositorio SQLite: CRUD, concurrencia optimista,
migraciones de esquema y el buscador de la bandeja.
"""

from __future__ import annotations

import sqlite3
import time
import uuid

import pytest

from config import Configuracion
from core.models import (
    DocumentoRegistro,
    EstadoDocumento,
    MetadatosOficio,
    MetodoExtraccion,
    OrigenIngesta,
    Procedencia,
)
from database import ErrorConcurrencia, RepositorioDocumentos, VERSION_ESQUEMA


def _documento(**overrides) -> DocumentoRegistro:
    datos = dict(
        id=str(uuid.uuid4()),
        nombre_archivo_original="oficio.pdf",
        ruta_archivo_actual="01_entrada/oficio.pdf",
        origen=OrigenIngesta.WEB_DRAG_DROP,
        estado=EstadoDocumento.INGESTADO,
        sha256=uuid.uuid4().hex + uuid.uuid4().hex,  # 64 chars, único por prueba
    )
    datos.update(overrides)
    return DocumentoRegistro(**datos)


class TestCrudBasico:
    def test_crear_y_obtener(self, repositorio: RepositorioDocumentos):
        creado = repositorio.crear(_documento())
        leido = repositorio.obtener(creado.id)
        assert leido is not None
        assert leido.nombre_archivo_original == "oficio.pdf"
        assert leido.extraccion_metodo == MetodoExtraccion.IA  # default de columna

    def test_obtener_por_hash(self, repositorio: RepositorioDocumentos):
        creado = repositorio.crear(_documento())
        assert repositorio.obtener_por_hash(creado.sha256).id == creado.id

    def test_hash_duplicado_viola_unique(self, repositorio: RepositorioDocumentos):
        hash_compartido = "a" * 64
        repositorio.crear(_documento(sha256=hash_compartido))
        with pytest.raises(sqlite3.IntegrityError):
            repositorio.crear(_documento(sha256=hash_compartido))


class TestListarRegresionBandeja:
    """
    Regresión crítica: `listar()` (la consulta que alimenta la bandeja
    principal en ui/views_dashboard.py, refrescada cada 2 s) tenía una
    variable de comprensión mal referenciada (`f` en vez de `fila`) que
    hacía que CADA llamada lanzara NameError — como la UI atrapa esa
    excepción y solo la registra en el log, la bandeja jamás mostró un
    solo documento. Esta prueba falla de inmediato si el bug reaparece.
    """

    def test_listar_sin_filtros(self, repositorio: RepositorioDocumentos):
        repositorio.crear(_documento(nombre_archivo_original="uno.pdf"))
        repositorio.crear(_documento(nombre_archivo_original="dos.pdf"))
        resultado = repositorio.listar()
        assert {d.nombre_archivo_original for d in resultado} == {"uno.pdf", "dos.pdf"}

    def test_listar_filtra_por_estado(self, repositorio: RepositorioDocumentos):
        repositorio.crear(_documento(estado=EstadoDocumento.INGESTADO))
        repositorio.crear(_documento(estado=EstadoDocumento.COMPLETADO))
        resultado = repositorio.listar(estados=[EstadoDocumento.COMPLETADO])
        assert len(resultado) == 1
        assert resultado[0].estado == EstadoDocumento.COMPLETADO

    def test_listar_busca_por_numero_oficio(self, repositorio: RepositorioDocumentos):
        repositorio.crear(_documento(numero_oficio="DSA-2026-777-OF"))
        repositorio.crear(_documento(numero_oficio="HCG-CA-045-2026"))
        resultado = repositorio.listar(texto_busqueda="777")
        assert len(resultado) == 1
        assert resultado[0].numero_oficio == "DSA-2026-777-OF"

    def test_listar_vacio_no_lanza(self, repositorio: RepositorioDocumentos):
        assert repositorio.listar() == []


class TestConcurrenciaOptimista:
    def test_version_desactualizada_lanza_error_concurrencia(self, repositorio: RepositorioDocumentos):
        doc = repositorio.crear(_documento())
        repositorio.actualizar_estado(doc.id, EstadoDocumento.EN_PREPROCESO, version_esperada=doc.version)
        with pytest.raises(ErrorConcurrencia):
            # version_esperada desactualizada (ya avanzó a la siguiente)
            repositorio.actualizar_estado(doc.id, EstadoDocumento.EXTRAYENDO, version_esperada=doc.version)

    def test_version_correcta_avanza(self, repositorio: RepositorioDocumentos):
        doc = repositorio.crear(_documento())
        actualizado = repositorio.actualizar_estado(
            doc.id, EstadoDocumento.EN_PREPROCESO, version_esperada=doc.version
        )
        assert actualizado.version == doc.version + 1
        assert actualizado.estado == EstadoDocumento.EN_PREPROCESO


class TestExtraccionMetodo:
    def test_guardar_metadatos_persiste_metodo_heuristico(self, repositorio: RepositorioDocumentos):
        doc = repositorio.crear(_documento())
        metadatos = MetadatosOficio(
            numero_oficio="S/N",
            fecha_emision="2026-01-01",
            procedencia=Procedencia.AJENA,
            dependencia_area="NO ESPECIFICADO",
            remitente_nombre="ILEGIBLE",
            destinatario_nombre="NO ESPECIFICADO",
            asunto="Extracción heurística de respaldo por falla de IA en el sistema.",
        )
        actualizado = repositorio.guardar_metadatos_extraidos(
            doc.id, metadatos, EstadoDocumento.PENDIENTE_REVISION,
            version_esperada=doc.version, extraccion_metodo=MetodoExtraccion.HEURISTICA_FALLBACK,
        )
        assert actualizado.extraccion_metodo == MetodoExtraccion.HEURISTICA_FALLBACK
        # Sobrevive un roundtrip completo por SQLite (no es solo el objeto en memoria).
        releido = repositorio.obtener(doc.id)
        assert releido.extraccion_metodo == MetodoExtraccion.HEURISTICA_FALLBACK


class TestMigracionEsquema:
    def test_bd_nueva_nace_en_la_version_objetivo(self, configuracion: Configuracion):
        repo = RepositorioDocumentos(configuracion)
        repo.inicializar()
        conn = sqlite3.connect(configuracion.database_path)
        try:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == VERSION_ESQUEMA
        finally:
            conn.close()

    def test_bd_antigua_sin_extraccion_metodo_migra_preservando_datos(self, configuracion: Configuracion):
        """
        Simula una BD creada por una versión anterior de la app (esquema
        v0, sin la columna `extraccion_metodo`) y verifica que
        `inicializar()` la migra sin perder las filas ya existentes — el
        escenario real de "actualicé la app sobre una instalación en
        producción" que motivó el mecanismo de migraciones.
        """
        configuracion.database_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(configuracion.database_path)
        conn.executescript(
            """
            CREATE TABLE documentos (
                id TEXT PRIMARY KEY, nombre_archivo_original TEXT NOT NULL,
                nombre_archivo_canonico TEXT, ruta_archivo_actual TEXT NOT NULL,
                ruta_espejo_json TEXT, origen TEXT NOT NULL, estado TEXT NOT NULL,
                sha256_hash TEXT NOT NULL UNIQUE, numero_oficio TEXT,
                metadatos_extraidos TEXT, metadatos_validados TEXT, preproceso_json TEXT,
                rpa_json TEXT, sheets_json TEXT, error_msg TEXT, revisor_usuario_id TEXT,
                fecha_ingesta TEXT NOT NULL, fecha_validacion_hitl TEXT, fecha_finalizacion TEXT,
                updated_at TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1
            );
            """
        )
        conn.execute(
            "INSERT INTO documentos (id, nombre_archivo_original, ruta_archivo_actual, origen, "
            "estado, sha256_hash, fecha_ingesta, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            ("legado-1", "legado.pdf", "03_procesados/legado.pdf", "WEB_DRAG_DROP",
             "COMPLETADO", "b" * 64, "2025-01-01T00:00:00Z", "2025-01-01T00:00:00Z"),
        )
        conn.commit()
        conn.close()

        repo = RepositorioDocumentos(configuracion)
        repo.inicializar()  # debe migrar, no fallar ni recrear desde cero

        conn2 = sqlite3.connect(configuracion.database_path)
        try:
            assert conn2.execute("PRAGMA user_version").fetchone()[0] == VERSION_ESQUEMA
            columnas = {f[1] for f in conn2.execute("PRAGMA table_info(documentos)")}
            assert "extraccion_metodo" in columnas
            assert {"locked_by", "locked_at", "lock_expires_at"} <= columnas
        finally:
            conn2.close()

        legado = repo.obtener("legado-1")
        assert legado is not None
        assert legado.nombre_archivo_original == "legado.pdf"
        assert legado.extraccion_metodo == MetodoExtraccion.IA  # default para filas preexistentes

    def test_inicializar_es_idempotente(self, repositorio: RepositorioDocumentos):
        """Llamar inicializar() más de una vez (reinicios de la app) no debe fallar."""
        repositorio.inicializar()
        repositorio.inicializar()


class TestBloqueoConcurrente:
    """RepositorioDocumentos.adquirir_bloqueo/renovar_bloqueo/liberar_bloqueo
    — advertencia temprana en la UI (ver core.models.EstadoBloqueo) para que
    dos revisores no editen el mismo oficio a la vez sin saberlo; NO es la
    garantía de concurrencia de fondo (esa sigue siendo `version`)."""

    def test_adquirir_libre_devuelve_adquirido(self, repositorio: RepositorioDocumentos):
        doc = repositorio.crear(_documento())
        resultado = repositorio.adquirir_bloqueo(doc.id, "ana", ttl_minutos=3)
        assert resultado.adquirido is True
        assert resultado.poseido_por is None

    def test_adquirir_ya_tomado_por_otro_falla_y_reporta_quien(self, repositorio: RepositorioDocumentos):
        doc = repositorio.crear(_documento())
        repositorio.adquirir_bloqueo(doc.id, "ana", ttl_minutos=3)

        resultado = repositorio.adquirir_bloqueo(doc.id, "beto", ttl_minutos=3)

        assert resultado.adquirido is False
        assert resultado.poseido_por == "ana"

    def test_adquirir_es_reentrante_para_el_mismo_usuario(self, repositorio: RepositorioDocumentos):
        """Reabrir la misma pestaña (el mismo revisor) no debe autobloquearse."""
        doc = repositorio.crear(_documento())
        repositorio.adquirir_bloqueo(doc.id, "ana", ttl_minutos=3)

        resultado = repositorio.adquirir_bloqueo(doc.id, "ana", ttl_minutos=3)

        assert resultado.adquirido is True

    def test_adquirir_tras_vencer_lo_toma_otro_usuario(self, repositorio: RepositorioDocumentos):
        doc = repositorio.crear(_documento())
        repositorio.adquirir_bloqueo(doc.id, "ana", ttl_minutos=0.001)  # ~60 ms
        time.sleep(0.15)

        resultado = repositorio.adquirir_bloqueo(doc.id, "beto", ttl_minutos=3)

        assert resultado.adquirido is True

    def test_renovar_extiende_el_vencimiento(self, repositorio: RepositorioDocumentos):
        doc = repositorio.crear(_documento())
        repositorio.adquirir_bloqueo(doc.id, "ana", ttl_minutos=0.001)
        assert repositorio.renovar_bloqueo(doc.id, "ana", ttl_minutos=3) is True
        time.sleep(0.15)

        # Sin la renovación ya habría vencido; con ella, "beto" NO puede tomarlo.
        resultado = repositorio.adquirir_bloqueo(doc.id, "beto", ttl_minutos=3)

        assert resultado.adquirido is False

    def test_renovar_de_otro_usuario_falla(self, repositorio: RepositorioDocumentos):
        doc = repositorio.crear(_documento())
        repositorio.adquirir_bloqueo(doc.id, "ana", ttl_minutos=3)

        assert repositorio.renovar_bloqueo(doc.id, "beto", ttl_minutos=3) is False

    def test_renovar_ya_vencido_falla(self, repositorio: RepositorioDocumentos):
        """No renueva un bloqueo ya vencido: pudo haberlo tomado otro
        revisor entretanto (ver docstring de renovar_bloqueo)."""
        doc = repositorio.crear(_documento())
        repositorio.adquirir_bloqueo(doc.id, "ana", ttl_minutos=0.001)
        time.sleep(0.15)

        assert repositorio.renovar_bloqueo(doc.id, "ana", ttl_minutos=3) is False

    def test_liberar_permite_que_otro_adquiera(self, repositorio: RepositorioDocumentos):
        doc = repositorio.crear(_documento())
        repositorio.adquirir_bloqueo(doc.id, "ana", ttl_minutos=3)

        assert repositorio.liberar_bloqueo(doc.id, "ana") is True
        resultado = repositorio.adquirir_bloqueo(doc.id, "beto", ttl_minutos=3)

        assert resultado.adquirido is True

    def test_liberar_de_otro_usuario_no_hace_nada(self, repositorio: RepositorioDocumentos):
        doc = repositorio.crear(_documento())
        repositorio.adquirir_bloqueo(doc.id, "ana", ttl_minutos=3)

        assert repositorio.liberar_bloqueo(doc.id, "beto") is False
        resultado = repositorio.adquirir_bloqueo(doc.id, "beto", ttl_minutos=3)
        assert resultado.adquirido is False  # sigue siendo de "ana"

    def test_bloqueo_no_afecta_version_de_contenido(self, repositorio: RepositorioDocumentos):
        """El bloqueo es ortogonal a la concurrencia optimista de escritura:
        adquirir/renovar/liberar NUNCA deben tocar `version`."""
        doc = repositorio.crear(_documento())
        version_original = doc.version

        repositorio.adquirir_bloqueo(doc.id, "ana", ttl_minutos=3)
        repositorio.renovar_bloqueo(doc.id, "ana", ttl_minutos=3)
        repositorio.liberar_bloqueo(doc.id, "ana")

        assert repositorio.obtener(doc.id).version == version_original
