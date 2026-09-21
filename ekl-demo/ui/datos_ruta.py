"""
datos_ruta.py — reconstrucción del grafo recorrido para la pestaña Ruta
(plan_demo.md §4.7).

El agente devuelve `ruta_nodos`: **qué** nodos visitó, no **cómo** están
conectados. Para dibujar un grafo hacen falta las aristas, y se sacan de
dos sitios distintos a propósito:

  * **Las de negocio, preguntándole al mismo servidor MCP que usó el
    agente**, con el rol del usuario. No es un atajo: hace que la
    pestaña Ruta respete el RBAC igual que todo lo demás — con rol
    `analista_junior` los clientes podados tampoco aparecen aquí.
  * **Las de infraestructura, del contrato que devolvió el Agente B por
    A2A.** La UI no consulta el grafo `infra`: no es su dominio, igual
    que no lo es el del Agente A. Lo único que sabe de ese lado es lo
    que B decidió contar, que es justo lo que hay que enseñar.

De ahí salen los tres colores de la pestaña: negocio, frontera y
"vino por A2A".
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
for _ruta in (str(ROOT_DIR), str(ROOT_DIR / "mcp"), str(ROOT_DIR / "data")):
    if _ruta not in sys.path:
        sys.path.insert(0, _ruta)

#: Tipos de arista del grafo de negocio que se intentan reconstruir.
ARISTAS_NEGOCIO = ("TIENE", "SE_EJECUTA_EN", "SE_LIQUIDA_EN", "EXPONE_ACCION")

ORIGEN_NEGOCIO = "negocio"
ORIGEN_FRONTERA = "frontera"
ORIGEN_A2A = "a2a"


def _tipo_por_prefijo(node_id: str) -> str:
    """Tipo probable de un nodo por su id. Solo para la etiqueta."""
    for prefijo, tipo in (
        ("CLI", "Cliente"), ("PROD", "Producto"), ("CTA", "Cuenta"),
        ("PER", "Persona"), ("CHG", "Cambio"),
    ):
        if node_id.startswith(prefijo):
            return tipo
    if node_id.endswith("-DB") or "-DB-" in node_id:
        return "BaseDatos"
    return "Servicio"


async def aristas_de_negocio(
    visitados: set[str], run_id: str, user_ctx: dict[str, Any]
) -> list[tuple[str, str, str]]:
    """Aristas del grafo de negocio entre nodos visitados, leídas por MCP.

    Una consulta por tipo de arista (son cuatro), no una por par de
    nodos. Devuelve `(origen, destino, tipo)`.
    """
    from agents.eventos import ContextoCorrida
    from agents.mcp_cliente import ClienteMCP, ErrorHerramienta

    ctx = ContextoCorrida(run_id=run_id, user_ctx=user_ctx, source="ui")
    aristas: list[tuple[str, str, str]] = []
    async with ClienteMCP(ctx, grafo_base="negocio", con_acciones=False) as mcp:
        for tipo in ARISTAS_NEGOCIO:
            consulta = f"MATCH (a)-[:{tipo}]->(b) RETURN a.id AS origen, b.id AS destino"
            try:
                datos = await mcp.llamar("ekl-graph", "read_cypher", {"query": consulta}, step="ruta")
            except ErrorHerramienta:
                continue
            for fila in datos.get("filas", []):
                origen, destino = fila.get("origen"), fila.get("destino")
                if origen in visitados and destino in visitados:
                    aristas.append((origen, destino, tipo))
    return aristas


def aristas_de_a2a(eventos: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
    """Aristas que el Agente B contó por A2A.

    Se leen de los contratos de los eventos `a2a_response`: lo que ese
    dominio decidió compartir, ni más ni menos.
    """
    aristas: list[tuple[str, str, str]] = []
    for evento in eventos:
        if evento.get("kind") != "a2a_response":
            continue
        contrato = (evento.get("payload") or {}).get("contrato") or {}
        objetivo = contrato.get("objetivo")
        for servicio in contrato.get("servicios_afectados") or []:
            if objetivo and servicio.get("id"):
                aristas.append((servicio["id"], objetivo, "DEPENDE_DE"))
        responsable = contrato.get("responsable")
        if responsable and responsable.get("id") and objetivo:
            aristas.append((objetivo, responsable["id"], "ES_RESPONSABLE"))
        for bd in (contrato.get("detalle") or {}).get("bases_de_datos_afectadas") or []:
            if objetivo:
                aristas.append((objetivo, bd, "AFECTA"))
    return aristas


def nodos_via_a2a(eventos: list[dict[str, Any]]) -> set[str]:
    """Ids de nodos que el Agente A solo conoce porque B se los contó."""
    ids: set[str] = set()
    for evento in eventos:
        if evento.get("kind") != "a2a_response":
            continue
        contrato = (evento.get("payload") or {}).get("contrato") or {}
        for servicio in contrato.get("servicios_afectados") or []:
            if servicio.get("id"):
                ids.add(servicio["id"])
        responsable = contrato.get("responsable")
        if responsable and responsable.get("id"):
            ids.add(responsable["id"])
        if contrato.get("objetivo"):
            ids.add(contrato["objetivo"])
        for bd in (contrato.get("detalle") or {}).get("bases_de_datos_afectadas") or []:
            ids.add(bd)
    return ids


def clasificar(node_id: str, via_a2a: set[str], nombres: dict[str, str]) -> dict[str, Any]:
    """Nodo listo para pintar: tipo, etiqueta y de qué dominio viene."""
    tipo = _tipo_por_prefijo(node_id)
    if node_id in via_a2a:
        origen = ORIGEN_A2A
    elif tipo in ("Servicio", "BaseDatos", "Persona", "Cambio"):
        origen = ORIGEN_FRONTERA
    else:
        origen = ORIGEN_NEGOCIO
    return {
        "id": node_id,
        "tipo": tipo,
        "etiqueta": nombres.get(node_id, node_id),
        "origen": origen,
    }


def construir_grafo(
    ruta: list[str],
    aristas_negocio: list[tuple[str, str, str]],
    eventos: list[dict[str, Any]],
    nombres: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], list[tuple[str, str, str]]]:
    """Nodos y aristas del grafo recorrido, listos para `streamlit-agraph`."""
    via_a2a = nodos_via_a2a(eventos)
    visitados = list(dict.fromkeys(list(ruta) + sorted(via_a2a)))
    nodos = [clasificar(n, via_a2a, nombres or {}) for n in visitados]
    conjunto = set(visitados)
    aristas = list(aristas_negocio) + [
        a for a in aristas_de_a2a(eventos) if a[0] in conjunto and a[1] in conjunto
    ]
    # Sin duplicados, conservando el orden.
    vistas, unicas = set(), []
    for arista in aristas:
        if arista not in vistas:
            vistas.add(arista)
            unicas.append(arista)
    return nodos, unicas
