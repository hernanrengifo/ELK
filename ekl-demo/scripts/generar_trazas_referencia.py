#!/usr/bin/env python3
"""
generar_trazas_referencia.py — trazas de referencia para el plan B
(plan_demo.md §10 y §8, riesgo "sin internet en la sala").

Corre la pregunta hilo conductor con los dos roles usando
`LLM_MODEL=fake` y guarda cada corrida en `traces/`:

    traces/referencia_riesgo.jsonl
    traces/referencia_junior.jsonl

Con esos dos archivos, la Sala de control puede **reproducir la demo
entera sin LLM, sin agentes y sin Docker**: aparecen en su selector de
corridas marcadas con ★ y el botón "reproducir" las anima paso a paso.
Es lo que se proyecta si el día de la demo falla la red.

`traces/` no es `observability/traces/`: aquello es la salida de trabajo
y `demo_reset.sh` la borra; esto son trazas curadas que se quedan.

    python scripts/generar_trazas_referencia.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import httpx  # noqa: E402

# El ruido de httpx/langgraph tapa la única salida que importa aquí.
for _ruidoso in ("httpx", "httpcore", "langchain", "langgraph", "mcp"):
    logging.getLogger(_ruidoso).setLevel(logging.WARNING)

DESTINO = ROOT_DIR / "traces"
AGENT_B_PORT = int(os.environ.get("AGENT_B_PORT", "8002"))
AGENT_B_URL = f"http://127.0.0.1:{AGENT_B_PORT}"

PREGUNTA = (
    "¿Qué clientes corporativos con exposición crediticia mayor a 5.000 millones se verían "
    "afectados si migramos la base de datos del servicio de pagos el próximo fin de semana, "
    "y quién es el responsable técnico de ese servicio hoy?"
)
CORRIDAS = [("riesgo", "referencia_riesgo"), ("analista_junior", "referencia_junior")]


def _esperar_agente_b(proceso: subprocess.Popen, limite_s: int = 45) -> None:
    limite = time.time() + limite_s
    while time.time() < limite:
        if proceso.poll() is not None:
            raise RuntimeError("el agente B murió al arrancar")
        try:
            if httpx.get(f"{AGENT_B_URL}/health", timeout=1).status_code == 200:
                return
        except httpx.HTTPError:
            time.sleep(0.4)
    raise RuntimeError(f"el agente B no respondió en {AGENT_B_URL}")


async def _correr(entorno: dict[str, str]) -> None:
    from agents.agent_a.server import PeticionAsk, ejecutar
    from observability.trace_store import TraceStore

    DESTINO.mkdir(parents=True, exist_ok=True)
    sufijo = uuid.uuid4().hex[:6]
    store = TraceStore()

    for rol, nombre in CORRIDAS:
        run_id = f"{nombre}-{sufijo}"
        print(f"  {rol:<16} run_id={run_id} … ", end="", flush=True)
        resultado = await ejecutar(PeticionAsk(query=PREGUNTA, user_role=rol, run_id=run_id))
        if resultado.get("error"):
            raise RuntimeError(f"la corrida con rol {rol} falló: {resultado['error']}")

        # Los eventos del Agente B llegan por su propio proceso; se le da
        # un momento al disco antes de leer la traza.
        await asyncio.sleep(1.0)
        eventos = store.read(run_id)
        if not eventos:
            raise RuntimeError(f"no se encontraron eventos para {run_id}")
        fuentes = {e.source for e in eventos}
        if "agent_b" not in fuentes:
            raise RuntimeError(
                f"la traza de {run_id} no tiene eventos del Agente B ({sorted(fuentes)}): "
                "¿hay un agente B viejo corriendo en otro puerto?"
            )

        destino = DESTINO / f"{nombre}.jsonl"
        destino.write_text("\n".join(e.to_jsonl() for e in eventos) + "\n", encoding="utf-8")
        print(f"{len(eventos)} eventos, fuentes {sorted(fuentes)} -> {destino.relative_to(ROOT_DIR)}")


def main() -> int:
    entorno = {
        **os.environ,
        "LLM_MODEL": os.environ.get("LLM_MODEL", "fake"),
        "GRAPH_BACKEND": os.environ.get("GRAPH_BACKEND", "memory"),
        "EVENT_BUS": os.environ.get("EVENT_BUS", "memory"),
    }
    os.environ.update({k: v for k, v in entorno.items() if k in
                       ("LLM_MODEL", "GRAPH_BACKEND", "EVENT_BUS")})

    print(f"Generando trazas de referencia (LLM_MODEL={entorno['LLM_MODEL']})")
    log = ROOT_DIR / ".referencia_agent_b.log"
    proceso = subprocess.Popen(
        [sys.executable, str(ROOT_DIR / "agents" / "agent_b" / "server.py")],
        stdout=open(log, "w"), stderr=subprocess.STDOUT, env=entorno, cwd=str(ROOT_DIR),
    )
    try:
        _esperar_agente_b(proceso)
        asyncio.run(_correr(entorno))
    except Exception as exc:
        print(f"\nFALLÓ: {exc}")
        return 1
    finally:
        proceso.terminate()
        try:
            proceso.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proceso.kill()

    print("\nListo. La Sala de control las ofrece en su selector marcadas con ★,")
    print("y se reproducen sin LLM, sin agentes y sin Docker.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
