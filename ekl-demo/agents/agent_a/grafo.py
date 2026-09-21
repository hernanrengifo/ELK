"""
grafo.py — LangGraph del Agente A, Orquestador de Negocio
(plan_demo.md §4.5).

Los **siete nodos del plan**, en su orden:

    planner -> resolver_contexto -> navegar_grafo -> ejecutar_accion
            -> delegar_a2a -> nodo_critico -> responder

y el bucle de refinamiento: cuando el crítico ve huecos, emite una
`directiva_refinamiento` y devuelve el control a `navegar_grafo`,
`ejecutar_accion` o `delegar_a2a` (el §4.5 dice "vuelve a 3 o 5").

Reglas duras, todas verificadas en `tests/test_agents.py`:

  * `max_iterations = 6`.
  * **Una directiva igual a la anterior corta el bucle** — si el crítico
    no sabe pedir algo nuevo, insistir no va a arreglarlo (§8, riesgo
    "nodo crítico entra en bucle").
  * **Ninguna cifra en la respuesta sin estar en `evidence[]` con
    linaje.** Lo comprueba `verificacion.py` de forma determinista y el
    nodo crítico **rechaza** la respuesta y la manda reescribir.
  * **Cada paso pide al LLM `{razonamiento (≤ 2 frases), decision}`** y
    emite un evento `thought` (§4.8).

Una desviación consciente del §4.5, anotada en STATUS.md: `responder` no
es terminal. Redacta y vuelve al `nodo_critico` en fase de
**verificación**, porque el encargo exige que sea el crítico quien
rechace una respuesta con cifras sin linaje — y para rechazarla tiene
que verla. Si la aprueba, ahí termina.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from langgraph.graph import END, StateGraph

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from agents import cache_plan  # noqa: E402
from agents.a2a import NO_SE_ENVIA, ClienteA2A, ErrorA2A, construir_tarea  # noqa: E402
from agents.agent_a.verificacion import (  # noqa: E402
    cifras_sin_linaje,
    evidencias_faltantes,
)
from agents.eventos import (  # noqa: E402
    ContextoCorrida,
    cronometro,
    describir_excepcion,
    emitir,
    emitir_plan,
    emitir_thought,
)
from agents.llm import LLM  # noqa: E402
from agents.mcp_cliente import ClienteMCP, ErrorHerramienta  # noqa: E402
from agents.modelos import (  # noqa: E402
    AgentState,
    Evidencia,
    SalidaAccion,
    SalidaContexto,
    SalidaCritico,
    SalidaDelegacion,
    SalidaNavegacion,
    SalidaPlanner,
    SalidaRespuesta,
    SalidaVerificacion,
)

MAX_ITERATIONS = 6
AGENT_B_URL = os.environ.get("AGENT_B_URL", "http://127.0.0.1:8002")

SISTEMA_BASE = (
    "Eres el Orquestador de Negocio de un banco colombiano. Navegas un grafo de conocimiento "
    "gobernado y ejecutas acciones expuestas por sus nodos.\n"
    "REGLAS INNEGOCIABLES:\n"
    "1. NUNCA inventes ni estimes una cifra. Toda cifra de la respuesta tiene que venir de una "
    "evidencia recogida con su linaje. Si no la tienes, di que no la tienes.\n"
    "2. No sabes dónde viven los datos ni cómo acceder a ellos: solo puedes usar las herramientas.\n"
    "3. Los nodos marcados como frontera pertenecen a otro dominio; para saber más hay que "
    "delegar en el agente dueño, no adivinar.\n"
    "4. NUNCA inventes un id, una etiqueta, un tipo de arista ni una propiedad. Usa solo los "
    "que hayas visto en el esquema o en el resultado de una herramienta. Si no conoces el id "
    "de algo, búscalo primero con find_entry_nodes.\n"
    "5. Responde siempre en español."
)

#: Qué Cypher acepta `read_cypher` (§4.3). Va en el prompt de navegación
#: para que un modelo real no escriba lo que el servidor va a rechazar.
AYUDA_CYPHER = (
    "read_cypher acepta SOLO lectura y un subconjunto: MATCH con un patrón lineal de k saltos "
    "—(var:Label {prop:'valor'}) encadenados con -[:REL]-> o <-[:REL]-—, WHERE con condiciones "
    "unidas por AND (= <> > >= < <= CONTAINS 'STARTS WITH' 'ENDS WITH' IN), "
    "RETURN [DISTINCT] var | var.prop [AS alias] | *, y LIMIT n. "
    "NO admite OPTIONAL MATCH, WITH, UNWIND, CALL, ORDER BY, agregaciones, caminos de longitud "
    "variable ([:REL*1..3]) ni OR. Ejemplo válido:\n"
    "MATCH (c:Cliente {segmento:'corporativo'})-[:TIENE]->(p:Producto)-[:SE_EJECUTA_EN]->"
    "(s:Servicio {id:'pagos-core'}) RETURN DISTINCT c.id AS cliente_id, c.nombre AS cliente\n"
    "Filtra los nodos por las propiedades que declara el esquema, y usa el campo `id` (no "
    "`nombre`) cuando conozcas el id exacto. Las cifras de negocio —la exposición crediticia, "
    "por ejemplo— NO son propiedades del grafo: se obtienen ejecutando la acción que expone el "
    "nodo, no con un WHERE."
)


# --------------------------------------------------------------------------
# Utilidades de estado
# --------------------------------------------------------------------------
def _registrar(bitacora: list[str], firma: str, resultado: str) -> None:
    """Anota una llamada ya ejecutada y qué devolvió."""
    linea = f"{firma} -> {resultado}"
    if linea not in bitacora:
        bitacora.append(linea)


def _ya_ejecutada(bitacora: list[str], firma: str) -> bool:
    return any(l.startswith(f"{firma} -> ") for l in bitacora)


def _ya_ejecutada_prefijo(bitacora: list[str], prefijo: str) -> bool:
    """Igual que `_ya_ejecutada` pero ignorando la cola de la firma.

    Sirve para "ya tengo la cifra de CLI01", sin que un cambio de nombre
    de la métrica entre iteraciones la vuelva a pedir.
    """
    return any(l.startswith(prefijo) for l in bitacora)


def _bitacora_para_prompt(bitacora: list[str], limite: int = 24) -> str:
    """Las llamadas ya hechas, para que el modelo no las repita.

    Los prompts enseñaban la evidencia pero no *qué se había consultado*,
    así que el modelo volvía a pedir la misma consulta en cada iteración:
    medido con el modelo real, 41 de 64 llamadas de la pregunta principal
    eran repeticiones exactas. Cada una arrastra una iteración completa.
    """
    if not bitacora:
        return "    (ninguna todavía)"
    recientes = bitacora[-limite:]
    return "\n".join(f"    {l}" for l in recientes)


def _evidencia_para_prompt(evidencias: list[dict[str, Any]], limite_desc: int = 90) -> str:
    """La evidencia tal como la ve el nodo crítico, **con los valores**.

    Antes se le pasaba `(id, tipo, descripcion)`, y la descripción de una
    cifra es "exposicion_crediticia de CLI01": dice qué se consultó, no
    cuánto dio. El crítico no podía ver los números que se le pedía
    juzgar, así que reclamaba "faltan los valores" una iteración tras
    otra aunque ya estuvieran recogidos — seis iteraciones para una
    pregunta que se cierra en dos.
    """
    if not evidencias:
        return "    (todavía no hay evidencia)"
    lineas = []
    for e in evidencias:
        valor = ""
        if isinstance(e.get("valor"), (int, float)):
            valor = f" = {e['valor']:,.0f} {e.get('unidad') or ''}".rstrip()
        lineas.append(f"    [{e['id']}] {e['tipo']}: {e['descripcion'][:limite_desc]}{valor}")
    return "\n".join(lineas)


def _resumen_esquema(esquema: dict[str, Any]) -> str:
    """El esquema en texto, **con la dirección de cada arista**.

    Pasarle solo los nombres (`['TIENE', 'SE_EJECUTA_EN']`) dejaba al
    modelo sin saber hacia dónde apunta cada una. Para ir de un Servicio
    a sus Clientes hay que recorrer las dos al revés, y sin la dirección
    el agente se perdía y acababa inventando etiquetas y propiedades.
    """
    tipos = []
    for t in esquema.get("tipos") or []:
        props = ", ".join(t.get("propiedades_clave") or []) or "sin propiedades declaradas"
        frontera = "   [FRONTERA: es de otro dominio, hay que delegar]" if t.get("frontera") else ""
        tipos.append(f"    {t['tipo']}({props}){frontera}")

    aristas = []
    for a in esquema.get("aristas") or []:
        de = " | ".join(a.get("de") or [])
        hacia = " | ".join(a.get("a") or [])
        aristas.append(f"    ({de})-[:{a['nombre']}]->({hacia})")

    return (
        "Tipos de nodo y sus propiedades:\n" + "\n".join(tipos)
        + "\n  Aristas, CON SU DIRECCIÓN:\n" + "\n".join(aristas)
        + "\n  Para recorrer una arista en sentido contrario a la flecha, escribe el patrón al "
        "revés. Ejemplo: los Productos que corren en un Servicio son\n"
        "    MATCH (p:Producto)-[:SE_EJECUTA_EN]->(s:Servicio {id:'...'}) RETURN p.id AS producto_id\n"
        "  y los Clientes de ese Servicio encadenan los dos saltos hacia atrás en un solo patrón."
    )


def _añadir_evidencia(evidencias: list[dict[str, Any]], **campos: Any) -> list[dict[str, Any]]:
    """Añade una evidencia **validada por el modelo `Evidencia`**.

    Pasar por el modelo no es ceremonia: su validador exige `linaje` no
    vacío, así que un dato que se colara sin fuente reventaría aquí y no
    en la respuesta. El id se asigna por posición.
    """
    evidencia = Evidencia(id=f"E{len(evidencias) + 1}", **campos)
    evidencias.append(evidencia.model_dump())
    return evidencias


def _anotar(estado: AgentState, nodo: str, resumen: dict[str, Any]) -> list[dict[str, Any]]:
    traza = list(estado.get("audit_trail") or [])
    traza.append({"nodo": nodo, "iteration": estado.get("iteration", 0), **resumen})
    return traza


def _marcar_subpreguntas(plan: list[dict[str, Any]], ids: list[str], estado_nuevo: str) -> list[dict[str, Any]]:
    salida = []
    for sub in plan:
        copia = dict(sub)
        if copia.get("id") in ids:
            copia["estado"] = estado_nuevo
        salida.append(copia)
    return salida


def _resolver_subpreguntas_por_dominio(
    plan: list[dict[str, Any]], dominio: str, estado_nuevo: str = "resuelta"
) -> list[dict[str, Any]]:
    return _marcar_subpreguntas(
        plan, [s["id"] for s in plan if s.get("dominio") == dominio], estado_nuevo
    )


def _clientes_en_ruta(estado: AgentState) -> list[str]:
    """Ids de `Cliente` que quedaron en la ruta recorrida.

    Si el RBAC podó alguno, aquí ya no está — por eso `ejecutar_accion`
    no puede pedir la posición de un cliente que el agente nunca vio.
    """
    return [n for n in (estado.get("visited_nodes") or []) if n.startswith("CLI")]


# --------------------------------------------------------------------------
# Construcción del grafo
# --------------------------------------------------------------------------
def construir_grafo(
    llm: LLM,
    ctx: ContextoCorrida,
    mcp: ClienteMCP,
    *,
    agent_b_url: str = AGENT_B_URL,
    llms_por_paso: dict[str, LLM] | None = None,
):
    """Compila el grafo del Agente A atado a esta corrida.

    `llms_por_paso` permite dar un modelo distinto a un paso concreto —
    el §8 propone bajar `planner` y `nodo_critico` a un modelo rápido si
    la corrida se pasa de 90 s. Si no se pasa nada, todos usan `llm`.
    """
    llms_por_paso = llms_por_paso or {}

    def modelo_de(paso: str) -> LLM:
        return llms_por_paso.get(paso, llm)

    # ---------------------------------------------------------------- 1
    async def planner(estado: AgentState) -> AgentState:
        """Descompone la pregunta. Antes mira qué existe en su dominio y
        qué es frontera (`get_schema`)."""
        esquema = await mcp.llamar("ekl-graph", "get_schema", {}, step="planner")
        frontera = [t["tipo"] for t in esquema.get("tipos", []) if t.get("frontera")]

        humano = (
            f"Pregunta del usuario: {estado['query']}\n\n"
            f"Tipos de nodo de tu dominio: {[t['tipo'] for t in esquema.get('tipos', [])]}\n"
            f"Tipos que son FRONTERA (de otro dominio, hay que delegar): {frontera}\n"
            f"Aristas disponibles: {[a['nombre'] for a in esquema.get('aristas', [])]}\n"
            f"Acciones descubribles en el grafo: {esquema.get('acciones', [])}\n\n"
            "Descompón la pregunta en sub-preguntas mínimas. Marca el dominio de cada una: "
            "'infra' si depende de tipos frontera, 'semantica' si necesita una métrica gobernada, "
            "'negocio' si la resuelves recorriendo tu grafo."
        )
        # Caché del plan (§8, mitigación de latencia). Apagada por
        # defecto: ver agents/cache_plan.py.
        k = cache_plan.clave(estado["query"], ctx.rol, esquema)
        cacheado = cache_plan.obtener(k)
        async with cronometro() as t:
            if cacheado is not None:
                salida = SalidaPlanner.model_validate(cacheado)
            else:
                salida, _uso = await modelo_de("planner").estructurado(
                    SalidaPlanner, sistema=SISTEMA_BASE, humano=humano, paso="planner"
                )
                cache_plan.guardar(k, salida.model_dump())
        await emitir_thought(
            ctx, "planner", salida,
            extra={"plan_cacheado": cacheado is not None} if cache_plan.activada() else None,
            duracion_ms=t["ms"],
        )

        plan = [s.model_dump() for s in salida.subpreguntas]
        # La pregunta viaja en el evento `plan` porque la Sala de control
        # la necesita para la primera flecha (Usuario -> Agente A) y para
        # etiquetar el run en el selector; es el primer evento de la
        # corrida que la conoce.
        await emitir_plan(ctx, plan, query=estado["query"])
        return {
            "esquema": esquema,
            "plan": plan,
            "servicios_frontera": salida.nodos_frontera,
            "audit_trail": _anotar(estado, "planner", {"subpreguntas": len(plan)}),
            "iteration": 1,
            "max_iterations": estado.get("max_iterations", MAX_ITERATIONS),
            "directivas_previas": [],
            "is_complete": False,
        }

    # ---------------------------------------------------------------- 2
    async def resolver_contexto(estado: AgentState) -> AgentState:
        """Fija las definiciones **antes** de navegar: qué significa cada
        métrica de negocio, según la capa semántica y no según el modelo."""
        plan = estado.get("plan") or []
        humano = (
            f"Pregunta: {estado['query']}\n"
            f"Plan: {[s['pregunta'] for s in plan]}\n\n"
            "¿Qué métricas de negocio hay que resolver contra la capa semántica antes de navegar? "
            "Da sus nombres técnicos (por ejemplo 'exposicion_crediticia'). No calcules nada."
        )
        async with cronometro() as t:
            salida, uso = await llm.estructurado(
                SalidaContexto, sistema=SISTEMA_BASE, humano=humano, paso="resolver_contexto"
            )
        await emitir_thought(ctx, "resolver_contexto", salida, duracion_ms=t["ms"])

        definiciones = dict(estado.get("definiciones") or {})
        evidencias = list(estado.get("evidence") or [])
        for metrica in salida.metricas_a_resolver:
            try:
                definicion = await mcp.llamar(
                    "ekl-actions", "resolve_metric", {"nombre": metrica}, step="resolver_contexto"
                )
            except ErrorHerramienta as exc:
                await emitir(ctx, "error", {"paso": "resolver_contexto", "metrica": metrica,
                                            "error": exc.mensaje}, step="resolver_contexto")
                continue
            definiciones[metrica] = definicion
            _añadir_evidencia(
                evidencias,
                tipo="definicion",
                descripcion=f"Definición vigente de {metrica} (v{definicion.get('version')})",
                linaje=definicion.get("linaje", ""),
                origen="mcp_actions",
                nodo=metrica,
            )

        return {
            "definiciones": definiciones,
            "evidence": evidencias,
            "plan": _resolver_subpreguntas_por_dominio(plan, "semantica", "en_curso"),
            "audit_trail": _anotar(estado, "resolver_contexto", {"metricas": list(definiciones)}),
        }

    # ---------------------------------------------------------------- 3
    async def navegar_grafo(estado: AgentState) -> AgentState:
        """`find_entry_nodes` -> `read_cypher` con recorridos de k saltos.
        Acumula `visited_nodes` y `evidence`."""
        iteracion = estado.get("iteration", 1)
        ctx.iteration = iteracion
        contexto_llm = {
            "servicio_principal": _servicio_principal(estado),
            "servicios_ids": [s["id"] for s in _servicios_de_b(estado)],
            "directiva": estado.get("directiva_refinamiento"),
        }
        humano = (
            f"Pregunta: {estado['query']}\n"
            f"Plan: {[(s['id'], s['pregunta'], s['estado']) for s in estado.get('plan') or []]}\n"
            f"Nodos ya visitados: {estado.get('visited_nodes') or []}\n"            "Llamadas que YA ejecutaste en esta corrida (NO las repitas, su "
            "resultado ya está en la evidencia):\n"
            + _bitacora_para_prompt(estado.get("consultas_hechas") or [])
            + "\n"
            f"Lo que te devolvió el otro dominio: {contexto_llm['servicios_ids'] or 'nada todavía'}\n"
            f"Directiva de refinamiento vigente: {estado.get('directiva_refinamiento') or 'ninguna'}\n\n"
            f"{_resumen_esquema(estado.get('esquema') or {})}\n\n"
            f"{AYUDA_CYPHER}\n\n"
            "Indica por qué texto entrar al grafo y qué consultas recorrer ahora.\n"
            "- `texto_entrada`: UNA o DOS palabras del dominio (por ejemplo 'pagos'), no la "
            "pregunta entera.\n"
            "- En las consultas usa SOLO etiquetas y aristas de las listas de arriba, e ids que "
            "ya hayas visto en un resultado. Si todavía no conoces el id, deja `consultas` vacío "
            "en esta iteración y búscalo primero.\n"
            "- Si el punto de partida no está en tu dominio, deja `consultas` vacío y dilo en el "
            "razonamiento."
        )
        async with cronometro() as t:
            salida, uso = await llm.estructurado(
                SalidaNavegacion, sistema=SISTEMA_BASE, humano=humano, paso="navegar_grafo",
                variante=f"iter{iteracion}", contexto=contexto_llm,
            )
        await emitir_thought(ctx, "navegar_grafo", salida,
                             extra={"consultas": len(salida.consultas)}, duracion_ms=t["ms"])

        visitados = list(estado.get("visited_nodes") or [])
        evidencias = list(estado.get("evidence") or [])
        bitacora = list(estado.get("consultas_hechas") or [])

        nombres = dict(estado.get("nombres_nodos") or {})

        firma_entrada = f"find_entry_nodes('{salida.texto_entrada}')"
        if salida.texto_entrada and _ya_ejecutada(bitacora, firma_entrada):
            salida.texto_entrada = ""
        if salida.texto_entrada:
            try:
                entrada = await mcp.llamar(
                    "ekl-graph", "find_entry_nodes",
                    {"texto": salida.texto_entrada}, step="navegar_grafo",
                )
                _registrar(bitacora, f"find_entry_nodes('{salida.texto_entrada}')",
                           f"{len(entrada.get('nodos') or [])} nodo(s): "
                           f"{[n['id'] for n in entrada.get('nodos', [])][:6]}")
                for nodo in entrada.get("nodos", []):
                    if nodo["id"] not in visitados:
                        visitados.append(nodo["id"])
                    if nodo.get("nombre"):
                        nombres[nodo["id"]] = nodo["nombre"]
            except ErrorHerramienta as exc:
                await emitir(ctx, "error", {"paso": "navegar_grafo", "error": exc.mensaje},
                             step="navegar_grafo")

        filtrados_total: list[str] = []
        for consulta in salida.consultas:
            firma = f"read_cypher({consulta[:80]})"
            if _ya_ejecutada(bitacora, firma):
                # El modelo la repitió pese a tener la bitácora delante;
                # no volvemos a pagar el viaje ni a duplicar la evidencia.
                continue
            datos = await _read_cypher_con_reintento(consulta, estado)
            if datos is None:
                _registrar(bitacora, firma, "rechazada por el servidor")
                continue
            _registrar(bitacora, firma, f"{datos.get('total')} fila(s)")
            filtrados_total.extend(datos.get("nodos_filtrados_por_politica") or [])
            for fila in datos.get("filas", []):
                _recoger_nodos_de_fila(fila, visitados, nombres)
            if datos.get("filas"):
                _añadir_evidencia(
                    evidencias,
                    tipo="nodo",
                    descripcion=f"{datos.get('total')} fila(s) del recorrido: {_resumen_filas(datos['filas'])}",
                    linaje=f"grafo negocio (MCP ekl-graph, base negocio): {consulta}",
                    origen="mcp_graph",
                )

        plan = estado.get("plan") or []
        if salida.consultas:
            plan = _resolver_subpreguntas_por_dominio(plan, "negocio", "resuelta")

        return {
            "visited_nodes": visitados,
            "evidence": evidencias,
            "plan": plan,
            "nombres_nodos": nombres,
            "consultas_hechas": bitacora,
            "nodos_podados": sorted(set((estado.get("nodos_podados") or []) + filtrados_total)),
            "audit_trail": _anotar(
                estado, "navegar_grafo",
                {"consultas": len(salida.consultas), "visitados": len(visitados),
                 "podados": len(filtrados_total)},
            ),
        }

    async def _read_cypher_con_reintento(consulta: str, estado: AgentState) -> dict[str, Any] | None:
        """Ejecuta una consulta y, si el servidor la rechaza, deja que el
        modelo la corrija **una vez** con el mensaje de error delante.

        Es lo que hace que un modelo real que se sale del subconjunto no
        tumbe la corrida en mitad de la demo.
        """
        try:
            return await mcp.llamar("ekl-graph", "read_cypher", {"query": consulta}, step="navegar_grafo")
        except ErrorHerramienta as exc:
            await emitir(ctx, "error",
                         {"paso": "navegar_grafo", "consulta": consulta, "error": exc.mensaje},
                         step="navegar_grafo")
            humano = (
                f"Esta consulta fue rechazada por el servidor:\n{consulta}\n\n"
                f"Motivo: {exc.mensaje}\n\n{AYUDA_CYPHER}\n\nReescríbela dentro del subconjunto."
            )
            try:
                correccion, _ = await llm.estructurado(
                    SalidaNavegacion, sistema=SISTEMA_BASE, humano=humano,
                    paso="navegar_grafo", variante="correccion",
                )
            except Exception:
                return None
            await emitir_thought(ctx, "navegar_grafo", correccion, extra={"reintento": True})
            if not correccion.consultas:
                return None
            try:
                previas = estado.get("consultas_hechas") or []
                if _ya_ejecutada(previas, f"read_cypher({correccion.consultas[0][:80]})"):
                    return None
                return await mcp.llamar(
                    "ekl-graph", "read_cypher", {"query": correccion.consultas[0]}, step="navegar_grafo"
                )
            except ErrorHerramienta as exc2:
                await emitir(ctx, "error",
                             {"paso": "navegar_grafo", "consulta": correccion.consultas[0],
                              "error": exc2.mensaje, "reintento": True}, step="navegar_grafo")
                return None

    # ---------------------------------------------------------------- 4
    async def ejecutar_accion(estado: AgentState) -> AgentState:
        """Descubre la acción que expone un nodo de la ruta
        (`list_node_actions`) y la invoca. El agente nunca sabe dónde se
        ejecuta: el contrato que recibe no trae `backend`."""
        iteracion = estado.get("iteration", 1)
        ctx.iteration = iteracion
        clientes = _clientes_en_ruta(estado)
        metrica_por_defecto = next(iter(estado.get("definiciones") or {}), "")
        contexto_llm = {"clientes_en_ruta": clientes, "metrica": metrica_por_defecto}

        humano = (
            f"Pregunta: {estado['query']}\n"
            f"Clientes que quedaron en la ruta: {clientes or 'ninguno'}\n"
            f"Métricas ya resueltas: {list(estado.get('definiciones') or {})}\n"            "Cifras que YA obtuviste (NO vuelvas a pedirlas):\n"
            + _bitacora_para_prompt(
                [l for l in (estado.get("consultas_hechas") or []) if l.startswith("accion(")]
            )
            + "\n"
            f"Directiva vigente: {estado.get('directiva_refinamiento') or 'ninguna'}\n\n"
            "¿Sobre qué nodos hay que ejecutar una acción gobernada y con qué métrica?\n"
            "- `node_ids` son los nodos **sobre los que se mide**, no el servicio por el que "
            "llegaste a ellos: si quieres la exposición de tres clientes, pon los tres ids de "
            "cliente, no el id del servicio.\n"
            "- Usa ids de la lista de clientes de la ruta, tal cual.\n"
            "- Si no hace falta ninguna cifra, devuelve la lista vacía."
        )
        async with cronometro() as t:
            salida, uso = await llm.estructurado(
                SalidaAccion, sistema=SISTEMA_BASE, humano=humano, paso="ejecutar_accion",
                variante=f"iter{iteracion}", contexto=contexto_llm,
            )
        await emitir_thought(ctx, "ejecutar_accion", salida,
                             extra={"nodos": salida.node_ids}, duracion_ms=t["ms"])

        evidencias = list(estado.get("evidence") or [])
        denegadas = list(estado.get("acciones_denegadas") or [])
        contratos_vistos = dict(estado.get("contratos_accion") or {})
        bitacora = list(estado.get("consultas_hechas") or [])
        sin_accion: list[str] = []

        metrica_pedida = salida.metrica or metrica_por_defecto
        for node_id in salida.node_ids:
            firma = f"accion({node_id}, {metrica_pedida})"
            if _ya_ejecutada_prefijo(bitacora, f"accion({node_id},"):
                continue
            # 1) Descubrir la acción navegando el grafo: el agente no la
            #    conoce de antemano (§3.2).
            try:
                acciones = await mcp.llamar(
                    "ekl-graph", "list_node_actions", {"node_id": node_id}, step="ejecutar_accion"
                )
            except ErrorHerramienta as exc:
                await emitir(ctx, "error", {"paso": "ejecutar_accion", "node_id": node_id,
                                            "error": exc.mensaje}, step="ejecutar_accion")
                continue
            for accion in acciones.get("acciones", []):
                contratos_vistos[accion["nombre"]] = accion.get("contrato", {})

            nombre_accion = next(
                (a["nombre"] for a in acciones.get("acciones", [])), None
            )
            if not nombre_accion:
                # Silencio no: si el nodo elegido no expone ninguna acción
                # (pedir la exposición sobre el *servicio* en vez de sobre
                # los clientes es el error típico del modelo), el bucle se
                # entera. Antes se hacía `continue` y el crítico daba por
                # buena una respuesta sin cifras.
                hueco = (
                    f"el nodo '{node_id}' no expone ninguna acción gobernada: "
                    f"las cifras por cliente se piden sobre los ids de cliente"
                )
                if hueco not in sin_accion:
                    sin_accion.append(hueco)
                continue

            # 2) Invocarla. Si la política la deniega, el servidor lo dice
            #    con un mensaje explicable y aquí se anota como hueco.
            try:
                posicion = await mcp.llamar(
                    "ekl-actions", nombre_accion,
                    {"customer_ref": node_id, "metrica": salida.metrica or metrica_por_defecto},
                    step="ejecutar_accion",
                )
            except ErrorHerramienta as exc:
                if exc.mensaje not in denegadas:
                    denegadas.append(exc.mensaje)
                continue

            _registrar(bitacora, firma,
                       f"{posicion.get('valor')} {posicion.get('moneda') or ''}".strip())
            _añadir_evidencia(
                evidencias,
                tipo="cifra",
                descripcion=f"{salida.metrica or metrica_por_defecto} de {node_id}",
                valor=posicion.get("valor"),
                unidad=posicion.get("moneda"),
                linaje=posicion.get("linaje", ""),
                origen="mcp_actions",
                nodo=node_id,
            )

        plan = estado.get("plan") or []
        if any(e["tipo"] == "cifra" for e in evidencias):
            plan = _resolver_subpreguntas_por_dominio(plan, "semantica", "resuelta")

        return {
            "evidence": evidencias,
            "plan": plan,
            "consultas_hechas": bitacora,
            "pending_gaps": sorted(set((estado.get("pending_gaps") or []) + sin_accion)),
            "acciones_denegadas": denegadas,
            "contratos_accion": contratos_vistos,
            "audit_trail": _anotar(
                estado, "ejecutar_accion",
                {"nodos": salida.node_ids, "denegadas": len(denegadas)},
            ),
        }

    # ---------------------------------------------------------------- 5
    async def delegar_a2a(estado: AgentState) -> AgentState:
        """Consulta el agent card del otro agente y le manda una tarea con
        el **contexto mínimo**: la sub-pregunta, el id del objeto y la
        identidad del usuario. Nada más (§4.6)."""
        iteracion = estado.get("iteration", 1)
        ctx.iteration = iteracion
        contexto_llm = {
            "servicio_principal": _servicio_principal(estado),
            "nodos_frontera": estado.get("servicios_frontera") or [],
            "directiva": estado.get("directiva_refinamiento"),
        }
        pendientes = [s for s in (estado.get("plan") or []) if s.get("dominio") == "infra"
                      and s.get("estado") in ("pendiente", "en_curso")]
        humano = (
            f"Pregunta: {estado['query']}\n"
            f"Sub-preguntas de otro dominio aún sin resolver: {[s['pregunta'] for s in pendientes]}\n"
            f"Nodos frontera detectados: {estado.get('servicios_frontera') or []}\n"
            f"Directiva vigente: {estado.get('directiva_refinamiento') or 'ninguna'}\n\n"
            f"Ids que ya has visto en el grafo: {(estado.get('visited_nodes') or [])[:20]}\n\n"            "Delegaciones que YA hiciste (NO repitas ninguna):\n"
            + _bitacora_para_prompt(
                [l for l in (estado.get("consultas_hechas") or []) if l.startswith("a2a(")]
            )
            + "\n\n"
            "Elige la skill del otro agente y el id del objeto sobre el que preguntar. "
            "El `parametro` tiene que ser un id **exacto** que hayas visto en un resultado (el de "
            "un Servicio o el de una base de datos), nunca uno inventado ni una descripción. "
            "Si no queda nada que delegar, deja `skill` vacía."
        )
        async with cronometro() as t:
            salida, uso = await llm.estructurado(
                SalidaDelegacion, sistema=SISTEMA_BASE, humano=humano, paso="delegar_a2a",
                variante=f"iter{iteracion}", contexto=contexto_llm,
            )
        await emitir_thought(ctx, "delegar_a2a", salida,
                             extra={"skill": salida.skill, "parametro": salida.parametro},
                             duracion_ms=t["ms"])

        if not salida.skill or not salida.parametro:
            return {"audit_trail": _anotar(estado, "delegar_a2a", {"delegado": False})}

        bitacora = list(estado.get("consultas_hechas") or [])
        firma_a2a = f"a2a({salida.skill}, {salida.parametro})"
        if _ya_ejecutada(bitacora, firma_a2a):
            return {"audit_trail": _anotar(estado, "delegar_a2a", {"repetida": True})}

        cliente = ClienteA2A(agent_b_url)
        try:
            card = await cliente.agent_card()
        except Exception as exc:
            await emitir(ctx, "error", {"paso": "delegar_a2a", "error": describir_excepcion(exc),
                                        "url": agent_b_url}, step="delegar_a2a")
            gaps = list(estado.get("pending_gaps") or [])
            gaps.append(f"no se pudo contactar al agente de infraestructura en {agent_b_url}")
            return {"pending_gaps": gaps,
                    "audit_trail": _anotar(estado, "delegar_a2a", {"error": "agent card"})}

        skills = [s.id for s in card.skills]
        skill = salida.skill if salida.skill in skills else (skills[0] if skills else "")

        tarea = construir_tarea(
            skill=skill,
            parametro=salida.parametro,
            pregunta=salida.pregunta_parcial or estado["query"],
            run_id=ctx.run_id,
            user_ctx=ctx.user_ctx,
        )
        await emitir(
            ctx, "a2a_request",
            {
                "de": "agent_a", "para": card.name, "url": agent_b_url,
                "task_id": tarea.id, "skill": skill,
                "skills_del_card": skills,
                "contexto_enviado": tarea.model_dump(exclude_none=True),
                "no_se_envia": NO_SE_ENVIA,
            },
            step="delegar_a2a",
        )

        try:
            resultado = await cliente.tasks_send(tarea)
        except (ErrorA2A, Exception) as exc:
            await emitir(ctx, "error", {"paso": "delegar_a2a", "task_id": tarea.id,
                                        "error": describir_excepcion(exc)}, step="delegar_a2a")
            gaps = list(estado.get("pending_gaps") or [])
            gaps.append(f"la delegación {skill}({salida.parametro}) falló")
            return {"pending_gaps": gaps,
                    "audit_trail": _anotar(estado, "delegar_a2a", {"error": "tasks/send"})}

        contrato = resultado.datos()
        evidencias = list(estado.get("evidence") or [])
        linaje_b = "; ".join(contrato.get("linaje") or []) or f"agente {card.name} vía A2A"
        # Cada servicio afectado puede traer su propia fuente documental
        # (la escribió `data/extract_from_docs.py`): se conserva para que
        # la respuesta pueda citar la línea exacta del runbook.
        fuentes_por_servicio = [
            f"{s.get('id')}: {s.get('linaje')}"
            for s in contrato.get("servicios_afectados") or []
            if s.get("linaje")
        ]
        if fuentes_por_servicio:
            linaje_b = linaje_b + " | " + " ; ".join(fuentes_por_servicio)
        _añadir_evidencia(
            evidencias,
            tipo="a2a",
            descripcion=(
                f"{skill}({salida.parametro}): "
                f"servicios={[s.get('id') for s in contrato.get('servicios_afectados') or []]}, "
                f"responsable={(contrato.get('responsable') or {}).get('nombre')}"
            ),
            linaje=f"agente de infraestructura (A2A tasks/send) — {linaje_b}",
            origen="agent_b",
            nodo=salida.parametro,
        )

        _registrar(bitacora, firma_a2a,
                   f"servicios={[x.get('id') for x in contrato.get('servicios_afectados') or []]}, "
                   f"responsable={(contrato.get('responsable') or {}).get('nombre')}")
        respuestas = list(estado.get("respuestas_a2a") or [])
        respuestas.append({"skill": skill, "parametro": salida.parametro, "contrato": contrato})

        visitados = list(estado.get("visited_nodes") or [])
        nombres = dict(estado.get("nombres_nodos") or {})
        for servicio in contrato.get("servicios_afectados") or []:
            if servicio.get("id") and servicio["id"] not in visitados:
                visitados.append(servicio["id"])
            if servicio.get("id") and servicio.get("nombre"):
                nombres[servicio["id"]] = servicio["nombre"]

        plan = _marcar_subpreguntas(
            estado.get("plan") or [], [s["id"] for s in pendientes[:1]], "resuelta"
        )
        return {
            "evidence": evidencias,
            "respuestas_a2a": respuestas,
            "consultas_hechas": bitacora,
            "visited_nodes": visitados,
            "nombres_nodos": nombres,
            "plan": plan,
            "audit_trail": _anotar(estado, "delegar_a2a",
                                   {"skill": skill, "parametro": salida.parametro}),
        }

    # ---------------------------------------------------------------- 6
    async def nodo_critico(estado: AgentState) -> AgentState:
        """Dos fases:

        * **suficiencia** — ¿cada sub-pregunta tiene evidencia? ¿la ruta
          llega de la base de datos a los clientes sin huecos? Si no,
          emite `refinement` y vuelve a 3, 4 o 5.
        * **verificación** — con la respuesta ya redactada: ¿hay alguna
          cifra que no esté en `evidence[]` con linaje? Si la hay, la
          rechaza y la manda reescribir.
        """
        if estado.get("respuesta"):
            return await _critico_verificacion(estado)
        return await _critico_suficiencia(estado)

    async def _critico_suficiencia(estado: AgentState) -> AgentState:
        iteracion = estado.get("iteration", 1)
        ctx.iteration = iteracion
        huecos, destino, directiva_sugerida = _detectar_huecos(estado)

        humano = (
            f"Pregunta: {estado['query']}\n"
            f"Plan y estado: {[(s['id'], s['estado']) for s in estado.get('plan') or []]}\n"
            "Evidencia recogida:\n"
            + _evidencia_para_prompt(estado.get("evidence") or [])
            + "\n"
            f"Nodos visitados: {estado.get('visited_nodes') or []}\n"
            f"Huecos que detecta la revisión automática: {huecos or 'ninguno'}\n"
            f"Iteración {iteracion} de {estado.get('max_iterations', MAX_ITERATIONS)}.\n"
            f"Directivas ya emitidas: {estado.get('directivas_previas') or []}\n\n"
            "¿Está la ruta completa? Si no, di qué falta y a qué nodo volver "
            "(navegar_grafo, ejecutar_accion o delegar_a2a)."
        )
        async with cronometro() as t:
            salida, uso = await modelo_de("nodo_critico").estructurado(
                SalidaCritico, sistema=SISTEMA_BASE, humano=humano, paso="nodo_critico",
                variante="suficiencia",
                contexto={"huecos": huecos, "destino_sugerido": destino,
                          "directiva_sugerida": directiva_sugerida},
            )
        await emitir_thought(ctx, "nodo_critico", salida,
                             extra={"fase": "suficiencia"}, duracion_ms=t["ms"])

        previas = list(estado.get("directivas_previas") or [])
        motivo_corte: str | None = None
        completo = bool(salida.is_complete)
        directiva = (salida.directiva_refinamiento or "").strip() or None

        if not completo:
            if iteracion >= estado.get("max_iterations", MAX_ITERATIONS):
                motivo_corte = (
                    f"se alcanzó max_iterations={estado.get('max_iterations', MAX_ITERATIONS)}"
                )
            elif directiva and directiva in previas:
                # §8: si el crítico repite la misma directiva, insistir no
                # va a cerrar el hueco. Se corta y se responde con lo que hay.
                motivo_corte = "la directiva de refinamiento es idéntica a la anterior"
            elif not directiva:
                motivo_corte = "el crítico no emitió ninguna directiva nueva"

        await emitir(
            ctx, "critic_verdict",
            {
                "is_complete": completo,
                "huecos": salida.huecos or huecos,
                "directiva_refinamiento": directiva,
                "nodo_destino": salida.nodo_destino or destino,
                "iteration": iteracion,
                "motivo_corte": motivo_corte,
            },
            step="nodo_critico",
        )

        if completo or motivo_corte:
            return {
                "is_complete": completo,
                "pending_gaps": salida.huecos or huecos,
                "directiva_refinamiento": None,
                "motivo_corte": motivo_corte,
                "proximo_nodo": "responder",
                "audit_trail": _anotar(estado, "nodo_critico",
                                       {"is_complete": completo, "corte": motivo_corte}),
            }

        previas.append(directiva)
        await emitir(
            ctx, "refinement",
            {
                "directiva": directiva,
                "huecos": salida.huecos or huecos,
                "nodo_destino": salida.nodo_destino or destino,
                "iteration_siguiente": iteracion + 1,
            },
            step="nodo_critico",
        )
        return {
            "is_complete": False,
            "pending_gaps": salida.huecos or huecos,
            "directiva_refinamiento": directiva,
            "directivas_previas": previas,
            "iteration": iteracion + 1,
            "proximo_nodo": salida.nodo_destino or destino,
            "audit_trail": _anotar(estado, "nodo_critico",
                                   {"is_complete": False, "directiva": directiva}),
        }

    async def _critico_verificacion(estado: AgentState) -> AgentState:
        """Rechaza la respuesta si trae cifras que no están en la
        evidencia con linaje."""
        respuesta = estado.get("respuesta") or {}
        texto = respuesta.get("respuesta", "")
        evidencias = estado.get("evidence") or []
        huerfanas = cifras_sin_linaje(texto, evidencias, estado.get("query", ""))
        ids_inventados = evidencias_faltantes(respuesta.get("hallazgos") or [], evidencias)

        problemas = list(huerfanas)
        problemas += [f"referencia a evidencia inexistente: {i}" for i in ids_inventados]

        humano = (
            f"Respuesta redactada:\n{texto}\n\n"
            f"Evidencia disponible: "
            f"{[(e['id'], e.get('valor'), e['linaje'][:50]) for e in evidencias]}\n"
            f"Cifras sin respaldo que detecta la revisión automática: {problemas or 'ninguna'}\n\n"
            "¿Es válida la respuesta? Solo lo es si toda cifra está en la evidencia con linaje."
        )
        async with cronometro() as t:
            salida, uso = await modelo_de("nodo_critico").estructurado(
                SalidaVerificacion, sistema=SISTEMA_BASE, humano=humano, paso="nodo_critico",
                variante="verificacion", contexto={"cifras_sin_linaje": problemas},
            )
        await emitir_thought(ctx, "nodo_critico", salida,
                             extra={"fase": "verificacion"}, duracion_ms=t["ms"])

        # La comprobación determinista manda sobre la opinión del modelo:
        # si hay cifras huérfanas, la respuesta se rechaza aunque el LLM
        # diga que está bien.
        valida = bool(salida.respuesta_valida) and not problemas
        intentos = int(estado.get("intentos_respuesta") or 0)

        await emitir(
            ctx, "critic_verdict",
            {
                "fase": "verificacion",
                "respuesta_valida": valida,
                "cifras_sin_linaje": problemas,
                "intento": intentos,
            },
            step="nodo_critico",
        )

        if valida:
            return {"is_complete": True, "proximo_nodo": "fin",
                    "audit_trail": _anotar(estado, "nodo_critico", {"respuesta_valida": True})}

        # El presupuesto de reescritura es **suyo**, no el de exploración:
        # reescribir el texto no recorre nada, cuesta una llamada. Atarlo a
        # `max_iterations` hacía que una corrida que hubiera gastado sus
        # iteraciones entregara una cifra sin linaje sin intentar siquiera
        # corregirla, que es justo la regla que la demo promete. El tope
        # sigue estando: dos intentos.
        if intentos >= 2:
            # Se corta y se entrega la respuesta marcada como no validada,
            # en vez de girar indefinidamente.
            return {
                "is_complete": False,
                "motivo_corte": "no se consiguió una respuesta sin cifras huérfanas",
                "proximo_nodo": "fin",
                "respuesta": {**respuesta, "cifras_sin_linaje": problemas, "validada": False},
                "audit_trail": _anotar(estado, "nodo_critico", {"respuesta_valida": False, "corte": True}),
            }

        directiva = salida.directiva_refinamiento or (
            "Reescribe la respuesta usando solo cifras presentes en la evidencia; "
            f"quita estas: {', '.join(problemas)}"
        )
        await emitir(
            ctx, "refinement",
            {"directiva": directiva, "fase": "verificacion", "cifras_sin_linaje": problemas},
            step="nodo_critico",
        )
        return {
            "respuesta": None,
            "directiva_refinamiento": directiva,
            "intentos_respuesta": intentos + 1,
            "proximo_nodo": "responder",
            "audit_trail": _anotar(estado, "nodo_critico", {"respuesta_valida": False}),
        }

    # ---------------------------------------------------------------- 7
    async def responder(estado: AgentState) -> AgentState:
        """Redacta: hallazgos, ruta de nodos, evidencia con linaje por
        dato, políticas aplicadas y advertencias colaterales."""
        ctx.iteration = estado.get("iteration", 1)
        evidencias = estado.get("evidence") or []
        nombres = estado.get("nombres_nodos") or {}
        cifras = [
            {
                "cliente_id": e.get("nodo") or "",
                "nombre": nombres.get(e.get("nodo") or "", e.get("nodo") or ""),
                "valor_formateado": f"{e['valor']:,.0f}".replace(",", "."),
                "valor": e["valor"],
                "unidad": e.get("unidad") or "",
                "evidencia_id": e["id"],
            }
            for e in evidencias
            if e["tipo"] == "cifra" and isinstance(e.get("valor"), (int, float))
        ]
        servicios = _servicios_de_b(estado)
        responsable = _responsable_de_b(estado)

        contexto_llm = {
            "guion": getattr(llm, "guion", "generico"),
            "cifras": cifras,
            "servicios_afectados": servicios,
            "servicio_principal": _servicio_principal(estado),
            "responsable": responsable,
            "clientes_en_ruta": _clientes_en_ruta(estado),
            "politicas": {
                "rol": ctx.rol,
                "filtrados": estado.get("nodos_podados") or [],
                "denegadas": estado.get("acciones_denegadas") or [],
            },
            "umbral_texto": _umbral_texto(estado["query"]),
        }
        humano = (
            f"Pregunta: {estado['query']}\n\n"
            f"EVIDENCIA DISPONIBLE (la única fuente de cifras permitida):\n"
            + "\n".join(
                f"- [{e['id']}] {e['descripcion']}"
                + (f" = {e['valor']} {e.get('unidad') or ''}" if e.get("valor") is not None else "")
                + f" | linaje: {e['linaje']}"
                for e in evidencias
            )
            + f"\n\nRuta de nodos recorrida: {estado.get('visited_nodes') or []}\n"
            # Al redactor se le da el **conteo**, no los ids: si el texto
            # nombra al cliente que la política escondió, el filtrado no
            # sirvió de nada — saber que existe un CLI01 al que no tienes
            # acceso ya es información (docs/demo_decisions.md §9). Los ids
            # siguen en `politicas_aplicadas` y en la traza, que es donde
            # los mira quien sí tiene permiso.
            f"Nodos podados por política para el rol '{ctx.rol}': "
            f"{len(estado.get('nodos_podados') or [])} nodo(s). NO escribas sus "
            f"identificadores en el texto; di cuántos fueron y por qué.\n"
            f"Acciones denegadas por política: {estado.get('acciones_denegadas') or []}\n"
            f"Huecos que quedaron abiertos: {estado.get('pending_gaps') or 'ninguno'}\n"
            f"Directiva de reescritura: {estado.get('directiva_refinamiento') or 'ninguna'}\n\n"
            "Redacta la respuesta. Cita el id de evidencia de cada afirmación con cifra. "
            "Si una cifra no está arriba, NO la escribas: di que no se pudo obtener y por qué. "
            "Añade las advertencias colaterales relevantes."
        )
        async with cronometro() as t:
            salida, uso = await llm.estructurado(
                SalidaRespuesta, sistema=SISTEMA_BASE, humano=humano, paso="responder",
                contexto=contexto_llm,
            )
        await emitir_thought(ctx, "responder", salida, duracion_ms=t["ms"])

        respuesta = {
            "respuesta": salida.respuesta,
            "hallazgos": [h.model_dump() for h in salida.hallazgos],
            "advertencias": salida.advertencias,
            "ruta_nodos": estado.get("visited_nodes") or [],
            "evidencia": evidencias,
            "politicas_aplicadas": {
                "rol": ctx.rol,
                "nodos_podados": estado.get("nodos_podados") or [],
                "acciones_denegadas": estado.get("acciones_denegadas") or [],
            },
            "validada": None,
        }
        return {
            "respuesta": respuesta,
            "audit_trail": _anotar(estado, "responder", {"hallazgos": len(salida.hallazgos)}),
        }

    # ---------------------------------------------------------------- rutas
    def _tras_critico(estado: AgentState) -> str:
        proximo = estado.get("proximo_nodo") or "responder"
        if proximo == "fin":
            return END
        if proximo in ("navegar_grafo", "ejecutar_accion", "delegar_a2a", "responder"):
            return proximo
        return "responder"

    grafo = StateGraph(AgentState)
    grafo.add_node("planner", planner)
    grafo.add_node("resolver_contexto", resolver_contexto)
    grafo.add_node("navegar_grafo", navegar_grafo)
    grafo.add_node("ejecutar_accion", ejecutar_accion)
    grafo.add_node("delegar_a2a", delegar_a2a)
    grafo.add_node("nodo_critico", nodo_critico)
    grafo.add_node("responder", responder)

    grafo.set_entry_point("planner")
    grafo.add_edge("planner", "resolver_contexto")
    grafo.add_edge("resolver_contexto", "navegar_grafo")
    grafo.add_edge("navegar_grafo", "ejecutar_accion")
    grafo.add_edge("ejecutar_accion", "delegar_a2a")
    grafo.add_edge("delegar_a2a", "nodo_critico")
    grafo.add_conditional_edges(
        "nodo_critico", _tras_critico,
        {
            "navegar_grafo": "navegar_grafo",
            "ejecutar_accion": "ejecutar_accion",
            "delegar_a2a": "delegar_a2a",
            "responder": "responder",
            END: END,
        },
    )
    # `responder` no es terminal: vuelve al crítico para que verifique las
    # cifras (ver docstring del módulo).
    grafo.add_edge("responder", "nodo_critico")

    return grafo.compile()


# --------------------------------------------------------------------------
# Auxiliares de lectura del estado
# --------------------------------------------------------------------------
def _servicios_de_b(estado: AgentState) -> list[dict[str, Any]]:
    servicios: list[dict[str, Any]] = []
    for respuesta in estado.get("respuestas_a2a") or []:
        for servicio in respuesta.get("contrato", {}).get("servicios_afectados") or []:
            if servicio.get("id") and servicio["id"] not in [s["id"] for s in servicios]:
                servicios.append(servicio)
    return servicios


def _servicio_principal(estado: AgentState) -> str | None:
    servicios = _servicios_de_b(estado)
    if servicios:
        return servicios[0]["id"]
    frontera = estado.get("servicios_frontera") or []
    return frontera[0] if frontera else None


def _responsable_de_b(estado: AgentState) -> dict[str, Any]:
    for respuesta in estado.get("respuestas_a2a") or []:
        responsable = respuesta.get("contrato", {}).get("responsable")
        if responsable:
            evidencia_id = None
            for e in estado.get("evidence") or []:
                if e["tipo"] == "a2a" and responsable.get("nombre", "") in e["descripcion"]:
                    evidencia_id = e["id"]
            return {**responsable, "evidencia_id": evidencia_id}
    return {}


def _recoger_nodos_de_fila(
    fila: dict[str, Any], visitados: list[str], nombres: dict[str, str]
) -> None:
    """Saca ids y nombres legibles de una fila de `read_cypher`.

    Las filas vienen en dos formas según lo que pidió el RETURN: el nodo
    entero (`RETURN c`, un dict con `id`) o columnas sueltas
    (`RETURN c.id AS cliente_id, c.nombre AS cliente`). Se soportan las
    dos para no atar la navegación a una forma concreta de consulta.
    """
    id_suelto: str | None = None
    nombre_suelto: str | None = None
    for clave, valor in fila.items():
        if isinstance(valor, dict) and valor.get("id"):
            if valor["id"] not in visitados:
                visitados.append(valor["id"])
            if valor.get("nombre"):
                nombres[valor["id"]] = valor["nombre"]
        elif clave.endswith("_id") and isinstance(valor, str):
            id_suelto = valor
            if valor not in visitados:
                visitados.append(valor)
        elif isinstance(valor, str) and clave in ("nombre", "cliente", "producto", "servicio", "persona"):
            nombre_suelto = valor
    if id_suelto and nombre_suelto:
        nombres[id_suelto] = nombre_suelto


def _resumen_filas(filas: list[dict[str, Any]], limite: int = 3) -> str:
    muestra = []
    for fila in filas[:limite]:
        muestra.append(", ".join(f"{k}={v}" for k, v in fila.items() if not isinstance(v, dict)))
    extra = "" if len(filas) <= limite else f" (+{len(filas) - limite} más)"
    return "; ".join(m for m in muestra if m) + extra


def _umbral_texto(query: str) -> str:
    return " con exposición crediticia superior al umbral de la pregunta" if "millones" in (query or "").lower() else ""


def _detectar_huecos(estado: AgentState) -> tuple[list[str], str, str]:
    """Revisión determinista de suficiencia: qué sub-pregunta no tiene
    evidencia y a qué nodo habría que volver.

    El LLM recibe esto y da el veredicto; tenerlo calculado evita que el
    crítico se declare satisfecho solo porque el texto suena completo.
    """
    plan = estado.get("plan") or []
    evidencias = estado.get("evidence") or []
    tipos = {e["tipo"] for e in evidencias}
    huecos: list[str] = []
    destino = "navegar_grafo"

    pendientes_infra = [s for s in plan if s.get("dominio") == "infra" and s.get("estado") != "resuelta"]
    pendientes_negocio = [s for s in plan if s.get("dominio") == "negocio" and s.get("estado") != "resuelta"]
    pendientes_sem = [s for s in plan if s.get("dominio") == "semantica" and s.get("estado") != "resuelta"]

    # Los huecos se enseñan **todos**, de todos los dominios; solo el
    # destino se prioriza. Con un `elif` aquí, una sub-pregunta de infra
    # sin resolver tapaba el hueco de las cifras y el crítico daba por
    # buena una respuesta sin un solo número: medido con el modelo real.
    falta_cifra = (
        bool(pendientes_sem)
        and "cifra" not in tipos
        and not (estado.get("acciones_denegadas") or [])
    )
    huecos += [f"{s['id']}: {s['pregunta']}" for s in pendientes_infra]
    huecos += [f"{s['id']}: {s['pregunta']}" for s in pendientes_negocio]
    if falta_cifra:
        huecos += [f"{s['id']}: {s['pregunta']}" for s in pendientes_sem]

    # Prioridad del destino: primero conseguir la cifra que falta, que es
    # lo que la pregunta pide literalmente.
    if falta_cifra:
        destino = "ejecutar_accion"
    elif pendientes_infra:
        destino = "delegar_a2a"
    elif pendientes_negocio:
        destino = "navegar_grafo"

    # Intentos de acción que no encontraron nada que ejecutar.
    huecos += list(estado.get("pending_gaps") or [])

    # Hallazgo colateral: si el otro dominio devolvió más servicios de los
    # que el agente había recorrido, hay ruta sin explorar (§3.4).
    servicios_b = {s["id"] for s in _servicios_de_b(estado)}
    visitados = set(estado.get("visited_nodes") or [])
    sin_explorar = servicios_b - visitados
    if sin_explorar:
        huecos.append(
            f"servicios afectados que aún no se han explorado en el grafo de negocio: {sorted(sin_explorar)}"
        )
        if not pendientes_infra:
            destino = "navegar_grafo"

    directiva = (
        f"Cerrar estos huecos volviendo a {destino}: " + "; ".join(huecos)
        if huecos
        else ""
    )
    return huecos, destino, directiva
