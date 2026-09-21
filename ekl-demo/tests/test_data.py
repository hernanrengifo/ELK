"""
tests/test_data.py — criterio de cierre de la sesión 1 (F0 + F1).

Verifica que la ruta de la pregunta hilo conductor (plan_demo.md §1 y
§3.4) existe en el grafo cargado:

    PAY-DB-01 <- pagos-core <- productos <- 3 clientes corporativos
    con exposición crediticia > 5.000 millones de COP

y que `nomina-batch` también depende de `PAY-DB-01` (el hallazgo
colateral que el nodo crítico agrega en fases posteriores).

Corre con GRAPH_BACKEND=memory por defecto (sin Docker ni Neo4j):

    pytest tests/test_data.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
sys.path.insert(0, str(DATA_DIR))

from load_graph import build_graph, DB_NEGOCIO, DB_INFRA  # noqa: E402
from graph_backend import get_backend  # noqa: E402

EXPOSICION_MINIMA = 5_000_000_000  # 5.000 millones de COP (plan_demo.md §1, salto 4)


@pytest.fixture(scope="module")
def backend():
    # Por defecto memory (criterio de cierre de la sesión); se puede
    # correr contra Neo4j real con GRAPH_BACKEND=neo4j si hay Docker.
    os.environ.setdefault("GRAPH_BACKEND", "memory")
    b = get_backend()
    build_graph(b)
    yield b
    b.close()


@pytest.fixture(scope="module")
def posiciones_df():
    return pd.read_csv(DATA_DIR / "posiciones.csv")


def _exposicion_crediticia(df: pd.DataFrame, cliente_id: str) -> float:
    """Reimplementa el SQL de semantic_layer.yaml (exposicion_crediticia
    v2) sobre el CSV directamente, ya que en F1 todavía no existe DuckDB
    ni el MCP server que resuelve la métrica (eso es F2)."""
    cli = df[df["cliente_id"] == cliente_id]
    fecha_max = cli["fecha"].max()
    fila = cli[cli["fecha"] == fecha_max]
    return float((fila["saldo_dispuesto"] + fila["cupo_comprometido"]).sum())


# --------------------------------------------------------------------------
# Sanity checks del grafo cargado
# --------------------------------------------------------------------------
def test_grafo_negocio_tiene_los_nodos_esperados(backend):
    assert backend.count_nodes(DB_NEGOCIO, "Cliente") == 12
    assert backend.count_nodes(DB_NEGOCIO, "Producto") == 6
    assert backend.count_nodes(DB_NEGOCIO, "Cuenta") == 6
    assert backend.count_nodes(DB_NEGOCIO, "Servicio") == 5
    assert backend.count_nodes(DB_NEGOCIO, "Accion") == 1
    assert backend.count_nodes(DB_NEGOCIO, "Metrica") == 2


def test_grafo_infra_tiene_los_nodos_esperados(backend):
    assert backend.count_nodes(DB_INFRA, "Servicio") == 5
    assert backend.count_nodes(DB_INFRA, "BaseDatos") == 4
    assert backend.count_nodes(DB_INFRA, "Persona") == 6
    assert backend.count_nodes(DB_INFRA, "Cambio") == 1


def test_pay_db_01_existe_en_infra(backend):
    node = backend.get_node(DB_INFRA, "PAY-DB-01")
    assert node is not None
    assert node["motor"] == "Oracle"
    assert node["ambiente"] == "prod"


# --------------------------------------------------------------------------
# Salto 1: ¿qué servicios dependen de PAY-DB-01?
# --------------------------------------------------------------------------
def test_pagos_core_depende_de_pay_db_01(backend):
    dependientes = {
        n["id"] for n in backend.neighbors(DB_INFRA, "PAY-DB-01", "DEPENDE_DE", direction="in")
    }
    assert "pagos-core" in dependientes


def test_nomina_batch_tambien_depende_de_pay_db_01(backend):
    """Hallazgo colateral (plan_demo.md §3.4): nomina-batch depende de la
    misma base de datos que pagos-core, aunque tiene su propio servicio."""
    dependientes = {
        n["id"] for n in backend.neighbors(DB_INFRA, "PAY-DB-01", "DEPENDE_DE", direction="in")
    }
    assert "nomina-batch" in dependientes

    # también en la dirección "out" desde nomina-batch
    destinos = {n["id"] for n in backend.neighbors(DB_INFRA, "nomina-batch", "DEPENDE_DE", direction="out")}
    assert "PAY-DB-01" in destinos


def test_laura_gomez_es_responsable_de_pagos_core(backend):
    responsables = backend.neighbors(DB_INFRA, "pagos-core", "ES_RESPONSABLE", direction="out")
    nombres = {n["nombre"] for n in responsables}
    assert "Laura Gómez" in nombres


# --------------------------------------------------------------------------
# Salto 2: ¿qué productos usan pagos-core?
# --------------------------------------------------------------------------
def test_productos_que_se_ejecutan_en_pagos_core(backend):
    productos = {
        n["id"] for n in backend.neighbors(DB_NEGOCIO, "pagos-core", "SE_EJECUTA_EN", direction="in")
    }
    # cash management (PROD04) y crédito rotativo (PROD01)
    assert productos == {"PROD04", "PROD01"}


# --------------------------------------------------------------------------
# Salto 3 + 4: ¿qué clientes corporativos tienen esos productos, y con
# qué exposición crediticia?
# --------------------------------------------------------------------------
def test_ruta_completa_pay_db_01_a_tres_clientes_corporativos(backend, posiciones_df):
    productos_pagos_core = {
        n["id"] for n in backend.neighbors(DB_NEGOCIO, "pagos-core", "SE_EJECUTA_EN", direction="in")
    }

    clientes_ids: set[str] = set()
    for producto_id in productos_pagos_core:
        for cliente in backend.neighbors(DB_NEGOCIO, producto_id, "TIENE", direction="in"):
            clientes_ids.add(cliente["id"])

    clientes_corporativos = []
    for cliente_id in clientes_ids:
        node = backend.get_node(DB_NEGOCIO, cliente_id)
        if node["segmento"] == "corporativo":
            clientes_corporativos.append(node)

    assert len(clientes_corporativos) == 3
    assert {c["id"] for c in clientes_corporativos} == {"CLI01", "CLI02", "CLI03"}

    for cliente in clientes_corporativos:
        exposicion = _exposicion_crediticia(posiciones_df, cliente["id"])
        assert exposicion > EXPOSICION_MINIMA, (
            f"{cliente['nombre']} ({cliente['id']}) tiene exposición "
            f"{exposicion:,.0f} COP, se esperaba > {EXPOSICION_MINIMA:,.0f}"
        )


def test_accion_retrieve_customer_position_expuesta_por_clientes(backend):
    node = backend.get_node(DB_NEGOCIO, "retrieve_customer_position")
    assert node is not None
    assert node["expuesta_por"] == "Cliente"

    expone = backend.neighbors(DB_NEGOCIO, "CLI01", "EXPONE_ACCION", direction="out")
    assert any(n["id"] == "retrieve_customer_position" for n in expone)


def test_metrica_exposicion_crediticia_version_2(backend):
    node = backend.get_node(DB_NEGOCIO, "exposicion_crediticia")
    assert node is not None
    assert str(node["version"]) == "2"
    assert node["owner"] == "Riesgo"
