"""
grafo.py — LangGraph del Agente B (plan_demo.md §4.6).

Dos nodos, como pide el plan:

    interpretar  -> el LLM decide qué skill del agent card resuelve la
                    tarea y sobre qué objeto (servicio, base de datos o
                    cambio). Emite `thought`.
    consultar    -> recorre el grafo `infra` por MCP (`ekl-graph` con
                    `--base infra`) y arma el contrato. Emite `tool_call`
                    / `tool_result` desde el lado del agente.

**Lo que devuelve es solo el contrato acordado**: servicios afectados,
responsable y linaje. Nunca su Cypher ni su esquema — eso se queda en
este archivo y no sale en el artefacto A2A (lo comprueba
`tests/test_agents.py`).

**La identidad del usuario original viaja en la tarea A2A** y se usa
para las llamadas MCP, así que el RBAC del dominio de infraestructura se
aplica con el rol de quien preguntó, no con uno del agente.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from agents.eventos import ContextoCorrida, cronometro, emitir, emitir_thought  # noqa: E402
from agents.llm import LLM  # noqa: E402
from agents.mcp_cliente import ClienteMCP, ErrorHerramienta  # noqa: E402
from agents.modelos import SalidaInterpretacionB  # noqa: E402

SKILLS = ("impacto_cambio", "responsable_servicio")

#: Recorridos del dominio infra. Viven aquí y **no** salen en la
#: respuesta A2A: el agente que pregunta recibe el contrato, no el cómo.
#: `r.fuente` trae el linaje de la dependencia: `carga_inicial` si vino
#: del CSV inicial, o `runbook_pagos.md#L24` si la extrajo del documento
#: `data/extract_from_docs.py` (F6). Es lo que permite que la advertencia
#: sobre `nomina-batch` cite la línea donde está escrita.
_Q_SERVICIOS_POR_BD = (
    "MATCH (s:Servicio)-[r:DEPENDE_DE]->(bd:BaseDatos {{id:'{objetivo}'}}) "
    "RETURN DISTINCT s.id AS id, s.nombre AS nombre, r.fuente AS fuente"
)
_Q_BD_POR_CAMBIO = (
    "MATCH (c:Cambio {{id:'{objetivo}'}})-[:AFECTA]->(bd:BaseDatos) "
    "RETURN DISTINCT bd.id AS id, bd.motor AS motor, bd.ambiente AS ambiente"
)
_Q_DETALLE_CAMBIO = (
    "MATCH (c:Cambio {{id:'{objetivo}'}}) "
    "RETURN c.id AS id, c.descripcion AS descripcion, c.fecha AS fecha, c.ventana AS ventana"
)
_Q_SERVICIOS_POR_SERVICIO = (
    "MATCH (s:Servicio)-[r:DEPENDE_DE]->(d:Servicio {{id:'{objetivo}'}}) "
    "RETURN DISTINCT s.id AS id, s.nombre AS nombre, r.fuente AS fuente"
)
#: Bases de las que depende un servicio. Hace falta para el impacto
#: colateral: quien pregunta por "migrar la base de datos del servicio X"
#: no conoce el id de esa base — vive en este dominio, no en el suyo.
_Q_BD_DE_SERVICIO = (
    "MATCH (s:Servicio {{id:'{objetivo}'}})-[:DEPENDE_DE]->(bd:BaseDatos) "
    "RETURN DISTINCT bd.id AS id, bd.motor AS motor, bd.ambiente AS ambiente"
)
_Q_RESPONSABLE = (
    "MATCH (s:Servicio {{id:'{objetivo}'}})-[:ES_RESPONSABLE]->(p:Persona) "
    "RETURN p.id AS id, p.nombre AS nombre, p.rol AS rol, p.contacto AS contacto"
)


class EstadoB(TypedDict, total=False):
    pregunta: str
    skill_pedida: str
    objetivo_pedido: str
    skill: str
    objetivo: str
    servicios_afectados: list[dict[str, Any]]
    responsable: dict[str, Any] | None
    detalle: dict[str, Any]
    linaje: list[str]
    nota: str | None


def _linaje_de_dependencia(fuente: str | None, objetivo: str | None) -> str:
    """Linaje de una dependencia concreta.

    Si la arista trae una `fuente` de documento (la escribió
    `data/extract_from_docs.py`), se cita el documento y la línea: es lo
    que un auditor puede abrir y verificar. Si no, se dice que viene de
    la carga inicial del grafo, sin fingir una fuente que no hay.
    """
    fuente = (fuente or "").strip()
    if fuente and fuente != "carga_inicial":
        return f"fuente: {fuente} (extraído del documento) | grafo infra"
    return f"grafo infra: dependencia declarada de {objetivo or 'la base de datos'}"


def _linaje_grafo(descripcion: str) -> str:
    """Linaje del dato, **en lenguaje de dominio**.

    Tira de una cuerda en dos direcciones: el §4.6 exige devolver linaje,
    y a la vez prohíbe exponer el Cypher y el esquema. Se resuelve
    nombrando la fuente y qué relación se usó en palabras ("dependencia
    declarada de PAY-DB-01"), no con el patrón del grafo
    (`(:Servicio)-[:DEPENDE_DE]->(:BaseDatos)`), que sí es esquema y no
    le hace falta a quien pregunta para auditar el dato.
    """
    return f"grafo infra (dominio Infraestructura y TI): {descripcion}"


def construir_grafo(llm: LLM, ctx: ContextoCorrida, mcp: ClienteMCP):
    """Construye el grafo de dos nodos atado a esta corrida."""

    async def interpretar(estado: EstadoB) -> EstadoB:
        sistema = (
            "Eres el agente del dominio de Infraestructura y TI de un banco. Otro agente te manda "
            "una tarea acotada. Tu trabajo aquí es decidir cuál de tus dos skills la resuelve y "
            "sobre qué objeto actuar.\n"
            "Skills disponibles:\n"
            "- impacto_cambio(objetivo): qué servicios se ven afectados si cambia o cae una base de "
            "datos, un servicio o un cambio planificado.\n"
            "- responsable_servicio(objetivo): quién es la persona responsable de un servicio.\n"
            "Responde en español."
        )
        humano = (
            f"Tarea recibida: {estado['pregunta']}\n"
            f"Skill sugerida por quien pregunta: {estado.get('skill_pedida') or '(ninguna)'}\n"
            f"Objeto sugerido: {estado.get('objetivo_pedido') or '(ninguno)'}\n"
            "Confirma la skill y el objeto, o corrígelos si la sugerencia no encaja."
        )
        async with cronometro() as t:
            salida, uso = await llm.estructurado(
                SalidaInterpretacionB,
                sistema=sistema,
                humano=humano,
                paso="interpretar",
                contexto={
                    "skill_pedida": estado.get("skill_pedida"),
                    "objetivo_pedido": estado.get("objetivo_pedido"),
                },
            )
        await emitir_thought(
            ctx, "interpretar", salida,
            extra={"skill": salida.skill, "objetivo": salida.objetivo},
            duracion_ms=t["ms"],
        )
        return {"skill": salida.skill, "objetivo": salida.objetivo}

    async def consultar(estado: EstadoB) -> EstadoB:
        skill = estado.get("skill") or "impacto_cambio"
        objetivo = estado.get("objetivo") or ""
        linaje: list[str] = []
        servicios: list[dict[str, Any]] = []
        responsable: dict[str, Any] | None = None
        detalle: dict[str, Any] = {}
        nota: str | None = None

        async def consultar_grafo(plantilla: str, descripcion: str, sobre: str | None = None) -> list[dict[str, Any]]:
            """Ejecuta un recorrido del grafo infra. `sobre` permite
            consultar por un objeto distinto del objetivo de la tarea
            (por ejemplo, las bases de datos que afecta un cambio)."""
            try:
                datos = await mcp.llamar(
                    "ekl-graph",
                    "read_cypher",
                    {"query": plantilla.format(objetivo=sobre if sobre is not None else objetivo)},
                    step="consultar",
                )
            except ErrorHerramienta as exc:
                await emitir(
                    ctx, "error",
                    {"paso": "consultar", "descripcion": descripcion, "error": exc.mensaje},
                    step="consultar",
                )
                return []
            if datos.get("filas"):
                linaje.append(_linaje_grafo(descripcion))
            if datos.get("nodos_filtrados_por_politica"):
                linaje.append(
                    "poda RBAC del dominio infra aplicada con el rol del usuario original "
                    f"('{ctx.rol}')"
                )
            return datos.get("filas", [])

        if skill == "responsable_servicio":
            filas = await consultar_grafo(
                _Q_RESPONSABLE, f"responsable declarado del servicio {objetivo}"
            )
            responsable = filas[0] if filas else None
            if responsable is None:
                nota = f"no hay ninguna persona responsable registrada para '{objetivo}'"
            # Nada más: el contrato de esta skill es el responsable y su
            # linaje. De qué bases depende el servicio es interno del
            # dominio infra y no se comparte (§4.6).
        else:
            # impacto_cambio acepta base de datos, servicio o cambio.
            if objetivo.upper().startswith("CHG"):
                cambio = await consultar_grafo(_Q_DETALLE_CAMBIO, f"ficha del cambio {objetivo}")
                detalle = cambio[0] if cambio else {}
                bds = await consultar_grafo(
                    _Q_BD_POR_CAMBIO, f"bases de datos que declara afectar el cambio {objetivo}"
                )
                for bd in bds:
                    filas = await consultar_grafo(
                        _Q_SERVICIOS_POR_BD,
                        f"servicios con dependencia declarada de {bd['id']}",
                        sobre=bd["id"],
                    )
                    servicios.extend(filas)
                detalle["bases_de_datos_afectadas"] = [b["id"] for b in bds]
            else:
                servicios = await consultar_grafo(
                    _Q_SERVICIOS_POR_BD, f"servicios con dependencia declarada de {objetivo}"
                )
                if servicios:
                    detalle = {"tipo_objetivo": "BaseDatos"}
                else:
                    # El objetivo es un servicio. "Qué se ve afectado si
                    # cambia su infraestructura" son dos cosas distintas:
                    # lo que depende de él, y lo que comparte su base de
                    # datos. Lo segundo es el hallazgo que nadie pide y
                    # que sin embargo tumba la ventana de mantenimiento —
                    # y quien pregunta no puede descubrirlo, porque el id
                    # de la base vive en este dominio.
                    servicios = await consultar_grafo(
                        _Q_SERVICIOS_POR_SERVICIO,
                        f"servicios que dependen del servicio {objetivo}",
                    )
                    bases = await consultar_grafo(
                        _Q_BD_DE_SERVICIO, f"bases de datos de las que depende {objetivo}"
                    )
                    hermanos: list[dict[str, Any]] = []
                    for bd in bases:
                        for otro in await consultar_grafo(
                            _Q_SERVICIOS_POR_BD,
                            f"servicios que comparten la base {bd['id']} con {objetivo}",
                            sobre=bd["id"],
                        ):
                            if otro.get("id") != objetivo:
                                hermanos.append(otro)
                    servicios.extend(hermanos)
                    if bases:
                        detalle = {
                            "tipo_objetivo": "Servicio",
                            "bases_de_datos": [b["id"] for b in bases],
                            "comparten_base": sorted({h["id"] for h in hermanos}),
                        }
                if not servicios:
                    nota = (
                        f"'{objetivo}' no tiene servicios dependientes registrados en el grafo de "
                        "infraestructura"
                    )

        # Quién aprueba un cambio no está escrito en la ficha del cambio:
        # es la persona responsable de los servicios que el cambio toca.
        # Sin esto, `impacto_cambio('CHG-...')` devolvía responsable=None y
        # la respuesta tenía que admitir que no sabía quién lo aprueba.
        if responsable is None and objetivo.upper().startswith("CHG"):
            for candidato in servicios:
                if not candidato.get("id"):
                    continue
                filas = await consultar_grafo(
                    _Q_RESPONSABLE,
                    f"responsable del servicio {candidato['id']}, afectado por {objetivo}",
                    sobre=candidato["id"],
                )
                if filas:
                    responsable = filas[0]
                    linaje.append(
                        f"quien aprueba se deriva de la persona responsable del servicio "
                        f"'{candidato['id']}', que es el que el cambio afecta"
                    )
                    break
            if responsable is None and not nota:
                nota = (
                    f"ningún servicio afectado por '{objetivo}' tiene una persona responsable "
                    "registrada, así que no hay a quién escalar la aprobación"
                )

        # Deduplicar por id conservando el orden.
        vistos, unicos = set(), []
        for s in servicios:
            if s.get("id") and s["id"] not in vistos:
                vistos.add(s["id"])
                unicos.append(s)

        return {
            "servicios_afectados": unicos,
            "responsable": responsable,
            "detalle": detalle,
            "linaje": linaje,
            "nota": nota,
        }

    grafo = StateGraph(EstadoB)
    grafo.add_node("interpretar", interpretar)
    grafo.add_node("consultar", consultar)
    grafo.set_entry_point("interpretar")
    grafo.add_edge("interpretar", "consultar")
    grafo.add_edge("consultar", END)
    return grafo.compile()


def contrato_de_salida(estado: EstadoB) -> dict[str, Any]:
    """El contrato que sale por A2A. **Nada más que esto.**

    Deliberadamente no incluye: las consultas Cypher ejecutadas, el
    esquema de la base `infra`, los ids internos de las herramientas MCP
    ni los nombres de las bases de datos del agente A.
    """
    contrato: dict[str, Any] = {
        "skill": estado.get("skill"),
        "objetivo": estado.get("objetivo"),
        # Cada servicio afectado va con **su** linaje, no solo con el del
        # bloque: si `nomina-batch` sale de la línea 24 del runbook y
        # `pagos-core` de la 16, quien pregunte puede citar la correcta.
        "servicios_afectados": [
            {
                "id": s.get("id"),
                "nombre": s.get("nombre"),
                "linaje": _linaje_de_dependencia(s.get("fuente"), estado.get("objetivo")),
            }
            for s in (estado.get("servicios_afectados") or [])
        ],
        "responsable": estado.get("responsable"),
        "linaje": estado.get("linaje") or [],
    }
    if estado.get("detalle"):
        contrato["detalle"] = estado["detalle"]
    if estado.get("nota"):
        contrato["nota"] = estado["nota"]
    return contrato
