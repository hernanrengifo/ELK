#!/usr/bin/env bash
# demo_reset.sh — recarga los datos del grafo y limpia la auditoría y las
# trazas (plan_demo.md §4.9).
#
# Desde la sesión 2 (F2/F2b) los servidores MCP escriben `audit.jsonl` y
# `trace.jsonl` en la raíz del repo, más una traza por ejecución en
# `observability/traces/<run_id>.jsonl`. Este script los borra todos para
# que la demo empiece con la Sala de control en blanco.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${ROOT_DIR}"

GRAPH_BACKEND="${GRAPH_BACKEND:-memory}"
EVENT_BUS="${EVENT_BUS:-memory}"
PYTHON="${PYTHON:-python3}"
if [ -x "${ROOT_DIR}/.venv/bin/python" ]; then
  PYTHON="${ROOT_DIR}/.venv/bin/python"
fi

echo "== demo_reset.sh =="
echo "Raíz del proyecto: ${ROOT_DIR}"
echo "GRAPH_BACKEND=${GRAPH_BACKEND}  EVENT_BUS=${EVENT_BUS}"

if [ "${GRAPH_BACKEND}" = "neo4j" ]; then
  echo "-- Reiniciando bases de Neo4j (negocio, infra) --"
  echo "   (asegúrate de tener 'docker compose up -d neo4j' corriendo)"
fi

echo "-- Recargando el grafo (data/load_graph.py) --"
"${PYTHON}" "${ROOT_DIR}/data/load_graph.py"

# Un proceso de una prueba anterior sigue atendiendo en su puerto y
# escribiendo donde lo arrancaron: la corrida nueva parece buena y las
# trazas se van a otro sitio. Conviene avisarlo antes de empezar.
for proc in "agents/agent_a/server.py:Agente A" \
            "agents/agent_b/server.py:Agente B" \
            "observability/trace_store.py:trace_store"; do
  patron="${proc%%:*}"
  nombre="${proc##*:}"
  if pgrep -f "${patron}" >/dev/null 2>&1; then
    echo "-- AVISO: hay un ${nombre} corriendo (${patron}) --"
    echo "   Escribe sus trazas donde lo arrancaron, no donde arranques la demo."
    echo "   Para empezar limpio:  pkill -f '${patron}'"
  fi
done

echo "-- Limpiando auditoría y trazas --"
for f in audit.jsonl trace.jsonl observability/trace.jsonl; do
  if [ -f "${ROOT_DIR}/${f}" ]; then
    rm -f "${ROOT_DIR}/${f}"
    echo "   eliminado: ${f}"
  fi
done

if [ -d "${ROOT_DIR}/observability/traces" ]; then
  n=$(find "${ROOT_DIR}/observability/traces" -name '*.jsonl' -type f | wc -l | tr -d ' ')
  find "${ROOT_DIR}/observability/traces" -name '*.jsonl' -type f -delete
  echo "   eliminadas ${n} traza(s) por run_id en observability/traces/"
fi

if [ "${EVENT_BUS}" = "redis" ]; then
  echo "-- Limpiando el stream de eventos en Redis --"
  # redis-cli local si está; si no, el del contenedor de docker compose
  # (que es el caso normal: nadie instala redis-cli solo para la demo).
  if command -v redis-cli >/dev/null 2>&1; then
    REDIS_CLI="redis-cli"
  elif docker compose ps --status running --services 2>/dev/null | grep -qx redis; then
    REDIS_CLI="docker compose exec -T redis redis-cli"
  else
    REDIS_CLI=""
  fi

  if [ -n "${REDIS_CLI}" ]; then
    ${REDIS_CLI} DEL ekl:events >/dev/null 2>&1 || true
    # shellcheck disable=SC2086
    ${REDIS_CLI} --scan --pattern 'ekl:seq:*' 2>/dev/null | tr -d '\r' | while read -r k; do
      [ -n "${k}" ] && ${REDIS_CLI} DEL "${k}" >/dev/null 2>&1 || true
    done
    echo "   stream ekl:events y contadores ekl:seq:* borrados"
  else
    echo "   no hay redis-cli ni contenedor 'redis' corriendo: no se limpió el stream"
  fi
fi

echo "== demo_reset.sh: listo =="
