#!/usr/bin/env python3
"""
medir_tiempos.py — cronómetro del ensayo (plan_demo.md §10 y §6, F7).

Corre `demo_smoke.py` **tres veces seguidas** y reporta los tiempos de
cada pregunta, con el foco puesto en el criterio del checklist: **la
pregunta hilo conductor por debajo de 90 segundos de extremo a extremo**.

    LLM_MODEL=fake python scripts/medir_tiempos.py                  # piso sin LLM
    LLM_MODEL=claude-sonnet-5 python scripts/medir_tiempos.py       # el de verdad

Si se pasa de 90 s, el propio script recuerda las mitigaciones del §8 y
los comandos para aplicarlas:

    # modelo rápido para planner y crítico (los dos pasos que no redactan)
    LLM_MODEL=claude-sonnet-5 \\
    LLM_MODEL_PLANNER=claude-haiku-4-5-20251001 \\
    LLM_MODEL_CRITICO=claude-haiku-4-5-20251001 \\
    python scripts/medir_tiempos.py

    # y si aún no basta, cachear el plan del planner
    EKL_CACHE_PLAN=1 ... python scripts/medir_tiempos.py

Levanta y apaga el Agente B él mismo, igual que `demo_smoke.py`.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import statistics
import sys
import time
import uuid
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

for _ruidoso in ("httpx", "httpcore", "langchain", "langgraph", "mcp"):
    logging.getLogger(_ruidoso).setLevel(logging.WARNING)

from scripts.demo_smoke import (  # noqa: E402
    PREGUNTA_CAMBIO,
    PREGUNTA_PRINCIPAL,
    PREGUNTA_TES_DB,
    AgenteB,
    Fallo,
)

#: El criterio del §10: "Tiempo de la pregunta principal < 90 s de
#: extremo a extremo".
PRESUPUESTO_S = float(os.environ.get("EKL_PRESUPUESTO_S", "90"))
CORRIDAS = int(os.environ.get("EKL_CORRIDAS", "3"))

VERDE, ROJO, AMARILLO, GRIS, FIN = "\033[32m", "\033[31m", "\033[33m", "\033[90m", "\033[0m"

PREGUNTAS = [
    ("principal", PREGUNTA_PRINCIPAL, "riesgo"),
    ("tes-db", PREGUNTA_TES_DB, "riesgo"),
    ("cambio", PREGUNTA_CAMBIO, "riesgo"),
    ("principal/junior", PREGUNTA_PRINCIPAL, "analista_junior"),
]


async def _una_corrida(indice: int, sufijo: str) -> list[dict[str, Any]]:
    from agents.agent_a.server import PeticionAsk, ejecutar

    medidas = []
    for etiqueta, query, rol in PREGUNTAS:
        run_id = f"medicion-{sufijo}-{indice}-{etiqueta.replace('/', '-')}"
        t0 = time.perf_counter()
        resultado = await ejecutar(PeticionAsk(query=query, user_role=rol, run_id=run_id))
        segundos = time.perf_counter() - t0
        medidas.append(
            {
                "pregunta": etiqueta,
                "segundos": segundos,
                "error": resultado.get("error"),
                "iteraciones": resultado.get("iteraciones"),
                "cifras": len([e for e in resultado.get("evidencia") or [] if e["tipo"] == "cifra"]),
                "run_id": run_id,
            }
        )
        marca = ROJO + "✗" + FIN if resultado.get("error") else (
            VERDE + "✓" + FIN if etiqueta != "principal" or segundos < PRESUPUESTO_S
            else AMARILLO + "!" + FIN
        )
        print(f"    {marca} {etiqueta:<18} {segundos:>6.1f} s   "
              f"{GRIS}it={resultado.get('iteraciones')} cifras={medidas[-1]['cifras']}{FIN}")
        if resultado.get("error"):
            print(f"      {ROJO}{resultado['error'][:160]}{FIN}")
    return medidas


def _resumen(todas: list[list[dict[str, Any]]]) -> int:
    print(f"\n{'=' * 72}\nRESUMEN\n{'=' * 72}")
    fallos = [m for corrida in todas for m in corrida if m["error"]]
    por_pregunta: dict[str, list[float]] = {}
    for corrida in todas:
        for m in corrida:
            por_pregunta.setdefault(m["pregunta"], []).append(m["segundos"])

    print(f"{'pregunta':<20} {'min':>7} {'mediana':>9} {'max':>7}   corridas")
    for etiqueta, tiempos in por_pregunta.items():
        print(f"{etiqueta:<20} {min(tiempos):>6.1f}s {statistics.median(tiempos):>8.1f}s "
              f"{max(tiempos):>6.1f}s   " + " ".join(f"{t:.1f}" for t in tiempos))

    totales = [sum(m["segundos"] for m in corrida) for corrida in todas]
    print(f"\n{'total por corrida':<20} " + " ".join(f"{t:.1f}s" for t in totales))

    principal = por_pregunta.get("principal", [])
    peor = max(principal) if principal else 0.0
    print()
    if fallos:
        print(f"{ROJO}FALLÓ{FIN}: {len(fallos)} corrida(s) con error")
        for f in fallos:
            print(f"  - {f['pregunta']} ({f['run_id']}): {str(f['error'])[:120]}")
        return 1
    print(f"{VERDE}Las {len(todas)} corridas terminaron sin fallo.{FIN}")

    if peor < PRESUPUESTO_S:
        print(f"{VERDE}La pregunta principal cumple el presupuesto:{FIN} "
              f"peor caso {peor:.1f} s < {PRESUPUESTO_S:.0f} s")
        return 0

    print(f"{AMARILLO}La pregunta principal se pasa del presupuesto:{FIN} "
          f"peor caso {peor:.1f} s ≥ {PRESUPUESTO_S:.0f} s")
    print(f"\nMitigaciones del §8, en orden (vuelve a medir después de cada una):\n"
          f"  1. Modelo rápido para planner y crítico — no tocan la redacción:\n"
          f"     {GRIS}LLM_MODEL_PLANNER=claude-haiku-4-5-20251001 \\{FIN}\n"
          f"     {GRIS}LLM_MODEL_CRITICO=claude-haiku-4-5-20251001 python scripts/medir_tiempos.py{FIN}\n"
          f"  2. Cachear el plan del planner (apaga el paso más caro en la repetición):\n"
          f"     {GRIS}EKL_CACHE_PLAN=1 python scripts/medir_tiempos.py{FIN}\n"
          f"     Ojo: con la caché encendida la Sala de control ya no enseña al planner pensando,\n"
          f"     que es parte del minuto 1-3 del guion.\n"
          f"  3. Si aún no basta, reducir max_iterations a 4 en agents/agent_a/grafo.py.")
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mide los tiempos del ensayo (plan §10)")
    parser.add_argument("--corridas", type=int, default=CORRIDAS)
    parser.add_argument(
        "--reusar-agente-b", action="store_true",
        help="usar el Agente B que ya esté corriendo (p. ej. tras ./scripts/arrancar.sh)",
    )
    args = parser.parse_args(argv)

    modelo = os.environ.get("LLM_MODEL", "fake")
    planner = os.environ.get("LLM_MODEL_PLANNER") or modelo
    critico = os.environ.get("LLM_MODEL_CRITICO") or modelo
    cache = os.environ.get("EKL_CACHE_PLAN", "0")

    print(f"\n{'=' * 72}")
    print(f"MEDICIÓN DE TIEMPOS — {args.corridas} corridas de las 4 preguntas")
    print(f"  modelo:   {modelo}" + (f"  (planner: {planner}, crítico: {critico})"
                                     if planner != modelo or critico != modelo else ""))
    print(f"  caché del plan: {'encendida' if cache not in ('', '0') else 'apagada'}")
    print(f"  presupuesto de la pregunta principal: {PRESUPUESTO_S:.0f} s")
    if modelo == "fake":
        print(f"  {AMARILLO}Con LLM_MODEL=fake esto mide el piso sin LLM (MCP, A2A, grafo, "
              f"DuckDB),{FIN}")
        print(f"  {AMARILLO}no la latencia real. Para el criterio del §10 hace falta un modelo "
              f"de verdad.{FIN}")
    print("=" * 72)

    entorno_b = {k: os.environ[k] for k in os.environ
                 if k.startswith(("EKL_", "LLM_", "GRAPH_", "EVENT_", "REDIS_", "DEMO_", "ANTHROPIC_"))}
    sufijo = uuid.uuid4().hex[:6]
    todas: list[list[dict[str, Any]]] = []

    try:
        with AgenteB(entorno_b, reusar=args.reusar_agente_b):
            for i in range(1, args.corridas + 1):
                print(f"\n  Corrida {i} de {args.corridas}")
                todas.append(asyncio.run(_una_corrida(i, sufijo)))
    except Fallo as exc:
        print(f"\n{ROJO}No se pudo preparar la medición:{FIN} {exc}\n")
        return 3

    return _resumen(todas)


if __name__ == "__main__":
    raise SystemExit(main())
