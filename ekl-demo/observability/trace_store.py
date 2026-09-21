"""
trace_store.py — persistencia de la traza y stream SSE para la Sala de
control (plan_demo.md §4.8).

Dos piezas en un mismo módulo:

1. `TraceStore` — escritura/lectura de la traza en disco. Persiste **por
   `run_id`**: cada evento se escribe en el `trace.jsonl` global (una
   línea JSON por evento, con su `run_id` dentro) y también en
   `observability/traces/<run_id>.jsonl`, que es lo que permite el
   "selector de run_id" y el botón "reproducir" de la Sala de control
   (§4.8, controles) sin releer y filtrar la traza entera.
   **Rechaza eventos sin `run_id`** (`MissingRunIdError`).

2. La app FastAPI (`app`) que sirve esa traza:
   - `GET  /stream` y `GET /stream/{run_id}` — SSE en vivo (sse-starlette).
   - `POST /events` — ingesta HTTP para procesos que no hablan con el bus
     directamente; responde 422 si el evento no trae `run_id`.
   - `GET  /runs`, `GET /runs/{run_id}` — listado y reproducción.
   - `GET  /health`.

   Escucha en el **puerto 8020**:

       python observability/trace_store.py
       # o: uvicorn observability.trace_store:app --port 8020

   Con `EVENT_BUS=redis` este proceso es además quien consume el stream
   y escribe `trace.jsonl`; con `EVENT_BUS=memory` esa escritura la hace
   el propio proceso emisor vía el sink local del bus (ver
   `event_bus.py`), y este servidor solo sirve lo que ya está en disco
   más lo que pase por su propio bus.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

if __name__ == "__main__" and __package__ in (None, ""):
    # `python observability/trace_store.py` ejecuta el archivo suelto, sin
    # paquete, y los imports relativos de abajo fallarían. Se relanza como
    # `python -m observability.trace_store` para que las dos formas de
    # arrancarlo funcionen igual.
    import runpy
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    runpy.run_module("observability.trace_store", run_name="__main__")
    raise SystemExit(0)

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from sse_starlette.sse import EventSourceResponse

from .event_bus import _trace_local_activo, get_event_bus
from .events import Event, MissingRunIdError

# FastAPI y el bus se importan arriba (no dentro de `_crear_app`) porque
# con `from __future__ import annotations` FastAPI resuelve las
# anotaciones de los handlers contra los globales del módulo: si `Request`
# solo existiera como local de la función, FastAPI la tomaría por un
# parámetro de query. `event_bus` importa de aquí solo `TraceStore`, y lo
# hace de forma diferida, así que no hay ciclo.

ROOT_DIR = Path(__file__).resolve().parent.parent

#: `trace.jsonl` global — el que revisa el criterio de cierre de la fase
#: ("trace.jsonl contiene eventos de ambos servidores con el mismo run_id")
#: y el que limpia `scripts/demo_reset.sh`.
DEFAULT_TRACE_FILE = ROOT_DIR / "trace.jsonl"
#: Un archivo por ejecución, para el selector de run_id y la reproducción.
DEFAULT_TRACE_DIR = ROOT_DIR / "observability" / "traces"

TRACE_PORT = int(os.environ.get("TRACE_STORE_PORT", "8020"))

#: Página de la Sala de control (§4.8). Vive en `ui/` y se sirve desde
#: aquí en `/control`, que es donde el plan la quiere para proyectarla en
#: una segunda pantalla. Se lee del disco en cada petición para poder
#: retocarla sin reiniciar el servidor durante el ensayo.
CONTROL_ROOM_HTML = ROOT_DIR / "ui" / "control_room.html"

#: Trazas de referencia para reproducir sin LLM ni agentes (§10, plan B).
#: Distinto de `observability/traces/`, que es la salida de trabajo y la
#: limpia `demo_reset.sh`: esto son trazas curadas que no se borran.
TRACES_REFERENCIA_DIR = ROOT_DIR / "traces"

#: Espera antes de reenganchar el consumidor de trazas si el bus falla.
REINTENTO_CONSUMIDOR_S = float(os.environ.get("EKL_REINTENTO_CONSUMIDOR_S", "2"))

logger = logging.getLogger(__name__)

_RUN_ID_SEGURO = re.compile(r"[^A-Za-z0-9._-]")


def ordenar_cronologicamente(eventos: list[Event]) -> list[Event]:
    """Orden de la corrida: `ts` primero, `seq` como desempate.

    Ver `TraceStore.read` para por qué `seq` solo no basta.
    """
    return sorted(eventos, key=lambda e: (e.ts, e.seq))


def _nombre_archivo_run(run_id: str) -> str:
    """`run_id` saneado para usarlo como nombre de archivo (el run_id
    viaja en metadatos MCP y en cuerpos A2A: no puede abrir una ruta)."""
    return _RUN_ID_SEGURO.sub("_", run_id)[:120] or "sin_id"


class TraceStore:
    """Traza en disco. Ver docstring del módulo.

    Barato de instanciar: no abre descriptores hasta el primer `append`.
    Las rutas se pueden fijar por entorno (`EKL_TRACE_FILE`,
    `EKL_TRACE_DIR`) — es lo que usan los tests y `demo_reset.sh`.
    """

    def __init__(self, trace_file: Path | str | None = None, runs_dir: Path | str | None = None) -> None:
        self.trace_file = Path(trace_file or os.environ.get("EKL_TRACE_FILE") or DEFAULT_TRACE_FILE)
        self.runs_dir = Path(runs_dir or os.environ.get("EKL_TRACE_DIR") or DEFAULT_TRACE_DIR)
        self._lock = threading.Lock()

    # -- escritura --------------------------------------------------------
    def append(self, event: Event | dict[str, Any]) -> Event:
        """Persiste un evento. Levanta `MissingRunIdError` si no trae
        `run_id` — el trace_store no acepta eventos huérfanos (§8)."""
        evento = Event.from_any(event)
        linea = evento.to_jsonl()
        with self._lock:
            self.trace_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.trace_file, "a", encoding="utf-8") as f:
                f.write(linea + "\n")
            self.runs_dir.mkdir(parents=True, exist_ok=True)
            destino = self.runs_dir / f"{_nombre_archivo_run(evento.run_id)}.jsonl"
            with open(destino, "a", encoding="utf-8") as f:
                f.write(linea + "\n")
        return evento

    # -- lectura ----------------------------------------------------------
    def read(self, run_id: str | None = None) -> list[Event]:
        """Eventos persistidos, en orden cronológico.

        Se ordena por `(ts, seq)` y no solo por `seq` porque con
        `EVENT_BUS=memory` cada proceso numera por su cuenta: si el
        Agente A y el Agente B corren separados, los dos empiezan en 1
        dentro del mismo `run_id` (ver `event_bus.InProcessEventBus`).
        `ts` sí es comparable entre procesos, y `seq` desempata dentro
        de uno.
        """
        if run_id:
            path = self.runs_dir / f"{_nombre_archivo_run(run_id)}.jsonl"
            if not path.exists():
                # Fallback: la traza global filtrada (por si el archivo por
                # run se borró o la traza vino de otro proceso).
                eventos = [e for e in self._leer_archivo(self.trace_file) if e.run_id == run_id]
            else:
                eventos = self._leer_archivo(path)
            return ordenar_cronologicamente(eventos)
        return self._leer_archivo(self.trace_file)

    def runs(self) -> list[str]:
        """run_ids presentes en la traza global, en orden de aparición."""
        vistos: list[str] = []
        for evento in self._leer_archivo(self.trace_file):
            if evento.run_id not in vistos:
                vistos.append(evento.run_id)
        return vistos

    @staticmethod
    def _leer_archivo(path: Path) -> list[Event]:
        if not Path(path).exists():
            return []
        eventos: list[Event] = []
        with open(path, encoding="utf-8") as f:
            for linea in f:
                linea = linea.strip()
                if not linea:
                    continue
                try:
                    eventos.append(Event.from_any(json.loads(linea)))
                except (json.JSONDecodeError, ValueError):
                    continue  # línea corrupta: la traza sigue siendo útil
        return eventos

    def clear(self) -> None:
        """Borra la traza global y las trazas por run (demo_reset.sh)."""
        with self._lock:
            if self.trace_file.exists():
                self.trace_file.unlink()
            if self.runs_dir.exists():
                for f in self.runs_dir.glob("*.jsonl"):
                    f.unlink()


# --------------------------------------------------------------------------
# Servidor: SSE + ingesta, puerto 8020
# --------------------------------------------------------------------------
def _crear_app() -> "FastAPI":
    """Construye la app FastAPI. Se llama de forma perezosa (ver
    `__getattr__` al final) para no montar el servidor solo por importar
    `TraceStore`."""
    store = TraceStore()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        import anyio

        bus = get_event_bus()
        backend = os.environ.get("EVENT_BUS", "memory").lower()
        async with anyio.create_task_group() as tg:
            # Con Redis, este proceso es el que persiste la traza. Con el
            # bus in-process ya la escribió el sink local del emisor, así
            # que no se duplica.
            if not _trace_local_activo(backend):
                tg.start_soon(_consumir_hacia_disco, bus, store)
            yield
            tg.cancel_scope.cancel()
        await bus.aclose()

    app = FastAPI(
        title="EKL trace_store",
        description="Sala de control: traza por run_id y stream SSE (plan_demo.md §4.8)",
        version="1.0",
        lifespan=lifespan,
    )

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "event_bus": os.environ.get("EVENT_BUS", "memory"),
            "trace_file": str(store.trace_file),
            "runs": len(store.runs()),
        }

    @app.post("/events", status_code=202)
    async def ingest(request: Request) -> dict[str, Any]:
        """Ingesta HTTP. Rechaza con 422 cualquier evento sin `run_id`."""
        cuerpo = await request.json()
        try:
            evento = Event.from_any(cuerpo)
        except MissingRunIdError as exc:
            raise HTTPException(status_code=422, detail=f"evento rechazado: {exc}") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"evento inválido: {exc}") from exc
        sellado = await get_event_bus().publish(evento)
        return {"aceptado": True, "run_id": sellado.run_id, "seq": sellado.seq}

    @app.get("/runs")
    async def listar_runs() -> dict[str, Any]:
        """Runs disponibles, con lo justo para poblar un selector.

        `runs` (la lista de ids) se mantiene tal cual por compatibilidad;
        `detalle` es lo que usa la Sala de control.
        """
        detalle = []
        for run_id in store.runs():
            eventos = store.read(run_id)
            if not eventos:
                continue
            query = next(
                (e.payload.get("query") for e in eventos
                 if e.kind in ("plan", "answer") and e.payload.get("query")),
                None,
            )
            detalle.append(
                {
                    "run_id": run_id,
                    "eventos": len(eventos),
                    "inicio": eventos[0].ts.isoformat(),
                    "fin": eventos[-1].ts.isoformat(),
                    "duracion_ms": round(
                        (eventos[-1].ts - eventos[0].ts).total_seconds() * 1000, 1
                    ),
                    "rol": next((e.meta.user_role for e in eventos if e.meta.user_role), None),
                    "modelo": next((e.meta.modelo for e in eventos if e.meta.modelo), None),
                    "query": query,
                    "referencia": False,
                }
            )
        for path in sorted(TRACES_REFERENCIA_DIR.glob("*.jsonl")):
            eventos = TraceStore._leer_archivo(path)
            if not eventos:
                continue
            detalle.append(
                {
                    "run_id": f"referencia:{path.stem}",
                    "eventos": len(eventos),
                    "inicio": eventos[0].ts.isoformat(),
                    "fin": eventos[-1].ts.isoformat(),
                    "duracion_ms": round(
                        (eventos[-1].ts - eventos[0].ts).total_seconds() * 1000, 1
                    ),
                    "rol": next((e.meta.user_role for e in eventos if e.meta.user_role), None),
                    "modelo": next((e.meta.modelo for e in eventos if e.meta.modelo), None),
                    "query": next(
                        (e.payload.get("query") for e in eventos if e.payload.get("query")), None
                    ),
                    "referencia": True,
                }
            )
        return {"runs": store.runs(), "detalle": detalle}

    def _eventos_de(run_id: str) -> list[Event]:
        """Eventos de un run, venga de la traza viva o de una de
        referencia (`referencia:<nombre>`). Es lo que permite reproducir
        una corrida guardada sin LLM ni agentes."""
        if run_id.startswith("referencia:"):
            nombre = _nombre_archivo_run(run_id.split(":", 1)[1])
            path = TRACES_REFERENCIA_DIR / f"{nombre}.jsonl"
            if not path.exists():
                return []
            return ordenar_cronologicamente(TraceStore._leer_archivo(path))
        return store.read(run_id)

    @app.get("/runs/{run_id}")
    async def leer_run(run_id: str) -> dict[str, Any]:
        eventos = _eventos_de(run_id)
        if not eventos:
            raise HTTPException(status_code=404, detail=f"sin eventos para run_id={run_id}")
        return {"run_id": run_id, "eventos": [e.model_dump(mode="json") for e in eventos]}

    @app.get("/runs/{run_id}/export")
    async def exportar_run(run_id: str):
        """Descarga los eventos de una corrida en formato `trace.jsonl`
        (§4.8, controles: "exportar trace.jsonl")."""
        eventos = _eventos_de(run_id)
        if not eventos:
            raise HTTPException(status_code=404, detail=f"sin eventos para run_id={run_id}")
        cuerpo = "\n".join(e.to_jsonl() for e in eventos) + "\n"
        nombre = f"{_nombre_archivo_run(run_id)}.jsonl"
        return Response(
            content=cuerpo,
            media_type="application/x-ndjson",
            headers={"Content-Disposition": f'attachment; filename="{nombre}"'},
        )

    @app.get("/export")
    async def exportar_todo():
        """La traza completa tal cual está en disco."""
        if not store.trace_file.exists():
            raise HTTPException(status_code=404, detail="todavía no hay trace.jsonl")
        return FileResponse(
            store.trace_file, media_type="application/x-ndjson", filename="trace.jsonl"
        )

    @app.get("/control", response_class=HTMLResponse)
    async def control_room() -> str:
        """Sala de control (§4.8) como página independiente, para
        proyectarla en una segunda pantalla."""
        if not CONTROL_ROOM_HTML.exists():
            raise HTTPException(status_code=404, detail=f"falta {CONTROL_ROOM_HTML}")
        return CONTROL_ROOM_HTML.read_text(encoding="utf-8")

    async def _stream(request: Request, run_id: str | None, desde_disco: bool):
        bus = get_event_bus()

        async def generador() -> AsyncIterator[dict[str, Any]]:
            # 1) lo que ya pasó (para quien abre la Sala de control tarde)
            if desde_disco and run_id:
                for evento in store.read(run_id):
                    yield {"event": evento.kind, "id": str(evento.seq), "data": evento.to_jsonl()}
            # 2) lo que venga en vivo
            async with bus.subscribe(run_id) as eventos:
                async for evento in eventos:
                    if await request.is_disconnected():
                        break
                    yield {"event": evento.kind, "id": str(evento.seq), "data": evento.to_jsonl()}

        return EventSourceResponse(generador(), ping=15)

    @app.get("/stream")
    async def stream_todo(request: Request, run_id: str | None = None, replay: bool = False):
        return await _stream(request, run_id, desde_disco=replay)

    @app.get("/stream/{run_id}")
    async def stream_run(request: Request, run_id: str, replay: bool = True):
        return await _stream(request, run_id, desde_disco=replay)

    return app


async def _consumir_hacia_disco(bus, store: TraceStore) -> None:
    """Consume el bus y escribe cada evento a disco (modo Redis).

    **Nunca propaga una excepción.** Vive dentro del task group del
    `lifespan`, así que si se cayera se llevaría por delante todo el
    servidor: la Sala de control dejaría de servir SSE porque el
    consumidor de disco tuvo un problema. Un fallo del transporte se
    registra y se reintenta.
    """
    import anyio

    while True:
        try:
            async with bus.subscribe(None) as eventos:
                async for evento in eventos:
                    try:
                        store.append(evento)
                    except MissingRunIdError:
                        continue  # ya se rechazó al publicar; defensa en profundidad
                    except OSError:
                        logger.exception("No se pudo escribir la traza en %s", store.trace_file)
        except anyio.get_cancelled_exc_class():
            raise  # apagado normal del servidor
        except Exception:
            logger.exception("El consumidor de trazas falló; reintentando en %ss", REINTENTO_CONSUMIDOR_S)
            await anyio.sleep(REINTENTO_CONSUMIDOR_S)


def __getattr__(name: str):
    """`app` perezosa: `uvicorn observability.trace_store:app` la crea al
    importarla, pero `from .trace_store import TraceStore` no arrastra
    FastAPI."""
    if name == "app":
        app = _crear_app()
        globals()["app"] = app
        return app
    raise AttributeError(name)


def main() -> None:
    import uvicorn

    uvicorn.run(
        "observability.trace_store:app",
        host=os.environ.get("TRACE_STORE_HOST", "0.0.0.0"),
        port=TRACE_PORT,
        log_level=os.environ.get("LOG_LEVEL", "info"),
    )


if __name__ == "__main__":
    main()
