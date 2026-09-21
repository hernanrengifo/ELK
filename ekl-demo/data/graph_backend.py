"""
graph_backend.py — interfaz común de grafo para la demo EKL.

Dos implementaciones intercambiables, seleccionadas por
`GRAPH_BACKEND=neo4j|memory` (plan_demo.md §4.2):

  - Neo4jBackend: usa el driver oficial `neo4j`, una sesión por base de
    datos lógica (`negocio` / `infra`). Requiere Neo4j 5 Enterprise
    real corriendo (ver docker-compose.yml) porque usa bases de datos
    múltiples — ver STATUS.md para la razón de usar Enterprise.
  - MemoryBackend: `networkx.MultiDiGraph`, uno por base lógica. Sin
    dependencias de infraestructura — es lo que usan los tests y lo que
    corre por defecto si no hay Docker disponible.

Ambas implementan el mismo API mínimo (create_constraint, create_node,
create_edge, get_node, neighbors, neighbors_with_edge, list_nodes,
count_nodes, count_edges, close), así que `load_graph.py` y los tests no
necesitan saber contra cuál backend están corriendo.

`list_nodes` se agregó en F2 (servidores MCP): el `read_cypher` de
`mcp/ekl_graph_server.py` necesita enumerar los nodos de partida de un
patrón y hasta F1 solo se podía contarlos o navegar desde un id
conocido. Es un método nuevo: no cambia el comportamiento de ninguno de
los anteriores.
"""

from __future__ import annotations

import os
import time
from abc import ABC, abstractmethod


class GraphBackend(ABC):
    """Interfaz común. Ver docstring del módulo."""

    @abstractmethod
    def create_constraint(self, db: str, label: str, prop: str = "id") -> None:
        ...

    @abstractmethod
    def create_node(self, db: str, label: str, node_id: str, properties: dict) -> None:
        ...

    @abstractmethod
    def create_edge(
        self, db: str, rel_type: str, from_id: str, to_id: str, properties: dict | None = None
    ) -> None:
        ...

    @abstractmethod
    def get_node(self, db: str, node_id: str) -> dict | None:
        ...

    @abstractmethod
    def neighbors(self, db: str, node_id: str, rel_type: str, direction: str = "out") -> list[dict]:
        """direction: 'out' (node_id)-[rel_type]->(vecino) | 'in' (vecino)-[rel_type]->(node_id)"""
        ...

    @abstractmethod
    def neighbors_with_edge(
        self, db: str, node_id: str, rel_type: str, direction: str = "out"
    ) -> list[tuple[dict, dict]]:
        """Como `neighbors`, pero devuelve `(vecino, propiedades_de_la_arista)`.

        Añadido en F6: el linaje de una dependencia vive en la arista
        (`fuente: "runbook_pagos.md#L24"`), no en los nodos, y hasta
        ahora no había forma de leerlo. Lo usa `read_cypher` cuando el
        patrón declara una variable de relación: `-[r:DEPENDE_DE]->`.
        """
        ...

    @abstractmethod
    def list_nodes(self, db: str, label: str | None = None) -> list[dict]:
        """Todos los nodos de la base (o solo los de `label`), cada uno
        con sus propiedades más `id` y `_label`.

        Añadido en F2: el `read_cypher` del MCP server necesita enumerar
        los nodos de partida de un patrón, y hasta F1 solo se podía
        contarlos o navegar desde un id conocido."""
        ...

    @abstractmethod
    def count_nodes(self, db: str, label: str | None = None) -> int:
        ...

    @abstractmethod
    def count_edges(self, db: str, rel_type: str | None = None) -> int:
        ...

    def close(self) -> None:  # pragma: no cover - no-op por defecto
        pass


# --------------------------------------------------------------------------
# Backend en memoria (networkx) — sin dependencias de infraestructura
# --------------------------------------------------------------------------
class MemoryBackend(GraphBackend):
    def __init__(self):
        import networkx as nx

        self._nx = nx
        self._graphs: dict[str, "nx.MultiDiGraph"] = {}

    def _g(self, db: str):
        if db not in self._graphs:
            self._graphs[db] = self._nx.MultiDiGraph()
        return self._graphs[db]

    def create_constraint(self, db, label, prop="id"):
        # networkx no tiene constraints reales; la unicidad de `id` la
        # garantiza que node_id es la clave del nodo en el grafo (un
        # create_node repetido con el mismo id actualiza, no duplica).
        pass

    def create_node(self, db, label, node_id, properties):
        g = self._g(db)
        props = dict(properties or {})
        g.add_node(node_id, _label=label, **props)

    def create_edge(self, db, rel_type, from_id, to_id, properties=None):
        g = self._g(db)
        if from_id not in g.nodes or to_id not in g.nodes:
            raise ValueError(
                f"No se puede crear arista {rel_type} {from_id}->{to_id}: "
                f"nodo origen o destino inexistente en la base '{db}'"
            )
        g.add_edge(from_id, to_id, key=rel_type, **(properties or {}))

    def get_node(self, db, node_id):
        g = self._g(db)
        if node_id not in g.nodes:
            return None
        data = dict(g.nodes[node_id])
        data["id"] = node_id
        return data

    def neighbors(self, db, node_id, rel_type, direction="out"):
        g = self._g(db)
        if node_id not in g.nodes:
            return []
        out = []
        if direction == "out":
            for _u, v, k in g.out_edges(node_id, keys=True):
                if k == rel_type:
                    n = dict(g.nodes[v])
                    n["id"] = v
                    out.append(n)
        elif direction == "in":
            for u, _v, k in g.in_edges(node_id, keys=True):
                if k == rel_type:
                    n = dict(g.nodes[u])
                    n["id"] = u
                    out.append(n)
        else:
            raise ValueError("direction debe ser 'out' o 'in'")
        return out

    def neighbors_with_edge(self, db, node_id, rel_type, direction="out"):
        g = self._g(db)
        if node_id not in g.nodes:
            return []
        out = []
        if direction == "out":
            aristas = g.out_edges(node_id, keys=True, data=True)
            for _u, v, k, datos in aristas:
                if k == rel_type:
                    n = dict(g.nodes[v])
                    n["id"] = v
                    out.append((n, dict(datos)))
        elif direction == "in":
            for u, _v, k, datos in g.in_edges(node_id, keys=True, data=True):
                if k == rel_type:
                    n = dict(g.nodes[u])
                    n["id"] = u
                    out.append((n, dict(datos)))
        else:
            raise ValueError("direction debe ser 'out' o 'in'")
        return out

    def list_nodes(self, db, label=None):
        g = self._g(db)
        out = []
        for node_id, data in g.nodes(data=True):
            if label is not None and data.get("_label") != label:
                continue
            n = dict(data)
            n["id"] = node_id
            out.append(n)
        return out

    def count_nodes(self, db, label=None):
        g = self._g(db)
        if label is None:
            return g.number_of_nodes()
        return sum(1 for _, d in g.nodes(data=True) if d.get("_label") == label)

    def count_edges(self, db, rel_type=None):
        g = self._g(db)
        if rel_type is None:
            return g.number_of_edges()
        return sum(1 for _u, _v, k in g.edges(keys=True) if k == rel_type)


# --------------------------------------------------------------------------
# Backend Neo4j real
# --------------------------------------------------------------------------
class Neo4jBackend(GraphBackend):
    def __init__(self, uri: str, user: str, password: str):
        from neo4j import GraphDatabase  # import local: no requerido en modo memory

        self._driver = GraphDatabase.driver(uri, auth=(user, password))

    def ensure_databases(self, db_names: list[str], timeout_s: int = 60) -> None:
        with self._driver.session(database="system") as session:
            for name in db_names:
                session.run(f"CREATE DATABASE `{name}` IF NOT EXISTS")
        deadline = time.time() + timeout_s
        pending = set(db_names)
        while pending and time.time() < deadline:
            with self._driver.session(database="system") as session:
                result = session.run("SHOW DATABASES YIELD name, currentStatus")
                status = {rec["name"]: rec["currentStatus"] for rec in result}
            pending = {n for n in pending if status.get(n) != "online"}
            if pending:
                time.sleep(1)
        if pending:
            raise RuntimeError(f"Bases de datos no quedaron online a tiempo: {pending}")

    def create_constraint(self, db, label, prop="id"):
        with self._driver.session(database=db) as session:
            session.run(
                f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:`{label}`) "
                f"REQUIRE n.`{prop}` IS UNIQUE"
            )

    def create_node(self, db, label, node_id, properties):
        props = dict(properties or {})
        props["id"] = node_id
        with self._driver.session(database=db) as session:
            session.run(
                f"MERGE (n:`{label}` {{id: $id}}) SET n += $props",
                id=node_id,
                props=props,
            )

    def create_edge(self, db, rel_type, from_id, to_id, properties=None):
        with self._driver.session(database=db) as session:
            session.run(
                f"MATCH (a {{id: $from_id}}), (b {{id: $to_id}}) "
                f"MERGE (a)-[r:`{rel_type}`]->(b) SET r += $props",
                from_id=from_id,
                to_id=to_id,
                props=properties or {},
            )

    def get_node(self, db, node_id):
        with self._driver.session(database=db) as session:
            rec = session.run(
                "MATCH (n {id: $id}) RETURN n AS n, labels(n) AS labels", id=node_id
            ).single()
            if not rec:
                return None
            n = dict(rec["n"])
            n["_label"] = rec["labels"][0] if rec["labels"] else None
            return n

    def neighbors(self, db, node_id, rel_type, direction="out"):
        if direction == "out":
            pattern = f"(a {{id: $id}})-[r:`{rel_type}`]->(b)"
        elif direction == "in":
            pattern = f"(a {{id: $id}})<-[r:`{rel_type}`]-(b)"
        else:
            raise ValueError("direction debe ser 'out' o 'in'")
        with self._driver.session(database=db) as session:
            result = session.run(f"MATCH {pattern} RETURN b AS b, labels(b) AS labels", id=node_id)
            out = []
            for rec in result:
                n = dict(rec["b"])
                n["_label"] = rec["labels"][0] if rec["labels"] else None
                out.append(n)
            return out

    def neighbors_with_edge(self, db, node_id, rel_type, direction="out"):
        if direction == "out":
            pattern = f"(a {{id: $id}})-[r:`{rel_type}`]->(b)"
        elif direction == "in":
            pattern = f"(a {{id: $id}})<-[r:`{rel_type}`]-(b)"
        else:
            raise ValueError("direction debe ser 'out' o 'in'")
        with self._driver.session(database=db) as session:
            result = session.run(
                f"MATCH {pattern} RETURN b AS b, labels(b) AS labels, r AS r", id=node_id
            )
            out = []
            for rec in result:
                n = dict(rec["b"])
                n["_label"] = rec["labels"][0] if rec["labels"] else None
                out.append((n, dict(rec["r"])))
            return out

    def list_nodes(self, db, label=None):
        q = (
            f"MATCH (n:`{label}`) RETURN n AS n, labels(n) AS labels"
            if label
            else "MATCH (n) RETURN n AS n, labels(n) AS labels"
        )
        with self._driver.session(database=db) as session:
            out = []
            for rec in session.run(q):
                n = dict(rec["n"])
                n["_label"] = rec["labels"][0] if rec["labels"] else None
                out.append(n)
            return out

    def count_nodes(self, db, label=None):
        q = f"MATCH (n:`{label}`) RETURN count(n) AS c" if label else "MATCH (n) RETURN count(n) AS c"
        with self._driver.session(database=db) as session:
            return session.run(q).single()["c"]

    def count_edges(self, db, rel_type=None):
        q = (
            f"MATCH ()-[r:`{rel_type}`]->() RETURN count(r) AS c"
            if rel_type
            else "MATCH ()-[r]->() RETURN count(r) AS c"
        )
        with self._driver.session(database=db) as session:
            return session.run(q).single()["c"]

    def close(self):
        self._driver.close()


def get_backend(name: str | None = None) -> GraphBackend:
    """Fábrica: lee GRAPH_BACKEND del entorno si `name` no se especifica."""
    name = (name or os.environ.get("GRAPH_BACKEND", "memory")).lower()
    if name == "memory":
        return MemoryBackend()
    if name == "neo4j":
        uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
        user = os.environ.get("NEO4J_USER", "neo4j")
        password = os.environ.get("NEO4J_PASSWORD", "eklpassword")
        backend = Neo4jBackend(uri, user, password)
        backend.ensure_databases(
            [
                os.environ.get("NEO4J_DB_NEGOCIO", "negocio"),
                os.environ.get("NEO4J_DB_INFRA", "infra"),
            ]
        )
        return backend
    raise ValueError(f"GRAPH_BACKEND desconocido: '{name}' (usa 'memory' o 'neo4j')")
