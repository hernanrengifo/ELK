# Plan detallado — Demo funcional EKL

**Objetivo de la demo:** demostrar en 10 minutos que un agente puede responder una pregunta multi-hop de negocio navegando un grafo de conocimiento gobernado, ejecutando acciones expuestas por MCP, delegando a otro agente vía A2A cuando el conocimiento está fuera de su dominio, y entregando una respuesta con ruta auditable y permisos aplicados — **sin que el agente conozca dónde viven los datos ni cómo acceder a ellos**.

**Decisiones tomadas:**
- LLM: Claude API (Anthropic). Modelo agnóstico por configuración (`LLM_MODEL` en `.env`) para poder mostrar el principio de LLM-agnosticism.
- Runtime: Docker Compose en laptop (Neo4j + 2 agentes + MCP server + UI). Sin dependencias cloud aparte de la API del LLM.
- A2A: dos agentes reales en procesos separados, con *agent card* y protocolo HTTP/JSON-RPC estilo A2A.
- Lenguaje: Python 3.11+. Orquestación con LangGraph. MCP con el SDK oficial de Python (`mcp`).

---

## 1. La pregunta hilo conductor y por qué es multi-hop

> **"¿Qué clientes corporativos con exposición crediticia mayor a 5.000 millones se verían afectados si migramos la base de datos del servicio de pagos el próximo fin de semana, y quién es el responsable técnico de ese servicio hoy?"**

Descomposición en saltos y dominios:

| Salto | Pregunta parcial | Dominio / grafo | Cómo se resuelve |
|-------|------------------|-----------------|------------------|
| 1 | ¿Qué servicios dependen de la BD `PAY-DB-01`? | Infraestructura (Agente B) | Recorrido `(:Database)<-[:DEPENDE_DE]-(:Servicio)` |
| 2 | ¿Qué productos usan el servicio de pagos? | Negocio (Agente A) | `(:Servicio)<-[:SE_EJECUTA_EN]-(:Producto)` |
| 3 | ¿Qué clientes corporativos tienen esos productos? | Negocio (Agente A) | `(:Producto)<-[:TIENE]-(:Cliente {segmento:'Corporativo'})` |
| 4 | ¿Cuál es su exposición crediticia? (métrica gobernada) | Capa semántica → datos | Acción MCP `retrieve_customer_position` → definición de "exposición crediticia" resuelta desde la capa semántica, no por el agente |
| 5 | ¿Quién es responsable del servicio? | Infraestructura (Agente B) | `(:Servicio)-[:ES_RESPONSABLE]->(:Persona)` (personas como nodos) |

El salto 1 y el 5 **no están en el grafo del Agente A**: obligan a la delegación A2A. El salto 4 obliga a usar una acción MCP con semántica gobernada. Esa es la gracia.

---

## 2. Arquitectura de la demo

```
┌──────────────────────────────────────────────────────────────────────┐
│  UI (Streamlit)  — pregunta, respuesta, ruta de nodos, linaje, RBAC  │
│  + SALA DE CONTROL — secuencia viva A↔MCP↔B, razonamiento, políticas │
└───────────────┬──────────────────────────────────────▲───────────────┘
                │ HTTP                                 │ SSE (eventos)
                │                    ┌─────────────────┴──────────────┐
                │                    │ event_bus (Redis Streams)       │
                │                    │ + trace_store (trace.jsonl)     │
                │                    │ + OTel exporter (opcional)      │
                │                    └───▲──────▲──────────▲──────▲───┘
                │       eventos: thought, plan, tool_call, a2a_*, policy_decision, critic_verdict
                │                        │      │          │      │  (todos los procesos emiten)
                │ HTTP
┌───────────────▼──────────────────────────────────────────────────────┐
│  AGENTE A — "Orquestador de Negocio" (LangGraph)                     │
│  Estado: query, user_ctx, plan, visited_nodes, evidence, is_complete │
│  Nodos: planner → resolver_contexto → navegar_grafo → ejecutar_accion│
│         → delegar_a2a → nodo_critico → responder                     │
└──────┬─────────────────────────┬───────────────────────┬─────────────┘
       │ MCP (stdio/SSE)         │ MCP                   │ A2A (HTTP JSON-RPC)
┌──────▼──────────┐   ┌──────────▼───────────┐   ┌───────▼──────────────────┐
│ MCP Server      │   │ MCP Server           │   │ AGENTE B — "Infra & TI"  │
│ ekl-graph       │   │ ekl-actions          │   │ (LangGraph, propio grafo)│
│ get_schema      │   │ retrieve_customer_   │   │ agent card /.well-known  │
│ read_cypher     │   │   position           │   │ skills: impacto_cambio,  │
│ find_entry_nodes│   │ list_node_actions    │   │   responsable_servicio   │
│ (RBAC filter)   │   │ resolve_metric       │   └───────┬──────────────────┘
└──────┬──────────┘   └──────────┬───────────┘           │ MCP
       │                         │                ┌──────▼──────────┐
┌──────▼─────────────────────────▼──────┐        │ MCP ekl-graph   │
│ Neo4j — grafo NEGOCIO (db: negocio)   │        │ (db: infra)     │
│ Cliente, Producto, Cuenta, Servicio*  │        └──────┬──────────┘
│ *Servicio = nodo frontera (referencia)│        ┌──────▼──────────┐
└──────┬────────────────────────────────┘        │ Neo4j — grafo   │
       │                                         │ INFRA           │
┌──────▼────────────────────────────────┐        │ Servicio, BD,   │
│ Capa semántica (semantic_layer.yaml)  │        │ Persona, Cambio │
│ métricas: exposicion_crediticia,      │        └─────────────────┘
│ cliente_activo → SQL sobre DuckDB     │
│ Capa de datos: DuckDB (posiciones.csv)│
└───────────────────────────────────────┘
```

**Mapeo a los 5 estratos de la presentación** (para decirlo en voz alta durante la demo):

| Estrato | Componente de la demo |
|---------|-----------------------|
| 1. Datos / sistemas de registro | DuckDB con `posiciones.csv` (simula el lakehouse); CSVs de carga |
| 2. Ingesta y extracción | Script `load_graph.py` + `extract_from_docs.py` (extrae relaciones `DEPENDE_DE` de un runbook en Markdown usando el LLM: NER + extracción de relaciones + canonicalización) |
| 3. Grafo + ontología | Neo4j con dos bases (`negocio`, `infra`); `ontology.yaml` con tipos, aristas permitidas y alineación a BIAN Service Domains |
| 4. Orquestación e interfaces | Agente A y B (LangGraph); MCP servers `ekl-graph` y `ekl-actions`; A2A |
| 5. Gobernanza y observabilidad | `policies.yaml` (RBAC por rol y por etiqueta de nodo), `audit.jsonl` (cada llamada MCP/A2A y cada salto), linaje en la respuesta, **Sala de control** (§4.8) con la traza completa de comunicación y razonamiento por ejecución |

---

## 3. Modelo de datos

### 3.1 Ontología (`ontology.yaml`) — alineada a BIAN

| Tipo de nodo | BIAN Service Domain de referencia | Propiedades clave | Grafo |
|--------------|----------------------------------|-------------------|-------|
| `Cliente` | Party / Customer Reference Data Mgmt | `id, nombre, segmento, sensibilidad` | negocio |
| `Producto` | Product Directory / Loans & Deposits | `id, nombre, tipo` | negocio |
| `Cuenta` | Current Account / Customer Position | `id, moneda` | negocio |
| `Servicio` | (frontera) | `id, nombre, dominio_owner: "infra"` | ambos (referencia en negocio, completo en infra) |
| `BaseDatos` | — (infra) | `id, motor, ambiente` | infra |
| `Persona` | — (nodo de experiencia) | `id, nombre, rol, contacto` | infra |
| `Cambio` | — (infra) | `id, descripcion, fecha, ventana` | infra |
| `Metrica` | (capa semántica) | `nombre, definicion, owner, version` | negocio |

Aristas: `TIENE` (Cliente→Producto), `SE_LIQUIDA_EN` (Producto→Cuenta), `SE_EJECUTA_EN` (Producto→Servicio), `DEPENDE_DE` (Servicio→BaseDatos, Servicio→Servicio), `ES_RESPONSABLE` (Servicio→Persona), `AFECTA` (Cambio→BaseDatos), `EXPONE_ACCION` (Servicio/Cliente→Accion).

### 3.2 Nodos accionables

Nodos de tipo `Accion` conectados al nodo que los expone. El agente descubre la acción navegando el grafo, nunca la conoce de antemano.

```yaml
# accion_retrieve_customer_position
nombre: retrieve_customer_position
expuesta_por: Cliente
contrato:
  entrada: { customer_ref: string, metrica: string }
  salida:  { valor: number, moneda: string, fecha_corte: date, linaje: string }
backend: mcp://ekl-actions/retrieve_customer_position   # el agente NO ve esto
politica: requiere_rol: [riesgo, direccion]
```

Para el técnico en la sala: el agente recibe `nombre + contrato + política`; el MCP server resuelve `backend`, la métrica y la fuente.

### 3.3 Capa semántica (`semantic_layer.yaml`)

```yaml
metricas:
  exposicion_crediticia:
    version: 2
    owner: Riesgo
    definicion: "Saldo dispuesto + cupo no utilizado comprometido, en COP, a fecha de corte"
    sql: "SELECT SUM(saldo_dispuesto + cupo_comprometido) FROM posiciones WHERE cliente_id = ? AND fecha = (SELECT MAX(fecha) FROM posiciones)"
  cliente_activo:
    version: 1
    definicion: "Cliente con al menos una transacción en los últimos 90 días"
```

Momento "wow" #1: cambiar `exposicion_crediticia` a versión 3 (por ejemplo, excluir cupo no utilizado) en vivo y repetir la pregunta: la lista de clientes afectados cambia **sin tocar prompt ni código**.

### 3.4 Datos sintéticos (tamaño mínimo)

- 12 clientes (8 corporativos, 4 pyme), nombres ficticios. 2 marcados `sensibilidad: alta`.
- 6 productos (crédito rotativo, tesorería, nómina, cash management, CDT, leasing).
- 5 servicios: `pagos-core`, `nomina-batch`, `tesoreria-api`, `onboarding`, `notificaciones`.
- 4 bases de datos: `PAY-DB-01` (Oracle, prod), `PAY-DB-02`, `TES-DB`, `ONB-DB`.
- 6 personas con roles (owner técnico, arquitecto, DBA…).
- 1 cambio: `CHG-2026-0917 migración PAY-DB-01 → Postgres, ventana sábado 02:00–06:00`.
- `posiciones.csv`: ~40 filas (cliente × fecha) con saldos.
- `runbook_pagos.md`: 1 página de texto que menciona dependencias en lenguaje natural (fuente para la extracción del estrato 2).

Resultado esperado de la pregunta: 3 clientes corporativos (con cifras), responsable "Laura Gómez — Líder técnico pagos-core", y una advertencia de que `nomina-batch` también depende de `PAY-DB-01` (hallazgo colateral que el nodo crítico agrega).

---

## 4. Componentes a construir

### 4.1 `infra/` — Docker Compose
- `neo4j:5` con dos bases (`negocio`, `infra`) y APOC.
- Servicios: `agent-a` (puerto 8001), `agent-b` (8002), `mcp-actions` (8010, SSE), `ui` (8501).
- `.env`: `ANTHROPIC_API_KEY`, `LLM_MODEL`, `NEO4J_*`, `DEMO_USER_ROLE`.

### 4.2 `data/` — carga
- `load_graph.py`: crea constraints, carga CSVs a ambas bases, crea nodos `Accion` y `Metrica`.
- `extract_from_docs.py`: lee `runbook_pagos.md`, pide al LLM tripletas `(servicio, DEPENDE_DE, bd)`, canonicaliza contra nodos existentes (fuzzy match), escribe aristas con propiedad `fuente: "runbook_pagos.md#L12"`. Sirve para mostrar el estrato 2 y el linaje.

### 4.3 `mcp/ekl_graph_server.py`
Herramientas (parametrizadas por base y por `user_ctx`):
- `get_schema()` → tipos, aristas, acciones disponibles.
- `find_entry_nodes(texto)` → búsqueda por nombre/embedding simple (índice full-text de Neo4j; opcional vector index) para localizar nodos de entrada.
- `read_cypher(query)` → solo lectura, con **filtro RBAC inyectado**: si el rol no puede ver `sensibilidad: alta`, el servidor reescribe/filtra el resultado y registra el podado en auditoría.
- `list_node_actions(node_id)` → acciones expuestas por ese nodo con su contrato (sin backend).
- Todo se escribe a `audit.jsonl` con `timestamp, user, tool, args, nodos_devueltos, filtrados`.

### 4.4 `mcp/ekl_actions_server.py`
- `resolve_metric(nombre)` → definición vigente + versión + owner (desde `semantic_layer.yaml`).
- `retrieve_customer_position(customer_ref, metrica)` → ejecuta el SQL de la métrica sobre DuckDB, devuelve valor + linaje (`fuente: posiciones.csv, metrica: exposicion_crediticia v2`).
- Verifica política de la acción contra el rol del usuario; deniega con mensaje explicable.

### 4.5 `agents/agent_a/` — Orquestador de Negocio (LangGraph)
Estado (`AgentState`): `query, user_ctx, plan[], visited_nodes[], evidence[], pending_gaps[], is_complete, iteration, max_iterations=6, audit_trail[]`.

Nodos del grafo de estado:
1. **planner** — descompone la pregunta en sub-preguntas (salida estructurada JSON). Usa `get_schema` para saber qué existe en su dominio y qué es frontera.
2. **resolver_contexto** — para cada concepto de negocio ("exposición crediticia", "cliente corporativo") llama `resolve_metric` / ontología; fija definiciones antes de navegar.
3. **navegar_grafo** — `find_entry_nodes` → `read_cypher` con recorridos de k saltos; acumula `visited_nodes` y `evidence`.
4. **ejecutar_accion** — cuando un nodo en la ruta expone una acción necesaria, la invoca vía MCP (`retrieve_customer_position`).
5. **delegar_a2a** — cuando el planner o el nodo crítico detectan que un nodo es frontera (`dominio_owner != negocio`), consulta el agent card de B y envía una tarea A2A con el contexto mínimo (id del servicio, identidad del usuario, pregunta parcial). Recibe respuesta estructurada + linaje de B.
6. **nodo_critico** — evalúa suficiencia topológica: ¿cada sub-pregunta del plan tiene evidencia? ¿la ruta llega de la BD hasta los clientes sin huecos? Si no, emite `directiva_refinamiento` (por ejemplo, "expandir desde `pagos-core` hacia otros servicios dependientes") y vuelve a 3 o 5. Corta en `max_iterations`.
7. **responder** — redacta la respuesta con: hallazgos, ruta de nodos, tabla de evidencia con linaje por dato, políticas aplicadas (qué se filtró y por qué), y advertencias colaterales.

### 4.6 `agents/agent_b/` — Agente de Infraestructura (A2A server)
- Expone `/.well-known/agent.json` (agent card) con skills: `impacto_cambio(servicio|bd)`, `responsable_servicio(servicio)`.
- Endpoint `tasks/send` (JSON-RPC) → LangGraph propio, más simple (2 nodos), que usa su MCP `ekl-graph` sobre la base `infra`.
- Devuelve solo el contrato acordado: lista de servicios afectados, responsable, y linaje (`fuente: runbook_pagos.md#L12`, `grafo infra`). No expone su Cypher ni su esquema.
- Propaga la identidad del usuario original para aplicar RBAC en su propio dominio.

### 4.7 `ui/app.py` — Streamlit
Layout en dos columnas:
- Izquierda: selector de rol (`direccion`, `riesgo`, `analista_junior`), caja de pregunta (con la pregunta hilo conductor precargada y 2 alternativas), botón "Ejecutar".
- Derecha, pestañas: **Respuesta** · **Ruta** (grafo recorrido renderizado con `pyvis`/`streamlit-agraph`, nodos coloreados por dominio y por "vino por A2A") · **Evidencia y linaje** (tabla) · **Auditoría** (tail de `audit.jsonl` en vivo) · **Capa semántica** (editor del YAML para el momento "wow" #1) · **Sala de control** (§4.8; también disponible como página independiente en `/control` para proyectarla en una segunda pantalla mientras la principal muestra la pregunta y la respuesta).

### 4.8 `observability/` — Sala de control: observabilidad de la comunicación y el razonamiento

**Propósito:** que la audiencia vea, en tiempo real y en una sola pantalla, qué está pensando cada agente, qué se están diciendo entre ellos (A2A), qué herramientas invocan (MCP) y qué devuelve cada capa. Es la pieza que convierte la demo de "caja negra que responde" en "sistema explicable". Se presenta en una segunda pantalla o en la pestaña **Sala de control** de la UI.

**Arquitectura de eventos**

```
Agente A ──┐                      ┌── UI: Sala de control (SSE/WebSocket)
Agente B ──┼─► event_bus ─► trace_store ─┤
MCP graph ─┤   (Redis Streams        │   └── trace.jsonl (persistente, por run_id)
MCP actions┘    o cola in-process)   └── OpenTelemetry exporter (opcional: Jaeger/LangSmith)
```

- Cada proceso emite eventos a un bus común (`Redis Streams` en Docker; fallback in-process para desarrollo). Todos los eventos comparten `run_id` (una ejecución de pregunta) y `trace_id`/`span_id` para poder anidar.
- `trace_store` persiste a `trace.jsonl` y sirve a la UI por SSE, de modo que la pantalla se actualiza mientras el agente trabaja (no al final).

**Esquema de evento** (`events.py`, Pydantic):

```yaml
run_id, ts, seq
source:   agent_a | agent_b | mcp_graph | mcp_actions | ui
kind:     thought | plan | tool_call | tool_result | a2a_request | a2a_response
          | policy_decision | critic_verdict | refinement | answer | error
step:     nombre del nodo LangGraph (planner, navegar_grafo, nodo_critico…)
payload:  contenido específico del tipo (ver abajo)
meta:     duracion_ms, tokens_in, tokens_out, modelo, user_role, iteration
```

Payloads por tipo:
- `thought` — texto del razonamiento del LLM en ese paso (se pide al modelo salida estructurada `{razonamiento, decision}` para poder mostrar el razonamiento sin exponer el prompt completo).
- `plan` — lista de sub-preguntas con estado (`pendiente / en_curso / resuelta / delegada`).
- `tool_call` / `tool_result` — servidor MCP, herramienta, argumentos, resultado resumido, nodos devueltos y **nodos filtrados por RBAC**.
- `a2a_request` / `a2a_response` — agente origen y destino, skill invocada, contexto enviado (mostrar explícitamente lo que **no** se envía: ni esquema, ni credenciales), respuesta con linaje, latencia.
- `policy_decision` — regla aplicada, resultado (permitido / denegado / podado), justificación.
- `critic_verdict` — `is_complete`, huecos detectados, directiva de refinamiento emitida.
- `answer` — respuesta final y resumen de evidencia.

**Instrumentación**

- Agente A y B: un `callback` de LangGraph que emite `thought`/`plan`/`critic_verdict` al entrar y salir de cada nodo del grafo de estado; wrapper sobre el cliente MCP y sobre el cliente A2A que emite `tool_call`/`a2a_request` y sus resultados.
- MCP servers: middleware que emite `tool_call`, `tool_result` y `policy_decision` (aquí es donde se ve el podado RBAC desde el lado del servidor).
- Propagación de contexto: `run_id` y `user_ctx` viajan en los metadatos de la llamada MCP y en el cuerpo A2A, para que los eventos de B y de los servidores se cuelguen de la misma ejecución.
- Opcional: exportar los mismos spans a OpenTelemetry → Jaeger (Docker) para mostrar a los técnicos que se integra con observabilidad estándar; LangSmith como alternativa si se quiere trazado de tokens y prompts.

**Pantalla "Sala de control"** (`ui/control_room.py`, Streamlit con `st.empty()` + SSE, o una página HTML/JS ligera servida por FastAPI si se quiere más fluidez)

Layout en cuatro zonas:
1. **Diagrama de secuencia vivo** (columna izquierda, ancho completo en alto): carriles Usuario · Agente A · MCP graph · MCP actions · Agente B · Datos. Cada `tool_call`/`a2a_request` dibuja una flecha entre carriles a medida que ocurre; color por tipo (azul MCP, ámbar A2A, rojo denegado/podado, verde respuesta). Al hacer clic en una flecha se abre el payload.
2. **Cadena de razonamiento** (columna derecha, arriba): línea de tiempo con tarjetas por paso del agente: nombre del nodo, `thought` resumido, decisión tomada, duración y tokens. Los pasos del Agente B aparecen anidados bajo la flecha A2A que los originó.
3. **Plan y nodo crítico** (columna derecha, medio): las sub-preguntas del plan con su estado cambiando en vivo; cuando el crítico emite una directiva de refinamiento, se resalta el hueco y la iteración sube.
4. **Gobernanza en vivo** (columna derecha, abajo): contador de llamadas MCP/A2A, nodos filtrados por RBAC, políticas aplicadas, latencia acumulada, tokens y costo estimado de la ejecución.

Controles: selector de `run_id` (para comparar la corrida con rol `riesgo` vs `analista_junior` lado a lado), botón "reproducir" que reanima la secuencia paso a paso a velocidad ajustable (útil para el plan B y para explicar despacio), filtro por fuente y por tipo de evento, exportar `trace.jsonl`.

**Qué debe poder ver la audiencia en 30 segundos de mirar la pantalla:** que el agente primero preguntó qué significa la métrica; que descubrió la acción en el grafo; que le pidió ayuda a otro agente y qué le mandó; que el servidor le negó/podó algo por política; que el crítico lo mandó a buscar más; y cuánto costó todo.

### 4.9 `scripts/`
- `demo_reset.sh` — recarga datos y limpia auditoría.
- `demo_smoke.py` — ejecuta las 3 preguntas y verifica resultados esperados (test de regresión antes de presentar).
- `record_backup.md` — instrucciones para grabar el plan B.

---

## 5. Guion de la demo (10 minutos)

| Min | Acción en pantalla | Lo que se dice | Mensaje de la presentación que refuerza |
|-----|--------------------|----------------|------------------------------------------|
| 0–1 | Mostrar el grafo en Neo4j Browser (base negocio) y señalar el nodo `pagos-core` como frontera | "Este agente solo conoce el negocio; infraestructura vive en otro dominio con otro equipo" | Bloque 8: grafo federado |
| 1–3 | Rol `riesgo`, lanzar la pregunta **con la Sala de control en la segunda pantalla**. Ver en vivo: tarjeta `planner` con las sub-preguntas, luego `resolver_contexto` llamando `resolve_metric` | "Antes de buscar nada, el agente resuelve qué significa 'exposición crediticia' — la definición no está en el prompt. Están viendo su razonamiento, no una animación" | Bloques 5 y 9 |
| 3–5 | Sala de control: aparece la flecha ámbar A→B (A2A) con su payload abierto; debajo se anidan los pasos del Agente B. Luego pestaña Ruta con los nodos de B en otro color | "El agente no sabía qué dependía de esa BD. Miren exactamente qué le mandó al agente de infraestructura: el id del servicio y la identidad del usuario. Ni esquema, ni credenciales. Y miren qué volvió: un contrato con linaje" | Bloque 8: MCP vs A2A |
| 5–6 | Pestaña Evidencia: cada cifra con su linaje (csv + métrica v2 + runbook línea 12) | "Esto es lo que un auditor puede seguir" | Bloque 1: no negociables |
| 6–7 | Sala de control, zona Plan y nodo crítico: el veredicto `is_complete: false`, el hueco resaltado y la directiva de refinamiento; segunda flecha a B; la advertencia colateral sobre `nomina-batch` | "Un RAG vectorial habría respondido con lo primero que encontró. Aquí el crítico detectó que la ruta estaba incompleta y lo mandó a buscar más" | Bloque 6: self-correcting |
| 7–8 | **Wow #1**: editar `exposicion_crediticia` a v3 en la pestaña Capa semántica, repetir. Cambia la lista | "Cambió la regla de negocio, no el agente" | Bloque 9: context engineering |
| 8–9 | **Wow #2**: cambiar rol a `analista_junior`, repetir. En la Sala de control aparece una flecha roja `policy_decision: podado` desde el MCP server; clientes de sensibilidad alta desaparecen | "Mismo agente, mismo grafo, distinto usuario: la política vive en la capa, no en el prompt, y la aplicó el servidor, no el modelo" | Bloque 2: gobernanza |
| 9–10 | Sala de control: comparar los dos `run_id` lado a lado (riesgo vs analista_junior); zona Gobernanza con conteo de llamadas, nodos filtrados, latencia, tokens y costo | "Todo lo que hizo el agente es reconstruible, comparable y tiene precio" | Conclusiones |

Preguntas alternativas precargadas (por si alguien pide otra): "¿Qué productos quedarían sin servicio si cae `TES-DB`?" y "¿Quién debe aprobar el cambio CHG-2026-0917 y a qué clientes hay que notificar?".

---

## 6. Plan de construcción por fases

| Fase | Entregable | Criterio de "listo" | Esfuerzo estimado | Modelo sugerido |
|------|------------|---------------------|-------------------|-----------------|
| F0 — Esqueleto | Repo, Docker Compose, Neo4j arriba con 2 bases, `.env.example`, README | `docker compose up` funciona en limpio | 0.5 sesión | económico |
| F1 — Datos y ontología | `ontology.yaml`, CSVs, `load_graph.py`, `semantic_layer.yaml`, `policies.yaml`, `posiciones.csv`, `runbook_pagos.md` | Grafo cargado; consulta Cypher manual devuelve la ruta esperada | 1 sesión | económico |
| F2 — MCP servers | `ekl_graph_server.py`, `ekl_actions_server.py`, auditoría, RBAC | Probados con MCP Inspector; `retrieve_customer_position` devuelve valor + linaje; podado RBAC funciona | 1 sesión | fuerte (diseño de RBAC y contratos) |
| F3 — Agente B + A2A | Agent card, endpoint, LangGraph simple sobre grafo infra | `curl` a `tasks/send` devuelve impacto y responsable con linaje | 1 sesión | fuerte |
| F4 — Agente A | LangGraph completo con nodo crítico y delegación | La pregunta hilo conductor devuelve resultado esperado; test `demo_smoke.py` pasa | 1–2 sesiones | fuerte |
| F2b — Bus de eventos e instrumentación | `events.py`, `event_bus.py`, `trace_store`, middleware MCP, callbacks LangGraph, wrapper A2A | Una corrida produce `trace.jsonl` completo con `run_id` propagado por A, B y ambos MCP servers | 1 sesión | fuerte (diseño del esquema y propagación de contexto) |
| F5 — UI | Streamlit con 6 pestañas y grafo de ruta | Los dos momentos "wow" funcionan desde la UI | 1 sesión | económico |
| F5b — Sala de control | `ui/control_room.py` (o página FastAPI+JS): secuencia viva, cadena de razonamiento, plan/crítico, gobernanza; reproducción y comparación de runs | Se ve en vivo la flecha A2A y el podado RBAC mientras corre la pregunta; reproducción de un `run_id` funciona sin LLM | 1–1.5 sesiones | fuerte para el diagrama de secuencia vivo; económico para el resto |
| F6 — Extracción desde documentos | `extract_from_docs.py` | Aristas con `fuente` creadas desde el runbook | 0.5 sesión | económico |
| F7 — Ensayo y plan B | Ensayo cronometrado, grabación en video/GIF, `demo_reset.sh`, exportación de un `trace.jsonl` de referencia para reproducir sin LLM | Demo en < 10 min, 3 corridas seguidas sin fallo | 0.5 sesión + tu tiempo | — |

Total: ~8–9 sesiones de construcción. Orden recomendado: F0 → F1 → F2 → F2b → F3 → F4 → F5 → F5b → F6 → F7. Conviene hacer F2b antes de los agentes para que A y B nazcan instrumentados en lugar de instrumentarlos después. F6 es opcional si el tiempo aprieta (se puede cargar la arista con `fuente` directamente en F1 y solo *contar* que viene de extracción).

---

## 7. Stack y dependencias

- Python 3.11: `langgraph`, `langchain-anthropic`, `anthropic`, `mcp` (SDK oficial), `neo4j`, `duckdb`, `pyyaml`, `fastapi`, `uvicorn`, `httpx`, `streamlit`, `streamlit-agraph` (o `pyvis`), `pydantic`.
- Neo4j 5 Community + APOC (Docker). Índice full-text sobre `nombre`; vector index opcional si se quiere mostrar "vector para entrar".
- A2A: implementación ligera propia del subconjunto necesario (agent card + `tasks/send`) o el SDK `a2a-sdk` si está estable al momento de construir — decidir en F3 y dejar constancia.
- Observabilidad: `redis` (Docker) + `redis-py` para el bus de eventos; `sse-starlette` para el stream a la UI; `opentelemetry-sdk` + `opentelemetry-exporter-otlp` y Jaeger en Docker como opcional para técnicos; LangSmith como alternativa hospedada si se quiere trazado de prompts y tokens sin construir nada.
- Sala de control: Streamlit con `st.empty()` refrescado por SSE, o página HTML/JS ligera (FastAPI + `EventSource`) si el diagrama de secuencia vivo requiere más fluidez que Streamlit; decidir en F5b.

---

## 8. Riesgos y mitigaciones

| Riesgo | Mitigación |
|--------|------------|
| Latencia del LLM alarga la demo (>10 min) | Cachear el plan del planner para la pregunta principal; usar modelo rápido para planner y crítico; mostrar trazas mientras corre |
| Sin internet en la sala | Plan B: video grabado + captura de la UI; alternativamente `LLM_MODEL` apuntando a Ollama local (calidad menor, pero funciona) |
| Nodo crítico entra en bucle | `max_iterations=6`, y la directiva de refinamiento debe ser distinta a la anterior o se corta |
| El agente "adivina" en lugar de usar la acción MCP | Prompt del planner: prohibido inventar cifras; toda cifra debe venir de `evidence[]` con linaje; el nodo crítico rechaza respuestas con cifras sin fuente |
| Datos sintéticos poco creíbles para banqueros | Nombres y montos plausibles para Colombia (COP), productos reales del portafolio corporativo |
| Confusión MCP vs A2A en la audiencia | Colores distintos en la pestaña Ruta y una línea de narración explícita en el minuto 3–5 |
| Docker falla en el equipo de presentación | Ensayar en el equipo final; tener `docker compose` con imágenes ya descargadas; plan B grabado |
| La Sala de control va demasiado rápido o demasiado lento para la narración | Modo "reproducir" con velocidad ajustable y pausa sobre un `trace.jsonl` grabado; en vivo, el presentador narra mientras corre y luego reproduce despacio los 3 momentos clave |
| El razonamiento del LLM expone texto confuso o demasiado largo en pantalla | Pedir salida estructurada `{razonamiento (≤ 2 frases), decision}` por paso; la tarjeta muestra el resumen y el detalle queda en un expander |
| Eventos de B o de los MCP servers no se asocian a la corrida | `run_id` obligatorio en metadatos MCP y en el cuerpo A2A; el `trace_store` rechaza eventos sin `run_id` en modo demo para detectarlo en el ensayo |

---

## 9. Qué se guarda en el proyecto al terminar

- `demo/README.md` con instrucciones de arranque y guion.
- `demo/ontology.yaml`, `semantic_layer.yaml`, `policies.yaml` (son también material para los slides 15, 17 y 27).
- Video del plan B.
- Nota de decisiones (`demo_decisions.md`): qué se simplificó respecto a producción y por qué (por ejemplo, DuckDB en lugar de lakehouse, A2A parcial).

---

## 10. Checklist antes de presentar

- [ ] `demo_smoke.py` pasa 3 veces seguidas en el equipo de presentación.
- [ ] Tiempo de la pregunta principal < 90 s de extremo a extremo.
- [ ] Los dos momentos "wow" ensayados y cronometrados.
- [ ] Video plan B grabado y accesible sin internet.
- [ ] `demo_reset.sh` ejecutado justo antes de empezar.
- [ ] Neo4j Browser abierto en la base `negocio` con el grafo ya renderizado.
- [ ] Sala de control abierta en la segunda pantalla, con un `trace.jsonl` de referencia cargado para reproducir si el LLM falla.
- [ ] Respuestas de una línea preparadas para: "¿esto escala?", "¿qué pasa con datos en tiempo real?", "¿cuánto cuesta Neo4j enterprise?", "¿por qué no solo RAG?".
