#!/usr/bin/env python3
"""
server.py — Agente A (Orquestador de Negocio), puerto 8001.

    POST /ask   {"query": "...", "user_role": "riesgo"}  -> {run_id, respuesta, ...}
    GET  /health

Cada petición abre una ejecución con su propio `run_id`, que viaja en
los metadatos de todas las llamadas MCP y en el cuerpo de la tarea A2A,
de modo que los eventos del Agente A, los del Agente B y los de los dos
servidores MCP quedan colgados de la misma corrida (§4.8).

Arranque:

    python agents/agent_a/server.py
    # o: uvicorn agents.agent_a.server:app --port 8001
"""

from __future__ import annotations

import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

if __name__ == "__main__" and __package__ in (None, ""):
    import runpy

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    runpy.run_module("agents.agent_a.server", run_name="__main__")
    raise SystemExit(0)

from fastapi import FastAPI
from pydantic import BaseModel, Field

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from agents.agent_a.grafo import MAX_ITERATIONS, construir_grafo  # noqa: E402
from agents.eventos import ContextoCorrida, describir_excepcion, emitir  # noqa: E402
from agents.llm import construir_llm, modelo_para  # noqa: E402
from agents.mcp_cliente import ClienteMCP  # noqa: E402

AGENT_A_PORT = int(os.environ.get("AGENT_A_PORT", "8001"))
AGENT_B_URL = os.environ.get("AGENT_B_URL", "http://127.0.0.1:8002")

#: La pregunta hilo conductor del §1, precargada.
PREGUNTA_PRINCIPAL = (
    "¿Qué clientes corporativos con exposición crediticia mayor a 5.000 millones se verían "
    "afectados si migramos la base de datos del servicio de pagos el próximo fin de semana, "
    "y quién es el responsable técnico de ese servicio hoy?"
)


class PeticionAsk(BaseModel):
    query: str = Field(default=PREGUNTA_PRINCIPAL)
    user_role: str = Field(default_factory=lambda: os.environ.get("DEMO_USER_ROLE", "riesgo"))
    usuario: str | None = None
    run_id: str | None = Field(
        default=None, description="Para forzar un run_id concreto (tests y reproducción)."
    )


async def ejecutar(peticion: PeticionAsk) -> dict[str, Any]:
    """Corre el grafo del Agente A para una pregunta. Es lo que usan el
    endpoint y `scripts/demo_smoke.py`."""
    run_id = peticion.run_id or f"run-{uuid.uuid4().hex[:10]}"
    user_ctx = {"usuario": peticion.usuario or f"demo_{peticion.user_role}", "rol": peticion.user_role}
    modelo = os.environ.get("LLM_MODEL", "fake")

    ctx = ContextoCorrida(run_id=run_id, user_ctx=user_ctx, source="agent_a", modelo=modelo)
    llm = construir_llm()
    # El LLM simulado escoge su guion según la pregunta.
    if hasattr(llm, "para_query"):
        llm = llm.para_query(peticion.query)

    # Modelo propio para planner y crítico si se configuró (§8): permite
    # bajar esos dos pasos a un modelo rápido sin tocar los que redactan.
    llms_por_paso = {}
    for paso in ("planner", "nodo_critico"):
        if modelo_para(paso) != modelo:
            propio = construir_llm(paso=paso)
            if hasattr(propio, "para_query"):
                propio = propio.para_query(peticion.query)
            llms_por_paso[paso] = propio

    t0 = time.perf_counter()
    try:
        async with ClienteMCP(ctx, grafo_base="negocio", con_acciones=True) as mcp:
            grafo = construir_grafo(llm, ctx, mcp, agent_b_url=AGENT_B_URL,
                                    llms_por_paso=llms_por_paso)
            estado = await grafo.ainvoke(
                {
                    "query": peticion.query,
                    "user_ctx": user_ctx,
                    "run_id": run_id,
                    "plan": [],
                    "visited_nodes": [],
                    "evidence": [],
                    "pending_gaps": [],
                    "is_complete": False,
                    "iteration": 0,
                    "max_iterations": MAX_ITERATIONS,
                    "audit_trail": [],
                },
                config={"recursion_limit": 60},
            )
    except BaseException as exc:  # noqa: BLE001 - se devuelve como error de la corrida
        detalle = describir_excepcion(exc)
        await emitir(ctx, "error", {"paso": "ejecutar", "error": detalle}, step="ask")
        return {"run_id": run_id, "error": detalle, "respuesta": None}

    duracion_ms = round((time.perf_counter() - t0) * 1000, 2)
    respuesta = estado.get("respuesta") or {}

    await emitir(
        ctx,
        "answer",
        {
            "query": peticion.query,
            "respuesta": respuesta.get("respuesta", ""),
            "hallazgos": respuesta.get("hallazgos", []),
            "advertencias": respuesta.get("advertencias", []),
            "ruta_nodos": respuesta.get("ruta_nodos", []),
            # La evidencia va completa (con `nodo` y `origen`) para que una
            # traza guardada se baste sola: la Sala de control reproduce el
            # plan B sin agentes, y ahí este evento es la única fuente.
            "evidencia": [
                {"id": e["id"], "tipo": e["tipo"], "descripcion": e["descripcion"],
                 "valor": e.get("valor"), "unidad": e.get("unidad"),
                 "linaje": e["linaje"], "nodo": e.get("nodo"), "origen": e.get("origen")}
                for e in respuesta.get("evidencia", [])
            ],
            "politicas_aplicadas": respuesta.get("politicas_aplicadas", {}),
            "nombres_nodos": estado.get("nombres_nodos") or {},
            "iteraciones": estado.get("iteration"),
            "motivo_corte": estado.get("motivo_corte"),
            "huecos_abiertos": estado.get("pending_gaps") or [],
        },
        step="responder",
        meta={"duracion_ms": duracion_ms},
    )

    return {
        "run_id": run_id,
        "query": peticion.query,
        "user_role": peticion.user_role,
        "modelo": modelo,
        "respuesta": respuesta.get("respuesta", ""),
        "hallazgos": respuesta.get("hallazgos", []),
        "advertencias": respuesta.get("advertencias", []),
        "ruta_nodos": respuesta.get("ruta_nodos", []),
        "evidencia": respuesta.get("evidencia", []),
        "politicas_aplicadas": respuesta.get("politicas_aplicadas", {}),
        # id de nodo -> nombre legible: lo usa la pestaña Ruta para poner
        # "Textiles Andinos S.A.S." debajo de `CLI01`.
        "nombres_nodos": estado.get("nombres_nodos") or {},
        "plan": estado.get("plan", []),
        "iteraciones": estado.get("iteration"),
        "motivo_corte": estado.get("motivo_corte"),
        "huecos_abiertos": estado.get("pending_gaps") or [],
        "validada": respuesta.get("validada"),
        "cifras_sin_linaje": respuesta.get("cifras_sin_linaje", []),
        "duracion_ms": duracion_ms,
        "audit_trail": estado.get("audit_trail", []),
    }


app = FastAPI(
    title="EKL — Agente A (Orquestador de Negocio)",
    description="LangGraph de 7 nodos sobre el grafo de negocio (plan_demo.md §4.5)",
    version="1.0",
)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "agente": "agent_a",
        "modelo": os.environ.get("LLM_MODEL", "fake"),
        "agent_b": AGENT_B_URL,
        "pregunta_principal": PREGUNTA_PRINCIPAL,
    }


@app.post("/ask")
async def ask(peticion: PeticionAsk) -> dict[str, Any]:
    return await ejecutar(peticion)


def main() -> None:
    import uvicorn

    uvicorn.run(
        "agents.agent_a.server:app",
        host=os.environ.get("AGENT_HOST", "0.0.0.0"),
        port=AGENT_A_PORT,
        log_level=os.environ.get("LOG_LEVEL", "info"),
    )


if __name__ == "__main__":
    main()
