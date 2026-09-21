"""
events.py — esquema de evento de la Sala de control (plan_demo.md §4.8).

Un único tipo de evento (`Event`) con los ocho campos del plan:

    run_id, ts, seq, source, kind, step, payload, meta

Todos los procesos de la demo (agentes A y B, ambos servidores MCP y la
UI) emiten instancias de este modelo al bus de eventos (`event_bus.py`),
que las persiste en `trace.jsonl` y las sirve por SSE (`trace_store.py`).

`run_id` es obligatorio y no puede ser vacío: es lo que cuelga los
eventos de los servidores MCP y del Agente B de la misma ejecución que
inició el Agente A. El plan lo pide explícitamente (§8, última fila de
la tabla de riesgos: "el trace_store rechaza eventos sin run_id en modo
demo para detectarlo en el ensayo").

`seq` es un contador monótono **por run_id** que asigna el bus en el
momento de publicar (ver `event_bus.py`), no quien construye el evento:
así el orden es consistente aunque los eventos vengan de procesos
distintos. Por eso `new_event()` lo deja en 0 y `EventBus.publish()` lo
sobreescribe.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# --- Vocabularios cerrados del §4.8 --------------------------------------

EventSource = Literal["agent_a", "agent_b", "mcp_graph", "mcp_actions", "ui"]

EventKind = Literal[
    "thought",
    "plan",
    "tool_call",
    "tool_result",
    "a2a_request",
    "a2a_response",
    "policy_decision",
    "critic_verdict",
    "refinement",
    "answer",
    "error",
]

#: Los mismos valores, como tuplas, para validar desde código sin
#: `typing.get_args` regado por todo el repo.
EVENT_SOURCES: tuple[str, ...] = ("agent_a", "agent_b", "mcp_graph", "mcp_actions", "ui")
EVENT_KINDS: tuple[str, ...] = (
    "thought",
    "plan",
    "tool_call",
    "tool_result",
    "a2a_request",
    "a2a_response",
    "policy_decision",
    "critic_verdict",
    "refinement",
    "answer",
    "error",
)


class MissingRunIdError(ValueError):
    """Un evento llegó sin `run_id` (o con `run_id` vacío).

    El bus y el `trace_store` la levantan en lugar de aceptar el evento:
    un evento sin `run_id` no se puede colgar de ninguna ejecución y
    ensuciaría la Sala de control.
    """


class EventMeta(BaseModel):
    """Metadatos del §4.8: `duracion_ms, tokens_in, tokens_out, modelo,
    user_role, iteration`.

    Admite claves extra (`extra="allow"`) para los campos que el plan
    menciona en la prosa pero no en la lista del esquema —`trace_id` y
    `span_id` para poder anidar spans en OpenTelemetry— y para lo que
    cada emisor quiera agregar sin cambiar el esquema.
    """

    model_config = ConfigDict(extra="allow")

    duracion_ms: float | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    modelo: str | None = None
    user_role: str | None = None
    iteration: int | None = None


class Event(BaseModel):
    """Evento de la Sala de control. Ver docstring del módulo."""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1, description="Ejecución (una pregunta) a la que pertenece el evento")
    ts: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    seq: int = Field(default=0, ge=0, description="Orden dentro del run_id; lo asigna el bus al publicar")
    source: EventSource
    kind: EventKind
    step: str | None = Field(
        default=None, description="Nodo del grafo LangGraph o herramienta MCP que originó el evento"
    )
    payload: dict[str, Any] = Field(default_factory=dict)
    meta: EventMeta = Field(default_factory=EventMeta)

    @field_validator("run_id")
    @classmethod
    def _run_id_no_vacio(cls, v: str) -> str:
        if not v or not v.strip():
            raise MissingRunIdError("run_id es obligatorio y no puede ser vacío")
        return v

    def to_jsonl(self) -> str:
        """Una línea de `trace.jsonl` (sin salto de línea final)."""
        return self.model_dump_json(exclude_none=False)

    @classmethod
    def from_any(cls, data: "Event | dict[str, Any]") -> "Event":
        """Normaliza un dict (venido de Redis, de HTTP o de trace.jsonl) a
        `Event`, levantando `MissingRunIdError` si le falta el `run_id`."""
        if isinstance(data, Event):
            return data
        if not isinstance(data, dict):
            raise TypeError(f"No se puede construir un Event desde {type(data).__name__}")
        if not str(data.get("run_id") or "").strip():
            raise MissingRunIdError("run_id es obligatorio y no puede ser vacío")
        return cls.model_validate(data)


def new_event(
    run_id: str,
    source: EventSource,
    kind: EventKind,
    *,
    step: str | None = None,
    payload: dict[str, Any] | None = None,
    meta: EventMeta | dict[str, Any] | None = None,
) -> Event:
    """Constructor de conveniencia. `seq` queda en 0: lo asigna el bus."""
    if not str(run_id or "").strip():
        raise MissingRunIdError("run_id es obligatorio y no puede ser vacío")
    if meta is None:
        meta_obj = EventMeta()
    elif isinstance(meta, EventMeta):
        meta_obj = meta
    else:
        meta_obj = EventMeta.model_validate(meta)
    return Event(
        run_id=run_id,
        source=source,
        kind=kind,
        step=step,
        payload=payload or {},
        meta=meta_obj,
    )
