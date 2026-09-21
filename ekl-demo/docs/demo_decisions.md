# Decisiones de la demo — qué se simplificó y por qué

Esta demo es una maqueta ejecutable de la arquitectura, no un piloto. Lo
que sigue es la lista de lo que se simplificó respecto a producción, qué
cambiaría al llevarlo a serio, y —lo importante— **qué parte del
argumento se sostiene igual**. Sirve para responder con precisión cuando
alguien pregunte "ya, ¿pero esto funciona de verdad?".

Regla que se siguió en todo el repo: **simplificar el sustrato, nunca el
mecanismo**. Los datos son pequeños y el motor es ligero; la gobernanza,
el linaje, la federación y la auditoría son reales.

---

## 1. DuckDB en lugar de un lakehouse

**En la demo:** `retrieve_customer_position` ejecuta el SQL de la capa
semántica contra DuckDB en memoria, sobre `data/posiciones.csv` (40
filas).

**En producción:** ese mismo SQL va contra Databricks, Snowflake,
BigQuery o lo que haya, y la acción MCP es un cliente de ese motor.

**Por qué se puede sustituir sin tocar nada más:** el agente nunca ve el
motor. Recibe el contrato de la acción (`customer_ref`, `metrica` →
`valor`, `moneda`, `fecha_corte`, `linaje`) y el `backend` se lo queda el
servidor MCP. Cambiar de motor es cambiar una función en
`mcp/ekl_actions_server.py`.

**Qué se sostiene igual:** que la cifra la calcula la capa semántica y no
el modelo, que sale con linaje, y que la política se verifica antes de
ejecutar.

**Qué no se demuestra:** latencia y concurrencia reales. 40 filas en
memoria responden en milisegundos; un lakehouse con particiones y colas
no.

---

## 2. A2A: un subconjunto propio, no el SDK

**En la demo:** `agents/a2a.py` implementa el *agent card* en
`/.well-known/agent.json` y el método JSON-RPC `tasks/send`, en unas 180
líneas.

**Por qué no el `a2a-sdk`:** se instaló la versión publicada (1.1.2) y se
revisaron sus métodos: `message/send`, `message/stream`, `tasks/get`,
`tasks/cancel`… **`tasks/send` ya no existe** — la especificación lo
sustituyó. Usar el SDK habría obligado a cambiar el contrato que el plan
fija. Además arrastra 12 dependencias (protobuf, google-auth, grpc) para
una demo de portátil.

**Qué falta respecto al protocolo completo:** streaming
(`message/stream`), push notifications, `tasks/get` / `tasks/cancel` —
las tareas de la demo son síncronas y terminan en un turno— y
autenticación, porque los dos agentes corren en la misma máquina. El
*agent card* declara `protocolVersion: "0.2-subset"` para que no haya
duda.

**Qué se sostiene igual:** lo que la demo quiere enseñar del minuto 3-5
es **qué cruza la frontera**: la sub-pregunta, el id del objeto y la
identidad del usuario; ni esquema, ni Cypher, ni credenciales. Eso se ve
mejor en 180 líneas propias que dentro de un SDK, y el evento
`a2a_request` lleva la lista explícita de lo que **no** se envía.

**En producción:** autenticación entre agentes (mTLS o tokens), y el SDK
oficial con `message/send` en cuanto el contrato interno se alinee con la
especificación vigente.

---

## 3. Grafo en memoria como respaldo de Neo4j

**En la demo:** `GRAPH_BACKEND=memory` levanta el grafo en `networkx`
dentro del proceso; `GRAPH_BACKEND=neo4j` usa Neo4j 5 Enterprise con dos
bases (`negocio`, `infra`). Los dos están probados y `pytest tests/` pasa
entero contra los dos.

**Por qué existe el modo memoria:** para que la demo no dependa de que
Docker arranque en la sala, y para que los tests corran en dos segundos.

**El precio:** `read_cypher` no ejecuta Cypher nativo. Ejecuta un
**subconjunto interpretado** (`mcp/ekl_cypher.py`) sobre la interfaz
`GraphBackend`, para que el resultado y el podado por política sean
idénticos con los dos backends en vez de tener dos caminos distintos.
Soporta patrones lineales de k saltos, variables de relación, `WHERE` con
`AND`, `RETURN [DISTINCT]` y `LIMIT`; no soporta `OPTIONAL MATCH`,
`WITH`, `UNWIND`, `CALL`, agregaciones, caminos de longitud variable ni
`OR`, y lo dice con un error explícito cuando se lo piden.

**En producción:** Cypher nativo contra el motor, y el intérprete
desaparece. Es el componente más claramente "de demo" del repo.

**Qué se sostiene igual:** el RBAC no vive en la consulta sino en el
servidor, que poda el resultado después de ejecutarla — eso funciona
igual con cualquier motor.

---

## 4. `seq` global solo con Redis

**En la demo:** el bus de eventos tiene dos backends. Con
`EVENT_BUS=redis` el contador de secuencia es un `INCR` compartido y el
orden de la corrida es global entre los cuatro procesos. Con
`EVENT_BUS=memory` **cada proceso numera por su cuenta**, así que el
Agente A y el Agente B empiezan los dos en 1 dentro del mismo `run_id`.

**Consecuencia práctica:** para proyectar la Sala de control con los dos
agentes hay que usar Redis. El modo memoria es para tests y para
desarrollo en un solo proceso. `TraceStore.read()` ordena por `(ts, seq)`
para que el modo memoria siga siendo legible, pero no es lo mismo.

**En producción:** Redis Streams o Kafka, y los mismos eventos exportados
a OpenTelemetry. El esquema de evento ya está pensado para eso: `meta`
admite `trace_id` y `span_id`.

---

## 5. El LLM simulado

**En la demo:** `LLM_MODEL=fake` responde con un guion fijo para las tres
preguntas del §5.

**Qué simula y qué no:** simula **solo las decisiones** del modelo (qué
sub-preguntas hay, qué Cypher escribir, a qué skill delegar, si la ruta
está completa). **No simula los datos**: las consultas se ejecutan de
verdad, el RBAC poda de verdad y las cifras salen de DuckDB. Una corrida
con `fake` sigue demostrando la gobernanza entera.

**Para qué está:** para que los tests sean deterministas y rápidos, y
para que el plan B funcione sin red (§8, riesgo "sin internet en la
sala").

**El riesgo que introduce:** es fácil creer que algo funciona porque pasa
con `fake`. Por eso `demo_smoke.py` con un modelo real comprueba **los
hechos** contra la evidencia y la ruta, y solo avisa —no falla— si cambia
la redacción.

---

## 6. Datos sintéticos pequeños

**En la demo:** 12 clientes, 6 productos, 5 servicios, 4 bases de datos,
40 filas de posiciones. Nombres y montos plausibles para banca corporativa
colombiana.

**Por qué:** la ruta de la pregunta hilo conductor tiene que dar
exactamente tres clientes para que la respuesta sea verificable a ojo en
una demo. Con datos reales no se puede afirmar "tienen que salir estos
tres".

**Qué no se demuestra:** el comportamiento del podado RBAC sobre miles de
nodos, ni la latencia del recorrido en un grafo grande.

---

## 7. Extracción desde documentos: un solo documento

**En la demo:** `data/extract_from_docs.py` lee un runbook de una página,
le pide al LLM las tripletas `(servicio, DEPENDE_DE, base_de_datos)`, las
canonicaliza contra los nodos existentes con coincidencia difusa, y
escribe la arista con `fuente: "runbook_pagos.md#L24"`.

**Lo que sí es de producción:** la canonicalización **descarta** lo que no
reconoce en vez de inventarse un nodo, y en particular no confunde
`PAY-DB-01` con un `PAY-DB-3` que no existe — en un identificador, un
dígito distinto es otro objeto, no una errata. La extracción es
idempotente. Y el linaje llega hasta la respuesta del agente: la
advertencia sobre `nomina-batch` cita la línea del documento.

**En producción:** un corpus de miles de documentos, con revisión humana
de las tripletas dudosas antes de escribir en el grafo, versionado de lo
extraído, y re-extracción cuando el documento cambia. Aquí no hay cola de
revisión: lo que supera el umbral se escribe.

---

## 8. Sin autenticación de usuario

**En la demo:** el rol se elige en un desplegable y viaja en los
metadatos de la llamada MCP y en el cuerpo A2A.

**En producción:** el rol sale del token del usuario autenticado, los
servidores MCP lo validan contra el proveedor de identidad, y el agente
nunca puede afirmarlo por su cuenta.

**Qué se sostiene igual:** que la política la aplica **el servidor** y no
el modelo, y que el agente no puede saltársela porque el dato podado
nunca le llega. Cambiar de dónde viene la identidad no cambia dónde se
aplica la decisión.

---

## 9. La respuesta dice **qué** nodo se podó, no solo cuántos

**En la demo:** con rol `analista_junior`, `CLI01` no aparece en el texto
de la respuesta, ni en la evidencia, ni en la ruta — pero sí en el campo
`politicas_aplicadas.nodos_podados`. El texto que lee el usuario dice "el
servidor podó 1 nodo(s)"; el **id** está en el bloque estructurado.

**Por qué:** en una demo hay que poder señalar la pantalla y decir "ese
es el cliente que desapareció". Sin el id, el momento "wow" #2 se queda
en una afirmación.

**En producción:** al usuario se le da el conteo y la razón; los ids de
lo podado van al log de auditoría, que lee seguridad, no el usuario.
Saber que existe un `CLI01` al que no tienes acceso ya es información.

---

## 10. La Sala de control dibuja un carril que no está instrumentado

**En la demo:** el carril "Datos" del diagrama de secuencia no sale de
eventos propios —la capa de datos no emite ninguno— sino que se **deriva
del linaje** que reporta cada `tool_result` del servidor MCP. Va en gris
punteado y el panel de detalle lo marca como `derivada`.

**Por qué se dejó así:** el carril cuenta algo verdadero (esa llamada
tocó DuckDB o el grafo) y la alternativa —instrumentar la capa de datos—
no aporta nada al argumento de la demo. Lo que no se quiso es dar a
entender que hay instrumentación donde no la hay, de ahí la marca.

---

## 11. Lo que NO se simplificó

Vale la pena decirlo explícitamente, porque es donde está el argumento:

- **El RBAC es real.** Lo aplica el servidor MCP, poda el resultado antes
  de devolverlo y emite un evento `policy_decision`. El agente no ve el
  nodo podado.
- **El linaje es real y obligatorio.** El modelo `Evidencia` no deja
  construir un dato sin fuente, y el nodo crítico **rechaza** una
  respuesta con una cifra que no esté en la evidencia — con una
  comprobación determinista, no preguntándole al modelo.
- **La federación es real.** Dos grafos, dos agentes en procesos
  distintos, y un contrato entre ellos que no expone el esquema de nadie.
- **La auditoría es real.** Cada llamada MCP queda en `audit.jsonl` con
  quién, qué, cuántos nodos devolvió y cuáles filtró.
- **La capa semántica manda.** Cambiar la definición de una métrica
  cambia la respuesta sin tocar prompt ni código, en caliente.
