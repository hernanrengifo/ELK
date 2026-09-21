# STATUS — ekl-demo

## Sesión 1 — Esqueleto, datos, ontología y capa semántica (F0 + F1)

**Fecha:** 2026-09-15
**Fases construidas:** F0 (esqueleto) y F1 (datos y ontología), tal
como pide `claude/sesiones_demo.md`. No se construyó nada de F2 en
adelante.

### Qué se construyó

- **F0 — Esqueleto**
  - `docker-compose.yml`: servicios `neo4j` (imagen `neo4j:5-enterprise`,
    con APOC) y `redis`. No incluye `agent-a`, `agent-b`, `mcp-actions`
    ni `ui` porque esos servicios no existen todavía (se agregan en las
    sesiones que los construyen).
  - `.env.example` con todas las variables mencionadas en
    `plan_demo.md` (incluidas las de fases futuras, comentadas por
    fase), para no tener que reescribir este archivo en cada sesión.
  - `README.md` con instrucciones de arranque sin Docker (backend
    memory) y con Docker (Neo4j real).

- **F1 — Datos y ontología**
  - `data/ontology.yaml`: los 8 tipos de nodo del §3.1 con su
    alineación BIAN, las 7 aristas del §3.1, y la sección `acciones:`
    con el nodo `Accion` `retrieve_customer_position` (contrato +
    política) tal como en el ejemplo del §3.2.
  - `data/semantic_layer.yaml`: métricas `exposicion_crediticia` (v2,
    con el SQL exacto del plan) y `cliente_activo` (v1).
  - `data/policies.yaml`: roles, RBAC por `sensibilidad: alta` en
    `Cliente` (poda para `analista_junior`), y las políticas de la
    acción `retrieve_customer_position` (requiere rol `riesgo` o
    `direccion`).
  - CSVs de datos sintéticos: 12 clientes (8 corporativos incluyendo
    los 3 de la ruta esperada, 4 pyme; 2 con `sensibilidad: alta`), 6
    productos, 6 cuentas, 5 servicios, 4 bases de datos, 6 personas
    (incluida Laura Gómez, líder técnico de `pagos-core`), 1 cambio
    (`CHG-2026-0917`), y las 6 aristas correspondientes.
  - `data/posiciones.csv`: 40 filas (cliente × fecha, 3-4 fechas entre
    2026-06-15 y 2026-09-15), montos en COP plausibles para banca
    corporativa colombiana (órdenes de magnitud de cientos de millones
    a ~6.300 millones COP para los corporativos más grandes).
  - `data/runbook_pagos.md`: una página en lenguaje natural que
    menciona explícitamente que `pagos-core` y `nomina-batch` dependen
    de `PAY-DB-01`, y de paso el cambio `CHG-2026-0917` — insumo listo
    para la extracción automática de la sesión 5 (F6).
  - `data/graph_backend.py`: interfaz común `GraphBackend` con dos
    implementaciones (`Neo4jBackend`, `MemoryBackend`/networkx),
    seleccionable con `GRAPH_BACKEND=neo4j|memory`.
  - `data/load_graph.py`: crea constraints, carga ambas bases desde los
    CSV, y crea los nodos `Accion` y `Metrica` (con la arista
    `EXPONE_ACCION` desde cada `Cliente`) leyendo directamente
    `ontology.yaml` y `semantic_layer.yaml` — sin CSVs para estos dos
    tipos de nodo, ya que no son "datos sintéticos" sino definiciones
    gobernadas.
  - `scripts/demo_reset.sh`: recarga el grafo (`load_graph.py`) y
    limpia `audit.jsonl` / `trace.jsonl` si existen (no falla si
    todavía no existen, porque F2/F2b no se ha construido).
  - `tests/test_data.py`: 10 tests. Cubren sanity checks del grafo, el
    salto 1 (`PAY-DB-01 ← pagos-core`, y el hallazgo colateral
    `nomina-batch ← PAY-DB-01`), el responsable (`Laura Gómez`), el
    salto 2 (productos que corren en `pagos-core`), el salto 3+4
    completo (los 3 clientes corporativos exactos —`CLI01`, `CLI02`,
    `CLI03`— con exposición crediticia > 5.000 millones COP,
    recalculada desde `posiciones.csv` con la misma fórmula del SQL en
    `semantic_layer.yaml`), la acción `retrieve_customer_position`
    expuesta por cada cliente, y la métrica `exposicion_crediticia` v2.

### Qué se asumió

1. **Neo4j Enterprise, no Community.** El plan pide "dos bases:
   `negocio` e `infra`" en el mismo servidor Neo4j 5. Neo4j Community
   Edition solo soporta una base de datos de usuario (`neo4j`); tener
   múltiples bases reales requiere Neo4j Enterprise (aquí, la imagen
   `neo4j:5-enterprise` con la licencia de evaluación/desarrollo
   gratuita vía `NEO4J_ACCEPT_LICENSE_AGREEMENT=yes`). Esto no cambia
   el modelo de datos ni los nombres del plan, solo la imagen Docker.
   Si esto no es aceptable para el entorno final de la demo, la
   alternativa sería un solo Neo4j Community con prefijos de label
   (`Negocio_Cliente`, `Infra_Servicio`...) en vez de bases separadas —
   no se implementó porque cambiaría el modelo de datos del plan.
2. **Mapeo producto → servicio.** El plan no especifica qué producto
   corre en qué servicio; se asumió: `cash management` y
   `crédito rotativo` → `pagos-core`; `nómina` → `nomina-batch`;
   `tesorería` → `tesoreria-api`; `CDT` y `leasing` → `onboarding`. Este
   mapeo es el que hace que la ruta de la pregunta hilo conductor dé
   como resultado exactamente los 3 clientes corporativos que pide el
   plan (§3.4).
3. **Qué 3 clientes corporativos y sus montos.** Se asumieron los
   montos exactos en `posiciones.csv` para que la exposición crediticia
   (saldo dispuesto + cupo comprometido, fecha de corte 2026-09-15) de
   `CLI01` (Textiles Andinos), `CLI02` (Grupo Cafetero del Oriente) y
   `CLI03` (Constructora Valle Verde) supere los 5.000 millones COP, y
   que ningún otro cliente corporativo tenga productos que corran en
   `pagos-core` (para que la respuesta sea exactamente 3, no más).
   `CLI01` está marcado `sensibilidad: alta` (útil para el momento
   "wow" #2 de la sesión 4, donde ese cliente debe desaparecer para el
   rol `analista_junior`).
4. **`Accion` y `Metrica` no tienen CSV propio.** Se generan
   programáticamente en `load_graph.py` desde `ontology.yaml` /
   `semantic_layer.yaml`, porque el §3.4 (datos sintéticos) no las
   menciona como datos sintéticos sino como definiciones gobernadas
   del §3.2/§3.3.
5. **`Cuenta` es una cuenta de liquidación por producto, no por
   cliente.** El plan define la arista `SE_LIQUIDA_EN` como
   `Producto→Cuenta` (no `Cliente→Cuenta`), así que se modeló una
   cuenta de liquidación por tipo de producto (6 en total), no una
   cuenta por cada relación cliente-producto.
6. **`requirements.txt` añadido** (no estaba en la lista de
   entregables) para que `pip install -r requirements.txt` sea
   reproducible; solo contiene lo que F0/F1 necesita (`pyyaml`,
   `networkx`, `pandas`, `neo4j`, `pytest`). Las dependencias de fases
   posteriores (`langgraph`, `mcp`, `fastapi`, `streamlit`, `duckdb`,
   `redis`...) se agregan cuando esas fases se construyan.
7. **Sin carpeta local conectada a esta sesión de Cowork.** Este repo
   se construyó en el entorno de trabajo en la nube (no había una
   carpeta del computador del usuario conectada a la sesión), así que
   además del ZIP entregado en la conversación, todos los archivos se
   guardaron también en el proyecto de Claude (`ekl-demo/...`) para que
   la sesión 2 pueda retomarlos sin depender de que este contenedor
   siga vivo.

### Qué quedó pendiente (fases posteriores, no construidas aquí)

- F2 — MCP servers (`ekl_graph_server.py`, `ekl_actions_server.py`),
  RBAC aplicado en tiempo de consulta, auditoría (`audit.jsonl`).
- F2b — Bus de eventos e instrumentación (`observability/`).
- F3 — Agente B (A2A) sobre el grafo `infra`.
- F4 — Agente A (LangGraph, nodo crítico, delegación A2A).
- F5 / F5b — UI Streamlit y Sala de control.
- F6 — Extracción automática de `DEPENDE_DE` desde `runbook_pagos.md`
  con el LLM (`data/extract_from_docs.py`); en F1 la arista
  `nomina-batch → PAY-DB-01` ya está cargada directamente en
  `rel_depende_de.csv` con `fuente: carga_inicial`, tal como permite el
  plan si el tiempo aprieta (§6, nota sobre F6 opcional).
- F7 — Ensayo, `docs/GUION.md`, `docs/PLAN_B.md`, medición de tiempos.

### Criterio de cierre — verificado

- `GRAPH_BACKEND=memory pytest tests/test_data.py` → **10 passed**
  (también pasa sin exportar `GRAPH_BACKEND`, usa `memory` por
  defecto).
- `python data/load_graph.py` con `GRAPH_BACKEND=memory` corre sin
  errores y reporta `negocio: 32 nodos, 39 aristas` / `infra: 16 nodos,
  12 aristas`.
- `scripts/demo_reset.sh` probado end-to-end con `GRAPH_BACKEND=memory`.
- **No se pudo probar contra Neo4j real** (no hay Docker/Neo4j
  disponible en este entorno de ejecución) — el código del backend
  Neo4j (`data/graph_backend.py::Neo4jBackend`) está escrito y
  sintácticamente validado, y sigue el mismo patrón de creación de
  bases + constraints + `MERGE` que se usa habitualmente con el driver
  oficial `neo4j`, pero **queda pendiente de validación real en la
  sesión 2 o en el equipo de presentación** con `docker compose up -d
  neo4j && GRAPH_BACKEND=neo4j python data/load_graph.py`.
  > **Nota de la sesión 2:** ya está validado. Ver
  > "Verificación con Docker real (Colima)" al final de este archivo:
  > Neo4j 5 Enterprise carga las dos bases con los mismos conteos que el
  > backend en memoria, y `pytest tests/` pasa entero con
  > `GRAPH_BACKEND=neo4j`.

---

## Sesión 2 — Servidores MCP, RBAC, auditoría y bus de eventos (F2 + F2b)

**Fecha:** 2026-09-15
**Fases construidas:** F2 (servidores MCP, §4.3 y §4.4) y F2b (bus de
eventos e instrumentación, §4.8). No se construyó nada de F3 en
adelante. Los datos y la ontología de la sesión 1 quedaron intactos
salvo una corrección, anotada más abajo.

### Qué se construyó

- **F2b — `observability/` (se construyó primero, como recomienda el §6:
  "conviene hacer F2b antes de los agentes para que A y B nazcan
  instrumentados")**
  - `observability/events.py`: modelo Pydantic `Event` con los ocho
    campos exactos del §4.8 (`run_id, ts, seq, source, kind, step,
    payload, meta`), los cinco `source` y los once `kind` de la lista
    del plan como `Literal` cerrados. `run_id` es obligatorio y no
    admite cadena vacía (`MissingRunIdError`). `meta` trae los seis
    campos del plan (`duracion_ms, tokens_in, tokens_out, modelo,
    user_role, iteration`) y admite claves extra.
  - `observability/event_bus.py`: dos backends por `EVENT_BUS`:
    `InProcessEventBus` (cola `asyncio` + historial por `run_id`) y
    `RedisStreamsEventBus` (`XADD`/`XREAD`/`XRANGE` sobre el stream
    `ekl:events`). **El `seq` lo asigna el bus, no el emisor**: en Redis
    con `INCR ekl:seq:<run_id>`, que es lo que hace que el orden sea
    consistente cuando publican cuatro procesos distintos.
  - `observability/trace_store.py`: `TraceStore` (persiste cada evento
    en el `trace.jsonl` global **y** en `observability/traces/<run_id>.jsonl`)
    más la app FastAPI en el **puerto 8020** con `GET /stream`,
    `GET /stream/<run_id>` (SSE, `sse-starlette`), `POST /events`
    (ingesta; **422 si falta `run_id`**), `GET /runs`, `GET /runs/<id>`
    y `GET /health`.

- **F2 — `mcp/`**
  - `mcp/ekl_graph_server.py`: servidor `ekl-graph` con el SDK oficial
    `mcp`, **parametrizado por base** (`--base negocio|infra` o
    `EKL_GRAPH_BASE`). Herramientas: `get_schema` (marca `Servicio`
    como nodo frontera cuando la base es `negocio`), `find_entry_nodes`
    (búsqueda por id/nombre, insensible a mayúsculas y tildes, con RBAC),
    `read_cypher` (solo lectura + poda RBAC + evento `policy_decision`)
    y `list_node_actions` (contrato **sin** `backend`).
  - `mcp/ekl_actions_server.py`: servidor `ekl-actions` con
    `resolve_metric` y `retrieve_customer_position` sobre **DuckDB**
    (vista `posiciones` sobre `data/posiciones.csv`), devolviendo el
    contrato exacto del §3.2 (`valor, moneda, fecha_corte, linaje`) y
    verificando `accion_policies` contra el rol antes de ejecutar.
  - `mcp/ekl_middleware.py`: **middleware común a los dos servidores**
    (`ServerMiddleware` del SDK). Lee `run_id` y `user_ctx` de los
    metadatos (`_meta`) de cada llamada, los deja en un `ContextVar`,
    emite `tool_call` / `tool_result` / `error` al bus, expone
    `emitir_policy_decision()` —el único camino por el que los dos
    servidores registran una decisión de política— y escribe
    `audit.jsonl` con los seis campos del §4.3 (`timestamp, user, tool,
    args, nodos_devueltos, filtrados`) más el contexto de la corrida.
  - `mcp/ekl_policies.py`: carga de `policies.yaml`, `ontology.yaml` y
    `semantic_layer.yaml`, con **recarga en caliente por `mtime`** para
    que el momento "wow" #1 (editar la métrica en vivo) funcione sin
    reiniciar los servidores.
  - `mcp/ekl_cypher.py`: intérprete de un subconjunto de Cypher de solo
    lectura (ver "Qué se asumió", punto 2).

- `tests/test_mcp.py`: 10 tests (los tres que pide la fase, más el
  criterio de cierre de la traza, la denegación de la acción, el rechazo
  de escritura, `get_schema`, `find_entry_nodes` y el rechazo de eventos
  sin `run_id`).
- `requirements.txt`, `.env.example`, `scripts/demo_reset.sh` y
  `README.md` actualizados (el README trae la sección **"Cómo probar con
  MCP Inspector"** con comandos verificados uno por uno).

### Correcciones a la sesión 1

1. **`.env.example` no existía.** El STATUS de la sesión 1 lo daba por
   construido y el README lo citaba, pero el archivo no estaba en el
   repo. Se creó con todas las variables del plan agrupadas por fase
   (las de F3+ comentadas). No cambia ningún dato ni la ontología.
2. **`data/graph_backend.py`: método nuevo `list_nodes(db, label=None)`**
   en la interfaz y en los dos backends. `read_cypher` necesita enumerar
   los nodos de partida de un patrón y hasta F1 solo se podía contarlos
   (`count_nodes`) o navegar desde un id conocido (`neighbors`). Es un
   método **aditivo**: no cambia el comportamiento de ninguno de los
   anteriores, y `tests/test_data.py` sigue pasando sin tocarlo.

Los CSV, `ontology.yaml`, `semantic_layer.yaml`, `policies.yaml`,
`load_graph.py` y `tests/test_data.py` **no se modificaron**.

### Qué se asumió

1. **SDK `mcp` 2.x.** El paquete oficial instalado es `mcp` 2.2.0, donde
   `FastMCP` pasó a llamarse `MCPServer` y la API cambió respecto a la
   1.x que asumía el plan (§7). Se usó la 2.x: trae un hook de
   `middleware` de primera clase en el servidor, que es exactamente lo
   que el §4.8 pide para emitir eventos desde ambos servidores, y evita
   tener que envolver cada herramienta a mano. `requirements.txt` fija
   `mcp>=2.0`.
2. **`read_cypher` ejecuta un subconjunto de Cypher, interpretado por la
   demo, no Cypher nativo de Neo4j.** El plan pide que `read_cypher`
   funcione y que la demo corra sin Docker (`GRAPH_BACKEND=memory`,
   donde el grafo es `networkx` y no hay motor Cypher). En vez de tener
   dos caminos —Cypher nativo en un backend y otra cosa en el otro, con
   el RBAC duplicado— se escribió `mcp/ekl_cypher.py`, que ejecuta el
   mismo patrón sobre la interfaz `GraphBackend`: el resultado y el
   podado son idénticos con Neo4j y con memoria.
   Soporta: `MATCH` con un patrón lineal de k saltos (`-[:REL]->` y
   `<-[:REL]-`), propiedades inline, `WHERE` con condiciones unidas por
   `AND` (`= <> != > >= < <= CONTAINS STARTS WITH ENDS WITH IN`),
   `RETURN [DISTINCT]` con `var`, `var.prop`, `AS` y `*`, y `LIMIT`.
   **No** soporta `OPTIONAL MATCH`, `WITH`, `UNWIND`, `CALL`, `ORDER BY`,
   agregaciones, caminos de longitud variable (`[:REL*1..3]`) ni `OR`;
   cada uno de esos casos devuelve un error que dice qué pasó. Toda
   escritura se rechaza (la consulta debe empezar por `MATCH` y no puede
   contener `CREATE/MERGE/DELETE/SET/REMOVE/DROP/CALL/LOAD/...`).
   Si en F4 el planner del Agente A necesita más Cypher del que hay
   aquí, la salida natural es ejecutar nativo cuando
   `GRAPH_BACKEND=neo4j` y dejar el intérprete solo para `memory`.
3. **La poda RBAC elimina la fila completa, no solo el nodo.** Si
   cualquier nodo del recorrido está vedado para el rol, desaparece toda
   la fila: devolver la fila sin ese nodo dejaría al agente con una ruta
   que no puede justificar ni citar en el linaje. El evento
   `policy_decision` reporta `filas_antes`, `filas_despues` y la lista de
   `nodos_podados`.
4. **`run_id` generado cuando la llamada no lo trae.** El plan exige
   `run_id` en los metadatos MCP y que el `trace_store` rechace eventos
   sin él (§8). MCP Inspector no manda `_meta` por defecto, así que el
   middleware genera `inspector-xxxxxxxx` y lo marca
   `run_id_generado: true` en `audit.jsonl`, en vez de rechazar la
   llamada: el servidor sigue siendo explorable a mano y ningún evento
   viaja sin `run_id`. Desde el Inspector se pueden mandar los dos:
   `--tool-metadata run_id=demo-1 user_ctx=analista_junior`.
5. **`user_ctx` admite forma corta.** Lo normal es el objeto
   `{"usuario": ..., "rol": ...}` que manda el agente; se acepta además
   `user_ctx` como cadena con el nombre del rol (o un `rol` suelto en
   `_meta`), porque es lo único que puede mandar un cliente de línea de
   comandos. Sin nada, el rol sale de `DEMO_USER_ROLE` o de
   `rol_por_defecto` de `policies.yaml`.
6. **La carpeta se llama `mcp/` (como pide el plan) y NO tiene
   `__init__.py` — nunca debe tenerlo.** Se llama igual que el SDK
   oficial. Un directorio sin `__init__.py` es solo una "porción de
   espacio de nombres": Python sigue buscando y encuentra el paquete
   regular instalado, así que `import mcp` resuelve al SDK (verificado).
   Con `__init__.py` pasaría a ser un paquete regular y taparía al SDK.
   Por eso sus módulos se importan como módulos sueltos, con `mcp/` en
   `sys.path` (lo hacen ellos mismos en el encabezado).
7. **Quién escribe `trace.jsonl`.** Con `EVENT_BUS=redis` lo hace el
   proceso `trace_store`, que consume el stream; con `EVENT_BUS=memory`
   no hay tal proceso, así que lo escribe el propio emisor vía un sink
   local del bus (es lo que hace que los tests produzcan una traza
   completa sin Docker). `EKL_TRACE_LOCAL=1|0` fuerza cualquiera de los
   dos.
8. **`trace.jsonl` global *y* por `run_id`.** El §4.8 dice "trace.jsonl
   (persistente, por run_id)" y el criterio de cierre habla de un
   `trace.jsonl` único. Se hacen los dos: el global en la raíz (el que
   mira el criterio de cierre y limpia `demo_reset.sh`) y uno por
   ejecución en `observability/traces/<run_id>.jsonl`, que es lo que
   necesitan el selector de `run_id` y el botón "reproducir" de la Sala
   de control (F5b) sin releer y filtrar la traza entera.
9. **`trace_id` / `span_id` van dentro de `meta`.** El §4.8 los menciona
   en la prosa pero no en la lista de campos del esquema, y el esquema
   pedido es exacto. `EventMeta` admite claves extra, así que cuando F3
   o F4 quieran anidar spans (o exportar a OpenTelemetry) los ponen ahí
   sin cambiar el modelo.
10. **`moneda: COP` es una constante del servidor de acciones.**
    `posiciones.csv` no tiene columna de moneda; la definición de
    `exposicion_crediticia` en `semantic_layer.yaml` dice "...en COP, a
    fecha de corte". Se declara como constante (`EKL_MONEDA`) y se
    reporta en el linaje para que la cifra nunca salga sin unidad.
11. **`typer` no está en `requirements.txt`.** Solo hace falta para el
    CLI `mcp dev` del SDK (`pip install "mcp[cli]"`); el camino
    documentado y verificado del Inspector es `npx
    @modelcontextprotocol/inspector`, que no lo necesita.

### Qué quedó pendiente (fases posteriores, no construidas aquí)

- F3 — Agente B (A2A) sobre el grafo `infra`.
- F4 — Agente A (LangGraph, nodo crítico, delegación A2A).
- F5 / F5b — UI Streamlit y Sala de control visual (el bus, la traza por
  `run_id` y el SSE que consumirá ya están).
- F6 — `data/extract_from_docs.py`.
- F7 — Ensayo, `docs/GUION.md`, `docs/PLAN_B.md`, medición de tiempos.

Además, de esta sesión:

- **Los `kind` de evento `thought`, `plan`, `a2a_request`,
  `a2a_response`, `critic_verdict`, `refinement` y `answer` están
  definidos en el esquema pero todavía no los emite nadie**: los emiten
  los agentes (F3/F4). Los cuatro que sí se emiten hoy son `tool_call`,
  `tool_result`, `policy_decision` y `error`, desde los servidores MCP.
- Los agentes de F3/F4 deben pasar `run_id` y `user_ctx` en los
  metadatos de **cada** llamada MCP (`meta={"run_id": ..., "user_ctx":
  {...}}` en `call_tool`) y propagar el mismo `run_id` en el cuerpo A2A.
  Es lo que cuelga todo de la misma ejecución.

### Criterio de cierre — verificado

- `pytest tests/test_mcp.py` → **10 passed**.
  `pytest tests/` (ambos archivos) → **20 passed**; los 10 tests de la
  sesión 1 siguen pasando sin cambios.
- `trace.jsonl` de la prueba: 29 eventos, y los **dos `run_id` de la
  corrida (`test-riesgo-*` y `test-junior-*`) tienen eventos de
  `mcp_graph` y de `mcp_actions`** — que es el criterio de cierre
  textual de la fase. `seq` resulta monótono y sin huecos dentro de cada
  ejecución pese a venir de dos servidores distintos (lo verifica un
  test).
- `audit.jsonl` se escribe con los seis campos del §4.3; el test del
  podado comprueba `filtrados == ["CLI01","CLI05"]`.
- **Servidores probados por stdio y por HTTP**, y con **MCP Inspector
  real** (`npx @modelcontextprotocol/inspector --cli`): `tools/list`
  (los esquemas de salida confirman que `AccionExpuesta` no tiene campo
  `backend`), `read_cypher` con `user_ctx=riesgo` (12 clientes) y con
  `user_ctx=analista_junior` (10, podando CLI01 y CLI05),
  `retrieve_customer_position` con valor + linaje y su denegación
  explicable. Los comandos exactos están en el README.
- `trace_store` probado en vivo en el puerto 8020: `/health`, `/runs`,
  `/runs/<id>`, ingesta `POST /events` y **SSE** (`GET /stream?run_id=`
  entregó el evento publicado mientras el stream estaba abierto). El
  `POST` sin `run_id` devuelve **422**.
- `scripts/demo_reset.sh` probado end-to-end con `GRAPH_BACKEND=memory`.

### Qué NO se pudo probar en este entorno

- **`mcp dev`** (el CLI del SDK) no se ejecutó: abre la UI web del
  Inspector y necesita `pip install "mcp[cli]"`. Sí se verificó que
  `mcp/ekl_graph_server.py:servidor` y `mcp/ekl_actions_server.py:servidor`
  resuelven al objeto servidor, que es lo que ese comando espera.
- El entorno de esta sesión usa un **venv con Python 3.12**
  (`.venv/`, creado con `uv`) porque el Python del sistema es 3.14 y no
  tenía ninguna de las dependencias. El plan pide 3.11+, así que 3.12
  está dentro de lo previsto.

### Verificación con Docker real (Colima)

Al final de la sesión apareció Docker (Colima), así que se pudo cerrar lo
que había quedado pendiente. **La prueba contra Redis real encontró dos
bugs que el doble en memoria no podía mostrar**; los dos están
corregidos y reverificados.

**Bug 1 — `XREAD BLOCK` que expira tumbaba al suscriptor.**
`observability/event_bus.py` hacía `xread(..., block=5000)` sin
capturar excepciones. Cuando la ventana de bloqueo se agota sin eventos
—el estado normal mientras el agente piensa— redis-py levanta
`redis.exceptions.TimeoutError`. Eso mataba la tarea suscriptora a los
cinco segundos de arrancar, antes de que llegara ningún evento. Efecto
visible: el `trace_store` levantaba y respondía `/health`, pero
`trace.jsonl` no se escribía nunca con `EVENT_BUS=redis`. Ahora el
bucle trata el timeout como "no pasó nada en esta ventana" y reintenta,
y una `ConnectionError` (Redis caído o reiniciado) espera
`EKL_REDIS_RECONEXION_S` y reengancha en vez de propagarse.

**Bug 2 — un fallo del consumidor de trazas se llevaba el servidor
entero.** `_consumir_hacia_disco` corre dentro del task group del
`lifespan` de FastAPI, así que cualquier excepción suya abortaba el
arranque de la app: un problema escribiendo la traza dejaba a la Sala de
control sin SSE. Ahora el consumidor no propaga nada (salvo la
cancelación del apagado): registra el fallo y reengancha.

Ninguno de los dos afecta a `EVENT_BUS=memory`, que es el camino de los
tests; por eso pasaban los 20 tests con el bug presente. Vale la pena
tenerlo en cuenta para F3/F4: **el modo `memory` no ejercita el
transporte**, y la demo se presenta con Redis.

Qué quedó verificado contra infraestructura real:

- **Redis real (`redis:7-alpine`).** `RedisStreamsEventBus` completo:
  `publish` con `seq` vía `INCR` (contador **independiente por
  `run_id`**, comprobado con dos ejecuciones simultáneas), `subscribe`
  con `XREAD` en vivo y filtro por `run_id`, `replay` con `XRANGE`,
  `runs()` y `aclose()`.
- **La arquitectura del §4.8 entre procesos.** Un proceso emisor con
  `EVENT_BUS=redis` llamando a los dos servidores MCP, y el proceso
  `trace_store` aparte consumiendo el stream: los 9 eventos llegaron,
  `trace.jsonl` y `traces/RUN-CROSS.jsonl` los escribió **el
  `trace_store`, no el emisor**, el `seq` salió 1..9 monótono y sin
  huecos pese a venir de dos servidores distintos, y los 9 eventos
  llegaron además por **SSE en vivo** a un cliente conectado antes de
  que el emisor arrancara. El `policy_decision: podado` con
  `["CLI01","CLI05"]` viajó entero por Redis.
- El consumidor sobrevive a una ventana de `XREAD` vacía (se dejó el
  servidor 12 s en silencio, más que `BLOCK_MS`, sin un solo error en el
  log).
- **Neo4j real (`neo4j:5-enterprise`).** Es la primera vez que se
  ejecuta el `Neo4jBackend` escrito en la sesión 1, que había quedado
  sin validar. `GRAPH_BACKEND=neo4j python data/load_graph.py` crea las
  dos bases (`CREATE DATABASE ... IF NOT EXISTS` + espera a que queden
  online), los constraints y los datos, y reporta **los mismos conteos
  que el backend en memoria**: `negocio: 32 nodos, 39 aristas` /
  `infra: 16 nodos, 12 aristas`. La asunción nº 1 de la sesión 1
  (Enterprise para tener dos bases) queda confirmada en la práctica.
- **Los cuatro tools contra Neo4j real**, incluido el intérprete de
  Cypher de `mcp/ekl_cypher.py`, que hasta ahora solo se había ejecutado
  sobre `networkx`. Da exactamente los mismos resultados: salto 1
  (`pagos-core`, `nomina-batch` dependen de `PAY-DB-01`), salto 5
  (`Laura Gómez`), saltos 2+3 (`CLI01`, `CLI02`, `CLI03`), y el RBAC
  poda igual (12 vs 10 clientes; `CLI01` desaparece de la ruta
  corporativa para `analista_junior`).
- **`pytest tests/` completo con `GRAPH_BACKEND=neo4j` → 20 passed.**
  Los 10 tests de datos de la sesión 1 también pasan contra Neo4j real,
  no solo contra memoria.
- **La pila completa de la demo a la vez**: `GRAPH_BACKEND=neo4j` +
  `EVENT_BUS=redis` + `trace_store` en su propio proceso. Los 9 eventos
  de una corrida llegaron al `trace.jsonl` que escribe el `trace_store`,
  por SSE en vivo, y con `seq` 1..9 sin huecos.
- **`scripts/demo_reset.sh` con la pila real**
  (`GRAPH_BACKEND=neo4j EVENT_BUS=redis`): recarga Neo4j, borra
  auditoría y trazas, y deja el stream de Redis vacío.

### Dos mejoras que salieron de probar con Docker

1. **El servidor MCP ya no recarga el grafo en cada arranque.**
   `ekl_graph_server.grafo()` llamaba a `build_graph()` siempre. Con
   `memory` hace falta (el grafo nace vacío en cada proceso), pero con
   `neo4j` los datos ya están cargados: recargarlos son ~50 viajes
   inútiles al arrancar y llenan la consola de avisos del driver. Ahora
   solo carga si la base está vacía o si `EKL_RELOAD_GRAPH=1`.
2. **`demo_reset.sh` limpia el stream de Redis aunque no tengas
   `redis-cli` instalado**: si no lo encuentra, usa el del contenedor
   (`docker compose exec -T redis redis-cli`). Antes solo imprimía un
   aviso, que es justo lo que pasa en un portátil recién preparado para
   la demo.

---

## Sesión 3 — Agentes A y B, A2A y bucle de refinamiento (F3 + F4)

**Fecha:** 2026-09-15
**Fases construidas:** F3 (Agente B y A2A, §4.6) y F4 (Agente A, §4.5).
Nada de F5 en adelante. No se tocaron los datos ni la ontología.

### Qué se construyó

- **`agents/agent_b/`** — Agente de Infraestructura y TI.
  - `server.py`: FastAPI en el **puerto 8002** con
    `GET /.well-known/agent.json` (agent card con las skills
    `impacto_cambio` y `responsable_servicio`) y `POST /a2a` JSON-RPC 2.0
    con el método `tasks/send`. Rechaza con `-32601` cualquier otro
    método y con `-32602` una tarea sin `run_id`.
  - `grafo.py`: LangGraph de **dos nodos** (`interpretar` →
    `consultar`), sobre el MCP `ekl-graph` con `--base infra`. Devuelve
    solo el contrato acordado —servicios afectados, responsable,
    linaje— y usa la identidad del usuario original para que el RBAC se
    aplique con ese rol en su propio dominio.
  - Emite `thought`, `tool_call`/`tool_result`, `a2a_request` (lo que
    recibió) y `a2a_response`.
- **`agents/agent_a/`** — Orquestador de Negocio.
  - `grafo.py`: LangGraph con el `AgentState` del §4.5 (los diez campos
    literales) y **los siete nodos exactos**, en su orden:
    `planner → resolver_contexto → navegar_grafo → ejecutar_accion →
    delegar_a2a → nodo_critico → responder`, con el bucle de
    refinamiento del crítico de vuelta a 3, 4 o 5.
  - `verificacion.py`: la comprobación determinista de "ninguna cifra
    sin linaje".
  - `server.py`: FastAPI en el **puerto 8001** con
    `POST /ask {query, user_role}` que devuelve `run_id` y la respuesta
    completa (hallazgos, ruta, evidencia con linaje, políticas
    aplicadas, advertencias).
- **`agents/llm.py` + `agents/llm_fake.py`** — el modelo se lee de
  `LLM_MODEL`; `fake` activa el simulado, que cubre las tres preguntas
  del §5 y no necesita API key ni red.
- **`agents/a2a.py`** — subconjunto propio del protocolo (ver más
  abajo). **`agents/mcp_cliente.py`** — cliente MCP instrumentado que
  propaga `run_id` y `user_ctx` en los metadatos de cada llamada.
  **`agents/eventos.py`** — emisión al bus de F2b; **no define ningún
  esquema nuevo**, todo pasa por `observability/events.py`.
- **`scripts/demo_smoke.py`** — las tres preguntas del §5 con rol
  `riesgo` y la principal con `analista_junior`. **Levanta y apaga él
  mismo el Agente B** (ver "Lo que costó caro", punto 4).
- **`tests/test_agents.py`** — 20 tests de las reglas duras.

### Qué versión de A2A se implementó, y por qué

**Subconjunto propio** (`agents/a2a.py`, ~180 líneas), no el `a2a-sdk`.
El plan dejaba la decisión abierta (§7: "o el SDK `a2a-sdk` si está
estable al momento de construir — decidir en F3 y dejar constancia").
Razones, en orden de peso:

1. **`a2a-sdk` ya no tiene `tasks/send`.** Se instaló la versión
   publicada (1.1.2) y se revisaron sus métodos JSON-RPC: `message/send`,
   `message/stream`, `tasks/get`, `tasks/cancel`, `tasks/list`,
   `tasks/resubscribe`, `tasks/pushNotificationConfig/*` y
   `agent/getAuthenticatedExtendedCard`. **`tasks/send` no existe**: la
   especificación lo sustituyó por `message/send`. El plan y el encargo
   de esta sesión piden `tasks/send` explícitamente, así que usar el SDK
   habría obligado a cambiar el contrato acordado.
2. **La demo enseña el sobre, no la librería.** El minuto 3-5 del guion
   es abrir el payload A2A en pantalla y decir "miren qué le mandó: el
   id del servicio y la identidad del usuario; ni esquema, ni
   credenciales". Con un archivo propio, ese sobre es literalmente lo
   que se ve; el evento `a2a_request` incluye además una lista
   `no_se_envia` con lo que deliberadamente no viaja.
3. **Peso.** El SDK arrastra 12 dependencias más (protobuf,
   google-auth, grpc…) para una demo que corre en un portátil.

Qué se implementó: agent card con la forma de A2A (`name`,
`description`, `url`, `version`, `capabilities`, `defaultInputModes`/
`defaultOutputModes`, `skills[]`) y `tasks/send` por JSON-RPC 2.0 con
`params = {id, sessionId, message:{role, parts[]}, metadata:{run_id,
user_ctx}}` y respuesta `{id, status:{state}, artifacts:[{parts:[{type:
"data", data}]}]}`. Qué no: streaming, push notifications, `tasks/get` /
`tasks/cancel` (las tareas son síncronas y terminan en un turno) y
autenticación (los dos agentes corren en el mismo portátil).
`protocolVersion` se declara como `0.2-subset` para que quede claro en
el propio card que no es el protocolo completo. Si más adelante se
quiere el SDK, el único archivo que cambia es `agents/a2a.py`.

### Qué se asumió

1. **`responder` no es terminal.** El §4.5 lo pone como último nodo,
   pero el encargo exige que **el nodo crítico rechace** una respuesta
   con cifras sin linaje, y para rechazarla tiene que verla. Así que
   `responder` redacta y devuelve el control al `nodo_critico` en fase
   de **verificación**; si la aprueba, ahí termina. Es la única
   desviación del grafo del §4.5, y es lo que hace cumplible la regla.
2. **La regla "ninguna cifra sin linaje" se comprueba de forma
   determinista, no preguntándole al modelo.**
   `agent_a/verificacion.py` extrae las cifras del texto (incluidas las
   formas del español: "5.300 millones", "6,3 mil millones",
   "1.234.567,89") y las compara con los valores de `evidence[]` que
   tienen linaje, con un 2% de tolerancia para el redondeo. **Solo se
   exige linaje a cifras de magnitud monetaria** (≥ 1.000 millones):
   exigírselo a "3 clientes", a un año o a `PAY-DB-01` daría falsos
   positivos constantes. Si la comprobación encuentra una cifra
   huérfana, la respuesta se rechaza **aunque el LLM diga que está
   bien**.
3. **El bucle se corta por tres vías**, no solo por `max_iterations=6`:
   una directiva de refinamiento idéntica a la anterior, que el crítico
   no emita ninguna directiva, o el tope de iteraciones. En los tres
   casos se responde con lo que haya y se deja el motivo en
   `motivo_corte` y en el evento `critic_verdict`.
4. **Los eventos `tool_call`/`tool_result` se emiten por los dos lados**
   (agente y servidor MCP), como ya se hacía en F2. No es duplicación:
   son los dos extremos de la misma flecha en el diagrama de secuencia
   de la Sala de control. Lo mismo con `a2a_request`, que emiten A (lo
   que envió) y B (lo que recibió) — es la única forma de auditar que lo
   que cruzó la frontera es lo que se dice que cruzó.
5. **Los agentes instancian los servidores MCP en su propio proceso**
   por defecto (rápido y sin piezas móviles, es lo que usan los tests).
   Con `EKL_MCP_GRAPH_URL` / `EKL_MCP_ACTIONS_URL` hablan por HTTP con
   servidores ya levantados, que es el diagrama del §2.
6. **El LLM simulado simula decisiones, no datos.** Escoge un guion
   según la pregunta y devuelve el razonamiento y la decisión de cada
   paso; las consultas se ejecutan de verdad contra los servidores MCP,
   el RBAC poda de verdad y las cifras salen de DuckDB. Por eso una
   corrida con `fake` sigue demostrando la gobernanza. Si llega una
   pregunta que no reconoce, responde con un guion genérico que **no
   inventa nada** y deja que el crítico detecte el hueco.
7. **`LLM.estructurado()` recibe un `contexto` estructurado** además del
   prompt. El modelo real lo ignora (esa información ya va redactada en
   el prompt); el simulado lo usa para decidir sin tener que analizar
   texto. Sin eso, el simulado tendría que llevar los ids de cliente
   escritos a mano y dejaría de reaccionar al podado del RBAC.

### Lo que costó caro (bugs encontrados, todos corregidos)

Los tres primeros solo aparecen con **varios procesos y Redis** — es
decir, en la configuración de la demo, no en la de los tests:

1. **`XREAD` perdía eventos entre lecturas.** `subscribe()` usaba `$` en
   cada `XREAD`, y `$` significa "solo lo que llegue mientras estoy
   bloqueado": todo lo publicado entre una lectura y la siguiente se
   perdía en silencio. Se veía como corridas con 1 evento en
   `trace.jsonl` mientras el stream de Redis las tenía completas. Ahora
   `$` se resuelve **una vez** a un id concreto (`XINFO STREAM`) y se
   avanza con cada entrada: sin ventanas ciegas.
2. **El cliente de Redis quedaba atado al primer bucle de eventos.**
   `redis.asyncio` ata sus conexiones al bucle en el que se crearon, y
   el bus es un singleton del proceso: la segunda pregunta del smoke
   test (otro `asyncio.run()`) fallaba con `Event loop is closed`. Ahora
   el bus detecta el cambio de bucle y reconstruye el cliente. De paso,
   `demo_smoke.py` corre todas las preguntas en **un solo bucle**, que
   es lo correcto de todos modos.
3. **`seq` no es global con `EVENT_BUS=memory`.** Cada proceso lleva su
   propio contador, así que con A y B separados los dos numeran desde 1
   dentro del mismo `run_id` y la Sala de control dibujaría la secuencia
   mal. No es arreglable sin un contador compartido: **para eso está
   `EVENT_BUS=redis`** (`INCR ekl:seq:<run_id>`). Queda documentado en
   el bus, y `TraceStore.read()` ordena por `(ts, seq)` para que el modo
   `memory` siga siendo legible.
4. **Un proceso viejo en el puerto hace que la corrida mienta.** Pasó
   dos veces durante la sesión: un `agent_b` de una prueba anterior
   seguía atendiendo en el 8002 y escribía sus trazas donde lo
   arrancaron, así que la corrida parecía buena y el `trace.jsonl` salía
   sin eventos de B. Ahora `demo_smoke.py` **levanta y apaga él mismo el
   Agente B** y aborta si el puerto está ocupado, y `demo_reset.sh`
   avisa si hay agentes o un `trace_store` corriendo.
5. **El linaje del Agente B filtraba su esquema.** Lo detectó
   `tests/test_agents.py`: el linaje decía
   `(:Servicio {pagos-core})-[:ES_RESPONSABLE]->(:Persona)`, que es
   exactamente el esquema que el §4.6 prohíbe exponer. El §4.6 pide las
   dos cosas —devolver linaje y no exponer el esquema— y estaban en
   tensión. Ahora el linaje va **en lenguaje de dominio**: "grafo infra
   (dominio Infraestructura y TI): responsable declarado del servicio
   pagos-core". Sigue siendo auditable y ya no dice cómo está modelado.

### Criterio de cierre — verificado

- **`demo_smoke.py` pasa con `LLM_MODEL=fake`**: 24 comprobaciones sobre
  las tres preguntas del §5 con rol `riesgo` más la principal con
  `analista_junior`. El resultado del §3.4 sale exacto: `CLI01`, `CLI02`
  y `CLI03` con sus cifras por encima de 5.000 millones COP,
  "Laura Gómez — Líder técnico pagos-core" y la advertencia colateral
  sobre `nomina-batch`. Con `analista_junior`, `CLI01` y `CLI05`
  (sensibilidad alta) no aparecen ni en la ruta, ni en la evidencia, ni
  en la respuesta.
- **La secuencia del `trace.jsonl`** de una corrida (87 eventos,
  `seq` 1..87 sin huecos con `EVENT_BUS=redis`, de cuatro procesos):

  ```
    5 agent_a  thought        planner
    6 agent_a  plan           planner
    7 agent_a  thought        resolver_contexto
   13 agent_a  thought        navegar_grafo
   28 agent_a  thought        ejecutar_accion
   59 agent_a  thought        delegar_a2a
   60 agent_a  a2a_request    delegar_a2a         impacto_cambio
     └ 61 agent_b  a2a_request    tasks/send
     └ 62 agent_b  thought        interpretar
     └ 68 agent_b  a2a_response   tasks/send
   69 agent_a  thought        nodo_critico
   70 agent_a  critic_verdict nodo_critico        is_complete=False
   71 agent_a  refinement     nodo_critico        -> delegar_a2a
   72 agent_a  thought        delegar_a2a
   73 agent_a  a2a_request    delegar_a2a         responsable_servicio
     └ 74 agent_b  a2a_request    tasks/send
     └ 75 agent_b  thought        interpretar
     └ 81 agent_b  a2a_response   tasks/send
   82 agent_a  thought        nodo_critico
   83 agent_a  critic_verdict nodo_critico        is_complete=True
   84 agent_a  thought        responder
   85 agent_a  thought        nodo_critico        (verificación)
   87 agent_a  answer         responder
  ```

- `pytest tests/` → **40 passed** (10 de datos, 10 de MCP, 20 de
  agentes), sin API key y sin Docker.
- Los 303 eventos de una corrida completa de las cuatro preguntas
  llegaron íntegros a `trace.jsonl` vía Redis (303 en disco = 303 en el
  stream), con `seq` sin huecos en los cuatro `run_id`.

### Qué NO se pudo probar en este entorno

- **La corrida con un modelo real está pendiente**: no hay
  `ANTHROPIC_API_KEY` en este entorno, y no es algo que se pueda
  suplir. Todo lo verificado arriba es con `LLM_MODEL=fake`. El comando
  exacto para hacerlo está en el README ("Con un modelo real"); conviene
  correrlo antes del ensayo, porque es el único camino que ejercita los
  prompts de verdad. Lo que sí está preparado para ese momento: el
  reintento de `read_cypher` cuando el modelo escribe Cypher fuera del
  subconjunto, el recorte del `razonamiento` a dos frases, y el
  `demo_smoke.py` que con un modelo real comprueba los hechos (evidencia
  y ruta) y solo avisa —no falla— si la redacción varía.

---

## Sesión 4 — UI y Sala de control (F5 + F5b)

**Fecha:** 2026-09-15
**Fases construidas:** F5 (UI Streamlit, §4.7) y F5b (Sala de control,
§4.8). No se tocaron los datos ni la ontología. De los agentes y los MCP
servers solo se cambió lo que la UI necesitaba y no existía (lista
abajo).

### Qué se construyó

- **`ui/app.py`** — Streamlit en el puerto 8501 con el layout del §4.7:
  columna izquierda con selector de rol (`direccion` / `riesgo` /
  `analista_junior`), la pregunta hilo conductor precargada más las dos
  alternativas del §5, botón **Ejecutar**, y un panel de salud de los
  tres servicios; columna derecha con las seis pestañas: **Respuesta**,
  **Ruta**, **Evidencia y linaje**, **Auditoría**, **Capa semántica** y
  **Sala de control**.
- **`ui/control_room.html`** — la Sala de control, servida por el
  trace_store en `/control` (puerto 8020) y embebida en su pestaña. Las
  cuatro zonas del §4.8:
  1. **Diagrama de secuencia vivo** con los seis carriles
     (Usuario · Agente A · MCP graph · MCP actions · Agente B · Datos) y
     una flecha por evento: azul MCP, ámbar A2A, rojo podado/denegado,
     verde respuesta. Al hacer clic en una flecha se abre su payload.
  2. **Cadena de razonamiento** con los pasos del Agente B **anidados**
     bajo la flecha A2A que los originó (se agrupan por el
     `meta.a2a_task_id` que ya emitía F3).
  3. **Plan y nodo crítico** con el estado de cada sub-pregunta en vivo,
     los huecos que detecta el crítico y la directiva de refinamiento.
  4. **Gobernanza en vivo**: llamadas MCP, tareas A2A, nodos filtrados,
     acciones denegadas, latencia, tokens y costo estimado.
  Controles: selector de `run_id`, **comparación de dos corridas lado a
  lado**, **reproducir** con velocidad ajustable (0,25× a 4×) y pausa,
  filtros por fuente y por tipo de evento, y exportar `trace.jsonl`.
- **`ui/grafo_svg.py`** — dibujo del grafo recorrido de la pestaña Ruta
  (ver "Por qué el grafo no lo dibuja streamlit-agraph").
- **`ui/datos_ruta.py`** — reconstrucción de las aristas del grafo
  recorrido.
- **`traces/referencia_riesgo.jsonl`** y
  **`traces/referencia_junior.jsonl`** — trazas de referencia para el
  plan B, generadas con `scripts/generar_trazas_referencia.py`.
- **`scripts/capturar_pantallas.py`** — regenera `docs/screenshots/`
  conduciendo un Chrome headless por CDP.

### Qué se cambió fuera de `ui/` (y por qué)

Cuatro cosas, todas aditivas; ninguna cambia el comportamiento de los
agentes ni de los servidores MCP:

1. **`agents/eventos.py` + `agents/agent_a/grafo.py`: el evento `plan`
   lleva ahora la `query` del usuario.** La Sala de control necesita el
   texto de la pregunta para dibujar la primera flecha
   (Usuario → Agente A) y para etiquetar la corrida en su selector, y no
   viajaba en ningún evento. El `plan` es el primer evento de la corrida
   que la conoce.
2. **`agents/agent_a/server.py`: `/ask` y el evento `answer` devuelven
   `nombres_nodos`.** El agente ya resolvía los nombres legibles de los
   nodos ("Textiles Andinos S.A.S." para `CLI01`), pero no los
   publicaba, así que la pestaña Ruta no tenía con qué etiquetar. El
   primer intento fue deducirlos de la descripción de la evidencia y
   salía el nombre de la métrica, no el del cliente.
3. **`agents/agent_a/server.py`: el evento `answer` lleva la evidencia
   completa** (con `nodo` y `origen`, no solo el subconjunto anterior).
   Sin eso, una traza guardada no se basta sola y el plan B perdía
   información que la UI sí muestra en vivo.
4. **`observability/trace_store.py`: rutas nuevas** — `GET /control`
   (sirve la página), `GET /export` y `GET /runs/<id>/export` (exportar
   `trace.jsonl`), y `GET /runs` ahora devuelve además un `detalle` con
   rol, modelo, número de eventos, duración y pregunta de cada corrida,
   más las trazas de referencia marcadas con `referencia:`. La clave
   `runs` original se mantuvo tal cual para no romper a nadie.

### Decisión: la Sala de control es HTML+JS, no Streamlit

El plan deja la decisión abierta (§4.7: "o una página HTML/JS ligera
servida por FastAPI si se quiere más fluidez; decidir en F5b") y se
eligió **HTML+JS servido en `/control`**, por tres razones:

1. **El modelo de Streamlit es incompatible con una animación.** Cada
   interacción vuelve a ejecutar el script entero y repinta el árbol; un
   diagrama de secuencia que crece flecha a flecha, con reproducción a
   velocidad ajustable, parpadearía en cada paso. En la página propia el
   repintado es un `innerHTML` sobre el mismo DOM y la reproducción va
   fluida a cualquier velocidad.
2. **El plan ya la quiere como página independiente** ("también
   disponible como página independiente en `/control` para proyectarla
   en una segunda pantalla"). Hacerla una página de verdad y embeberla
   en la pestaña da las dos cosas con un solo desarrollo; al revés no.
3. **El SSE ya estaba.** El trace_store sirve `/stream` desde F2b, así
   que la página vive en el mismo origen y no hace falta ni CORS ni un
   puerto nuevo.

La pestaña **Sala de control** de Streamlit embebe esa misma página en
un iframe, así que no hay dos implementaciones que mantener.

### Por qué el grafo de la pestaña Ruta no lo dibuja `streamlit-agraph`

El plan admite `pyvis` o `streamlit-agraph` (§4.7). Se implementó con
`streamlit-agraph` y **no funciona dentro de una pestaña**:

- Streamlit renderiza **todas** las pestañas y oculta las inactivas con
  CSS. El componente monta con el canvas a tamaño cero, vis.js encuadra
  la vista (`stabilization.fit`) sobre ese cero, y cuando la pestaña se
  muestra el grafo queda comprimido en una esquina de unos pocos
  píxeles. Se comprobó que ni redimensionar la ventana lo recupera: el
  ajuste ya ocurrió y el componente no expone `network.fit()`.
- Además, su `Config.__init__` hace `self.__dict__.update(**kwargs)`, de
  modo que todos los parámetros de layout acaban **también** como claves
  de primer nivel del objeto de opciones. vis.js las rechaza una por una
  y descarta el objeto entero ("Errors have been found in the supplied
  options object"). Hubo que sanear la Config a mano para quitarlas.

Lo que se recorre es un DAG por capas (cliente → producto → servicio →
base de datos / persona), así que dibujarlo es calcular niveles por
camino más largo y pintar un SVG: `ui/grafo_svg.py`, ~150 líneas. Sale
determinista —se ve igual en cada corrida, que importa en una demo
cronometrada—, usa los mismos colores que la Sala de control, y no
depende de un componente sin mantenimiento. **La opción de
`streamlit-agraph` sigue disponible** con un interruptor en la propia
pestaña, por si una versión futura lo arregla.

### Qué se asumió

1. **Las aristas del grafo recorrido se piden al mismo servidor MCP que
   usó el agente, con el rol del usuario.** El agente devuelve
   `ruta_nodos` (qué nodos visitó), no cómo se conectan. Pedirlas por
   MCP tiene un efecto que vale la pena: **la pestaña Ruta respeta el
   RBAC** igual que el resto — con `analista_junior` los nodos podados
   tampoco salen en el dibujo. Son cuatro consultas (una por tipo de
   arista de negocio), no una por par de nodos.
2. **Las aristas de infraestructura salen del contrato A2A, no del grafo
   `infra`.** La UI no consulta ese grafo: no es su dominio, igual que
   no lo es el del Agente A. Lo único que sabe de ese lado es lo que el
   Agente B decidió contar, que es justo lo que la demo quiere enseñar.
3. **El carril "Datos" del diagrama de secuencia es derivado.** La capa
   de datos (DuckDB, Neo4j) no emite eventos propios, así que sus
   flechas se deducen del linaje que reporta cada `tool_result` del
   servidor MCP. Van en gris punteado y el panel de detalle las marca
   como `derivada`, para no dar a entender que hay instrumentación donde
   no la hay.
4. **El costo estimado usa una tabla de precios en la propia página.**
   Con `LLM_MODEL=fake` los tokens son 0 y el costo es $0.00, que es
   correcto y así se muestra. Con un modelo real la cifra es una
   estimación a partir de `meta.tokens_in/out`, y la tarjeta lo dice.
5. **`traces/` no es `observability/traces/`.** El segundo es la salida
   de trabajo y `demo_reset.sh` la borra; el primero son las dos trazas
   curadas del plan B, que no se borran y sí se versionan.
6. **La auditoría distingue una denegación por política de un error.**
   En `audit.jsonl` las dos llegan como `estado: error` (el servidor MCP
   rechaza la llamada), pero confundirlas en una demo de gobernanza es
   lo contrario de lo que se quiere enseñar: una es el sistema
   funcionando y la otra es el sistema roto. La pestaña las separa
   mirando si la llamada trae un `policy_decision` con
   `resultado: denegado`.
7. **Las capturas se generan con un script, no a mano.** Chrome headless
   por CDP (`scripts/capturar_pantallas.py`), sin Selenium ni Playwright
   —son descargas grandes para ocho fotos— usando el `websockets` que ya
   venía con langgraph.

### Criterio de cierre — verificado

- **Las seis pestañas del §4.7 funcionan** y están capturadas en
  `docs/screenshots/` (más la Sala de control a pantalla completa y la
  comparación de corridas: ocho imágenes).
- **El momento "wow" #1 funciona de verdad**: editando
  `exposicion_crediticia` de v2 (saldo + cupo) a v3 (solo saldo) y
  guardando, la misma pregunta pasa de
  `CLI01 5.300M · CLI02 6.300M · CLI03 5.300M` a
  `CLI01 3.200M · CLI02 4.500M · CLI03 2.900M`, con el linaje diciendo
  `v3`, **sin reiniciar ningún proceso**.
- **El momento "wow" #2** se ve en la pestaña Respuesta (políticas
  aplicadas), en la Auditoría (denegadas por política) y sobre todo en
  la **comparación de corridas** de la Sala de control, que pone en
  ámbar lo que cambia entre `riesgo` y `analista_junior`: nodos
  filtrados (— vs `CLI01`), acciones denegadas (0 vs 2), cifras en la
  evidencia (3 vs 0) y la ruta recorrida.
- **La reproducción funciona sin LLM ni agentes.** Se apagaron el Agente
  A, el Agente B y Streamlit (comprobado: `/health` no responde en 8001
  ni en 8002) y la Sala de control siguió listando, reproduciendo y
  comparando las dos trazas de referencia — 87 y 77 eventos, con las
  cuatro fuentes y el podado de `CLI01` intactos.
- `traces/referencia_riesgo.jsonl` (87 eventos) y
  `traces/referencia_junior.jsonl` (77 eventos) generados con
  `LLM_MODEL=fake`, ambos con eventos de `agent_a`, `agent_b`,
  `mcp_graph` y `mcp_actions`.
- `pytest tests/` sigue en **40 passed** y `demo_smoke.py` en **24
  comprobaciones**, sin cambios respecto a la sesión 3.

### Qué NO se pudo probar en este entorno

- **La UI con un modelo real** sigue pendiente por la misma razón que en
  la sesión 3: no hay `ANTHROPIC_API_KEY`. Lo que sí está previsto para
  ese momento: `UI_TIMEOUT` (300 s por defecto) para que la UI no corte
  una corrida lenta, y la zona de gobernanza mostrando tokens y costo
  reales en cuanto `meta.tokens_in/out` dejen de ser 0.
- **La Sala de control en vivo (SSE) durante una corrida real** se probó
  en la sesión 2 con eventos inyectados y aquí con reproducción; queda
  por cronometrar cómo se ve con la latencia de un modelo real, que es
  precisamente cuando la pantalla tiene algo que contar mientras espera.

---

## Sesión 5 — Extracción desde documentos y ensayo (F6 + F7) · cierre

**Fecha:** 2026-09-15
**Fases construidas:** F6 (extracción desde documentos, §4.2) y F7
(ensayo, guion y plan B). **Con esto la demo está completa: F0 a F7.**

### Qué se construyó

- **`data/extract_from_docs.py`** — lee `data/runbook_pagos.md`, le pide
  al LLM las tripletas `(servicio, DEPENDE_DE, base_de_datos)` con salida
  estructurada Pydantic, las canonicaliza contra los nodos existentes con
  coincidencia difusa, y escribe las aristas con
  `fuente: "runbook_pagos.md#L24"`. Con `LLM_MODEL=fake` devuelve las
  cuatro dependencias que el runbook afirma, con su línea.
  **Idempotente**: el propio script lo verifica y lo reporta.
- **`docs/GUION.md`** — el §5 minuto a minuto: qué pantalla y qué se
  dice, más las cuatro respuestas de una línea del §10.
- **`docs/PLAN_B.md`** — tres niveles de degradación (simulado,
  reproducción de trazas, video), con el comando de grabación verificado.
- **`docs/demo_decisions.md`** — once puntos de qué se simplificó y por
  qué, y uno explícito de qué **no** se simplificó.
- **`scripts/arrancar.sh`** — levanta los cuatro servicios, espera a que
  respondan y dice cuál falló si alguno no lo hace. **2,9 segundos.**
- **`scripts/medir_tiempos.py`** — tres corridas cronometradas contra el
  presupuesto de 90 s del §10; si no se cumple, imprime las mitigaciones
  del §8 con sus comandos.
- **`tests/test_extraccion.py`** (19 tests) y **`tests/test_ui.py`** (7,
  con `streamlit.testing.v1.AppTest`).

### Cambios fuera de F6/F7 que hicieron falta

El entregable 2 pedía que el linaje de la advertencia sobre
`nomina-batch` citara la línea del runbook. Ese linaje vive en la
**arista**, y no había forma de leerlo:

1. **`data/graph_backend.py`: `neighbors_with_edge()`** en la interfaz y
   en los dos backends — devuelve `(vecino, propiedades_de_la_arista)`.
   Método nuevo; no cambia ninguno de los anteriores.
2. **`mcp/ekl_cypher.py`: variables de relación.** `-[r:DEPENDE_DE]->`
   ahora se puede nombrar y consultar (`RETURN r.fuente`). Sin variable,
   el comportamiento es idéntico al de antes.
3. **`agents/agent_b/grafo.py`:** las consultas piden `r.fuente` y cada
   servicio del contrato viaja con **su** linaje, no solo con el del
   bloque.
4. **`agents/agent_a/grafo.py` y `agents/llm_fake.py`:** la evidencia A2A
   conserva las fuentes por servicio y la advertencia las cita.

### Decisiones de F6

1. **La extracción escribe en `data/rel_depende_de.csv`, no solo en el
   grafo.** Es la fuente de la que `load_graph.py` construye el grafo en
   los dos backends: escribir solo en memoria se perdería al terminar el
   proceso, y escribir solo en Neo4j dejaría el modo `memory` sin linaje.
   Si `GRAPH_BACKEND=neo4j`, además actualiza las aristas en la base.
   Las filas no cambian —quién depende de quién ya estaba—: lo que cambia
   es la columna `fuente`, que pasa de `carga_inicial` a la cita del
   documento. Es exactamente para lo que existe F6.
2. **La canonicalización descarta antes que inventar.** Umbral de 0,82 y
   una regla dura: **si los grupos de dígitos difieren, no hay
   coincidencia**, por parecidos que sean los nombres. Sin ella,
   `PAY-DB-3` —una base que no existe— se mapeaba alegremente a
   `PAY-DB-01` con 0,82 de similitud. En un identificador un dígito no es
   una errata, es otro objeto. Lo cubre un test.
3. **El linaje de B va en lenguaje de dominio, no en Cypher.** El §4.6
   pide devolver linaje y a la vez no exponer el esquema. Se resuelve
   citando el documento y la línea (`runbook_pagos.md#L24`), que es lo
   que un auditor puede abrir, en vez del patrón del grafo.

### Bugs encontrados al probar la UI de verdad

Probar la pestaña Capa semántica con `AppTest` —el marco oficial de
Streamlit, que ejecuta la app sin navegador— sacó dos fallos que no se
ven hasta que alguien pulsa el botón en vivo:

1. **"Descartar cambios" lanzaba una excepción en pantalla.** El handler
   escribía `st.session_state["editor_semantica"]` después de que el
   widget con esa clave ya se había instanciado en la misma pasada, y
   Streamlit levanta `StreamlitWidgetAlreadyInstantiatedError`. El botón
   no descartaba nada y el presentador se habría comido un traceback rojo
   en mitad del wow #1. Corregido con el patrón correcto: una bandera que
   se consume **antes** de crear el widget.
2. **El editor no se refrescaba si el archivo cambiaba en disco.**
   `st.session_state` se sembraba una vez y no volvía a mirar el
   archivo: tras un `demo_reset.sh` —o tras revertir la métrica a mano—
   el editor seguía mostrando lo viejo, y Guardar lo habría vuelto a
   escribir deshaciendo el cambio sin avisar. Ahora se refresca por
   `mtime` si no hay cambios sin guardar, y avisa si los hay. Además,
   "Vigente en disco" lee del archivo y no del editor, que es lo que los
   servidores MCP van a usar.

### Entregable 6 — **NO se pudo completar**

El encargo pide *"ejecuta `demo_smoke.py` tres veces seguidas con el
modelo real y reporta tiempos"*. **No hay `ANTHROPIC_API_KEY` en este
entorno** —comprobado al empezar esta sesión, como en las dos
anteriores— así que la medición con modelo real **está pendiente y es lo
único del plan que queda sin hacer.** No se aplicó ninguna mitigación del
§8 porque aplicarlas sin haber medido sería adivinar.

Lo que sí se hizo para que sea **un comando** cuando tengas la clave:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
LLM_MODEL=claude-sonnet-5 python scripts/medir_tiempos.py
```

Y lo que se midió y se puede afirmar:

- **El piso sin LLM es 0,2 s** para la pregunta principal (mediana de
  tres corridas; peor caso 1,1 s incluyendo el arranque en frío). Eso es
  todo lo que cuestan los servidores MCP, el RBAC, la delegación A2A, el
  recorrido del grafo y DuckDB juntos. **Prácticamente los 90 s del
  presupuesto están disponibles para el modelo.**
- **La pregunta principal hace 12 llamadas estructuradas al LLM**
  (1 planner, 1 resolver_contexto, 1 navegar_grafo, 1 ejecutar_accion,
  2 delegar_a2a, 3 nodo_critico, 1 responder, más 2 del Agente B). El
  presupuesto por llamada es de **7,5 s**.
- **Las mitigaciones del §8 están implementadas y listas**, apagadas por
  defecto:
  - `LLM_MODEL_PLANNER` / `LLM_MODEL_CRITICO` — modelo distinto solo para
    esos dos pasos, que no redactan (`agents/llm.py::modelo_para`).
    Bajarlos a Haiku quita 4 de las 12 llamadas del modelo caro.
  - `EKL_CACHE_PLAN=1` — caché del plan del planner
    (`agents/cache_plan.py`), con clave sobre pregunta + rol + huella del
    esquema. **Apagada a propósito**: con ella la Sala de control ya no
    enseña al planner pensando, y eso es el minuto 1-3 del guion.
  - `medir_tiempos.py` imprime las tres, en orden, si se pasa del
    presupuesto.

**Si al medir con modelo real se pasa de 90 s**, el orden a aplicar es el
que imprime el script: primero los modelos por paso, luego la caché del
plan, y como último recurso bajar `max_iterations` a 4.

### Criterio de cierre — verificado

- **Tres corridas seguidas sin fallo**: `medir_tiempos.py` con
  `LLM_MODEL=fake`, 12 preguntas en total, cero errores.
- **Pregunta principal por debajo de 90 s**: 0,9 s en el peor caso **con
  el LLM simulado**. Con modelo real está pendiente de medir (arriba).
- **El guion se puede seguir de principio a fin en menos de 10 minutos**:
  - narración de los ocho bloques: **5,0 minutos** a 145 palabras/minuto
    (720 palabras), y **cada bloque cabe en su minuto** con margen;
  - tiempo de máquina de todos los beats juntos: **3,3 segundos**
    (ejecutar, abrir la traza, wow #1, wow #2, comparar);
  - quedan ~5 minutos para navegar, hacer clic, pausar y que la audiencia
    reaccione.
  - Recorrido en la configuración real (`GRAPH_BACKEND=neo4j`,
    `EVENT_BUS=redis`), comprobando cada beat: la consulta del minuto 0-1
    devuelve 11 filas con `pagos-core` marcado `dominio_owner: infra`;
    3 cifras con linaje; la advertencia cita `runbook_pagos.md#L24`; con
    `analista_junior` `CLI01` no aparece ni en el texto, ni en la
    evidencia, ni en la ruta.
- `pytest tests/` → **66 passed** (10 datos + 10 MCP + 20 agentes + 19
  extracción + 7 UI).
- `demo_smoke.py` → **24 comprobaciones**.
- `./scripts/arrancar.sh` levanta los cuatro servicios en **2,9 s**.
- La extracción es idempotente: dos corridas dejan
  `rel_depende_de.csv` **idéntico byte a byte** (md5 comprobado).

### Estado final del repo

| Fase | Qué |
|------|-----|
| F0, F1 | Datos, ontología, capa semántica, políticas |
| F2, F2b | Servidores MCP con RBAC y auditoría; bus de eventos y trazas |
| F3, F4 | Agente B (A2A) y Agente A (LangGraph, 7 nodos, nodo crítico) |
| F5, F5b | UI Streamlit (6 pestañas) y Sala de control |
| F6 | Extracción desde documentos con linaje por línea |
| F7 | Guion, plan B, decisiones, arranque en un comando, medición |

**Lo único pendiente de todo el plan** es medir con un modelo real y, si
hace falta, aplicar las mitigaciones que ya están implementadas.

---

## Corrección — `temperature` con modelos nuevos

**Fecha:** 2026-09-16
**Síntoma:** con `LLM_MODEL=claude-sonnet-5` y una API key real, la
corrida se caía en el primer paso del planner:

```
AnthropicInvalidRequestError: Error code: 400 —
invalid_request_error: `temperature` is deprecated for this model.
```

**Causa:** `agents/llm.py::LLMAnthropic` construía el cliente con
`temperature=0.0` siempre. Se había puesto buscando corridas repetibles,
pero `claude-sonnet-5` y los modelos posteriores rechazan el parámetro.
Como el planner es el primer paso, la corrida no llegaba a ningún lado.
Nunca se detectó antes porque todo se había probado con `LLM_MODEL=fake`
(ver "Qué NO se pudo probar" de las sesiones 3 a 5): el simulado no
construye cliente HTTP.

**Arreglo:** `temperature` solo se manda si alguien lo pide con
`EKL_LLM_TEMPERATURE`. Sirve para los dos casos: los modelos que lo
aceptan usan su valor por defecto y los nuevos no lo ven. De paso,
`max_tokens` pasa a ser configurable con `EKL_LLM_MAX_TOKENS` (2048 por
defecto), y un valor mal escrito en cualquiera de las dos avisa y sigue
con el valor por defecto en vez de impedir el arranque.

**Verificación:** tres tests nuevos en `tests/test_agents.py` que
levantan un servidor local, capturan **el cuerpo real de la petición** y
comprueban que `temperature` no viaja por defecto, que sí viaja con
`EKL_LLM_TEMPERATURE=0`, y que un valor inválido no rompe nada.
Comprobar el atributo del cliente no habría bastado: lo que rechaza la
API es el payload. `pytest tests/` → **69 passed**.

**Lo que esto no arregla:** sigue sin haberse ejecutado una corrida
completa contra un modelo real desde este entorno (no hay API key aquí),
así que **la medición de tiempos del §10 sigue pendiente** y puede
aparecer otro desajuste de API más adelante en el flujo. El siguiente
paso natural en el equipo que sí tiene clave:

```bash
LLM_MODEL=claude-sonnet-5 python scripts/demo_smoke.py --solo-principal
LLM_MODEL=claude-sonnet-5 python scripts/medir_tiempos.py
```

---

## Ensayo con el modelo real — medición y arreglos (F7, criterio §10)

Medido con `claude-sonnet-5`, `GRAPH_BACKEND=memory`, `EVENT_BUS=memory`,
una corrida de las 4 preguntas por configuración.

### Lo que se midió

| configuración | principal | total 4 preguntas | iteraciones |
|---|---|---|---|
| base (tras ver las cifras el crítico) | 200.5 s | 741.5 s | 5 |
| §8 mit.1: Haiku en planner y crítico | **261.4 s** | 756.0 s | 6 (tope) |
| bitácora de consultas | 131.6 s | 518.1 s | 3 — **pero sin cifras** |
| bitácora + huecos visibles (correcto) | 194.4 s | — | 6 |

**La pregunta principal NO cumple el presupuesto de 90 s del §10.** El
mejor tiempo con una respuesta correcta es ~194 s.

### La mitigación 1 del §8 es contraproducente, y está medido

Poner un modelo rápido en el crítico baja el coste por llamada de 16.6 s a
5.9 s, pero el crítico deja de converger: 7 veredictos (el tope) en 3 de
las 4 preguntas, y la principal **empeora** un 30 %. Lo que domina el
tiempo no es el precio de cada llamada sino **cuántas iteraciones**, y
cada iteración arrastra los nodos caros (navegación, delegación, acción).
Un crítico más barato y peor sale más caro. No se aplica.

### Cuatro fallos que solo aparecen con el modelo real

1. **El crítico no veía las cifras que exigía.** Su prompt renderizaba la
   evidencia como `(id, tipo, descripción)` y la descripción de una cifra
   es "exposicion_crediticia de CLI01": dice qué se consultó, no cuánto
   dio. Pedía cinco veces seguidas "los valores numéricos exactos" con los
   valores ya recogidos. Ahora ve `= 5.300.000.000 COP`.

2. **Sin timeout, un corte de red cuelga la demo para siempre.** Una
   corrida quedó 4 h parada con el socket abierto y cero bytes: el cliente
   no tenía `timeout` ni `max_retries`. Ahora 60 s y 2 reintentos
   (`EKL_LLM_TIMEOUT`, `EKL_LLM_REINTENTOS`).

3. **41 de 64 llamadas eran repeticiones exactas.** Los prompts enseñaban
   la evidencia pero no *qué se había consultado*, así que el modelo volvía
   a pedir lo mismo cada iteración. Se añadió `consultas_hechas`, una
   bitácora que va en los prompts y corta la repetición. Bajaron a 18/34.

4. **Los huecos se reportaban solo del primer dominio pendiente.** Un
   `elif` en `_detectar_huecos` hacía que una sub-pregunta de infra sin
   resolver tapara el hueco de las cifras: el crítico daba por buena una
   respuesta **sin un solo número** y el smoke lo cazó. Ahora se enseñan
   todos los huecos y solo se prioriza el destino. Además, un nodo sin
   acción gobernada deja un hueco explícito en vez de un `continue` mudo.

### Dos decisiones de diseño que salieron de aquí

- **El presupuesto de reescritura de la respuesta es suyo, no el de
  exploración.** Estaban atados, así que una corrida que hubiera gastado
  sus iteraciones entregaba una cifra sin linaje sin intentar corregirla,
  que es justo la regla que la demo promete. Siguen siendo 2 intentos.

- **El texto dice cuántos nodos se podaron, no cuáles.** El modelo real
  escribía "el cliente CLI01 fue podado por política" en las advertencias:
  un control de acceso que nombra el id de lo que te escondió ya filtró lo
  que protegía. Al redactor se le pasa el conteo; los ids siguen en
  `politicas_aplicadas` y en la traza, que es lo que mira quien audita
  (era la decisión ya escrita en `docs/demo_decisions.md` §9, que el
  prompt no respetaba).

- **`impacto_cambio('CHG-…')` ya resuelve quién aprueba**, derivándolo de
  la persona responsable del servicio afectado. Antes devolvía
  `responsable: None` y la respuesta admitía no saberlo con el dato a un
  salto en el grafo.

### Estado de la verificación

- `LLM_MODEL=fake`: **75 tests** y **24 comprobaciones** del smoke, en verde.
- `LLM_MODEL=claude-sonnet-5`: la pregunta principal pasa sus **8**
  comprobaciones (3 clientes con cifra, todas > 5.000 M, Laura Gómez,
  `nomina-batch`, linaje completo, sin cifras huérfanas) y la alternativa 1
  sus 3. Las preguntas 3 y 4 de la última pasada quedaron sin verificar
  porque **la clave de API se quedó sin saldo a mitad de corrida**
  (`credit balance is too low`), no por el código.

### Pendiente

Bajar de 90 s exige tocar el diseño, no los parámetros: hoy son ~30
llamadas LLM secuenciales de 5-8 s. Las salidas serían reducir
`max_iterations` (arriesga perder las cifras: la corrida buena necesitó 5)
o agrupar varios pasos en una sola llamada. Queda documentado, no aplicado.
