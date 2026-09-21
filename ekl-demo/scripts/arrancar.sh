#!/usr/bin/env bash
# arrancar.sh — levanta la demo entera y espera a que responda.
#
#   ./scripts/arrancar.sh              # con LLM simulado (sin API key)
#   LLM_MODEL=claude-sonnet-5 ./scripts/arrancar.sh
#   ./scripts/arrancar.sh --parar      # apaga todo
#
# Arranca cuatro procesos —Agente B, Agente A, trace_store y la UI— y no
# devuelve el control hasta que los cuatro responden. Si algo no levanta,
# dice cuál y dónde está su log, en vez de dejarte con una pantalla en
# blanco a mitad de la demo.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${ROOT_DIR}"

LOGS="${ROOT_DIR}/.logs"
PY="${ROOT_DIR}/.venv/bin/python"
[ -x "${PY}" ] || PY="python3"

VERDE='\033[32m'; ROJO='\033[31m'; GRIS='\033[90m'; FIN='\033[0m'

PROCESOS=(
  "agent_b:agents/agent_b/server.py:8002:/health"
  "agent_a:agents/agent_a/server.py:8001:/health"
  "trace_store:observability/trace_store.py:8020/health"
)

parar() {
  echo "Apagando la demo…"
  pkill -f 'agents/agent_a/server.py'      2>/dev/null && echo "  agent_a"
  pkill -f 'agents/agent_b/server.py'      2>/dev/null && echo "  agent_b"
  pkill -f 'observability/trace_store.py'  2>/dev/null && echo "  trace_store"
  pkill -f 'streamlit run ui/app.py'       2>/dev/null && echo "  ui"
  sleep 1
  echo "Listo."
}

if [ "${1:-}" = "--parar" ] || [ "${1:-}" = "--stop" ]; then
  parar; exit 0
fi

esperar() {  # esperar <url> <nombre> <log> [segundos]
  local url="$1" nombre="$2" log="$3" limite="${4:-45}"
  local fin=$((SECONDS + limite))
  while [ $SECONDS -lt $fin ]; do
    if curl -s -m 2 -o /dev/null "${url}"; then
      printf "  ${VERDE}✓${FIN} %-12s %s\n" "${nombre}" "${url}"
      return 0
    fi
    sleep 0.6
  done
  printf "  ${ROJO}✗${FIN} %-12s no respondió en %ss — mira %s\n" "${nombre}" "${limite}" "${log}"
  tail -5 "${log}" 2>/dev/null | sed 's/^/      /'
  return 1
}

export LLM_MODEL="${LLM_MODEL:-fake}"
export GRAPH_BACKEND="${GRAPH_BACKEND:-memory}"
export EVENT_BUS="${EVENT_BUS:-memory}"

echo "== Demo EKL =="
echo -e "${GRIS}LLM_MODEL=${LLM_MODEL}  GRAPH_BACKEND=${GRAPH_BACKEND}  EVENT_BUS=${EVENT_BUS}${FIN}"

if [ "${LLM_MODEL}" != "fake" ] && [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  echo -e "${ROJO}Falta ANTHROPIC_API_KEY${FIN} y LLM_MODEL=${LLM_MODEL}."
  echo "  exporta la clave, o usa LLM_MODEL=fake para correr sin ella."
  exit 1
fi

# Un proceso viejo en un puerto haría que la demo pareciera funcionar
# mientras las trazas se van a otro sitio. Se apaga antes de empezar.
parar >/dev/null 2>&1
mkdir -p "${LOGS}"

echo "-- Recargando datos y limpiando trazas --"
./scripts/demo_reset.sh >"${LOGS}/reset.log" 2>&1 || {
  echo -e "${ROJO}demo_reset.sh falló${FIN} — mira ${LOGS}/reset.log"; exit 1; }

echo "-- Levantando procesos --"
nohup "${PY}" agents/agent_b/server.py      >"${LOGS}/agent_b.log"     2>&1 &
nohup "${PY}" agents/agent_a/server.py      >"${LOGS}/agent_a.log"     2>&1 &
nohup "${PY}" observability/trace_store.py  >"${LOGS}/trace_store.log" 2>&1 &
nohup "${PY}" -m streamlit run ui/app.py --server.port 8501 \
      --server.headless true --browser.gatherUsageStats false \
      >"${LOGS}/ui.log" 2>&1 &

fallos=0
esperar "http://127.0.0.1:8002/health" "agent_b"     "${LOGS}/agent_b.log"     || fallos=$((fallos+1))
esperar "http://127.0.0.1:8001/health" "agent_a"     "${LOGS}/agent_a.log"     || fallos=$((fallos+1))
esperar "http://127.0.0.1:8020/health" "trace_store" "${LOGS}/trace_store.log" || fallos=$((fallos+1))
esperar "http://127.0.0.1:8501"        "ui"          "${LOGS}/ui.log" 60       || fallos=$((fallos+1))

if [ "${fallos}" -gt 0 ]; then
  echo -e "\n${ROJO}${fallos} servicio(s) no levantaron.${FIN} Logs en ${LOGS}/"
  exit 1
fi

cat <<EOF

  Pantalla principal   http://localhost:8501
  Sala de control      http://localhost:8020/control
$( [ "${GRAPH_BACKEND}" = "neo4j" ] && echo "  Neo4j Browser        http://localhost:7474" )

  Guion: docs/GUION.md          Apagar: ./scripts/arrancar.sh --parar
EOF
