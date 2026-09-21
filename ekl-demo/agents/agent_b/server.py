#!/usr/bin/env python3
"""
server.py — Agente B como servidor A2A (plan_demo.md §4.6), puerto 8002.

Expone:

    GET  /.well-known/agent.json   agent card con las skills
                                   `impacto_cambio` y `responsable_servicio`
    POST /a2a                      JSON-RPC 2.0, método `tasks/send`
    GET  /health

Cada tarea recibida:

1. Saca `run_id` y `user_ctx` de `params.metadata` — **la identidad del
   usuario original**, con la que se harán las llamadas MCP para que el
   RBAC del dominio infra se aplique con ese rol (§4.6).
2. Emite `a2a_request` (lo que recibió) y corre su LangGraph de dos
   nodos, que emite `thought` y `tool_call`/`tool_result`.
3. Devuelve **solo el contrato** y emite `a2a_response`.

Arranque:

    python agents/agent_b/server.py
    # o: uvicorn agents.agent_b.server:app --port 8002
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any

if __name__ == "__main__" and __package__ in (None, ""):
    import runpy

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    runpy.run_module("agents.agent_b.server", run_name="__main__")
    raise SystemExit(0)

from fastapi import FastAPI, Request

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from agents.a2a import (  # noqa: E402
    ERROR_INTERNO,
    METODO_NO_ENCONTRADO,
    PARAMS_INVALIDOS,
    AgentCard,
    Artefacto,
    Capabilities,
    EstadoTarea,
    ParamsTarea,
    Parte,
    RespuestaJSONRPC,
    ResultadoTarea,
    SkillCard,
)
from agents.eventos import ContextoCorrida, describir_excepcion, emitir  # noqa: E402
from agents.llm import construir_llm  # noqa: E402
from agents.mcp_cliente import ClienteMCP  # noqa: E402
from agents.agent_b.grafo import construir_grafo, contrato_de_salida  # noqa: E402

AGENT_B_PORT = int(os.environ.get("AGENT_B_PORT", "8002"))
AGENT_B_URL = os.environ.get("AGENT_B_URL", f"http://127.0.0.1:{AGENT_B_PORT}")


def agent_card(url: str = AGENT_B_URL) -> AgentCard:
    """El agent card que el Agente A descubre antes de delegar."""
    return AgentCard(
        name="EKL — Agente de Infraestructura y TI",
        description=(
            "Responde sobre el dominio de infraestructura del banco: de qué depende cada servicio, "
            "a qué alcanza un cambio planificado y quién es responsable de qué. No expone su grafo "
            "ni sus consultas: devuelve contratos con linaje."
        ),
        url=url,
        version="1.0",
        capabilities=Capabilities(streaming=False, pushNotifications=False),
        skills=[
            SkillCard(
                id="impacto_cambio",
                name="Impacto de un cambio",
                description=(
                    "Dado el id de una base de datos, un servicio o un cambio planificado, devuelve "
                    "los servicios que se verían afectados, con linaje."
                ),
                tags=["infraestructura", "impacto", "cambio"],
                examples=[
                    "¿Qué servicios se verían afectados si se migra PAY-DB-01?",
                    "¿A qué servicios alcanza el cambio CHG-2026-0917?",
                ],
            ),
            SkillCard(
                id="responsable_servicio",
                name="Responsable de un servicio",
                description=(
                    "Dado el id de un servicio, devuelve la persona responsable hoy (nombre, rol y "
                    "contacto), con linaje."
                ),
                tags=["infraestructura", "responsable", "guardia"],
                examples=["¿Quién es el responsable técnico de pagos-core?"],
            ),
        ],
    )


def _leer_tarea(params: dict[str, Any]) -> tuple[ParamsTarea, str, str, dict[str, Any]]:
    """Valida `params` y extrae pregunta, skill sugerida y metadatos."""
    tarea = ParamsTarea.model_validate(params)
    pregunta = ""
    skill = ""
    objetivo = ""
    for parte in tarea.message.parts:
        if parte.type == "text" and parte.text:
            pregunta = parte.text
        elif parte.type == "data" and parte.data:
            skill = str(parte.data.get("skill") or skill)
            objetivo = str(parte.data.get("parametro") or objetivo)
    return tarea, pregunta, skill, {"objetivo": objetivo}


app = FastAPI(
    title="EKL — Agente B (Infraestructura)",
    description="Servidor A2A del dominio de infraestructura (plan_demo.md §4.6)",
    version="1.0",
)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "agente": "agent_b", "modelo": os.environ.get("LLM_MODEL", "fake")}


@app.get("/.well-known/agent.json")
async def well_known() -> dict[str, Any]:
    return agent_card().model_dump()


@app.post("/a2a")
async def a2a(request: Request) -> dict[str, Any]:
    cuerpo = await request.json()
    peticion_id = cuerpo.get("id")
    metodo = cuerpo.get("method")

    if metodo != "tasks/send":
        return RespuestaJSONRPC(
            id=peticion_id,
            error={
                "code": METODO_NO_ENCONTRADO,
                "message": f"método no soportado: {metodo!r}",
                "data": {"soportados": ["tasks/send"]},
            },
        ).model_dump(exclude_none=True)

    try:
        tarea, pregunta, skill, extra = _leer_tarea(cuerpo.get("params") or {})
    except Exception as exc:  # params mal formados
        return RespuestaJSONRPC(
            id=peticion_id,
            error={"code": PARAMS_INVALIDOS, "message": f"params inválidos: {exc}"},
        ).model_dump(exclude_none=True)

    run_id = str(tarea.metadata.get("run_id") or "").strip()
    if not run_id:
        # El plan exige `run_id` en el cuerpo A2A (§4.8, propagación de
        # contexto): sin él los pasos de B no se pueden colgar de la
        # ejecución y la Sala de control los perdería.
        return RespuestaJSONRPC(
            id=peticion_id,
            error={
                "code": PARAMS_INVALIDOS,
                "message": "params.metadata.run_id es obligatorio: sin él la tarea no es trazable",
            },
        ).model_dump(exclude_none=True)

    user_ctx = tarea.metadata.get("user_ctx") or {}
    if not isinstance(user_ctx, dict):
        user_ctx = {"rol": str(user_ctx)}

    ctx = ContextoCorrida(
        run_id=run_id,
        user_ctx=user_ctx,
        source="agent_b",
        modelo=os.environ.get("LLM_MODEL", "fake"),
        a2a_task_id=tarea.id,
    )

    await emitir(
        ctx,
        "a2a_request",
        {
            "de": "agent_a",
            "para": "agent_b",
            "task_id": tarea.id,
            "skill": skill,
            "objetivo": extra["objetivo"],
            "pregunta": pregunta,
            "contexto_recibido": {"run_id": run_id, "user_ctx": user_ctx},
        },
        step="tasks/send",
    )

    t0 = time.perf_counter()
    try:
        llm = construir_llm()
        async with ClienteMCP(ctx, grafo_base="infra", con_acciones=False) as mcp:
            grafo = construir_grafo(llm, ctx, mcp)
            estado = await grafo.ainvoke(
                {
                    "pregunta": pregunta or f"{skill} sobre {extra['objetivo']}",
                    "skill_pedida": skill,
                    "objetivo_pedido": extra["objetivo"],
                }
            )
        contrato = contrato_de_salida(estado)
    except BaseException as exc:  # noqa: BLE001 - se convierte en error JSON-RPC
        detalle = describir_excepcion(exc)
        await emitir(ctx, "error", {"paso": "tasks/send", "error": detalle}, step="tasks/send")
        return RespuestaJSONRPC(
            id=peticion_id,
            error={"code": ERROR_INTERNO, "message": f"el agente B falló: {detalle}"},
        ).model_dump(exclude_none=True)

    duracion_ms = round((time.perf_counter() - t0) * 1000, 2)
    await emitir(
        ctx,
        "a2a_response",
        {
            "de": "agent_b",
            "para": "agent_a",
            "task_id": tarea.id,
            "contrato": contrato,
            "linaje": contrato.get("linaje", []),
        },
        step="tasks/send",
        meta={"duracion_ms": duracion_ms},
    )

    resultado = ResultadoTarea(
        id=tarea.id,
        sessionId=tarea.sessionId,
        status=EstadoTarea(state="completed"),
        artifacts=[Artefacto(name="contrato", parts=[Parte(type="data", data=contrato)])],
    )
    return RespuestaJSONRPC(id=peticion_id, result=resultado.model_dump(exclude_none=True)).model_dump(
        exclude_none=True
    )


def main() -> None:
    import uvicorn

    uvicorn.run(
        "agents.agent_b.server:app",
        host=os.environ.get("AGENT_HOST", "0.0.0.0"),
        port=AGENT_B_PORT,
        log_level=os.environ.get("LOG_LEVEL", "info"),
    )


if __name__ == "__main__":
    main()
