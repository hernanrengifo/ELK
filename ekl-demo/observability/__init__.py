"""
observability/ — bus de eventos y Sala de control de la demo EKL
(plan_demo.md §4.8, fase F2b).

    events.py       esquema Pydantic del evento (run_id, ts, seq, source,
                    kind, step, payload, meta)
    event_bus.py    dos backends por variable de entorno EVENT_BUS:
                    `redis` (Redis Streams) e `memory` (cola in-process)
    trace_store.py  persistencia a trace.jsonl por run_id + endpoint SSE
                    (FastAPI + sse-starlette) en el puerto 8020

Todo lo que emite eventos en la demo (agentes A y B, ambos servidores
MCP, la UI) usa `event_bus.get_event_bus()` y `events.new_event()`.
"""

from .events import (  # noqa: F401
    EVENT_KINDS,
    EVENT_SOURCES,
    Event,
    EventKind,
    EventMeta,
    EventSource,
    MissingRunIdError,
    new_event,
)
