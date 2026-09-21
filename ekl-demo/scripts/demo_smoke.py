#!/usr/bin/env python3
"""
demo_smoke.py — test de regresión antes de presentar (plan_demo.md §4.9).

Ejecuta **las tres preguntas del guion (§5)** con rol `riesgo` y
comprueba el resultado esperado del §3.4, y después repite la pregunta
principal con rol `analista_junior` para comprobar el momento "wow" #2:
los clientes de sensibilidad alta desaparecen.

    LLM_MODEL=fake python scripts/demo_smoke.py       # determinista, sin API key
    LLM_MODEL=claude-sonnet-5 python scripts/demo_smoke.py

Con `LLM_MODEL=fake` las comprobaciones son estrictas. Con un modelo
real **se tolera la variación de redacción**: se comprueban los hechos
(qué clientes, qué cifras, qué responsable, qué se podó) contra la
evidencia y la ruta, no las palabras de la respuesta — un modelo puede
decir "Textiles Andinos" o "el cliente CLI01" y las dos están bien.

El script **levanta y apaga él mismo el Agente B**, en vez de dar por
hecho que hay uno corriendo: un agente B viejo en el puerto 8002 haría
que la corrida pareciera buena mientras las trazas se van a otro sitio.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import signal
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import httpx  # noqa: E402

for _ruidoso in ("httpx", "httpcore", "langchain", "langgraph", "mcp"):
    logging.getLogger(_ruidoso).setLevel(logging.WARNING)

AGENT_B_PORT = int(os.environ.get("AGENT_B_PORT", "8002"))
AGENT_B_URL = os.environ.get("AGENT_B_URL", f"http://127.0.0.1:{AGENT_B_PORT}")

PREGUNTA_PRINCIPAL = (
    "¿Qué clientes corporativos con exposición crediticia mayor a 5.000 millones se verían "
    "afectados si migramos la base de datos del servicio de pagos el próximo fin de semana, "
    "y quién es el responsable técnico de ese servicio hoy?"
)
PREGUNTA_TES_DB = "¿Qué productos quedarían sin servicio si cae TES-DB?"
PREGUNTA_CAMBIO = (
    "¿Quién debe aprobar el cambio CHG-2026-0917 y a qué clientes hay que notificar?"
)

#: Resultado esperado del §3.4 para la pregunta hilo conductor.
CLIENTES_ESPERADOS = {"CLI01", "CLI02", "CLI03"}
RESPONSABLE_ESPERADO = "Laura Gómez"
COLATERAL_ESPERADO = "nomina-batch"
EXPOSICION_MINIMA = 5_000_000_000
#: Clientes con `sensibilidad: alta` en data/clientes.csv.
CLIENTES_SENSIBLES = {"CLI01", "CLI05"}

VERDE, ROJO, AMARILLO, GRIS, FIN = "\033[32m", "\033[31m", "\033[33m", "\033[90m", "\033[0m"


class Fallo(AssertionError):
    pass


class Resultados:
    def __init__(self) -> None:
        self.ok = 0
        self.fallos: list[str] = []
        self.avisos: list[str] = []

    def comprobar(self, condicion: bool, descripcion: str, detalle: str = "") -> None:
        if condicion:
            self.ok += 1
            print(f"  {VERDE}✓{FIN} {descripcion}")
        else:
            self.fallos.append(f"{descripcion}{(' — ' + detalle) if detalle else ''}")
            print(f"  {ROJO}✗{FIN} {descripcion}")
            if detalle:
                print(f"    {GRIS}{detalle}{FIN}")

    def avisar(self, descripcion: str) -> None:
        self.avisos.append(descripcion)
        print(f"  {AMARILLO}!{FIN} {descripcion}")


# --------------------------------------------------------------------------
# Agente B: se levanta y se apaga aquí
# --------------------------------------------------------------------------
def _puerto_ocupado(puerto: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", puerto)) == 0


class AgenteB:
    """Levanta `agents/agent_b/server.py` como subproceso y lo apaga al
    salir.

    Si el puerto ya está ocupado **aborta por defecto**: casi siempre es
    un agente de una prueba anterior, y seguir daría un resultado que no
    corresponde a este código (pasó dos veces durante la construcción, y
    la corrida parecía buena con la traza incompleta). Con
    `reusar=True` se usa el que ya está — que es lo que quieres si
    acabas de levantar la demo con `arrancar.sh` y solo estás
    comprobando.
    """

    def __init__(self, entorno: dict[str, str], *, reusar: bool = False) -> None:
        self.entorno = entorno
        self.reusar = reusar
        self.proceso: subprocess.Popen | None = None
        self.log = ROOT_DIR / ".demo_smoke_agent_b.log"

    def __enter__(self) -> "AgenteB":
        if _puerto_ocupado(AGENT_B_PORT):
            if not self.reusar:
                raise Fallo(
                    f"el puerto {AGENT_B_PORT} ya está ocupado.\n"
                    f"  · Si es la demo levantada con arrancar.sh: vuelve a lanzar esto con "
                    f"--reusar-agente-b\n"
                    f"  · Si es un proceso viejo: pkill -f 'agents/agent_b'\n"
                    f"  Se aborta para no medir contra un código que no es el de este repo."
                )
            try:
                if httpx.get(f"{AGENT_B_URL}/health", timeout=2).status_code == 200:
                    print(f"  {GRIS}reutilizando el Agente B que ya está en {AGENT_B_URL}{FIN}")
                    return self
            except httpx.HTTPError:
                pass
            raise Fallo(f"el puerto {AGENT_B_PORT} está ocupado por algo que no responde a /health")
        self.proceso = subprocess.Popen(
            [sys.executable, str(ROOT_DIR / "agents" / "agent_b" / "server.py")],
            stdout=open(self.log, "w"),
            stderr=subprocess.STDOUT,
            env={**os.environ, **self.entorno},
            cwd=str(ROOT_DIR),
        )
        limite = time.time() + 45
        while time.time() < limite:
            if self.proceso.poll() is not None:
                raise Fallo(f"el agente B murió al arrancar. Log: {self.log}")
            try:
                if httpx.get(f"{AGENT_B_URL}/health", timeout=1).status_code == 200:
                    return self
            except httpx.HTTPError:
                time.sleep(0.4)
        raise Fallo(f"el agente B no respondió en {AGENT_B_URL}/health. Log: {self.log}")

    def __exit__(self, *exc) -> None:
        if self.proceso and self.proceso.poll() is None:
            self.proceso.send_signal(signal.SIGTERM)
            try:
                self.proceso.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover
                self.proceso.kill()


# --------------------------------------------------------------------------
# Comprobaciones
# --------------------------------------------------------------------------
def _cifras_de(resultado: dict[str, Any]) -> dict[str, float]:
    """Cifras de la evidencia, por nodo. Es la fuente de verdad: lo que
    diga la prosa se comprueba contra esto."""
    return {
        e["nodo"]: float(e["valor"])
        for e in resultado.get("evidencia") or []
        if e.get("tipo") == "cifra" and e.get("nodo") and isinstance(e.get("valor"), (int, float))
    }


def _texto_completo(resultado: dict[str, Any]) -> str:
    """Solo lo que el usuario lee como prosa.

    `politicas_aplicadas` queda fuera a propósito: ahí el id del nodo
    podado está por diseño (docs/demo_decisions.md §9), y es el bloque
    estructurado que mira quien audita, no el párrafo que lee el junior.
    """
    return " ".join(
        [resultado.get("respuesta") or ""]
        + list(resultado.get("advertencias") or [])
        + [h.get("texto", "") for h in resultado.get("hallazgos") or []]
    )


def _evidencia_sin_linaje(resultado: dict[str, Any]) -> list[str]:
    return [
        e["id"] for e in resultado.get("evidencia") or [] if not str(e.get("linaje") or "").strip()
    ]


def comprobar_principal(r: Resultados, resultado: dict[str, Any], estricto: bool) -> None:
    """Resultado esperado del §3.4: 3 clientes corporativos con cifras,
    Laura Gómez, y la advertencia colateral sobre nomina-batch."""
    cifras = _cifras_de(resultado)
    texto = _texto_completo(resultado)
    ruta = set(resultado.get("ruta_nodos") or [])

    r.comprobar(
        set(cifras) == CLIENTES_ESPERADOS,
        f"hay cifra para los 3 clientes corporativos esperados {sorted(CLIENTES_ESPERADOS)}",
        f"obtenidos: {sorted(cifras)}",
    )
    r.comprobar(
        all(v > EXPOSICION_MINIMA for v in cifras.values()),
        f"todas las exposiciones superan {EXPOSICION_MINIMA:,.0f} COP",
        f"valores: { {k: f'{v:,.0f}' for k, v in cifras.items()} }",
    )
    r.comprobar(
        RESPONSABLE_ESPERADO in texto,
        f"la respuesta nombra al responsable técnico ({RESPONSABLE_ESPERADO})",
        f"texto: {texto[:160]}",
    )
    r.comprobar(
        COLATERAL_ESPERADO in texto,
        f"la respuesta advierte del hallazgo colateral ({COLATERAL_ESPERADO})",
        f"advertencias: {resultado.get('advertencias')}",
    )
    r.comprobar(
        COLATERAL_ESPERADO in ruta,
        "la ruta de nodos incluye el servicio colateral",
        f"ruta: {sorted(ruta)}",
    )
    r.comprobar(not _evidencia_sin_linaje(resultado), "toda la evidencia tiene linaje")
    r.comprobar(
        not resultado.get("cifras_sin_linaje"),
        "el nodo crítico no dejó pasar cifras sin linaje",
        f"{resultado.get('cifras_sin_linaje')}",
    )
    r.comprobar(
        int(resultado.get("iteraciones") or 0) >= 2,
        "hubo al menos una iteración de refinamiento",
        f"iteraciones: {resultado.get('iteraciones')}",
    )

    if estricto:
        # Con el LLM simulado la redacción es fija: se puede exigir el texto.
        for cliente in sorted(CLIENTES_ESPERADOS):
            r.comprobar(cliente in texto, f"la respuesta menciona {cliente}")
    else:
        for cliente in sorted(CLIENTES_ESPERADOS):
            if cliente not in texto:
                r.avisar(
                    f"la respuesta no menciona {cliente} por su id (con modelo real se tolera: "
                    "la cifra sí está en la evidencia)"
                )


def comprobar_tes_db(r: Resultados, resultado: dict[str, Any]) -> None:
    texto = _texto_completo(resultado).lower()
    ruta = {n.lower() for n in resultado.get("ruta_nodos") or []}
    r.comprobar(bool(resultado.get("respuesta")), "la pregunta sobre TES-DB produjo respuesta")
    r.comprobar(
        "tesoreria-api" in ruta or "tesorer" in texto,
        "la ruta llega al servicio de tesorería",
        f"ruta: {sorted(ruta)}",
    )
    r.comprobar(not _evidencia_sin_linaje(resultado), "toda la evidencia tiene linaje")


def comprobar_cambio(r: Resultados, resultado: dict[str, Any]) -> None:
    texto = _texto_completo(resultado)
    r.comprobar(bool(resultado.get("respuesta")), "la pregunta sobre CHG-2026-0917 produjo respuesta")
    r.comprobar(
        RESPONSABLE_ESPERADO in texto,
        f"identifica a quien debe aprobar ({RESPONSABLE_ESPERADO})",
        f"texto: {texto[:160]}",
    )
    r.comprobar(not _evidencia_sin_linaje(resultado), "toda la evidencia tiene linaje")


def comprobar_analista_junior(r: Resultados, resultado: dict[str, Any]) -> None:
    """Momento "wow" #2: mismo agente, mismo grafo, otro usuario."""
    texto = _texto_completo(resultado)
    cifras = _cifras_de(resultado)
    ruta = set(resultado.get("ruta_nodos") or [])
    politicas = resultado.get("politicas_aplicadas") or {}

    for sensible in sorted(CLIENTES_SENSIBLES):
        r.comprobar(
            sensible not in ruta,
            f"{sensible} (sensibilidad alta) no aparece en la ruta recorrida",
            f"ruta: {sorted(ruta)}",
        )
        r.comprobar(
            sensible not in texto,
            f"{sensible} no aparece en la respuesta",
        )
        r.comprobar(
            sensible not in cifras,
            f"no hay cifra de {sensible} en la evidencia",
        )
    r.comprobar(
        bool(politicas.get("nodos_podados")) or bool(politicas.get("acciones_denegadas")),
        "la respuesta declara qué aplicó la política",
        f"politicas: {politicas}",
    )


# --------------------------------------------------------------------------
# Corrida
# --------------------------------------------------------------------------
async def _preguntar(query: str, rol: str, run_id: str) -> dict[str, Any]:
    from agents.agent_a.server import PeticionAsk, ejecutar

    return await ejecutar(PeticionAsk(query=query, user_role=rol, run_id=run_id))


async def _correr_casos(casos: list[tuple[str, str, str, Any]], r: "Resultados", sufijo: str) -> None:
    """Todas las preguntas en **un solo bucle de eventos**.

    No es un detalle: el bus de eventos es un singleton del proceso y su
    cliente de Redis queda ligado al bucle en el que se creó, así que un
    `asyncio.run()` por pregunta rompería las corridas a partir de la
    segunda.
    """
    for indice, (titulo, query, rol, comprobaciones) in enumerate(casos):
        run_id = f"smoke-{sufijo}-{rol}-{indice}"
        print(f"\n{titulo}\n{'-' * 72}")
        t0 = time.perf_counter()
        resultado = await _preguntar(query, rol, run_id)
        segundos = time.perf_counter() - t0

        if resultado.get("error"):
            r.comprobar(False, "la corrida terminó sin error", resultado["error"])
            continue
        print(f"  {GRIS}run_id={resultado['run_id']}  {segundos:.1f}s  "
              f"iteraciones={resultado.get('iteraciones')}{FIN}")
        comprobaciones(resultado)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Smoke test de la demo EKL (plan_demo.md §4.9)")
    parser.add_argument(
        "--solo-principal", action="store_true",
        help="ejecutar solo la pregunta hilo conductor (más rápido con un modelo real)",
    )
    parser.add_argument(
        "--reusar-agente-b", action="store_true",
        help="usar el Agente B que ya esté corriendo (p. ej. tras ./scripts/arrancar.sh)",
    )
    args = parser.parse_args(argv)

    modelo = os.environ.get("LLM_MODEL", "fake")
    estricto = modelo == "fake"
    sufijo = uuid.uuid4().hex[:6]

    print(f"\n{'=' * 72}\nDEMO SMOKE — LLM_MODEL={modelo} "
          f"({'comprobaciones estrictas' if estricto else 'se tolera variación de redacción'})")
    print(f"GRAPH_BACKEND={os.environ.get('GRAPH_BACKEND', 'memory')} "
          f"EVENT_BUS={os.environ.get('EVENT_BUS', 'memory')}\n{'=' * 72}")

    r = Resultados()
    entorno_b = {k: os.environ[k] for k in os.environ if k.startswith(("EKL_", "LLM_", "GRAPH_", "EVENT_", "REDIS_", "DEMO_"))}

    try:
        with AgenteB(entorno_b, reusar=args.reusar_agente_b):
            casos: list[tuple[str, str, str, Any]] = [
                ("Pregunta hilo conductor (rol riesgo)", PREGUNTA_PRINCIPAL, "riesgo",
                 lambda res: comprobar_principal(r, res, estricto)),
            ]
            if not args.solo_principal:
                casos += [
                    ("Alternativa 1: caída de TES-DB (rol riesgo)", PREGUNTA_TES_DB, "riesgo",
                     lambda res: comprobar_tes_db(r, res)),
                    ("Alternativa 2: aprobación de CHG-2026-0917 (rol riesgo)", PREGUNTA_CAMBIO, "riesgo",
                     lambda res: comprobar_cambio(r, res)),
                ]
            casos.append(
                ("Pregunta hilo conductor (rol analista_junior)", PREGUNTA_PRINCIPAL, "analista_junior",
                 lambda res: comprobar_analista_junior(r, res))
            )

            asyncio.run(_correr_casos(casos, r, sufijo))
    except Fallo as exc:
        print(f"\n{ROJO}No se pudo preparar la prueba:{FIN} {exc}\n")
        return 2

    print(f"\n{'=' * 72}")
    if r.fallos:
        print(f"{ROJO}FALLÓ{FIN}: {len(r.fallos)} comprobación(es) de {r.ok + len(r.fallos)}")
        for fallo in r.fallos:
            print(f"  - {fallo}")
        print(f"{'=' * 72}\n")
        return 1
    print(f"{VERDE}OK{FIN}: {r.ok} comprobaciones pasaron"
          + (f", {len(r.avisos)} aviso(s)" if r.avisos else ""))
    for aviso in r.avisos:
        print(f"  {AMARILLO}!{FIN} {aviso}")
    print(f"{'=' * 72}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
