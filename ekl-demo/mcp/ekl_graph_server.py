#!/usr/bin/env python3
"""
ekl_graph_server.py — servidor MCP `ekl-graph` (plan_demo.md §4.3).

Expone el grafo de conocimiento como herramientas MCP, **parametrizado
por base**: una instancia sirve la base `negocio` (la que usa el Agente
A) y otra la base `infra` (la que usa el Agente B). El agente nunca sabe
si detrás hay Neo4j o un grafo en memoria, ni dónde viven los datos.

Herramientas:

  get_schema()              tipos de nodo, aristas y acciones disponibles
                            en esta base (+ qué nodos son frontera)
  find_entry_nodes(texto)   localiza nodos de entrada por id o nombre
  read_cypher(query)        recorrido de k saltos, **solo lectura**, con
                            **filtro RBAC inyectado**: los nodos que el
                            rol no puede ver se podan del resultado y se
                            registra un `policy_decision: podado`
  list_node_actions(node_id) acciones que expone ese nodo, con su
                            contrato y su política — **nunca el campo
                            `backend`** (§3.2: el agente recibe
                            `nombre + contrato + política`; dónde vive la
                            acción lo resuelve el MCP server)

`run_id` y `user_ctx` llegan en los **metadatos de cada llamada MCP**
(`_meta` del request); `EklMiddleware` los lee, emite `tool_call` /
`tool_result` / `policy_decision` al bus de eventos y escribe
`audit.jsonl`. Ver `mcp/ekl_middleware.py`.

Arranque:

    # base negocio (por defecto), stdio — así lo lanza MCP Inspector
    python mcp/ekl_graph_server.py
    # base infra
    python mcp/ekl_graph_server.py --base infra
    # por HTTP (streamable) en el puerto 8011
    python mcp/ekl_graph_server.py --base negocio --transport http --port 8011

Funciona con `GRAPH_BACKEND=memory` (sin Docker) y con
`GRAPH_BACKEND=neo4j`.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import unicodedata
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

import ekl_cypher  # noqa: E402
from ekl_middleware import EklMiddleware, emitir_policy_decision, llamada_actual  # noqa: E402
from ekl_policies import (  # noqa: E402
    cargar_ontology,
    contrato_de_accion,
    evaluar_nodo,
)

BASES_VALIDAS = ("negocio", "infra")


# --------------------------------------------------------------------------
# Grafo (perezoso, compartido por el proceso)
# --------------------------------------------------------------------------
_backend = None


def grafo():
    """Backend de grafo del proceso. Se conecta en la primera llamada
    para que importar el módulo (tests, Inspector) sea barato.

    **Solo carga los datos si hacen falta.** Con `GRAPH_BACKEND=memory`
    el grafo nace vacío en cada proceso, así que siempre se construye.
    Con `GRAPH_BACKEND=neo4j` los datos ya están en la base (los cargó
    `data/load_graph.py` o `scripts/demo_reset.sh`): recargarlos en cada
    arranque del servidor MCP son ~50 viajes de ida y vuelta inútiles y
    llena la consola de avisos del driver. `EKL_RELOAD_GRAPH=1` fuerza la
    recarga.
    """
    global _backend
    if _backend is None:
        from graph_backend import get_backend
        from load_graph import DB_INFRA, DB_NEGOCIO, build_graph

        backend = get_backend()
        forzar = os.environ.get("EKL_RELOAD_GRAPH", "").strip().lower() in ("1", "true", "yes", "si", "sí")
        vacio = backend.count_nodes(DB_NEGOCIO) == 0 or backend.count_nodes(DB_INFRA) == 0
        if forzar or vacio:
            build_graph(backend)
        _backend = backend
    return _backend


def reset_grafo() -> None:
    """Solo para tests que quieren un grafo limpio."""
    global _backend
    _backend = None


# --------------------------------------------------------------------------
# Modelos de salida (contratos visibles para el agente y el Inspector)
# --------------------------------------------------------------------------
class TipoNodo(BaseModel):
    tipo: str
    bian_service_domain: str | None = None
    propiedades_clave: list[str] = Field(default_factory=list)
    frontera: bool = Field(
        default=False,
        description="El nodo es una referencia a otro dominio: hay que delegar por A2A para saber más",
    )


class TipoArista(BaseModel):
    nombre: str
    de: list[str]
    a: list[str]


class Esquema(BaseModel):
    base: str
    tipos: list[TipoNodo]
    aristas: list[TipoArista]
    acciones: list[str] = Field(description="Acciones descubribles con list_node_actions")
    nota: str


class NodoEncontrado(BaseModel):
    id: str
    tipo: str | None = None
    nombre: str | None = None
    propiedades: dict[str, Any] = Field(default_factory=dict)


class ResultadoEntrada(BaseModel):
    texto: str
    nodos: list[NodoEncontrado]
    nodos_filtrados_por_politica: int = 0
    sugerencia: str | None = Field(
        default=None,
        description="Qué hacer si la búsqueda no encontró nada utilizable.",
    )


class ResultadoCypher(BaseModel):
    filas: list[dict[str, Any]]
    total: int
    nodos_filtrados_por_politica: list[str] = Field(
        default_factory=list, description="Ids de nodos podados por RBAC antes de proyectar el RETURN"
    )
    politica_aplicada: str | None = None
    subconjunto_soportado: str = Field(
        default="MATCH patrón lineal [WHERE ... AND ...] RETURN [DISTINCT] ... [LIMIT n] — solo lectura"
    )


class AccionExpuesta(BaseModel):
    nombre: str
    expuesta_por: str | None = None
    contrato: dict[str, Any] = Field(default_factory=dict)
    politica: dict[str, Any] = Field(default_factory=dict)
    # Deliberadamente NO hay campo `backend`: §3.2.


class ResultadoAcciones(BaseModel):
    node_id: str
    tipo: str | None = None
    acciones: list[AccionExpuesta]
    nota: str = (
        "El contrato no incluye `backend`: dónde se ejecuta la acción lo resuelve el servidor MCP, "
        "no el agente."
    )


# --------------------------------------------------------------------------
# Utilidades
# --------------------------------------------------------------------------
def _normalizar(texto: str) -> str:
    """Minúsculas y sin tildes, para que `find_entry_nodes('pagos core')`
    encuentre `Pagos Core` y `nomina` encuentre `Nómina Batch`."""
    sin_tildes = unicodedata.normalize("NFKD", str(texto))
    sin_tildes = "".join(c for c in sin_tildes if not unicodedata.combining(c))
    return sin_tildes.lower()


#: Palabras que no aportan nada al buscar un nodo. La lista es corta a
#: propósito: solo lo que aparece en cualquier pregunta de negocio.
VACIAS = {
    "de", "del", "la", "el", "los", "las", "un", "una", "unos", "unas", "y", "o", "en",
    "con", "que", "cual", "cuales", "se", "su", "sus", "para", "por", "al", "es", "son",
    "hoy", "si", "sobre", "como", "cuando", "donde", "quien", "quienes", "mas", "menos",
    "todo", "todos", "toda", "todas", "este", "esta", "estos", "estas", "ese", "esa",
}


def _terminos(texto: str) -> list[str]:
    """Palabras útiles de la búsqueda, normalizadas.

    Un modelo pasa con frecuencia la pregunta entera
    (`"servicio de pagos exposición crediticia clientes corporativos"`).
    Buscarla como una sola subcadena no encuentra nada y el agente se
    queda sin un solo id real, que es como acaba inventándoselos.
    """
    crudo = _normalizar(texto)
    palabras = re.split(r"[^a-z0-9\-_]+", crudo)
    return [p for p in palabras if len(p) >= 3 and p not in VACIAS]


def _nodo_publico(nodo: dict[str, Any]) -> NodoEncontrado:
    props = {k: v for k, v in nodo.items() if k != "_label"}
    return NodoEncontrado(
        id=str(nodo.get("id")),
        tipo=nodo.get("_label"),
        nombre=nodo.get("nombre"),
        propiedades=props,
    )


def _tipos_de_la_base(base: str) -> list[TipoNodo]:
    tipos = []
    for nodo in cargar_ontology().get("nodos") or []:
        grafo_del_tipo = nodo.get("grafo", "ambos")
        if grafo_del_tipo not in (base, "ambos"):
            continue
        # Un `Servicio` visto desde `negocio` es un nodo frontera: su dueño
        # es el dominio `infra` (§4.5, nodo `delegar_a2a`).
        frontera = base == "negocio" and nodo.get("tipo") == "Servicio"
        tipos.append(
            TipoNodo(
                tipo=nodo["tipo"],
                bian_service_domain=nodo.get("bian_service_domain"),
                propiedades_clave=[str(p) for p in (nodo.get("propiedades_clave") or [])],
                frontera=frontera,
            )
        )
    return tipos


def _aristas_de_la_base(base: str) -> list[TipoArista]:
    aristas = []
    for arista in cargar_ontology().get("aristas") or []:
        if arista.get("grafo", "ambos") not in (base, "ambos"):
            continue
        de = arista.get("de")
        a = arista.get("a")
        aristas.append(
            TipoArista(
                nombre=arista["nombre"],
                de=de if isinstance(de, list) else [de],
                a=a if isinstance(a, list) else [a],
            )
        )
    return aristas


async def _podar_por_politica(
    filas: list[dict[str, dict[str, Any]]], rol: str
) -> tuple[list[dict[str, dict[str, Any]]], list[str], str | None]:
    """Aplica el RBAC de `policies.yaml` sobre los bindings del patrón.

    Una fila desaparece completa si **cualquiera** de sus nodos está
    vedado para el rol: devolver la fila sin ese nodo dejaría al agente
    con una ruta que no puede justificar. Emite un único
    `policy_decision` agregado (podado o permitido) para que la Sala de
    control muestre una sola flecha por consulta.
    """
    permitidas: list[dict[str, dict[str, Any]]] = []
    podados: list[str] = []
    regla_aplicada: str | None = None
    justificacion: str | None = None

    for fila in filas:
        vedado = False
        for nodo in fila.values():
            decision = evaluar_nodo(rol, nodo.get("_label"), nodo)
            if not decision.permitido:
                vedado = True
                regla_aplicada = decision.regla
                justificacion = decision.justificacion
                node_id = str(nodo.get("id"))
                if node_id not in podados:
                    podados.append(node_id)
        if not vedado:
            permitidas.append(fila)

    llamada = llamada_actual()
    llamada.nodos_filtrados.extend(n for n in podados if n not in llamada.nodos_filtrados)

    if podados:
        await emitir_policy_decision(
            regla=regla_aplicada or "node_policies",
            resultado="podado",
            justificacion=justificacion or "nodos podados por RBAC",
            nodos_podados=podados,
            filas_antes=len(filas),
            filas_despues=len(permitidas),
        )
    else:
        await emitir_policy_decision(
            regla="node_policies",
            resultado="permitido",
            justificacion=f"ningún nodo del resultado está restringido para el rol '{rol}'",
            filas=len(permitidas),
        )
    return permitidas, podados, regla_aplicada


# --------------------------------------------------------------------------
# Fábrica del servidor (parametrizado por base)
# --------------------------------------------------------------------------
def crear_servidor(base: str | None = None) -> MCPServer:
    """Construye el servidor MCP para una base (`negocio` | `infra`)."""
    base = (base or os.environ.get("EKL_GRAPH_BASE") or "negocio").lower()
    if base not in BASES_VALIDAS:
        raise ValueError(f"base inválida: {base!r} (usa {' o '.join(BASES_VALIDAS)})")

    servidor = MCPServer(
        name=f"ekl-graph[{base}]",
        title=f"EKL — grafo de conocimiento ({base})",
        version="1.0",
        instructions=(
            f"Grafo de conocimiento gobernado del dominio '{base}'. Empieza por get_schema() "
            "para saber qué tipos y aristas existen y cuáles son nodos frontera (de otro "
            "dominio: hay que delegar por A2A). Usa find_entry_nodes() para localizar nodos de "
            "entrada, read_cypher() para recorrer, y list_node_actions() para descubrir qué "
            "acciones expone un nodo. Las políticas se aplican en el servidor: puede que un "
            "resultado venga podado."
        ),
        middleware=[EklMiddleware(servidor="ekl-graph", source="mcp_graph", base=base)],
    )

    @servidor.tool(
        description=(
            "Esquema del grafo de este dominio: tipos de nodo (con su alineación BIAN y si son "
            "nodos frontera de otro dominio), aristas permitidas y acciones descubribles."
        )
    )
    async def get_schema() -> Esquema:
        tipos = _tipos_de_la_base(base)
        aristas = _aristas_de_la_base(base)
        acciones = [a["nombre"] for a in (cargar_ontology().get("acciones") or [])] if base == "negocio" else []
        llamada = llamada_actual()
        llamada.resumen = {"tipos": len(tipos), "aristas": len(aristas), "acciones": len(acciones)}
        return Esquema(
            base=base,
            tipos=tipos,
            aristas=aristas,
            acciones=acciones,
            nota=(
                "Los tipos marcados `frontera: true` son referencias a otro dominio: este grafo "
                "solo tiene su id y su nombre. Para saber más hay que delegar en el agente dueño."
            ),
        )

    @servidor.tool(
        description=(
            "Localiza nodos de entrada al grafo buscando en su id y su nombre, sin distinguir "
            "mayúsculas ni tildes. Acepta una o varias palabras y devuelve primero los nodos que "
            "coinciden con más de ellas. Es el primer salto de cualquier recorrido: usa los ids "
            "que devuelve, no ids inventados."
        )
    )
    async def find_entry_nodes(texto: str, limite: int = 10) -> ResultadoEntrada:
        terminos = _terminos(texto)
        if not terminos:
            raise ToolError(
                "find_entry_nodes necesita al menos una palabra de búsqueda con contenido "
                f"(recibido: {texto!r})"
            )

        llamada = llamada_actual()
        rol = llamada.rol
        # Se puntúa por número de términos distintos que coinciden, así una
        # frase entera sigue encontrando el nodo correcto en vez de nada.
        puntuados: list[tuple[int, dict[str, Any]]] = []
        filtrados = 0
        for nodo in grafo().list_nodes(base):
            campos = _normalizar(
                " ".join(str(nodo.get(k, "")) for k in ("id", "nombre", "descripcion"))
            )
            aciertos = sum(1 for t in terminos if t in campos)
            if not aciertos:
                continue
            decision = evaluar_nodo(rol, nodo.get("_label"), nodo)
            if not decision.permitido:
                filtrados += 1
                node_id = str(nodo.get("id"))
                if node_id not in llamada.nodos_filtrados:
                    llamada.nodos_filtrados.append(node_id)
                continue
            puntuados.append((aciertos, nodo))

        puntuados.sort(key=lambda par: (-par[0], str(par[1].get("id"))))
        encontrados = [_nodo_publico(n) for _, n in puntuados[:limite]]

        if filtrados:
            await emitir_policy_decision(
                regla="node_policies",
                resultado="podado",
                justificacion=(
                    f"{filtrados} nodo(s) coincidían con '{texto}' pero están restringidos "
                    f"para el rol '{rol}'"
                ),
                nodos_podados=list(llamada.nodos_filtrados),
            )

        llamada.nodos_devueltos = len(encontrados)
        llamada.resumen = {"texto": texto, "encontrados": len(encontrados), "filtrados": filtrados}
        sugerencia = None
        if not encontrados and not filtrados:
            sugerencia = (
                f"Ninguno de los términos {terminos} coincide con un nodo de la base '{base}'. "
                "Prueba con una sola palabra del dominio (por ejemplo el nombre de un servicio o "
                "de un producto) y usa después el id exacto que devuelva."
            )
        return ResultadoEntrada(
            texto=texto, nodos=encontrados,
            nodos_filtrados_por_politica=filtrados, sugerencia=sugerencia,
        )

    @servidor.tool(
        description=(
            "Recorre el grafo con un subconjunto de Cypher DE SOLO LECTURA: "
            "MATCH con un patrón lineal de k saltos, WHERE con condiciones unidas por AND, "
            "RETURN [DISTINCT] y LIMIT. Las políticas del dominio se aplican en el servidor: "
            "los nodos que tu rol no puede ver desaparecen del resultado y quedan registrados."
        )
    )
    async def read_cypher(query: str, limite: int = 200) -> ResultadoCypher:
        llamada = llamada_actual()
        try:
            consulta = ekl_cypher.parsear(query)
        except (ekl_cypher.CypherNoPermitido, ekl_cypher.CypherNoSoportado) as exc:
            raise ToolError(str(exc)) from exc

        if consulta.limite is None:
            consulta.limite = limite

        filas = ekl_cypher.ejecutar(consulta, grafo(), base)
        permitidas, podados, regla = await _podar_por_politica(filas, llamada.rol)
        proyectadas = ekl_cypher.proyectar(consulta, permitidas)

        llamada.nodos_devueltos = len(proyectadas)
        llamada.resumen = {
            "query": query,
            "filas": len(proyectadas),
            "podados": len(podados),
        }
        return ResultadoCypher(
            filas=proyectadas,
            total=len(proyectadas),
            nodos_filtrados_por_politica=podados,
            politica_aplicada=regla,
        )

    @servidor.tool(
        description=(
            "Acciones que expone un nodo, con su contrato de entrada/salida y la política que "
            "las gobierna. El contrato NO incluye dónde se ejecuta la acción."
        )
    )
    async def list_node_actions(node_id: str) -> ResultadoAcciones:
        llamada = llamada_actual()
        nodo = grafo().get_node(base, node_id)
        if nodo is None:
            raise ToolError(f"no existe el nodo '{node_id}' en la base '{base}'")

        decision = evaluar_nodo(llamada.rol, nodo.get("_label"), nodo)
        if not decision.permitido:
            await emitir_policy_decision(
                regla=decision.regla,
                resultado="denegado",
                justificacion=decision.justificacion,
                nodos_podados=[node_id],
            )
            llamada.nodos_filtrados.append(node_id)
            raise ToolError(
                f"acceso denegado al nodo '{node_id}': {decision.justificacion}"
            )

        acciones: list[AccionExpuesta] = []
        for vecino in grafo().neighbors(base, node_id, "EXPONE_ACCION", direction="out"):
            # El id del nodo Accion es el nombre de la acción (load_graph.py).
            contrato = contrato_de_accion(str(vecino.get("id")))
            if contrato is None:
                continue
            acciones.append(AccionExpuesta(**contrato))

        await emitir_policy_decision(
            regla="node_policies",
            resultado="permitido",
            justificacion=f"el rol '{llamada.rol}' puede leer el nodo '{node_id}'",
            acciones=len(acciones),
        )
        llamada.nodos_devueltos = len(acciones)
        llamada.resumen = {"node_id": node_id, "acciones": [a.nombre for a in acciones]}
        return ResultadoAcciones(node_id=node_id, tipo=nodo.get("_label"), acciones=acciones)

    return servidor


def __getattr__(name: str):
    """`servidor` a nivel de módulo, creado la primera vez que se pide.

    Es lo que necesitan MCP Inspector vía `mcp dev` y cualquier runner
    que espere un objeto servidor ya construido:

        EKL_GRAPH_BASE=infra mcp dev mcp/ekl_graph_server.py:servidor
    """
    if name == "servidor":
        servidor = crear_servidor()
        globals()["servidor"] = servidor
        return servidor
    raise AttributeError(name)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Servidor MCP ekl-graph (plan_demo.md §4.3)")
    parser.add_argument(
        "--base",
        choices=BASES_VALIDAS,
        default=os.environ.get("EKL_GRAPH_BASE", "negocio"),
        help="base del grafo que sirve esta instancia (default: negocio)",
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "http", "sse"),
        default=os.environ.get("EKL_MCP_TRANSPORT", "stdio"),
        help="stdio (MCP Inspector, por defecto) | http (streamable) | sse",
    )
    parser.add_argument("--port", type=int, default=int(os.environ.get("EKL_GRAPH_PORT", "8011")))
    parser.add_argument("--host", default=os.environ.get("EKL_MCP_HOST", "127.0.0.1"))
    args = parser.parse_args(argv)

    servidor = crear_servidor(args.base)
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
