"""
eventos.py — emisión de eventos desde los agentes (plan_demo.md §4.8).

**No define ningún esquema nuevo.** Todo pasa por
`observability/events.py` y `observability/event_bus.py` tal como
quedaron en F2b; aquí solo están los envoltorios que ahorran repetir el
`run_id`, el rol y la iteración en cada llamada.

`ContextoCorrida` es lo que viaja por toda la ejecución: el `run_id` que
cuelga los eventos de A, los de B y los de los dos servidores MCP de la
misma corrida, y el `user_ctx` que hace que el RBAC se aplique con la
identidad del usuario original también en el dominio de B (§4.6).
"""

from __future__ import annotations

import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from observability.event_bus import get_event_bus  # noqa: E402
from observability.events import EventKind, EventSource, new_event  # noqa: E402

from .modelos import PasoRazonado  # noqa: E402


@dataclass
class ContextoCorrida:
    """Identidad de una ejecución. Se propaga a los metadatos MCP y al
    cuerpo A2A para que todo cuelgue del mismo `run_id`."""

    run_id: str
    user_ctx: dict[str, Any]
    source: EventSource
    iteration: int = 0
    modelo: str | None = None
    #: Cuando el Agente B atiende una tarea de A, aquí queda el id de la
    #: petición A2A que la originó: es lo que permite anidar los pasos de
    #: B bajo la flecha ámbar en la Sala de control (§4.8, zona 2).
    a2a_task_id: str | None = None
    extra_meta: dict[str, Any] = field(default_factory=dict)

    @property
    def rol(self) -> str:
        return str(self.user_ctx.get("rol") or "riesgo")

    @property
    def usuario(self) -> str:
        return str(self.user_ctx.get("usuario") or "anonimo")

    def meta_mcp(self) -> dict[str, Any]:
        """Lo que viaja en `_meta` de cada llamada MCP."""
        return {"run_id": self.run_id, "user_ctx": dict(self.user_ctx)}


async def emitir(
    ctx: ContextoCorrida,
    kind: EventKind,
    payload: dict[str, Any],
    *,
    step: str | None = None,
    meta: dict[str, Any] | None = None,
) -> None:
    """Publica un evento del esquema de F2b colgado de esta corrida."""
    meta_final: dict[str, Any] = {
        "user_role": ctx.rol,
        "iteration": ctx.iteration,
        "modelo": ctx.modelo,
        **ctx.extra_meta,
        **(meta or {}),
    }
    if ctx.a2a_task_id:
        meta_final.setdefault("a2a_task_id", ctx.a2a_task_id)
    await get_event_bus().publish(
        new_event(
            run_id=ctx.run_id,
            source=ctx.source,
            kind=kind,
            step=step,
            payload=payload,
            meta=meta_final,
        )
    )


async def emitir_thought(
    ctx: ContextoCorrida,
    step: str,
    salida: PasoRazonado,
    *,
    extra: dict[str, Any] | None = None,
    duracion_ms: float | None = None,
) -> None:
    """El evento `thought` del §4.8: el razonamiento resumido del modelo y
    la decisión que tomó, sin el prompt."""
    await emitir(
        ctx,
        "thought",
        {"razonamiento": salida.razonamiento, "decision": salida.decision, **(extra or {})},
        step=step,
        meta={"duracion_ms": duracion_ms} if duracion_ms is not None else None,
    )


async def emitir_plan(
    ctx: ContextoCorrida,
    plan: list[dict[str, Any]],
    *,
    step: str = "planner",
    query: str | None = None,
) -> None:
    """El evento `plan`: sub-preguntas con su estado (§4.8).

    Lleva además la `query` del usuario: es el primer evento de la
    corrida que la conoce, y la Sala de control la usa para la flecha
    inicial y para etiquetar el run en su selector.
    """
    payload: dict[str, Any] = {"subpreguntas": plan}
    if query:
        payload["query"] = query
    await emitir(ctx, "plan", payload, step=step)


def describir_excepcion(exc: BaseException, _nivel: int = 0) -> str:
    """Mensaje legible de una excepción, aplanando `ExceptionGroup`.

    LangGraph corre cada nodo en un task group, así que un fallo dentro
    de un nodo llega como `ExceptionGroup: unhandled errors in a
    TaskGroup (1 sub-exception)` — inútil en la traza y en la Sala de
    control. Esto baja hasta las excepciones hoja y las nombra.
    """
    if _nivel > 5:  # pragma: no cover - cinturón contra anidamientos absurdos
        return f"{type(exc).__name__}: {exc}"
    hijas = getattr(exc, "exceptions", None)
    if hijas:
        return " | ".join(describir_excepcion(h, _nivel + 1) for h in hijas)
    causa = exc.__cause__
    propio = f"{type(exc).__name__}: {exc}"
    if causa is not None and _nivel < 3:
        return f"{propio} (causa: {describir_excepcion(causa, _nivel + 1)})"
    return propio


@asynccontextmanager
async def cronometro():
    """Mide la duración de un paso para el `meta.duracion_ms` del evento."""
    t0 = time.perf_counter()
    marca = {}
    try:
        yield marca
    finally:
        marca["ms"] = round((time.perf_counter() - t0) * 1000, 2)
