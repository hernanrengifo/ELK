"""
ekl_middleware.py — middleware común de los dos servidores MCP
(plan_demo.md §4.3 "todo se escribe a audit.jsonl", §4.8 "MCP servers:
middleware que emite tool_call, tool_result y policy_decision").

Una sola pieza compartida por `ekl_graph_server.py` y
`ekl_actions_server.py`, para que la auditoría y la traza salgan iguales
desde los dos:

  * `EklMiddleware` — `ServerMiddleware` del SDK oficial `mcp`. Envuelve
    cada `tools/call`:
      1. lee `run_id` y `user_ctx` de los **metadatos de la llamada**
         (`_meta` del request MCP) y los deja en un `ContextVar` para que
         la herramienta los use sin plomería;
      2. emite `tool_call` al bus de eventos;
      3. ejecuta la herramienta;
      4. emite `tool_result` (o `error`) con duración, nodos devueltos y
         **nodos filtrados por RBAC**;
      5. escribe una línea en `audit.jsonl` con
         `timestamp, user, tool, args, nodos_devueltos, filtrados`
         (los seis campos que pide el §4.3) más el contexto de la corrida.

  * `emitir_policy_decision(...)` — lo llaman las herramientas cuando
    aplican una política (podar un nodo, denegar una acción). Emite el
    evento `policy_decision` y lo anota en la auditoría de esa llamada.

  * `llamada_actual()` — acceso de la herramienta al `run_id`, al
    `user_ctx` y al acumulador de nodos filtrados.

**`run_id` en las llamadas del Inspector.** El plan exige `run_id` en los
metadatos MCP. Si la llamada no lo trae, el middleware genera uno
(`inspector-xxxxxxxx`) y lo marca como `run_id_generado: true` en la
auditoría, en vez de rechazar la llamada: así el servidor sigue siendo
explorable a mano sin romper la regla de que ningún evento viaja sin
`run_id`. Desde MCP Inspector se pueden mandar los dos explícitamente:

    --tool-metadata run_id=demo-1 user_ctx=analista_junior

(ver "Cómo probar con MCP Inspector" en el README).
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MCP_DIR = Path(__file__).resolve().parent
ROOT_DIR = MCP_DIR.parent
# `mcp/` no es un paquete (no tiene __init__.py, a propósito: se llama
# igual que el SDK oficial `mcp`). Sus módulos se importan como módulos
# sueltos con el directorio en sys.path; un directorio sin __init__.py
# no puede tapar al paquete instalado, así que `import mcp` sigue
# resolviendo al SDK.
for _ruta in (str(ROOT_DIR), str(MCP_DIR)):
    if _ruta not in sys.path:
        sys.path.insert(0, _ruta)

from observability.event_bus import get_event_bus  # noqa: E402
from observability.events import EventKind, EventSource, new_event  # noqa: E402

#: `audit.jsonl` en la raíz del repo (lo limpia `scripts/demo_reset.sh`).
DEFAULT_AUDIT_FILE = ROOT_DIR / "audit.jsonl"


def audit_file() -> Path:
    return Path(os.environ.get("EKL_AUDIT_FILE") or DEFAULT_AUDIT_FILE)


@dataclass
class LlamadaMCP:
    """Contexto de una llamada `tools/call` en curso.

    Lo crea el middleware y lo leen/completan las herramientas: ahí
    anotan qué nodos devolvieron, cuáles podaron por política y qué
    decisiones de política tomaron.
    """

    run_id: str
    user_ctx: dict[str, Any]
    servidor: str          # "ekl-graph" | "ekl-actions"
    source: EventSource    # "mcp_graph" | "mcp_actions"
    herramienta: str
    argumentos: dict[str, Any]
    base: str | None = None          # negocio | infra (solo ekl-graph)
    run_id_generado: bool = False
    nodos_devueltos: int = 0
    nodos_filtrados: list[str] = field(default_factory=list)
    decisiones: list[dict[str, Any]] = field(default_factory=list)
    resumen: dict[str, Any] = field(default_factory=dict)

    @property
    def rol(self) -> str:
        return str(self.user_ctx.get("rol") or rol_por_defecto())

    @property
    def usuario(self) -> str:
        return str(self.user_ctx.get("usuario") or self.user_ctx.get("user") or "anonimo")


_llamada_actual: ContextVar[LlamadaMCP | None] = ContextVar("ekl_llamada_actual", default=None)


def llamada_actual() -> LlamadaMCP:
    """Contexto de la llamada MCP en curso. Las herramientas la usan para
    conocer el rol del usuario y el `run_id` de la ejecución."""
    llamada = _llamada_actual.get()
    if llamada is None:
        raise RuntimeError(
            "No hay una llamada MCP en curso: ¿se registró EklMiddleware en el servidor?"
        )
    return llamada


def rol_por_defecto() -> str:
    """Rol si el cliente MCP no manda `user_ctx` (Inspector, curl…).

    Sale de `DEMO_USER_ROLE` o, si no está, de `rol_por_defecto` en
    `policies.yaml` — nunca hardcodeado aquí."""
    del_entorno = os.environ.get("DEMO_USER_ROLE")
    if del_entorno:
        return del_entorno
    from ekl_policies import cargar_policies  # import local: evita ciclo al importar el módulo

    return str(cargar_policies().get("rol_por_defecto", "riesgo"))


# --------------------------------------------------------------------------
# Emisión de eventos
# --------------------------------------------------------------------------
async def emitir(
    kind: EventKind,
    payload: dict[str, Any],
    *,
    llamada: LlamadaMCP | None = None,
    step: str | None = None,
    meta: dict[str, Any] | None = None,
) -> None:
    """Publica un evento al bus colgado de la llamada MCP en curso."""
    llamada = llamada or llamada_actual()
    evento = new_event(
        run_id=llamada.run_id,
        source=llamada.source,
        kind=kind,
        step=step or llamada.herramienta,
        payload=payload,
        meta={"user_role": llamada.rol, **(meta or {})},
    )
    await get_event_bus().publish(evento)


async def emitir_policy_decision(
    regla: str,
    resultado: str,
    justificacion: str,
    *,
    llamada: LlamadaMCP | None = None,
    **extra: Any,
) -> None:
    """Emite `policy_decision` (§4.8: regla aplicada, resultado
    —permitido / denegado / podado— y justificación) y lo anota en la
    auditoría de esta llamada.

    Es el único camino por el que los dos servidores registran una
    decisión de política, para que la Sala de control las vea iguales
    vengan de donde vengan.
    """
    if resultado not in ("permitido", "denegado", "podado"):
        raise ValueError(f"resultado de política inválido: {resultado!r}")
    llamada = llamada or llamada_actual()
    decision = {
        "regla": regla,
        "resultado": resultado,
        "justificacion": justificacion,
        "rol": llamada.rol,
        **extra,
    }
    llamada.decisiones.append(decision)
    await emitir(
        "policy_decision",
        {"servidor": llamada.servidor, "herramienta": llamada.herramienta, **decision},
        llamada=llamada,
    )


# --------------------------------------------------------------------------
# Auditoría
# --------------------------------------------------------------------------
def _escribir_auditoria(registro: dict[str, Any]) -> None:
    path = audit_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(registro, ensure_ascii=False, default=str) + "\n")


# --------------------------------------------------------------------------
# Middleware
# --------------------------------------------------------------------------
class EklMiddleware:
    """`ServerMiddleware` del SDK `mcp`. Ver docstring del módulo.

    Se registra igual en los dos servidores:

        MCPServer("ekl-graph", middleware=[EklMiddleware("ekl-graph", "mcp_graph")])
    """

    def __init__(self, servidor: str, source: EventSource, base: str | None = None) -> None:
        self.servidor = servidor
        self.source = source
        self.base = base

    async def __call__(self, ctx, call_next):
        if ctx.method != "tools/call":
            return await call_next(ctx)

        params = ctx.params or {}
        meta = dict(ctx.meta or {})
        run_id = str(meta.get("run_id") or "").strip()
        run_id_generado = False
        if not run_id:
            run_id = f"inspector-{uuid.uuid4().hex[:8]}"
            run_id_generado = True

        # `user_ctx` normalmente es un objeto {"usuario": ..., "rol": ...},
        # que es lo que manda el agente. Se acepta además la forma corta
        # —`user_ctx` como cadena con el nombre del rol, o un `rol` suelto
        # en los metadatos— porque es lo único que puede mandar un cliente
        # de línea de comandos como MCP Inspector
        # (`--tool-metadata user_ctx=analista_junior`).
        user_ctx = meta.get("user_ctx") or meta.get("rol") or {}
        if not isinstance(user_ctx, dict):
            user_ctx = {"rol": str(user_ctx)}

        llamada = LlamadaMCP(
            run_id=run_id,
            user_ctx=user_ctx,
            servidor=self.servidor,
            source=self.source,
            herramienta=str(params.get("name") or "?"),
            argumentos=dict(params.get("arguments") or {}),
            base=self.base,
            run_id_generado=run_id_generado,
        )
        token = _llamada_actual.set(llamada)
        t0 = time.perf_counter()
        error: BaseException | None = None
        es_error = False
        resultado: Any = None
        try:
            await emitir(
                "tool_call",
                {
                    "servidor": llamada.servidor,
                    "base": llamada.base,
                    "herramienta": llamada.herramienta,
                    "argumentos": llamada.argumentos,
                    "user_ctx": {"usuario": llamada.usuario, "rol": llamada.rol},
                },
                llamada=llamada,
            )
            resultado = await call_next(ctx)
            es_error = bool(isinstance(resultado, dict) and resultado.get("isError"))
            return resultado
        except BaseException as exc:  # noqa: BLE001 - se re-lanza tras auditar
            error = exc
            raise
        finally:
            duracion_ms = round((time.perf_counter() - t0) * 1000, 2)
            try:
                await self._cerrar(llamada, duracion_ms, error, es_error, resultado)
            finally:
                _llamada_actual.reset(token)

    async def _cerrar(
        self,
        llamada: LlamadaMCP,
        duracion_ms: float,
        error: BaseException | None,
        es_error: bool,
        resultado: Any,
    ) -> None:
        estado = "error" if (error or es_error) else "ok"
        detalle_error = None
        if error is not None:
            detalle_error = f"{type(error).__name__}: {error}"
        elif es_error:
            detalle_error = _texto_de_resultado(resultado)

        payload: dict[str, Any] = {
            "servidor": llamada.servidor,
            "base": llamada.base,
            "herramienta": llamada.herramienta,
            "estado": estado,
            "resumen": llamada.resumen,
            "nodos_devueltos": llamada.nodos_devueltos,
            "nodos_filtrados": llamada.nodos_filtrados,
            "politicas_aplicadas": llamada.decisiones,
        }
        if detalle_error:
            payload["error"] = detalle_error

        await emitir(
            "error" if estado == "error" else "tool_result",
            payload,
            llamada=llamada,
            meta={"duracion_ms": duracion_ms},
        )

        # audit.jsonl — los seis campos del §4.3 y el contexto de la corrida
        _escribir_auditoria(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "user": llamada.usuario,
                "tool": llamada.herramienta,
                "args": llamada.argumentos,
                "nodos_devueltos": llamada.nodos_devueltos,
                "filtrados": llamada.nodos_filtrados,
                "run_id": llamada.run_id,
                "run_id_generado": llamada.run_id_generado,
                "rol": llamada.rol,
                "servidor": llamada.servidor,
                "base": llamada.base,
                "estado": estado,
                "duracion_ms": duracion_ms,
                "politicas_aplicadas": llamada.decisiones,
                **({"error": detalle_error} if detalle_error else {}),
            }
        )


def _texto_de_resultado(resultado: Any) -> str | None:
    """Mensaje de error de un CallToolResult ya serializado a dict."""
    if not isinstance(resultado, dict):
        return None
    for bloque in resultado.get("content") or []:
        if isinstance(bloque, dict) and bloque.get("type") == "text":
            return str(bloque.get("text"))
    return None
