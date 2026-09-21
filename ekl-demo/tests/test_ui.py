"""
tests/test_ui.py — la pestaña Capa semántica (F5).

Se prueba con `streamlit.testing.v1.AppTest`, el marco oficial de
Streamlit: ejecuta la app de verdad sin navegador, así que esto corre en
CI y no depende de hacer clic en píxeles.

Lo que se comprueba es el **momento "wow" #1** y sus dos formas de salir
mal, que son las que arruinarían la demo en vivo:

  1. Guardar una definición nueva **escribe el archivo**, y los
     servidores MCP la recogen sin reiniciar (lo garantiza la recarga por
     `mtime` de `mcp/ekl_policies.py`).
  2. Guardar un YAML inválido **no toca el archivo**: si el presentador
     se come una comilla en vivo, la demo no se queda sin capa semántica.
  3. Si el archivo cambia en disco por fuera (un `demo_reset.sh`, o
     revertir la métrica a mano), el editor se refresca en vez de
     seguir mostrando lo viejo y volver a escribirlo al guardar.

    LLM_MODEL=fake pytest tests/test_ui.py -v
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest
import yaml

ROOT_DIR = Path(__file__).resolve().parent.parent
for _ruta in (str(ROOT_DIR), str(ROOT_DIR / "mcp"), str(ROOT_DIR / "data")):
    if _ruta not in sys.path:
        sys.path.insert(0, _ruta)

os.environ.setdefault("GRAPH_BACKEND", "memory")
os.environ.setdefault("EVENT_BUS", "memory")
os.environ.setdefault("LLM_MODEL", "fake")

SEMANTIC = ROOT_DIR / "data" / "semantic_layer.yaml"
APP = ROOT_DIR / "ui" / "app.py"

pytest.importorskip("streamlit", reason="la UI necesita streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402


@pytest.fixture
def capa_semantica_intacta():
    """Restaura `semantic_layer.yaml` pase lo que pase en el test."""
    original = SEMANTIC.read_text(encoding="utf-8")
    yield original
    SEMANTIC.write_text(original, encoding="utf-8")


def _abrir_app() -> AppTest:
    app = AppTest.from_file(str(APP), default_timeout=60)
    app.run()
    return app


def _editor(app: AppTest):
    return app.get_by_key("editor_semantica")


def _boton(app: AppTest, etiqueta: str):
    for b in app.button:
        if b.label == etiqueta:
            return b
    raise AssertionError(f"no hay botón {etiqueta!r}: {[b.label for b in app.button]}")


def test_el_editor_carga_lo_que_hay_en_disco(capa_semantica_intacta):
    app = _abrir_app()
    assert _editor(app).value == capa_semantica_intacta


def test_guardar_una_version_nueva_escribe_el_archivo(capa_semantica_intacta):
    """El wow #1: cambiar la métrica y guardar, sin reiniciar nada."""
    app = _abrir_app()
    nuevo = capa_semantica_intacta.replace(
        "    version: 2\n    owner: Riesgo", "    version: 3\n    owner: Riesgo", 1
    ).replace(
        "SELECT SUM(saldo_dispuesto + cupo_comprometido)", "SELECT SUM(saldo_dispuesto)", 1
    )
    assert nuevo != capa_semantica_intacta

    _editor(app).set_value(nuevo).run()
    _boton(app, "Guardar").click().run()

    en_disco = yaml.safe_load(SEMANTIC.read_text(encoding="utf-8"))
    assert en_disco["metricas"]["exposicion_crediticia"]["version"] == 3
    assert "cupo_comprometido" not in en_disco["metricas"]["exposicion_crediticia"]["sql"]
    assert any("Guardado" in s.value for s in app.success), "debía confirmar el guardado"


def test_la_capa_semantica_recargada_cambia_la_cifra(capa_semantica_intacta):
    """La prueba de que el cambio **sirve**: la métrica v3 da otro número,
    y la recarga por `mtime` la recoge sin reiniciar el servidor MCP."""
    import anyio

    from agents.eventos import ContextoCorrida
    from agents.mcp_cliente import ClienteMCP

    async def _exposicion(cliente: str) -> tuple[float, int]:
        ctx = ContextoCorrida(run_id="test-ui-semantica", user_ctx={"rol": "riesgo"}, source="ui")
        async with ClienteMCP(ctx, grafo_base="negocio", con_acciones=True) as mcp:
            r = await mcp.llamar(
                "ekl-actions", "retrieve_customer_position",
                {"customer_ref": cliente, "metrica": "exposicion_crediticia"},
            )
        return r["valor"], r["version"]

    valor_v2, version_v2 = anyio.run(_exposicion, "CLI01")
    assert version_v2 == 2

    app = _abrir_app()
    _editor(app).set_value(
        capa_semantica_intacta
        .replace("    version: 2\n    owner: Riesgo", "    version: 3\n    owner: Riesgo", 1)
        .replace("SELECT SUM(saldo_dispuesto + cupo_comprometido)", "SELECT SUM(saldo_dispuesto)", 1)
    ).run()
    _boton(app, "Guardar").click().run()

    # `mtime` tiene resolución de milisegundos; un respiro evita una
    # carrera en máquinas rápidas.
    time.sleep(0.05)
    valor_v3, version_v3 = anyio.run(_exposicion, "CLI01")
    assert version_v3 == 3, "el servidor MCP tenía que recoger la definición nueva sin reiniciar"
    assert valor_v3 < valor_v2, "excluir el cupo comprometido tiene que bajar la cifra"


def test_un_yaml_invalido_no_toca_el_archivo(capa_semantica_intacta):
    app = _abrir_app()
    _editor(app).set_value("metricas: [esto: no es\n  yaml valido").run()
    _boton(app, "Guardar").click().run()

    assert SEMANTIC.read_text(encoding="utf-8") == capa_semantica_intacta
    assert app.error, "tenía que avisar del YAML inválido"


def test_un_yaml_valido_pero_sin_metricas_no_toca_el_archivo(capa_semantica_intacta):
    """YAML correcto pero que no es una capa semántica: también se rechaza."""
    app = _abrir_app()
    _editor(app).set_value("cualquier_cosa: 1\n").run()
    _boton(app, "Guardar").click().run()

    assert SEMANTIC.read_text(encoding="utf-8") == capa_semantica_intacta
    assert app.error


def test_descartar_cambios_vuelve_a_lo_que_hay_en_disco(capa_semantica_intacta):
    app = _abrir_app()
    _editor(app).set_value("metricas:\n  otra_cosa:\n    version: 9\n").run()
    _boton(app, "Descartar cambios").click().run()

    assert _editor(app).value == capa_semantica_intacta
    assert SEMANTIC.read_text(encoding="utf-8") == capa_semantica_intacta


def test_si_el_archivo_cambia_por_fuera_el_editor_se_refresca(capa_semantica_intacta):
    """Tras un `demo_reset.sh` el editor no puede seguir mostrando —ni
    volver a escribir— la versión vieja."""
    app = _abrir_app()
    assert _editor(app).value == capa_semantica_intacta

    externo = capa_semantica_intacta.replace(
        "    version: 2\n    owner: Riesgo", "    version: 7\n    owner: Riesgo", 1
    )
    time.sleep(0.05)
    SEMANTIC.write_text(externo, encoding="utf-8")

    app.run()   # el siguiente refresco de la UI
    assert _editor(app).value == externo, (
        "el editor tenía que recoger el cambio externo, no quedarse con lo viejo"
    )
