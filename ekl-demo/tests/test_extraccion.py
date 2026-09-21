"""
tests/test_extraccion.py — extracción desde documentos (F6).

Comprueba las tres cosas que hacen útil al extractor y una que lo hace
seguro:

  1. Canonicaliza contra los nodos existentes, aguantando cómo escribe un
     modelo (backticks, negritas, tildes, separadores distintos).
  2. **No inventa nodos**: un identificador que no existe se descarta, y
     en particular `PAY-DB-3` no se mapea a `PAY-DB-01` por parecido.
  3. Es idempotente: dos pasadas dejan el archivo igual.
  4. El linaje llega hasta el contrato del Agente B, que es para lo que
     existe todo esto.

    LLM_MODEL=fake pytest tests/test_extraccion.py -v
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import anyio
import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent
for _ruta in (str(ROOT_DIR), str(ROOT_DIR / "data"), str(ROOT_DIR / "mcp")):
    if _ruta not in sys.path:
        sys.path.insert(0, _ruta)

os.environ.setdefault("GRAPH_BACKEND", "memory")
os.environ.setdefault("EVENT_BUS", "memory")
os.environ.setdefault("LLM_MODEL", "fake")

from extract_from_docs import (  # noqa: E402
    TRIPLETAS_CONOCIDAS,
    UMBRAL_SIMILITUD,
    canonicalizar,
    catalogo_de_nodos,
    fusionar,
    leer_aristas,
    normalizar,
    procesar,
)

RUNBOOK = ROOT_DIR / "data" / "runbook_pagos.md"


@pytest.fixture(scope="module")
def catalogo():
    return catalogo_de_nodos()


# --------------------------------------------------------------------------
# 1. Canonicalización
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("escrito", "esperado"),
    [
        ("`pagos-core`", "pagos-core"),        # backticks del Markdown
        ("**PAY-DB-01**", "PAY-DB-01"),        # negrita
        ("Pagos Core", "pagos-core"),          # nombre en vez de id
        ("PAY DB 01", "PAY-DB-01"),            # separadores distintos
        ("pay_db_01", "PAY-DB-01"),
        ("Nómina Batch", "nomina-batch"),      # tildes
        ("notificaciones.", "notificaciones"), # puntuación pegada
    ],
)
def test_canonicaliza_como_escribe_un_modelo(catalogo, escrito, esperado):
    nodo, puntaje = canonicalizar(escrito, catalogo)
    assert nodo is not None, f"{escrito!r} debía resolver a {esperado}"
    assert nodo.id == esperado
    assert puntaje >= UMBRAL_SIMILITUD


# --------------------------------------------------------------------------
# 2. No inventar nodos
# --------------------------------------------------------------------------
def test_un_identificador_inexistente_no_se_mapea_al_parecido():
    """`PAY-DB-3` se parece un 0,95 a `PAY-DB-01` y es otra base de datos.

    En un identificador un dígito no es una errata: es la diferencia.
    Aceptarlo metería en el grafo una dependencia que nadie afirmó.
    """
    nodo, _ = canonicalizar("PAY-DB-3", catalogo_de_nodos())
    assert nodo is None


@pytest.mark.parametrize("escrito", ["cobranzas-api", "el servicio de pagos", "CRM-DB", ""])
def test_lo_que_no_existe_se_descarta(catalogo, escrito):
    nodo, _ = canonicalizar(escrito, catalogo)
    assert nodo is None


def test_pay_db_01_y_pay_db_02_no_se_confunden(catalogo):
    assert canonicalizar("PAY-DB-01", catalogo)[0].id == "PAY-DB-01"
    assert canonicalizar("PAY-DB-02", catalogo)[0].id == "PAY-DB-02"


def test_normalizar_unifica_separadores_y_tildes():
    assert normalizar("**PAY_DB-01**") == normalizar("pay db 01")
    assert normalizar("`Nómina Batch`") == normalizar("nomina-batch")


# --------------------------------------------------------------------------
# 3. Tripletas y citas
# --------------------------------------------------------------------------
def test_las_tripletas_del_simulado_citan_lineas_reales_del_runbook():
    """Cada tripleta apunta a una línea que de verdad afirma eso.

    Si el runbook se edita y las líneas se mueven, esto falla — que es
    justo lo que tiene que pasar: una cita que ya no apunta a donde dice
    es peor que no citar.
    """
    lineas = RUNBOOK.read_text(encoding="utf-8").splitlines()
    for tripleta in TRIPLETAS_CONOCIDAS.tripletas:
        assert 1 <= tripleta.linea <= len(lineas)
        texto = normalizar(lineas[tripleta.linea - 1])
        assert normalizar(tripleta.servicio) in texto or normalizar(tripleta.destino) in texto, (
            f"la línea {tripleta.linea} no menciona "
            f"{tripleta.servicio} ni {tripleta.destino}: {lineas[tripleta.linea - 1]!r}"
        )


def test_nomina_batch_se_extrae_de_la_linea_que_lo_dice():
    aristas, descartadas = procesar(TRIPLETAS_CONOCIDAS, verboso=False)
    assert not descartadas
    colateral = [a for a in aristas if a["servicio_id"] == "nomina-batch"]
    assert colateral, "el hallazgo colateral del §3.4 tiene que salir de la extracción"
    assert colateral[0]["destino_id"] == "PAY-DB-01"
    assert colateral[0]["fuente"].startswith("runbook_pagos.md#L")


# --------------------------------------------------------------------------
# 4. Idempotencia
# --------------------------------------------------------------------------
def test_la_fusion_es_idempotente():
    aristas, _ = procesar(TRIPLETAS_CONOCIDAS, verboso=False)
    existentes = leer_aristas()

    primera, cambios_1 = fusionar(existentes, aristas)
    segunda, cambios_2 = fusionar(primera, aristas)

    assert segunda == primera, "una segunda pasada no puede cambiar nada"
    assert cambios_2 == [], f"la segunda pasada reportó cambios: {cambios_2}"
    # Y nunca duplica una arista.
    claves = [(f["servicio_id"], f["destino_id"]) for f in primera]
    assert len(claves) == len(set(claves))


def test_la_extraccion_no_borra_aristas_que_el_documento_no_menciona():
    """El runbook solo habla de pagos; `tesoreria-api` y `onboarding` no
    pueden desaparecer por no estar en él."""
    aristas, _ = procesar(TRIPLETAS_CONOCIDAS, verboso=False)
    fusionadas, _ = fusionar(leer_aristas(), aristas)
    ids = {(f["servicio_id"], f["destino_id"]) for f in fusionadas}
    assert ("tesoreria-api", "TES-DB") in ids
    assert ("onboarding", "ONB-DB") in ids


# --------------------------------------------------------------------------
# 5. El linaje llega al contrato del Agente B
# --------------------------------------------------------------------------
def test_impacto_de_un_servicio_incluye_quien_comparte_su_base():
    """El hallazgo colateral del §3.4, descubierto sin hacer trampa.

    Quien pregunta por "migrar la base de datos del servicio de pagos"
    **no puede** conocer el id `PAY-DB-01`: vive en el dominio de infra.
    Así que `impacto_cambio(pagos-core)` tiene que resolver las bases del
    servicio y devolver también lo que las comparte — que es como aparece
    `nomina-batch`.
    """
    from agents.agent_b.grafo import construir_grafo, contrato_de_salida
    from agents.eventos import ContextoCorrida
    from agents.llm_fake import LLMFake
    from agents.mcp_cliente import ClienteMCP

    async def _run():
        ctx = ContextoCorrida(run_id="test-impacto-servicio", user_ctx={"rol": "riesgo"},
                              source="agent_b", modelo="fake")
        async with ClienteMCP(ctx, grafo_base="infra", con_acciones=False) as mcp:
            grafo = construir_grafo(LLMFake(), ctx, mcp)
            estado = await grafo.ainvoke(
                {"pregunta": "impacto", "skill_pedida": "impacto_cambio",
                 "objetivo_pedido": "pagos-core"}
            )
        return contrato_de_salida(estado)

    contrato = anyio.run(_run)
    afectados = {s["id"]: s for s in contrato["servicios_afectados"]}
    assert "nomina-batch" in afectados, (
        "sin esto el hallazgo colateral solo aparece si alguien ya conocía el id de la base"
    )
    assert "notificaciones" in afectados, "lo que depende del servicio sigue contando"
    assert "pagos-core" not in afectados, "el propio objetivo no es un afectado colateral"
    assert "runbook_pagos.md#L" in afectados["nomina-batch"]["linaje"]
    assert contrato["detalle"]["comparten_base"] == ["nomina-batch"]
    assert "PAY-DB-01" in contrato["detalle"]["bases_de_datos"]


def test_el_contrato_de_b_cita_el_runbook_por_servicio():
    from agents.agent_b.grafo import construir_grafo, contrato_de_salida
    from agents.eventos import ContextoCorrida
    from agents.llm_fake import LLMFake
    from agents.mcp_cliente import ClienteMCP

    async def _run():
        ctx = ContextoCorrida(run_id="test-linaje-doc", user_ctx={"rol": "riesgo"},
                              source="agent_b", modelo="fake")
        async with ClienteMCP(ctx, grafo_base="infra", con_acciones=False) as mcp:
            grafo = construir_grafo(LLMFake(), ctx, mcp)
            estado = await grafo.ainvoke(
                {"pregunta": "impacto", "skill_pedida": "impacto_cambio",
                 "objetivo_pedido": "PAY-DB-01"}
            )
        return contrato_de_salida(estado)

    contrato = anyio.run(_run)
    por_id = {s["id"]: s for s in contrato["servicios_afectados"]}
    assert set(por_id) == {"pagos-core", "nomina-batch"}
    assert "runbook_pagos.md#L" in por_id["nomina-batch"]["linaje"], (
        "la dependencia colateral tiene que citar la línea del documento"
    )
    # Y sigue sin filtrar el esquema (regla del §4.6).
    import json

    crudo = json.dumps(contrato, ensure_ascii=False)
    assert "DEPENDE_DE" not in crudo and "MATCH " not in crudo
