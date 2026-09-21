"""
tests/test_mcp.py — criterio de cierre de la sesión 2 (F2 + F2b).

Verifica los tres comportamientos que pide la fase y, de paso, el
criterio de cierre de la instrumentación:

1. Con rol `riesgo`, `retrieve_customer_position` devuelve **valor y
   linaje** para un cliente (plan_demo.md §4.4).
2. Con rol `analista_junior`, `read_cypher` sobre clientes devuelve
   **menos nodos** que con rol `riesgo`, y queda un evento
   `policy_decision` con resultado `podado` en `trace.jsonl` (§4.3, §4.8).
3. `list_node_actions` de un `Cliente` devuelve el contrato **sin el
   campo `backend`** (§3.2).
4. `trace.jsonl` contiene eventos de **ambos servidores MCP** con el
   **mismo `run_id`** — la propagación de contexto del §4.8.

Corre sin Docker ni Neo4j ni Redis:

    GRAPH_BACKEND=memory EVENT_BUS=memory pytest tests/test_mcp.py -v

(los dos valores son los que ya vienen por defecto).

Los tests usan el `trace.jsonl` y el `audit.jsonl` reales de la raíz del
repo —los mismos que mira el criterio de cierre y que limpia
`scripts/demo_reset.sh`— y los truncan al empezar la sesión de tests.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

import anyio
import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent
for _ruta in (str(ROOT_DIR), str(ROOT_DIR / "mcp"), str(ROOT_DIR / "data")):
    if _ruta not in sys.path:
        sys.path.insert(0, _ruta)

# El backend y el bus se fijan ANTES de importar los servidores: el bus de
# eventos y el grafo se construyen perezosamente en la primera llamada.
os.environ.setdefault("GRAPH_BACKEND", "memory")
os.environ.setdefault("EVENT_BUS", "memory")

TRACE_FILE = ROOT_DIR / "trace.jsonl"
AUDIT_FILE = ROOT_DIR / "audit.jsonl"

#: run_id compartido por las llamadas con rol `riesgo`: es el que debe
#: aparecer en trace.jsonl con eventos de los dos servidores.
RUN_RIESGO = f"test-riesgo-{uuid.uuid4().hex[:8]}"
#: run_id de la corrida con rol `analista_junior` (el "wow" #2 del guion:
#: misma pregunta, otro usuario, otro resultado).
RUN_JUNIOR = f"test-junior-{uuid.uuid4().hex[:8]}"

QUERY_CLIENTES = "MATCH (c:Cliente) RETURN c.id AS id, c.nombre AS nombre"


# --------------------------------------------------------------------------
# Infraestructura de los tests
# --------------------------------------------------------------------------
@pytest.fixture(scope="session", autouse=True)
def traza_limpia():
    """Trunca `trace.jsonl` / `audit.jsonl` (y las trazas por run) antes de
    la sesión de tests, para que lo que quede al final sea exactamente lo
    que produjeron estas pruebas."""
    from observability.trace_store import TraceStore

    TraceStore().clear()
    yield


@pytest.fixture(scope="session")
def servidor_grafo():
    from ekl_graph_server import crear_servidor

    return crear_servidor("negocio")


@pytest.fixture(scope="session")
def servidor_acciones():
    from ekl_actions_server import crear_servidor

    return crear_servidor()


def llamar(servidor, herramienta: str, argumentos: dict[str, Any], *, rol: str, run_id: str):
    """Invoca una herramienta MCP como lo hará el agente: cliente del SDK
    oficial, en proceso, con `run_id` y `user_ctx` en los **metadatos** de
    la llamada."""
    from mcp import Client

    async def _run():
        async with Client(servidor) as cliente:
            return await cliente.call_tool(
                herramienta,
                argumentos,
                meta={"run_id": run_id, "user_ctx": {"usuario": f"demo_{rol}", "rol": rol}},
            )

    return anyio.run(_run)


def datos(resultado) -> dict[str, Any]:
    """Contenido estructurado de un CallToolResult (o el JSON del bloque de
    texto, si el servidor no devolvió salida estructurada)."""
    if resultado.structured_content is not None:
        return resultado.structured_content
    return json.loads(resultado.content[0].text)


def eventos_de_traza(run_id: str | None = None) -> list[dict[str, Any]]:
    """Lee `trace.jsonl` crudo (no vía TraceStore) — así el test comprueba
    el archivo real que pide el criterio de cierre."""
    if not TRACE_FILE.exists():
        return []
    eventos = []
    with open(TRACE_FILE, encoding="utf-8") as f:
        for linea in f:
            linea = linea.strip()
            if not linea:
                continue
            evento = json.loads(linea)
            if run_id is None or evento.get("run_id") == run_id:
                eventos.append(evento)
    return eventos


# --------------------------------------------------------------------------
# 1. Rol `riesgo`: valor + linaje
# --------------------------------------------------------------------------
def test_riesgo_obtiene_posicion_con_valor_y_linaje(servidor_acciones):
    resultado = llamar(
        servidor_acciones,
        "retrieve_customer_position",
        {"customer_ref": "CLI01", "metrica": "exposicion_crediticia"},
        rol="riesgo",
        run_id=RUN_RIESGO,
    )
    assert not resultado.is_error, resultado.content

    posicion = datos(resultado)
    assert posicion["customer_ref"] == "CLI01"
    assert posicion["valor"] > 0
    assert posicion["moneda"] == "COP"
    assert posicion["fecha_corte"] == "2026-09-15"
    assert posicion["version"] == 2

    # El linaje dice de dónde salió la cifra: fuente + métrica + versión.
    linaje = posicion["linaje"]
    assert "posiciones.csv" in linaje
    assert "exposicion_crediticia v2" in linaje
    assert "fecha_corte: 2026-09-15" in linaje

    # Y la cifra es la que da la definición vigente (saldo + cupo a la
    # fecha de corte), no una inventada por el servidor.
    assert posicion["valor"] == pytest.approx(5_300_000_000)


def test_analista_junior_no_puede_ejecutar_la_accion(servidor_acciones):
    """§4.4: la política de la acción se verifica contra el rol y se
    deniega con un mensaje explicable."""
    resultado = llamar(
        servidor_acciones,
        "retrieve_customer_position",
        {"customer_ref": "CLI02", "metrica": "exposicion_crediticia"},
        rol="analista_junior",
        run_id=RUN_JUNIOR,
    )
    assert resultado.is_error
    mensaje = resultado.content[0].text
    assert "denegada" in mensaje.lower()
    assert "riesgo" in mensaje and "direccion" in mensaje

    denegaciones = [
        e
        for e in eventos_de_traza(RUN_JUNIOR)
        if e["kind"] == "policy_decision" and e["payload"].get("resultado") == "denegado"
    ]
    assert denegaciones, "debía quedar registrado un policy_decision: denegado"


def test_resolve_metric_devuelve_la_definicion_vigente(servidor_acciones):
    resultado = llamar(
        servidor_acciones,
        "resolve_metric",
        {"nombre": "exposicion_crediticia"},
        rol="riesgo",
        run_id=RUN_RIESGO,
    )
    metrica = datos(resultado)
    assert metrica["version"] == 2
    assert metrica["owner"] == "Riesgo"
    assert metrica["ejecutable"] is True
    assert "cupo" in metrica["definicion"].lower()


# --------------------------------------------------------------------------
# 2. Rol `analista_junior`: menos nodos + policy_decision podado
# --------------------------------------------------------------------------
def test_analista_junior_ve_menos_clientes_y_queda_el_podado_en_la_traza(servidor_grafo):
    con_riesgo = datos(
        llamar(servidor_grafo, "read_cypher", {"query": QUERY_CLIENTES}, rol="riesgo", run_id=RUN_RIESGO)
    )
    con_junior = datos(
        llamar(servidor_grafo, "read_cypher", {"query": QUERY_CLIENTES}, rol="analista_junior", run_id=RUN_JUNIOR)
    )

    assert con_junior["total"] < con_riesgo["total"], (
        "el rol analista_junior debía ver menos clientes que el rol riesgo"
    )
    # Los dos clientes con `sensibilidad: alta` de data/clientes.csv.
    assert con_riesgo["total"] - con_junior["total"] == 2
    assert set(con_junior["nodos_filtrados_por_politica"]) == {"CLI01", "CLI05"}

    ids_junior = {fila["id"] for fila in con_junior["filas"]}
    assert "CLI01" not in ids_junior and "CLI05" not in ids_junior
    assert "CLI02" in ids_junior

    # El podado quedó registrado en trace.jsonl, emitido por el servidor.
    podados = [
        e
        for e in eventos_de_traza(RUN_JUNIOR)
        if e["kind"] == "policy_decision" and e["payload"].get("resultado") == "podado"
    ]
    assert podados, "falta el evento policy_decision: podado en trace.jsonl"
    evento = podados[-1]
    assert evento["source"] == "mcp_graph"
    assert evento["payload"]["regla"] == "node_policies.Cliente.sensibilidad.alta"
    assert set(evento["payload"]["nodos_podados"]) == {"CLI01", "CLI05"}
    assert evento["meta"]["user_role"] == "analista_junior"

    # Y en la auditoría, con los seis campos del §4.3.
    registros = [
        json.loads(l)
        for l in AUDIT_FILE.read_text(encoding="utf-8").splitlines()
        if l.strip()
    ]
    del_junior = [r for r in registros if r["run_id"] == RUN_JUNIOR and r["tool"] == "read_cypher"]
    assert del_junior
    ultimo = del_junior[-1]
    assert {"timestamp", "user", "tool", "args", "nodos_devueltos", "filtrados"} <= set(ultimo)
    assert sorted(ultimo["filtrados"]) == ["CLI01", "CLI05"]


def test_read_cypher_rechaza_escritura(servidor_grafo):
    resultado = llamar(
        servidor_grafo,
        "read_cypher",
        {"query": "MATCH (c:Cliente) DETACH DELETE c"},
        rol="direccion",
        run_id=RUN_RIESGO,
    )
    assert resultado.is_error
    assert "solo lectura" in resultado.content[0].text.lower()


# --------------------------------------------------------------------------
# 3. list_node_actions: contrato sin `backend`
# --------------------------------------------------------------------------
def test_list_node_actions_devuelve_contrato_sin_backend(servidor_grafo):
    resultado = llamar(
        servidor_grafo, "list_node_actions", {"node_id": "CLI02"}, rol="riesgo", run_id=RUN_RIESGO
    )
    assert not resultado.is_error, resultado.content
    salida = datos(resultado)

    assert salida["tipo"] == "Cliente"
    acciones = salida["acciones"]
    assert len(acciones) == 1
    accion = acciones[0]

    assert accion["nombre"] == "retrieve_customer_position"
    assert accion["expuesta_por"] == "Cliente"
    assert accion["contrato"]["entrada"] == {"customer_ref": "string", "metrica": "string"}
    assert set(accion["contrato"]["salida"]) == {"valor", "moneda", "fecha_corte", "linaje"}
    assert accion["politica"]["requiere_rol"] == ["riesgo", "direccion"]

    # Lo importante (§3.2): el agente nunca ve dónde vive la acción.
    assert "backend" not in accion
    assert "mcp://" not in json.dumps(salida)


def test_get_schema_marca_el_servicio_como_nodo_frontera(servidor_grafo):
    esquema = datos(
        llamar(servidor_grafo, "get_schema", {}, rol="riesgo", run_id=RUN_RIESGO)
    )
    assert esquema["base"] == "negocio"
    frontera = {t["tipo"] for t in esquema["tipos"] if t["frontera"]}
    assert frontera == {"Servicio"}
    assert "retrieve_customer_position" in esquema["acciones"]


def test_find_entry_nodes_ignora_tildes_y_aplica_rbac(servidor_grafo):
    como_riesgo = datos(
        llamar(servidor_grafo, "find_entry_nodes", {"texto": "textiles"}, rol="riesgo", run_id=RUN_RIESGO)
    )
    assert [n["id"] for n in como_riesgo["nodos"]] == ["CLI01"]

    como_junior = datos(
        llamar(
            servidor_grafo, "find_entry_nodes", {"texto": "textiles"}, rol="analista_junior", run_id=RUN_JUNIOR
        )
    )
    assert como_junior["nodos"] == []
    assert como_junior["nodos_filtrados_por_politica"] == 1

    # Sin tildes: "nomina" encuentra "Nómina Batch".
    servicios = datos(
        llamar(servidor_grafo, "find_entry_nodes", {"texto": "nomina"}, rol="riesgo", run_id=RUN_RIESGO)
    )
    assert "nomina-batch" in {n["id"] for n in servicios["nodos"]}


# --------------------------------------------------------------------------
# 4. Criterio de cierre: trace.jsonl con los dos servidores y un run_id
# --------------------------------------------------------------------------
def test_trace_jsonl_tiene_eventos_de_ambos_servidores_con_el_mismo_run_id(
    servidor_grafo, servidor_acciones
):
    eventos = eventos_de_traza(RUN_RIESGO)
    assert eventos, "trace.jsonl no tiene eventos de la corrida"

    fuentes = {e["source"] for e in eventos}
    assert {"mcp_graph", "mcp_actions"} <= fuentes, (
        f"faltan eventos de algún servidor MCP para run_id={RUN_RIESGO}: {fuentes}"
    )

    tipos = {e["kind"] for e in eventos}
    assert {"tool_call", "tool_result", "policy_decision"} <= tipos

    # `seq` es monótono y sin huecos dentro de la ejecución, aunque los
    # eventos vengan de servidores distintos.
    seqs = sorted(e["seq"] for e in eventos)
    assert seqs == list(range(1, len(seqs) + 1))

    # Cada tool_call tiene su tool_result (o su error) con la misma
    # herramienta: es lo que dibuja las flechas de la Sala de control.
    llamadas = [e for e in eventos if e["kind"] == "tool_call"]
    respuestas = [e for e in eventos if e["kind"] in ("tool_result", "error")]
    assert len(llamadas) == len(respuestas)

    # Y ninguna corrida generó un run_id automático (todas mandaron el suyo).
    assert all(e["run_id"] == RUN_RIESGO for e in eventos)


def test_el_bus_rechaza_eventos_sin_run_id():
    """§8: el trace_store rechaza eventos sin `run_id` para que un fallo de
    propagación se note en el ensayo, no en la demo."""
    from observability.events import Event, MissingRunIdError, new_event
    from observability.trace_store import TraceStore

    with pytest.raises(MissingRunIdError):
        new_event(run_id="", source="mcp_graph", kind="tool_call")

    with pytest.raises(MissingRunIdError):
        TraceStore().append({"source": "mcp_graph", "kind": "tool_call", "payload": {}})

    with pytest.raises(MissingRunIdError):
        Event.from_any({"run_id": "   ", "source": "ui", "kind": "answer"})
