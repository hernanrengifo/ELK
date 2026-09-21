# ekl-demo — Demo funcional EKL (Enterprise Knowledge Layer)

Demo funcional de la arquitectura descrita en `plan_demo.md`: un agente
responde una pregunta multi-hop navegando un grafo de conocimiento
gobernado, ejecutando acciones por MCP y delegando en otro agente por
A2A — con permisos aplicados y ruta auditable.

**Está completa** (F0 a F7). Para presentarla, lo único que hace falta
leer es esto y [`docs/GUION.md`](docs/GUION.md).

## Levantar todo desde cero (5 minutos)

```bash
git clone <repo> && cd ekl-demo
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt        # ~3 min (pyarrow es lo que tarda)

./scripts/arrancar.sh                  # levanta los 4 servicios y espera
```

Cuando termine imprime:

```
  Pantalla principal   http://localhost:8501
  Sala de control      http://localhost:8020/control
```

Abre la primera, pulsa **Ejecutar**, y ya está corriendo la demo. **No
hace falta API key ni Docker**: `LLM_MODEL=fake` es el valor por defecto
y corre el sistema entero (MCP, RBAC, A2A, DuckDB) con el razonamiento
del modelo en modo determinista.

Para apagarlo: `./scripts/arrancar.sh --parar`

### Con el modelo real

```bash
export ANTHROPIC_API_KEY=sk-ant-...
LLM_MODEL=claude-sonnet-5 ./scripts/arrancar.sh
```

> La demo **no manda `temperature`**: `claude-sonnet-5` y los modelos
> posteriores responden `400 · temperature is deprecated for this model`
> y la corrida se cae en el primer paso del planner. Si usas un modelo
> antiguo y quieres fijarla, `EKL_LLM_TEMPERATURE=0`.

### Con Neo4j y Redis en vez de memoria

```bash
docker compose up -d neo4j redis        # ~30 s la primera vez
GRAPH_BACKEND=neo4j python data/load_graph.py
GRAPH_BACKEND=neo4j EVENT_BUS=redis ./scripts/arrancar.sh
```

Con los dos agentes proyectando la Sala de control, **usa
`EVENT_BUS=redis`**: con el bus en memoria cada proceso numera los
eventos por su cuenta y la secuencia sale desordenada.

### Comprobar que todo está bien antes de presentar

```bash
pytest tests/ -q                       # 66 tests, 6 s
python scripts/demo_smoke.py           # las 3 preguntas y los 2 roles
python scripts/medir_tiempos.py        # 3 corridas cronometradas
```

Los dos últimos levantan y apagan su propio Agente B. Si la demo ya está
corriendo, añade `--reusar-agente-b` para que usen el que hay en vez de
abortar.

## Para presentar

| Documento | Para qué |
|-----------|----------|
| [`docs/GUION.md`](docs/GUION.md) | El guion minuto a minuto: qué pantalla y qué se dice. Incluye las respuestas de una línea a las preguntas difíciles. |
| [`docs/PLAN_B.md`](docs/PLAN_B.md) | Qué hacer sin internet — tres niveles de degradación, todos probados. |
| [`docs/demo_decisions.md`](docs/demo_decisions.md) | Qué se simplificó respecto a producción y qué no. Para responder "¿esto funciona de verdad?". |
| [`docs/screenshots/`](docs/screenshots/) | Capturas de cada pestaña. |

## Qué hay dentro

**F0 + F1 — esqueleto, datos, ontología y capa semántica** (sesión 1)

- `docker-compose.yml` — Neo4j 5 (dos bases: `negocio` e `infra`) +
  APOC, y Redis (bus de eventos).
- `data/ontology.yaml`, `data/semantic_layer.yaml`, `data/policies.yaml`
  — modelo de datos, capa semántica y gobernanza (plan §3).
- `data/*.csv` — datos sintéticos (clientes, productos, cuentas,
  servicios, bases de datos, personas, cambios, posiciones y aristas).
- `data/runbook_pagos.md` — runbook en lenguaje natural (insumo de F6).
- `data/graph_backend.py` / `data/load_graph.py` — carga del grafo, con
  backend intercambiable `GRAPH_BACKEND=neo4j|memory`.
- `tests/test_data.py` — valida la ruta de la pregunta hilo conductor.

**F2 — servidores MCP** (sesión 2, plan §4.3 y §4.4)

- `mcp/ekl_graph_server.py` — servidor MCP `ekl-graph`, **parametrizado
  por base** (`negocio` | `infra`): `get_schema`, `find_entry_nodes`,
  `read_cypher` (solo lectura, con filtro RBAC inyectado) y
  `list_node_actions` (contrato **sin** el campo `backend`).
- `mcp/ekl_actions_server.py` — servidor MCP `ekl-actions`:
  `resolve_metric` y `retrieve_customer_position` sobre **DuckDB**,
  devolviendo valor + linaje y verificando la política de la acción
  contra el rol.
- `mcp/ekl_middleware.py` — middleware común a los dos servidores: emite
  `tool_call`, `tool_result` y `policy_decision` al bus y escribe
  `audit.jsonl`.
- `mcp/ekl_policies.py` — lectura de `policies.yaml` / `ontology.yaml` /
  `semantic_layer.yaml` con recarga en caliente por `mtime`.
- `mcp/ekl_cypher.py` — subconjunto de Cypher de solo lectura, para que
  `read_cypher` funcione igual con Neo4j y con el grafo en memoria.

**F2b — bus de eventos y Sala de control** (sesión 2, plan §4.8)

- `observability/events.py` — esquema Pydantic del evento
  (`run_id, ts, seq, source, kind, step, payload, meta`).
- `observability/event_bus.py` — dos backends por `EVENT_BUS`:
  `redis` (Redis Streams) y `memory` (cola in-process).
- `observability/trace_store.py` — persistencia por `run_id`
  (`trace.jsonl` + `observability/traces/<run_id>.jsonl`) y **endpoint
  SSE en el puerto 8020** (FastAPI + sse-starlette). Rechaza eventos sin
  `run_id`.
- `tests/test_mcp.py` — criterio de cierre de la fase.

**F3 + F4 — los dos agentes** (sesión 3, plan §4.5 y §4.6)

- `agents/agent_b/` — Agente de Infraestructura: servidor **A2A** en el
  puerto 8002 con `/.well-known/agent.json` (skills `impacto_cambio` y
  `responsable_servicio`) y `tasks/send` por JSON-RPC. Dentro, un
  LangGraph de dos nodos sobre el MCP `ekl-graph` en la base `infra`.
  Devuelve solo el contrato acordado, con linaje.
- `agents/agent_a/` — Orquestador de Negocio: LangGraph con los **siete
  nodos** del §4.5 y el bucle de refinamiento del nodo crítico, servido
  en el puerto 8001 con `POST /ask`.
- `agents/llm.py` / `agents/llm_fake.py` — el modelo se lee de
  `LLM_MODEL`; `fake` corre todo el flujo **sin API key ni red**.
- `agents/a2a.py` — subconjunto propio del protocolo A2A (por qué, en
  `STATUS.md`).
- `scripts/demo_smoke.py` — las tres preguntas del guion, con los dos
  roles.

**F6 — extracción desde documentos** (plan §4.2)

- `data/extract_from_docs.py` — lee `runbook_pagos.md`, pide al LLM las
  tripletas `(servicio, DEPENDE_DE, base_de_datos)`, las canonicaliza
  contra los nodos existentes y escribe la arista con
  `fuente: "runbook_pagos.md#L24"`. Idempotente.

```bash
LLM_MODEL=fake python data/extract_from_docs.py
```

Gracias a eso, la advertencia sobre `nomina-batch` en la respuesta del
agente **cita la línea del runbook** donde está escrita.

**F5 + F5b — UI y Sala de control** (sesión 4, plan §4.7 y §4.8)

- `ui/app.py` — Streamlit en el 8501: rol, pregunta y las seis pestañas
  (Respuesta · Ruta · Evidencia y linaje · Auditoría · Capa semántica ·
  Sala de control).
- `ui/control_room.html` — la **Sala de control**, servida por el
  trace_store en `/control` y embebida en su pestaña: diagrama de
  secuencia vivo, cadena de razonamiento con los pasos del Agente B
  anidados, plan y nodo crítico en vivo, y gobernanza. Con reproducción,
  comparación de corridas y exportación.
- `ui/grafo_svg.py`, `ui/datos_ruta.py` — el grafo recorrido de la
  pestaña Ruta.
- `traces/referencia_*.jsonl` — trazas de referencia para el plan B.
- `docs/screenshots/` — las capturas de cada pestaña.

**F7 — ensayo**

- `scripts/arrancar.sh` — levanta la demo entera con un comando.
- `scripts/medir_tiempos.py` — tres corridas cronometradas contra el
  presupuesto de 90 s del §10, y las mitigaciones si no se cumple.
- `docs/GUION.md`, `docs/PLAN_B.md`, `docs/demo_decisions.md`.

Ver `STATUS.md` para el detalle de cada fase y lo que quedó pendiente.

## Arranque rápido (sin Docker) — recomendado para probar

```bash
cd ekl-demo
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

GRAPH_BACKEND=memory python data/load_graph.py
pytest tests/ -v          # 40 tests, sin API key y sin Docker
```

## Los cuatro procesos

`arrancar.sh` levanta estos cuatro; si prefieres verlos por separado:

```bash
LLM_MODEL=fake python agents/agent_b/server.py &      # 8002 — Agente B (A2A)
LLM_MODEL=fake python agents/agent_a/server.py &      # 8001 — Agente A
python observability/trace_store.py &                 # 8020 — traza + Sala de control
streamlit run ui/app.py --server.port 8501            # 8501 — UI
```

Ejecuta las tres preguntas del guion (§5) con rol `riesgo` y la
principal con `analista_junior`, y comprueba el resultado esperado del
§3.4. Levanta y apaga él mismo el Agente B.

Para preguntar a mano, con los dos agentes vivos:

```bash
LLM_MODEL=fake python agents/agent_b/server.py &   # puerto 8002
LLM_MODEL=fake python agents/agent_a/server.py &   # puerto 8001

curl -s -X POST http://127.0.0.1:8001/ask -H 'content-type: application/json' -d '{
  "query": "¿Qué clientes corporativos con exposición crediticia mayor a 5.000 millones se verían afectados si migramos la base de datos del servicio de pagos el próximo fin de semana, y quién es el responsable técnico de ese servicio hoy?",
  "user_role": "riesgo"}' | python -m json.tool
```

Cambiando `"user_role"` a `"analista_junior"` desaparecen los clientes
de sensibilidad alta y la acción que calcula la exposición se deniega:
es el momento "wow" #2 del guion.

> **Antes de cada corrida, comprueba que no queda un agente vivo de una
> prueba anterior.** Seguiría atendiendo en su puerto y escribiendo sus
> trazas donde lo arrancaron, y la corrida parecería buena con la traza
> incompleta. `./scripts/demo_reset.sh` avisa si los detecta;
> `pkill -f 'agents/agent_'` los apaga.

## Con un modelo real

```bash
export ANTHROPIC_API_KEY=sk-ant-...
export LLM_MODEL=claude-sonnet-5        # o el que quieras: es agnóstico
python scripts/demo_smoke.py
```

Con un modelo real el smoke comprueba **los hechos** (qué clientes, qué
cifras, qué responsable, qué se podó) contra la evidencia y la ruta, y
solo avisa —no falla— si la redacción varía. `--solo-principal` corre
únicamente la pregunta hilo conductor, que es más rápido.

## La Sala de control (§4.8)

Cuatro zonas, en `http://localhost:8020/control` o en la pestaña
homónima de la UI:

1. **Secuencia** — carriles Usuario · Agente A · MCP graph · MCP actions
   · Agente B · Datos, y una flecha por evento: azul MCP, ámbar A2A,
   rojo podado o denegado, verde respuesta. Clic en una flecha para ver
   su payload.
2. **Cadena de razonamiento** — el `{razonamiento, decision}` de cada
   paso, con los del Agente B anidados bajo su flecha A2A.
3. **Plan y nodo crítico** — las sub-preguntas cambiando de estado, los
   huecos que detecta el crítico y su directiva de refinamiento.
4. **Gobernanza en vivo** — llamadas MCP, tareas A2A, nodos filtrados,
   acciones denegadas, latencia, tokens y costo estimado.

Controles: selector de corrida, **● en vivo** (SSE), **▶ reproducir**
con velocidad de 0,25× a 4× y pausa (barra espaciadora), **⇋ comparar**
dos corridas lado a lado, filtros por fuente y tipo, y **⤓ trace.jsonl**.

### Plan B: reproducir sin LLM ni agentes

`traces/referencia_riesgo.jsonl` y `traces/referencia_junior.jsonl` son
dos corridas guardadas. Aparecen en el selector marcadas con ★ y se
reproducen **con solo el trace_store levantado** — sin agentes, sin
API key y sin Docker. Es el plan B del §8 si falla la red en la sala.

```bash
python observability/trace_store.py          # y abre :8020/control
```

Para regenerarlas tras un cambio en los agentes:

```bash
LLM_MODEL=fake python scripts/generar_trazas_referencia.py
```

## Los dos agentes

| | Agente A | Agente B |
|---|---|---|
| Papel | Orquestador de Negocio | Infraestructura y TI |
| Puerto | 8001 | 8002 |
| Entrada | `POST /ask {query, user_role}` | `POST /a2a` (`tasks/send`) |
| Grafo | LangGraph, 7 nodos (§4.5) | LangGraph, 2 nodos (§4.6) |
| Datos | MCP `ekl-graph` (base `negocio`) + `ekl-actions` | MCP `ekl-graph` (base `infra`) |
| Descubrimiento | lee el agent card de B | publica `/.well-known/agent.json` |

El Agente A delega en B cuando el plan o el nodo crítico detectan un
nodo frontera. Lo que cruza la frontera es la sub-pregunta, el id del
objeto y la identidad del usuario — nada más; el evento `a2a_request`
lleva una lista `no_se_envia` con lo que deliberadamente se queda en
casa.

```bash
curl -s http://127.0.0.1:8002/.well-known/agent.json | python -m json.tool
```

`GRAPH_BACKEND=memory` y `EVENT_BUS=memory` son los valores por defecto:
no hace falta exportarlos.

Después de correr los tests, la traza de la corrida queda en
`trace.jsonl` (y una por ejecución en `observability/traces/`), y la
auditoría en `audit.jsonl`.

## Sala de control (trace_store, puerto 8020)

```bash
python observability/trace_store.py
# equivalente: uvicorn observability.trace_store:app --port 8020
```

| Endpoint | Para qué |
|----------|----------|
| `GET /health` | estado, backend del bus y ruta de `trace.jsonl` |
| `GET /stream?run_id=<id>` | **SSE en vivo** de una ejecución |
| `GET /stream/<run_id>` | SSE con reproducción de lo ya ocurrido + lo nuevo |
| `GET /runs` | `run_id`s presentes en la traza |
| `GET /runs/<run_id>` | todos los eventos de esa ejecución (reproducción) |
| `POST /events` | ingesta HTTP; **422 si el evento no trae `run_id`** |

```bash
curl -N "http://127.0.0.1:8020/stream?run_id=demo-1"   # en una terminal
curl -X POST http://127.0.0.1:8020/events -H 'content-type: application/json' \
  -d '{"run_id":"demo-1","source":"agent_a","kind":"thought","step":"planner",
       "payload":{"razonamiento":"primero resuelvo la métrica"}}'
```

Con `EVENT_BUS=redis` (`docker compose up -d redis`) el bus usa Redis
Streams y este proceso es el que consume el stream y escribe la traza;
con `EVENT_BUS=memory` la escribe el propio proceso emisor.

> **Para una corrida con los dos agentes, usa `EVENT_BUS=redis`.** Con
> el bus en memoria cada proceso lleva su propio contador de `seq`, así
> que el Agente A y el Agente B numeran los dos desde 1 dentro del mismo
> `run_id` y la Sala de control dibujaría la secuencia desordenada. Con
> Redis el contador es compartido (`INCR ekl:seq:<run_id>`) y el `seq`
> queda global. El modo `memory` es para los tests y para desarrollo en
> un solo proceso.

## Cómo probar con MCP Inspector

Los dos servidores hablan **stdio** por defecto, que es lo que espera
MCP Inspector. Necesitas Node (para `npx`); no necesitas Docker.

### Interfaz web

```bash
cd ekl-demo
npx @modelcontextprotocol/inspector .venv/bin/python mcp/ekl_graph_server.py --base negocio
```

Abre la URL que imprime, pulsa **Connect** y luego **List Tools**. Para
la base de infraestructura y para el servidor de acciones:

```bash
npx @modelcontextprotocol/inspector .venv/bin/python mcp/ekl_graph_server.py --base infra
npx @modelcontextprotocol/inspector .venv/bin/python mcp/ekl_actions_server.py
```

En la UI, el `run_id` y el rol se mandan en **Meta** (el `_meta` de la
llamada), como los manda el agente:

```json
{ "run_id": "demo-1", "user_ctx": { "usuario": "ana", "rol": "analista_junior" } }
```

Si no mandas nada, el servidor genera un `run_id` (`inspector-xxxxxxxx`,
marcado como `run_id_generado: true` en `audit.jsonl`) y usa el rol de
`DEMO_USER_ROLE` o el `rol_por_defecto` de `policies.yaml`.

### Línea de comandos (lo que se usó para verificar esta fase)

```bash
# 1) Listar herramientas y ver sus esquemas
npx @modelcontextprotocol/inspector --cli .venv/bin/python mcp/ekl_graph_server.py --base negocio \
  --method tools/list
```

```bash
# 2) RBAC en vivo: mismo query, dos roles. Con analista_junior se podan
#    CLI01 y CLI05 (los `sensibilidad: alta` de data/clientes.csv)
npx @modelcontextprotocol/inspector --cli .venv/bin/python mcp/ekl_graph_server.py --base negocio \
  --method tools/call --tool-name read_cypher \
  --tool-arg "query=MATCH (c:Cliente) RETURN c.id AS id" \
  --tool-metadata run_id=demo-1 user_ctx=analista_junior
```

Devuelve 10 filas y `"nodos_filtrados_por_politica": ["CLI01","CLI05"]`.
Cambiando a `user_ctx=riesgo` devuelve las 12.

```bash
# 3) Acción gobernada: valor + linaje
npx @modelcontextprotocol/inspector --cli .venv/bin/python mcp/ekl_actions_server.py \
  --method tools/call --tool-name retrieve_customer_position \
  --tool-arg customer_ref=CLI02 --tool-arg metrica=exposicion_crediticia \
  --tool-metadata run_id=demo-1 user_ctx=riesgo
```

```bash
# 4) La misma acción con un rol sin permiso: se rechaza con la razón
npx @modelcontextprotocol/inspector --cli .venv/bin/python mcp/ekl_actions_server.py \
  --method tools/call --tool-name retrieve_customer_position \
  --tool-arg customer_ref=CLI02 --tool-arg metrica=exposicion_crediticia \
  --tool-metadata run_id=demo-1 user_ctx=analista_junior
```

```bash
# 5) El contrato de la acción NO trae `backend` (plan §3.2)
npx @modelcontextprotocol/inspector --cli .venv/bin/python mcp/ekl_graph_server.py --base negocio \
  --method tools/call --tool-name list_node_actions --tool-arg node_id=CLI02 \
  --tool-metadata run_id=demo-1 user_ctx=riesgo
```

> **Ojo con el entorno.** MCP Inspector **no** hereda las variables de tu
> shell: el proceso del servidor las recibe solo si las pasas con `-e`
> (`-e DEMO_USER_ROLE=analista_junior -e GRAPH_BACKEND=neo4j`). Por eso
> el rol se manda mejor en `--tool-metadata`, que sí viaja en la llamada.

Cada una de estas llamadas deja su rastro: mira `audit.jsonl` y
`trace.jsonl` mientras juegas con el Inspector.

```bash
tail -f audit.jsonl
```

### Alternativa: `mcp dev` (SDK oficial)

```bash
pip install "mcp[cli]"
EKL_GRAPH_BASE=infra mcp dev mcp/ekl_graph_server.py:servidor
mcp dev mcp/ekl_actions_server.py:servidor
```

### Por HTTP en vez de stdio

```bash
python mcp/ekl_graph_server.py   --base negocio --transport http --port 8011
python mcp/ekl_actions_server.py --transport http --port 8010
npx @modelcontextprotocol/inspector --cli http://127.0.0.1:8011/mcp --method tools/list
```

## Arranque con Neo4j real (Docker)

> **Nota:** se usa la imagen `neo4j:5-enterprise` (con
> `NEO4J_ACCEPT_LICENSE_AGREEMENT=yes`, licencia de evaluación/desarrollo
> gratuita) porque tener **dos bases de datos** (`negocio`, `infra`) en
> el mismo servidor requiere Neo4j Enterprise — Community solo soporta
> una base de usuario. Ver `STATUS.md`.

```bash
cd ekl-demo
cp .env.example .env   # y ajusta credenciales si quieres

docker compose up -d neo4j redis
# espera ~20-30s a que Neo4j quede healthy (docker compose ps)

pip install -r requirements.txt
GRAPH_BACKEND=neo4j python data/load_graph.py

# Sala de control consumiendo el stream de Redis (proceso aparte)
EVENT_BUS=redis python observability/trace_store.py &

# Servidores MCP contra Neo4j, publicando a Redis
GRAPH_BACKEND=neo4j EVENT_BUS=redis python mcp/ekl_graph_server.py --base negocio
GRAPH_BACKEND=neo4j EVENT_BUS=redis python mcp/ekl_actions_server.py
```

Con `GRAPH_BACKEND=neo4j` los servidores MCP **no recargan** el grafo al
arrancar (ya lo cargó `load_graph.py`); para forzarlo,
`EKL_RELOAD_GRAPH=1`.

La suite pasa entera contra Neo4j real:

```bash
GRAPH_BACKEND=neo4j pytest tests/ -q
```

Luego, en **Neo4j Browser** (http://localhost:7474, base `negocio`):

```cypher
// Ruta de negocio: pagos-core (referencia) <- productos <- clientes corporativos
MATCH (c:Cliente {segmento: "corporativo"})-[:TIENE]->(p:Producto)-[:SE_EJECUTA_EN]->(s:Servicio {id: "pagos-core"})
RETURN c.nombre, c.id, p.nombre
```

Y cambiando a la base `infra` (`:use infra`):

```cypher
// Servicios que dependen de PAY-DB-01 (incluye el hallazgo colateral nomina-batch)
MATCH (s:Servicio)-[:DEPENDE_DE]->(bd:BaseDatos {id: "PAY-DB-01"})
RETURN s.id, s.nombre
```

Debe devolver `pagos-core` y `nomina-batch`.

## Reset

```bash
GRAPH_BACKEND=memory ./scripts/demo_reset.sh
# o, con Neo4j y Redis reales:
GRAPH_BACKEND=neo4j EVENT_BUS=redis ./scripts/demo_reset.sh
```

Recarga el grafo y borra `audit.jsonl`, `trace.jsonl`, las trazas por
`run_id` y (con `EVENT_BUS=redis`) el stream de Redis.

## Variables de entorno

Ver `.env.example`, agrupadas por fase. Las más usadas:

| Variable | Valores | Para qué |
|----------|---------|----------|
| `GRAPH_BACKEND` | `memory` (def.) / `neo4j` | dónde vive el grafo |
| `EVENT_BUS` | `memory` (def.) / `redis` | bus de eventos |
| `DEMO_USER_ROLE` | `direccion` / `riesgo` / `analista_junior` | rol cuando la llamada MCP no trae `user_ctx` |
| `EKL_GRAPH_BASE` | `negocio` (def.) / `infra` | base que sirve `ekl-graph` |
| `TRACE_STORE_PORT` | `8020` (def.) | puerto del SSE |
| `EKL_RELOAD_GRAPH` | `1` | fuerza recargar el grafo al arrancar un servidor MCP |
| `LLM_MODEL` | `fake` (def.) / `claude-sonnet-5` / … | modelo de los agentes |
| `ANTHROPIC_API_KEY` | — | solo si `LLM_MODEL` no es `fake` |
| `AGENT_B_URL` | `http://127.0.0.1:8002` | dónde busca A el agent card de B |
| `EKL_MCP_GRAPH_URL` / `EKL_MCP_ACTIONS_URL` | URL | hablar con servidores MCP ya levantados en vez de instanciarlos en proceso |
| `UI_TIMEOUT` | `300` (def.) | segundos que la UI espera al Agente A |
| `LLM_MODEL_PLANNER` / `LLM_MODEL_CRITICO` | modelo | modelo rápido solo para esos pasos (mitigación de latencia del §8) |
| `EKL_CACHE_PLAN` | `1` | cachea el plan del planner (apagado: oculta el razonamiento que la demo quiere enseñar) |
| `EKL_LLM_TEMPERATURE` | `0` | fija la temperatura; **no se manda por defecto** porque los modelos nuevos la rechazan |
| `EKL_LLM_MAX_TOKENS` | `2048` (def.) | tope de tokens de salida por llamada |

## Estructura

```
ekl-demo/
├── docker-compose.yml, .env.example, requirements.txt
├── data/
│   ├── ontology.yaml, semantic_layer.yaml, policies.yaml
│   ├── graph_backend.py, load_graph.py
│   ├── *.csv, runbook_pagos.md
├── mcp/                        # OJO: sin __init__.py (ver STATUS.md)
│   ├── ekl_graph_server.py     # MCP ekl-graph (negocio | infra)
│   ├── ekl_actions_server.py   # MCP ekl-actions (DuckDB)
│   ├── ekl_middleware.py       # eventos + audit.jsonl
│   ├── ekl_policies.py         # RBAC y capa semántica
│   └── ekl_cypher.py           # Cypher de solo lectura (subconjunto)
├── agents/
│   ├── modelos.py              # AgentState + salidas estructuradas
│   ├── llm.py, llm_fake.py     # LLM por LLM_MODEL; simulado sin API key
│   ├── eventos.py              # emisión al bus de F2b (sin esquema nuevo)
│   ├── mcp_cliente.py          # cliente MCP con run_id y user_ctx
│   ├── a2a.py                  # subconjunto propio de A2A
│   ├── agent_a/                # grafo.py, verificacion.py, server.py :8001
│   └── agent_b/                # grafo.py, server.py :8002
├── ui/
│   ├── app.py                  # Streamlit :8501, las 6 pestañas
│   ├── control_room.html       # Sala de control (se sirve en :8020/control)
│   ├── grafo_svg.py            # dibujo del grafo recorrido
│   └── datos_ruta.py           # aristas: MCP para negocio, A2A para infra
├── observability/
│   ├── events.py, event_bus.py, trace_store.py
│   └── traces/<run_id>.jsonl   # salida de trabajo (la limpia demo_reset)
├── traces/referencia_*.jsonl   # trazas curadas del plan B (se versionan)
├── scripts/
│   ├── arrancar.sh             # levanta / apaga la demo entera
│   ├── demo_reset.sh, demo_smoke.py, medir_tiempos.py
│   ├── generar_trazas_referencia.py
│   └── capturar_pantallas.py
├── docs/
│   ├── GUION.md, PLAN_B.md, demo_decisions.md
│   └── screenshots/
├── tests/test_data.py, tests/test_mcp.py, tests/test_agents.py
├── audit.jsonl, trace.jsonl    # se generan al correr
└── STATUS.md
```
