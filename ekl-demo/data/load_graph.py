#!/usr/bin/env python3
"""
load_graph.py — carga el grafo de la demo EKL (plan_demo.md §4.2)

Crea los constraints, carga ambas bases (`negocio`, `infra`) desde los
CSV de esta carpeta, y crea los nodos `Accion` (desde ontology.yaml) y
`Metrica` (desde semantic_layer.yaml), con la arista EXPONE_ACCION desde
cada `Cliente` hacia la acción `retrieve_customer_position`.

Funciona contra Neo4j real (GRAPH_BACKEND=neo4j) o contra un grafo en
memoria con networkx (GRAPH_BACKEND=memory) — misma interfaz, ver
graph_backend.py.

Uso:
    GRAPH_BACKEND=memory python data/load_graph.py
    GRAPH_BACKEND=neo4j  python data/load_graph.py   # requiere: docker compose up -d neo4j
"""

from __future__ import annotations

import csv
import os
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from graph_backend import get_backend  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent
DB_NEGOCIO = os.environ.get("NEO4J_DB_NEGOCIO", "negocio")
DB_INFRA = os.environ.get("NEO4J_DB_INFRA", "infra")


def _read_csv(name: str) -> list[dict]:
    path = DATA_DIR / name
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def create_constraints(backend) -> None:
    for label in ("Cliente", "Producto", "Cuenta", "Servicio", "Accion", "Metrica"):
        backend.create_constraint(DB_NEGOCIO, label, "id")
    for label in ("Servicio", "BaseDatos", "Persona", "Cambio"):
        backend.create_constraint(DB_INFRA, label, "id")


def load_negocio_nodes(backend) -> None:
    for row in _read_csv("clientes.csv"):
        row = dict(row)
        rid = row.pop("id")
        backend.create_node(DB_NEGOCIO, "Cliente", rid, row)

    for row in _read_csv("productos.csv"):
        row = dict(row)
        rid = row.pop("id")
        backend.create_node(DB_NEGOCIO, "Producto", rid, row)

    for row in _read_csv("cuentas.csv"):
        row = dict(row)
        rid = row.pop("id")
        backend.create_node(DB_NEGOCIO, "Cuenta", rid, row)

    # Servicio: nodo de referencia en negocio (id, nombre, dominio_owner)
    for row in _read_csv("servicios.csv"):
        row = dict(row)
        rid = row.pop("id")
        backend.create_node(DB_NEGOCIO, "Servicio", rid, row)


def load_infra_nodes(backend) -> None:
    # Servicio: nodo completo en infra (mismos campos que en negocio en
    # esta demo; en un modelo real infra tendría más propiedades)
    for row in _read_csv("servicios.csv"):
        row = dict(row)
        rid = row.pop("id")
        backend.create_node(DB_INFRA, "Servicio", rid, row)

    for row in _read_csv("bases_datos.csv"):
        row = dict(row)
        rid = row.pop("id")
        backend.create_node(DB_INFRA, "BaseDatos", rid, row)

    for row in _read_csv("personas.csv"):
        row = dict(row)
        rid = row.pop("id")
        backend.create_node(DB_INFRA, "Persona", rid, row)

    for row in _read_csv("cambios.csv"):
        row = dict(row)
        rid = row.pop("id")
        backend.create_node(DB_INFRA, "Cambio", rid, row)


def load_negocio_edges(backend) -> None:
    for row in _read_csv("rel_tiene.csv"):
        backend.create_edge(DB_NEGOCIO, "TIENE", row["cliente_id"], row["producto_id"])

    for row in _read_csv("rel_se_liquida_en.csv"):
        backend.create_edge(DB_NEGOCIO, "SE_LIQUIDA_EN", row["producto_id"], row["cuenta_id"])

    for row in _read_csv("rel_se_ejecuta_en.csv"):
        backend.create_edge(DB_NEGOCIO, "SE_EJECUTA_EN", row["producto_id"], row["servicio_id"])


def load_infra_edges(backend) -> None:
    for row in _read_csv("rel_depende_de.csv"):
        backend.create_edge(
            DB_INFRA,
            "DEPENDE_DE",
            row["servicio_id"],
            row["destino_id"],
            {"destino_tipo": row["destino_tipo"], "fuente": row.get("fuente", "")},
        )

    for row in _read_csv("rel_es_responsable.csv"):
        backend.create_edge(DB_INFRA, "ES_RESPONSABLE", row["servicio_id"], row["persona_id"])

    for row in _read_csv("rel_afecta.csv"):
        backend.create_edge(DB_INFRA, "AFECTA", row["cambio_id"], row["basedatos_id"])


def load_acciones_y_expone_accion(backend) -> None:
    """Nodos Accion (ontology.yaml §3.2) + arista EXPONE_ACCION desde cada
    Cliente que los expone.

    El campo `backend` (mcp://ekl-actions/...) se guarda en el nodo
    porque tiene que vivir en algún lugar del grafo gobernado, pero el
    agente nunca debe verlo: `list_node_actions` (sesión 2, MCP
    ekl_graph_server) tiene que excluirlo explícitamente de lo que
    devuelve.
    """
    with open(DATA_DIR / "ontology.yaml", encoding="utf-8") as f:
        ontology = yaml.safe_load(f)

    clientes = [row["id"] for row in _read_csv("clientes.csv")]

    for accion in ontology.get("acciones", []):
        nombre = accion["nombre"]
        backend.create_node(
            DB_NEGOCIO,
            "Accion",
            nombre,
            {
                "expuesta_por": accion["expuesta_por"],
                "contrato": yaml.safe_dump(accion["contrato"], allow_unicode=True, sort_keys=False),
                "backend": accion["backend"],
                "requiere_rol": ",".join(accion["politica"]["requiere_rol"]),
            },
        )
        if accion["expuesta_por"] == "Cliente":
            for cliente_id in clientes:
                backend.create_edge(DB_NEGOCIO, "EXPONE_ACCION", cliente_id, nombre)


def load_metricas(backend) -> None:
    with open(DATA_DIR / "semantic_layer.yaml", encoding="utf-8") as f:
        semantic = yaml.safe_load(f)

    for nombre, metrica in semantic.get("metricas", {}).items():
        backend.create_node(
            DB_NEGOCIO,
            "Metrica",
            nombre,
            {
                "definicion": metrica["definicion"],
                "owner": metrica["owner"],
                "version": metrica["version"],
            },
        )


def build_graph(backend=None):
    """Construye el grafo completo sobre `backend` (o uno nuevo leído de
    GRAPH_BACKEND si no se pasa ninguno) y lo devuelve SIN cerrarlo, para
    que quien llame (tests, u otro script) pueda seguir consultándolo."""
    if backend is None:
        backend = get_backend()
    create_constraints(backend)
    load_negocio_nodes(backend)
    load_infra_nodes(backend)
    load_negocio_edges(backend)
    load_infra_edges(backend)
    load_acciones_y_expone_accion(backend)
    load_metricas(backend)
    return backend


def main() -> None:
    backend_name = os.environ.get("GRAPH_BACKEND", "memory")
    print(f"GRAPH_BACKEND={backend_name}")
    backend = build_graph()
    try:
        print(f"negocio: {backend.count_nodes(DB_NEGOCIO)} nodos, {backend.count_edges(DB_NEGOCIO)} aristas")
        print(f"infra:   {backend.count_nodes(DB_INFRA)} nodos, {backend.count_edges(DB_INFRA)} aristas")
        print("Carga completa.")
    finally:
        backend.close()


if __name__ == "__main__":
    main()
