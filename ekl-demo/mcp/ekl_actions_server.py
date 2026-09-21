#!/usr/bin/env python3
"""
ekl_actions_server.py — servidor MCP `ekl-actions` (plan_demo.md §4.4).

Ejecuta las **acciones gobernadas** que los nodos del grafo exponen. El
agente descubre la acción navegando el grafo (`list_node_actions`, que
le da `nombre + contrato + política`) y la invoca aquí; este servidor es
el único que sabe qué métrica hay detrás, qué SQL la calcula y sobre qué
fuente corre.

Herramientas:

  resolve_metric(nombre)
      Definición vigente de una métrica de negocio: versión, owner,
      definición y SQL, leídos de `data/semantic_layer.yaml`. Es el paso
      del minuto 1-3 del guion: el agente resuelve qué significa
      "exposición crediticia" ANTES de buscar nada, y la definición no
      está en el prompt.

  retrieve_customer_position(customer_ref, metrica)
      Ejecuta el SQL de esa métrica sobre **DuckDB** (`data/posiciones.csv`)
      y devuelve `valor, moneda, fecha_corte, linaje` — exactamente el
      contrato declarado en `ontology.yaml` §3.2.
      Antes de ejecutar **verifica la política de la acción contra el rol
      del usuario** y, si no lo tiene, deniega con un mensaje explicable
      (`ToolError`) tras emitir `policy_decision: denegado`.

`run_id` y `user_ctx` viajan en los metadatos de la llamada MCP; el
middleware común (`mcp/ekl_middleware.py`) emite `tool_call`,
`tool_result` y `policy_decision` al bus y escribe `audit.jsonl`.

Arranque:

    python mcp/ekl_actions_server.py                       # stdio (Inspector)
    python mcp/ekl_actions_server.py --transport http --port 8010
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

MCP_DIR = Path(__file__).resolve().parent
ROOT_DIR = MCP_DIR.parent
for _ruta in (str(ROOT_DIR), str(MCP_DIR), str(ROOT_DIR / "data")):
    if _ruta not in sys.path:
        sys.path.insert(0, _ruta)

from mcp.server.mcpserver import MCPServer  # noqa: E402  (SDK oficial)
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402

from ekl_middleware import EklMiddleware, emitir_policy_decision, llamada_actual  # noqa: E402
from ekl_policies import (  # noqa: E402
    DATA_DIR,
    cargar_semantic_layer,
    evaluar_accion,
)

#: Fuente de datos del estrato 1 (§2: "DuckDB con posiciones.csv, simula
#: el lakehouse"). Se registra como vista `posiciones` para que el SQL de
#: la capa semántica se pueda escribir sin saber que detrás hay un CSV.
POSICIONES_CSV = DATA_DIR / "posiciones.csv"

#: Moneda de las posiciones. No está en `posiciones.csv`: viene de la
#: definición de la métrica en `semantic_layer.yaml` ("...en COP, a fecha
#: de corte"). Se declara aquí y se reporta en el linaje para que la
#: cifra nunca salga sin unidad.
MONEDA = os.environ.get("EKL_MONEDA", "COP")


# --------------------------------------------------------------------------
# DuckDB (conexión perezosa, compartida por el proceso)
# --------------------------------------------------------------------------
_conexion = None


def duckdb_conn():
    """Conexión DuckDB en memoria con la vista `posiciones` sobre el CSV."""
    global _conexion
    if _conexion is None:
        import duckdb

        _conexion = duckdb.connect(database=":memory:")
        # DuckDB no admite parámetros preparados en un CREATE VIEW, así que
        # la ruta se interpola escapando las comillas simples. No viene del
        # usuario: es una constante del repo (o EKL_DATA_DIR).
        ruta = str(POSICIONES_CSV).replace("'", "''")
        _conexion.execute(
            f"CREATE OR REPLACE VIEW posiciones AS SELECT * FROM read_csv_auto('{ruta}', header=true)"
        )
    return _conexion


def reset_duckdb() -> None:
    """Solo para tests / recarga tras editar el CSV."""
    global _conexion
    if _conexion is not None:
        _conexion.close()
    _conexion = None


# --------------------------------------------------------------------------
# Modelos de salida
# --------------------------------------------------------------------------
class DefinicionMetrica(BaseModel):
    nombre: str
    version: int
    owner: str | None = None
    definicion: str
    sql: str | None = None
    ejecutable: bool = Field(description="False si la métrica está declarada pero aún no tiene SQL")
    linaje: str


class PosicionCliente(BaseModel):
    """Contrato de salida declarado en ontology.yaml §3.2:
    `valor, moneda, fecha_corte, linaje`."""

    customer_ref: str
    metrica: str
    valor: float
    moneda: str
    fecha_corte: str
    linaje: str
    definicion: str
    version: int


# --------------------------------------------------------------------------
# Lógica de métricas
# --------------------------------------------------------------------------
def _metrica(nombre: str) -> dict[str, Any]:
    metricas = cargar_semantic_layer().get("metricas") or {}
    definicion = metricas.get(nombre)
    if definicion is None:
        disponibles = ", ".join(sorted(metricas)) or "(ninguna)"
        raise ToolError(
            f"la métrica '{nombre}' no existe en la capa semántica. Disponibles: {disponibles}"
        )
    return definicion


def _linaje_metrica(nombre: str, definicion: dict[str, Any]) -> str:
    return (
        f"fuente: data/semantic_layer.yaml | metrica: {nombre} "
        f"v{definicion.get('version')} (owner: {definicion.get('owner', 'sin owner')})"
    )


# --------------------------------------------------------------------------
# Fábrica del servidor
# --------------------------------------------------------------------------
def crear_servidor() -> MCPServer:
    servidor = MCPServer(
        name="ekl-actions",
        title="EKL — acciones gobernadas",
        version="1.0",
        instructions=(
            "Acciones que los nodos del grafo exponen. Resuelve primero la definición de la "
            "métrica con resolve_metric() y ejecútala después con retrieve_customer_position(). "
            "Toda cifra sale con su linaje; las políticas de cada acción se verifican en el "
            "servidor contra el rol del usuario."
        ),
        middleware=[EklMiddleware(servidor="ekl-actions", source="mcp_actions")],
    )

    @servidor.tool(
        description=(
            "Definición vigente de una métrica de negocio en la capa semántica: versión, owner, "
            "definición en palabras y SQL. Úsala antes de calcular nada: la definición no está "
            "en tu prompt, vive en la capa semántica y puede cambiar de versión."
        )
    )
    async def resolve_metric(nombre: str) -> DefinicionMetrica:
        definicion = _metrica(nombre)
        llamada = llamada_actual()
        llamada.resumen = {"metrica": nombre, "version": definicion.get("version")}
        llamada.nodos_devueltos = 1
        await emitir_policy_decision(
            regla="semantic_layer.metricas",
            resultado="permitido",
            justificacion=(
                f"la definición de '{nombre}' (v{definicion.get('version')}) es de lectura "
                "abierta: gobierna el significado, no el dato"
            ),
        )
        return DefinicionMetrica(
            nombre=nombre,
            version=int(definicion.get("version", 1)),
            owner=definicion.get("owner"),
            definicion=str(definicion.get("definicion", "")).strip(),
            sql=definicion.get("sql"),
            ejecutable=bool(definicion.get("sql")),
            linaje=_linaje_metrica(nombre, definicion),
        )

    @servidor.tool(
        description=(
            "Calcula una métrica gobernada para un cliente y devuelve valor, moneda, fecha de "
            "corte y linaje. Requiere el rol declarado en la política de la acción; si no lo "
            "tienes, la llamada se rechaza con la razón."
        )
    )
    async def retrieve_customer_position(customer_ref: str, metrica: str) -> PosicionCliente:
        llamada = llamada_actual()

        # 1) Política de la acción contra el rol (§4.4)
        decision = evaluar_accion(llamada.rol, "retrieve_customer_position")
        if not decision.permitido:
            await emitir_policy_decision(
                regla=decision.regla,
                resultado="denegado",
                justificacion=decision.justificacion,
                accion="retrieve_customer_position",
                customer_ref=customer_ref,
            )
            raise ToolError(
                f"Acción 'retrieve_customer_position' denegada: {decision.justificacion}. "
                "La política la define data/policies.yaml, no este servidor ni el modelo."
            )
        await emitir_policy_decision(
            regla=decision.regla,
            resultado="permitido",
            justificacion=decision.justificacion,
            accion="retrieve_customer_position",
            customer_ref=customer_ref,
        )

        # 2) Definición vigente de la métrica (capa semántica)
        definicion = _metrica(metrica)
        sql = definicion.get("sql")
        if not sql:
            raise ToolError(
                f"la métrica '{metrica}' está declarada (v{definicion.get('version')}) pero no "
                "tiene SQL en la capa semántica: no se puede calcular todavía"
            )

        # 3) Ejecución sobre DuckDB
        conn = duckdb_conn()
        try:
            fila = conn.execute(sql, [customer_ref]).fetchone()
        except Exception as exc:  # SQL de la capa semántica mal formado
            raise ToolError(
                f"el SQL de la métrica '{metrica}' v{definicion.get('version')} falló: {exc}"
            ) from exc

        valor = fila[0] if fila else None
        if valor is None:
            raise ToolError(
                f"no hay posiciones para '{customer_ref}' en data/posiciones.csv: "
                f"la métrica '{metrica}' no se puede calcular para ese cliente"
            )

        fecha_corte = conn.execute(
            "SELECT CAST(MAX(fecha) AS VARCHAR) FROM posiciones WHERE cliente_id = ?",
            [customer_ref],
        ).fetchone()[0]

        linaje = (
            f"fuente: data/posiciones.csv (DuckDB, vista `posiciones`) | "
            f"metrica: {metrica} v{definicion.get('version')} "
            f"(owner: {definicion.get('owner', 'sin owner')}, data/semantic_layer.yaml) | "
            f"fecha_corte: {fecha_corte} | moneda: {MONEDA}"
        )

        llamada.nodos_devueltos = 1
        llamada.resumen = {
            "customer_ref": customer_ref,
            "metrica": metrica,
            "version": definicion.get("version"),
            "valor": float(valor),
        }
        return PosicionCliente(
            customer_ref=customer_ref,
            metrica=metrica,
            valor=float(valor),
            moneda=MONEDA,
            fecha_corte=str(fecha_corte),
            linaje=linaje,
            definicion=str(definicion.get("definicion", "")).strip(),
            version=int(definicion.get("version", 1)),
        )

    return servidor


def __getattr__(name: str):
    """`servidor` a nivel de módulo, creado la primera vez que se pide
    (lo necesita `mcp dev mcp/ekl_actions_server.py:servidor`)."""
    if name == "servidor":
        servidor = crear_servidor()
        globals()["servidor"] = servidor
        return servidor
    raise AttributeError(name)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Servidor MCP ekl-actions (plan_demo.md §4.4)")
    parser.add_argument(
        "--transport",
        choices=("stdio", "http", "sse"),
        default=os.environ.get("EKL_MCP_TRANSPORT", "stdio"),
        help="stdio (MCP Inspector, por defecto) | http (streamable) | sse",
    )
    parser.add_argument("--port", type=int, default=int(os.environ.get("EKL_ACTIONS_PORT", "8010")))
    parser.add_argument("--host", default=os.environ.get("EKL_MCP_HOST", "127.0.0.1"))
    args = parser.parse_args(argv)

    servidor = crear_servidor()
    if args.transport == "stdio":
        servidor.run("stdio")
    else:
        servidor.run(
            "streamable-http" if args.transport == "http" else "sse",
            host=args.host,
            port=args.port,
        )


if __name__ == "__main__":
    main()
