"""
mcp_cliente.py — cliente MCP instrumentado para los agentes
(plan_demo.md §4.8, "wrapper sobre el cliente MCP que emite `tool_call`
y sus resultados").

Envuelve el cliente del SDK oficial `mcp` y hace tres cosas:

1. Propaga `run_id` y `user_ctx` en los **metadatos de cada llamada**
   (`_meta`), que es lo que hace que los eventos del servidor MCP y los
   del agente cuelguen de la misma ejecución y que el RBAC se aplique
   con la identidad del usuario original.
2. Emite `tool_call` / `tool_result` **desde el lado del agente**
   (`source: agent_a | agent_b`). El servidor emite los suyos
   (`source: mcp_graph | mcp_actions`) con su middleware: son los dos
   extremos de la misma flecha en el diagrama de secuencia de la Sala de
   control, no un duplicado.
3. Convierte el error de una herramienta en `ErrorHerramienta`, para que
   un nodo pueda reintentar con el mensaje del servidor en vez de
   romperse (lo usa `navegar_grafo` cuando el modelo escribe Cypher
   fuera del subconjunto soportado).

A qué servidor se conecta lo decide el entorno:

    EKL_MCP_GRAPH_URL / EKL_MCP_ACTIONS_URL   -> por HTTP a un servidor ya levantado
    (sin ellas)                               -> en proceso, instanciando el servidor

El modo en proceso es el de los tests y el de la demo en un portátil; el
modo HTTP es el que corresponde al diagrama del §2 con los servidores
MCP como servicios aparte.
"""

from __future__ import annotations

import json
import os
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
for _ruta in (str(ROOT_DIR), str(ROOT_DIR / "mcp"), str(ROOT_DIR / "data")):
    if _ruta not in sys.path:
        sys.path.insert(0, _ruta)

from mcp import Client  # noqa: E402  (SDK oficial; `mcp/` local no lo tapa, ver STATUS)

from .eventos import ContextoCorrida, emitir  # noqa: E402


class ErrorHerramienta(RuntimeError):
    """Una herramienta MCP devolvió `isError`. El mensaje es el del
    servidor (política denegada, Cypher no soportado, nodo inexistente…),
    que está pensado para ser explicable."""

    def __init__(self, servidor: str, herramienta: str, mensaje: str) -> None:
        super().__init__(mensaje)
        self.servidor = servidor
        self.herramienta = herramienta
        self.mensaje = mensaje


def _contenido(resultado) -> dict[str, Any]:
    if resultado.structured_content is not None:
        return resultado.structured_content
    for bloque in resultado.content or []:
        texto = getattr(bloque, "text", None)
        if texto:
            try:
                return json.loads(texto)
            except json.JSONDecodeError:
                return {"texto": texto}
    return {}


def _texto_error(resultado) -> str:
    for bloque in resultado.content or []:
        texto = getattr(bloque, "text", None)
        if texto:
            return texto
    return "error sin mensaje"


def _resumir(datos: Any, limite: int = 400) -> Any:
    """Resumen del resultado para el evento: la Sala de control muestra
    esto, no el volcado entero."""
    try:
        texto = json.dumps(datos, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(datos)[:limite]
    if len(texto) <= limite:
        return datos
    return {"_truncado": True, "_longitud": len(texto), "_inicio": texto[:limite]}


class ClienteMCP:
    """Conexión a los servidores MCP que necesita un agente.

    Se abre una vez por corrida (`async with`), no una por llamada:

        async with ClienteMCP(ctx, grafo_base="negocio") as mcp:
            await mcp.llamar("ekl-graph", "get_schema", {})
    """

    def __init__(
        self,
        ctx: ContextoCorrida,
        *,
        grafo_base: str = "negocio",
        con_acciones: bool = True,
    ) -> None:
        self.ctx = ctx
        self.grafo_base = grafo_base
        self.con_acciones = con_acciones
        self._stack = AsyncExitStack()
        self._clientes: dict[str, Client] = {}

    async def __aenter__(self) -> "ClienteMCP":
        await self._stack.__aenter__()

        url_grafo = os.environ.get("EKL_MCP_GRAPH_URL")
        if url_grafo:
            destino: Any = url_grafo
        else:
            import ekl_graph_server

            destino = ekl_graph_server.crear_servidor(self.grafo_base)
        self._clientes["ekl-graph"] = await self._stack.enter_async_context(Client(destino))

        if self.con_acciones:
            url_acciones = os.environ.get("EKL_MCP_ACTIONS_URL")
            if url_acciones:
                destino_a: Any = url_acciones
            else:
                import ekl_actions_server

                destino_a = ekl_actions_server.crear_servidor()
            self._clientes["ekl-actions"] = await self._stack.enter_async_context(Client(destino_a))
        return self

    async def __aexit__(self, *exc) -> None:
        await self._stack.__aexit__(*exc)

    async def llamar(
        self,
        servidor: str,
        herramienta: str,
        argumentos: dict[str, Any],
        *,
        step: str | None = None,
    ) -> dict[str, Any]:
        """Invoca una herramienta y devuelve su contenido estructurado.

        Levanta `ErrorHerramienta` si el servidor marcó error (política
        denegada, consulta no soportada…)."""
        cliente = self._clientes.get(servidor)
        if cliente is None:
            raise KeyError(f"este agente no está conectado al servidor MCP '{servidor}'")

        await emitir(
            self.ctx,
            "tool_call",
            {
                "servidor": servidor,
                "base": self.grafo_base if servidor == "ekl-graph" else None,
                "herramienta": herramienta,
                "argumentos": argumentos,
                "lado": "cliente",
            },
            step=step or herramienta,
        )

        resultado = await cliente.call_tool(herramienta, argumentos, meta=self.ctx.meta_mcp())

        if resultado.is_error:
            mensaje = _texto_error(resultado)
            await emitir(
                self.ctx,
                "tool_result",
                {
                    "servidor": servidor,
                    "herramienta": herramienta,
                    "estado": "error",
                    "error": mensaje,
                    "lado": "cliente",
                },
                step=step or herramienta,
            )
            raise ErrorHerramienta(servidor, herramienta, mensaje)

        datos = _contenido(resultado)
        await emitir(
            self.ctx,
            "tool_result",
            {
                "servidor": servidor,
                "herramienta": herramienta,
                "estado": "ok",
                "resumen": _resumir(datos),
                "nodos_filtrados": datos.get("nodos_filtrados_por_politica", []),
                "lado": "cliente",
            },
            step=step or herramienta,
        )
        return datos
