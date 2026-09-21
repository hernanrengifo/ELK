"""
event_bus.py — bus de eventos de la Sala de control (plan_demo.md §4.8).

Dos backends intercambiables por variable de entorno, exactamente como
pide el plan ("Redis Streams en Docker; fallback in-process para
desarrollo"):

    EVENT_BUS=redis    -> RedisStreamsEventBus  (redis.asyncio, XADD/XREAD)
    EVENT_BUS=memory   -> InProcessEventBus     (cola asyncio en memoria)

Ambos exponen la misma interfaz:

    await bus.publish(event)        -> Event (con `seq` ya asignado)
    async with bus.subscribe(run_id) as stream:   # AsyncIterator[Event]
    await bus.replay(run_id)        -> list[Event]
    bus.add_sink(fn)                # callback síncrono por evento

**`seq` lo asigna el bus, no el emisor.** Es un contador monótono por
`run_id`: en Redis con `INCR ekl:seq:{run_id}` (global entre procesos,
que es lo que hace falta cuando el Agente A, el Agente B y los dos
servidores MCP corren separados) y en memoria con un diccionario
protegido por lock. Por eso `new_event()` deja `seq=0`.

**Eventos sin `run_id` se rechazan** (`MissingRunIdError`) antes de
llegar al transporte: ver `events.py` y §8 de la tabla de riesgos del
plan.

**Sink de trazas en modo memory.** Con `EVENT_BUS=redis` hay un proceso
`trace_store` aparte que consume el stream y escribe `trace.jsonl`. Con
`EVENT_BUS=memory` no hay tal proceso, así que el bus engancha el
escritor de `trace.jsonl` como sink local — es lo que hace que los tests
y el desarrollo sin Docker produzcan una traza completa. Se puede forzar
en cualquier dirección con `EKL_TRACE_LOCAL=1|0`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from abc import ABC, abstractmethod
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable

from .events import Event, MissingRunIdError

logger = logging.getLogger(__name__)

#: Cuántos eventos por run_id guarda el bus en memoria para que un
#: suscriptor que llega tarde (la Sala de control abierta a mitad de la
#: corrida) pueda recuperar lo que ya pasó.
HISTORY_MAX = 2000

REDIS_STREAM_KEY = os.environ.get("EKL_REDIS_STREAM", "ekl:events")
REDIS_SEQ_PREFIX = os.environ.get("EKL_REDIS_SEQ_PREFIX", "ekl:seq:")

#: Cuánto bloquea cada `XREAD` esperando eventos nuevos. Al expirar,
#: redis-py levanta `TimeoutError`: el bucle lo trata como "no pasó nada"
#: y vuelve a leer (ver `RedisStreamsEventBus.subscribe`).
BLOCK_MS = int(os.environ.get("EKL_REDIS_BLOCK_MS", "5000"))
#: Espera antes de reintentar tras perder la conexión con Redis.
RECONEXION_S = float(os.environ.get("EKL_REDIS_RECONEXION_S", "1"))


Sink = Callable[[Event], None]


class EventBus(ABC):
    """Interfaz común de los dos backends. Ver docstring del módulo."""

    def __init__(self) -> None:
        self._sinks: list[Sink] = []

    # -- sinks -----------------------------------------------------------
    def add_sink(self, sink: Sink) -> None:
        """Registra un callback síncrono que recibe cada evento publicado
        por ESTE proceso (se usa para escribir `trace.jsonl` sin depender
        de un consumidor aparte)."""
        self._sinks.append(sink)

    def _fanout_sinks(self, event: Event) -> None:
        for sink in self._sinks:
            try:
                sink(event)
            except Exception:  # pragma: no cover - un sink roto no debe tumbar la corrida
                logger.exception("Sink de eventos falló para run_id=%s seq=%s", event.run_id, event.seq)

    # -- API -------------------------------------------------------------
    @abstractmethod
    async def publish(self, event: Event) -> Event:
        """Asigna `seq`, entrega a los sinks locales y al transporte.
        Devuelve el evento ya sellado."""

    @abstractmethod
    def subscribe(self, run_id: str | None = None) -> "AsyncIterator[Event]":
        """Context manager asíncrono que produce un `AsyncIterator[Event]`.
        `run_id=None` suscribe a todas las ejecuciones."""

    @abstractmethod
    async def replay(self, run_id: str) -> list[Event]:
        """Eventos ya publicados de esa ejecución, en orden de `seq`."""

    async def aclose(self) -> None:  # pragma: no cover - no-op por defecto
        pass

    # -- utilidades compartidas -------------------------------------------
    @staticmethod
    def _validar(event: Event) -> Event:
        event = Event.from_any(event)
        if not str(event.run_id or "").strip():
            raise MissingRunIdError("run_id es obligatorio y no puede ser vacío")
        return event


# --------------------------------------------------------------------------
# Backend in-process (cola en memoria)
# --------------------------------------------------------------------------
class InProcessEventBus(EventBus):
    """Bus en memoria: sin infraestructura, para tests y desarrollo.

    Los suscriptores son colas `asyncio.Queue` sin límite (el volumen de
    una corrida de la demo son decenas de eventos, no miles por segundo).

    !!! warning "`seq` es local al proceso"
        Este bus **no comparte nada entre procesos**: cada proceso lleva
        su propio contador, así que si el Agente A y el Agente B corren
        separados, los dos numeran desde 1 dentro del mismo `run_id` y
        el `seq` deja de servir para ordenar la corrida. Lo único que
        comparten en ese modo es el archivo `trace.jsonl`, cuyo orden de
        escritura —y el campo `ts`— sí son correctos.

        Para una corrida multiproceso con el `seq` ordenado de verdad
        hay que usar `EVENT_BUS=redis`, donde el contador es un
        `INCR ekl:seq:<run_id>` compartido. Es la configuración de la
        demo con la Sala de control; `memory` es para tests y para
        desarrollo en un solo proceso.
    """

    def __init__(self) -> None:
        super().__init__()
        self._seq: dict[str, int] = defaultdict(int)
        self._seq_lock = threading.Lock()
        self._subscribers: list[tuple[str | None, asyncio.Queue[Event]]] = []
        self._history: dict[str, deque[Event]] = defaultdict(lambda: deque(maxlen=HISTORY_MAX))

    def _next_seq(self, run_id: str) -> int:
        with self._seq_lock:
            self._seq[run_id] += 1
            return self._seq[run_id]

    async def publish(self, event: Event) -> Event:
        event = self._validar(event)
        sellado = event.model_copy(update={"seq": self._next_seq(event.run_id)})
        self._history[sellado.run_id].append(sellado)
        self._fanout_sinks(sellado)
        for run_filter, queue in list(self._subscribers):
            if run_filter is None or run_filter == sellado.run_id:
                queue.put_nowait(sellado)
        return sellado

    @asynccontextmanager
    async def subscribe(self, run_id: str | None = None):
        queue: asyncio.Queue[Event] = asyncio.Queue()
        entry = (run_id, queue)
        self._subscribers.append(entry)

        async def _iter() -> AsyncIterator[Event]:
            while True:
                yield await queue.get()

        try:
            yield _iter()
        finally:
            try:
                self._subscribers.remove(entry)
            except ValueError:  # pragma: no cover
                pass

    async def replay(self, run_id: str) -> list[Event]:
        return sorted(self._history.get(run_id, ()), key=lambda e: e.seq)

    def runs(self) -> list[str]:
        return list(self._history.keys())


# --------------------------------------------------------------------------
# Backend Redis Streams
# --------------------------------------------------------------------------
class RedisStreamsEventBus(EventBus):
    """Bus sobre Redis Streams: lo que corre en Docker (§4.8).

    - `XADD ekl:events * event <json>` para publicar.
    - `XREAD BLOCK` desde el último id conocido para suscribirse.
    - `INCR ekl:seq:<run_id>` para el `seq`, de modo que el orden sea
      consistente aunque publiquen cuatro procesos distintos.
    - `XRANGE` completo + filtro por `run_id` para el replay.

    Se usa un único stream (`ekl:events`) en lugar de uno por `run_id`
    para que la Sala de control pueda mirar "todo lo que está pasando"
    con una sola suscripción; el filtro por `run_id` es del lado del
    consumidor.
    """

    def __init__(self, url: str | None = None, stream_key: str = REDIS_STREAM_KEY) -> None:
        super().__init__()
        try:
            from redis.asyncio import Redis
        except ImportError as exc:  # pragma: no cover - depende del entorno
            raise RuntimeError(
                "EVENT_BUS=redis requiere el paquete `redis` (pip install redis). "
                "Usa EVENT_BUS=memory si no quieres levantar Redis."
            ) from exc
        self._url = url or os.environ.get("REDIS_URL", "redis://localhost:6379/0")
        self._stream_key = stream_key
        self._Redis = Redis
        self._redis = None
        self._loop = None

        # Se guardan las excepciones de redis-py para el bucle de XREAD: un
        # `XREAD BLOCK` que expira sin datos levanta `TimeoutError`, que es
        # el caso normal de "no pasó nada en esta ventana", no un fallo.
        from redis import exceptions as _redis_exc

        self._timeout_exc = _redis_exc.TimeoutError
        self._conn_exc = _redis_exc.ConnectionError
        self._resp_exc = _redis_exc.ResponseError

    def _cliente(self):
        """Cliente de Redis ligado al bucle de eventos **actual**.

        `redis.asyncio` ata sus conexiones al bucle en el que se
        crearon: reutilizar el cliente desde otro bucle falla con
        `RuntimeError: Event loop is closed`. Eso pasa en cuanto un
        proceso llama a `asyncio.run()` más de una vez —el smoke test
        con varias preguntas, o una UI que atiende peticiones sueltas—,
        y como el bus es un singleton del proceso, el fallo aparece en
        la segunda corrida y no en la primera. Aquí se detecta el cambio
        de bucle y se reconstruye el cliente.
        """
        bucle = asyncio.get_running_loop()
        if self._redis is None or self._loop is not bucle:
            self._redis = self._Redis.from_url(self._url, decode_responses=True)
            self._loop = bucle
        return self._redis

    async def publish(self, event: Event) -> Event:
        event = self._validar(event)
        redis = self._cliente()
        seq = int(await redis.incr(f"{REDIS_SEQ_PREFIX}{event.run_id}"))
        sellado = event.model_copy(update={"seq": seq})
        await redis.xadd(
            self._stream_key,
            {"run_id": sellado.run_id, "event": sellado.to_jsonl()},
        )
        self._fanout_sinks(sellado)
        return sellado

    async def _ultimo_id(self) -> str:
        """Id de la última entrada del stream, o `0-0` si aún no existe.

        Hace falta para suscribirse **sin ventanas ciegas**: si se usa
        `$` en cada `XREAD`, cada llamada solo ve lo que llegue mientras
        está bloqueada, y todo lo que se publique entre una lectura y la
        siguiente se pierde en silencio. Resolviendo `$` a un id
        concreto una sola vez y avanzándolo con cada entrada, no hay
        hueco entre lecturas.
        """
        try:
            info = await self._cliente().xinfo_stream(self._stream_key)
        except self._resp_exc:
            return "0-0"  # el stream todavía no existe: nada que perderse
        return info.get("last-generated-id") or "0-0"

    @asynccontextmanager
    async def subscribe(self, run_id: str | None = None):
        last_id: str | None = None  # se resuelve en la primera lectura

        async def _iter() -> AsyncIterator[Event]:
            nonlocal last_id
            if last_id is None:
                last_id = await self._ultimo_id()
            while True:
                try:
                    respuesta = await self._cliente().xread(
                        {self._stream_key: last_id}, block=BLOCK_MS, count=100
                    )
                except self._timeout_exc:
                    # Ventana de bloqueo agotada sin eventos: se vuelve a
                    # leer. Es el estado normal mientras el agente piensa.
                    continue
                except self._conn_exc:
                    # Redis se cayó o se reinició: se reintenta en vez de
                    # tumbar al suscriptor (la Sala de control o el
                    # consumidor que escribe trace.jsonl).
                    logger.warning("Conexión con Redis perdida; reintentando en %ss", RECONEXION_S)
                    await asyncio.sleep(RECONEXION_S)
                    continue
                if not respuesta:
                    continue
                for _stream, entradas in respuesta:
                    for entry_id, campos in entradas:
                        last_id = entry_id
                        evento = _decodificar(campos)
                        if evento is None:
                            continue
                        if run_id is None or evento.run_id == run_id:
                            yield evento

        try:
            yield _iter()
        finally:
            pass

    async def replay(self, run_id: str) -> list[Event]:
        entradas = await self._cliente().xrange(self._stream_key)
        eventos = []
        for _entry_id, campos in entradas:
            evento = _decodificar(campos)
            if evento is not None and evento.run_id == run_id:
                eventos.append(evento)
        return sorted(eventos, key=lambda e: e.seq)

    async def runs(self) -> list[str]:
        entradas = await self._cliente().xrange(self._stream_key)
        vistos: list[str] = []
        for _entry_id, campos in entradas:
            rid = campos.get("run_id")
            if rid and rid not in vistos:
                vistos.append(rid)
        return vistos

    async def aclose(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None
            self._loop = None


def _decodificar(campos: dict[str, Any]) -> Event | None:
    """Una entrada del stream -> Event. Descarta (con log) lo que no sea
    un evento válido en vez de tumbar al consumidor."""
    raw = campos.get("event")
    if not raw:
        return None
    try:
        return Event.from_any(json.loads(raw))
    except (json.JSONDecodeError, MissingRunIdError, ValueError):
        logger.warning("Entrada del stream descartada (no es un Event válido): %r", raw[:200])
        return None


# --------------------------------------------------------------------------
# Fábrica
# --------------------------------------------------------------------------
_bus_singleton: EventBus | None = None
_bus_lock = threading.Lock()


def build_event_bus(name: str | None = None) -> EventBus:
    """Construye un bus nuevo (sin cachear). `name` por defecto viene de
    `EVENT_BUS` (memory|redis)."""
    name = (name or os.environ.get("EVENT_BUS", "memory")).lower()
    if name == "memory":
        bus: EventBus = InProcessEventBus()
    elif name == "redis":
        bus = RedisStreamsEventBus()
    else:
        raise ValueError(f"EVENT_BUS desconocido: '{name}' (usa 'memory' o 'redis')")

    if _trace_local_activo(name):
        from .trace_store import TraceStore  # import diferido: trace_store importa este módulo

        bus.add_sink(TraceStore().append)
    return bus


def _trace_local_activo(backend: str) -> bool:
    """¿Debe este proceso escribir `trace.jsonl` él mismo?

    Por defecto sí con `memory` (no hay proceso trace_store que consuma)
    y no con `redis` (lo hace el servicio trace_store). `EKL_TRACE_LOCAL`
    fuerza cualquiera de los dos."""
    forzado = os.environ.get("EKL_TRACE_LOCAL")
    if forzado is not None:
        return forzado.strip().lower() in ("1", "true", "yes", "si", "sí")
    return backend == "memory"


def get_event_bus() -> EventBus:
    """Bus compartido del proceso (singleton). Es lo que usan el
    middleware MCP y los agentes."""
    global _bus_singleton
    if _bus_singleton is None:
        with _bus_lock:
            if _bus_singleton is None:
                _bus_singleton = build_event_bus()
    return _bus_singleton


def reset_event_bus() -> None:
    """Descarta el singleton. Solo para tests que cambian de backend."""
    global _bus_singleton
    _bus_singleton = None
