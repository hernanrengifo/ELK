"""
llm_fake.py — LLM simulado (`LLM_MODEL=fake`).

Sirve para dos cosas:

1. **Probar el flujo completo sin API key ni red** — `demo_smoke.py` y
   los tests corren así, y por eso son deterministas.
2. **Plan B de la demo** (§8, riesgos: "sin internet en la sala"): con
   `LLM_MODEL=fake` la corrida entera funciona, incluidos los eventos de
   la Sala de control, aunque no haya conexión con Anthropic.

**Qué simula y qué no.** Simula solo las *decisiones* del modelo: qué
sub-preguntas hay, qué Cypher escribir, a qué skill delegar, si la ruta
está completa. **No simula los datos**: las consultas se ejecutan de
verdad contra los servidores MCP, el RBAC poda de verdad y las cifras
salen de DuckDB. Por eso una corrida con `fake` sigue demostrando la
gobernanza — lo único fijo es el razonamiento.

Cubre las tres preguntas del guion (§5): la pregunta hilo conductor y
las dos alternativas. Si llega una pregunta que no reconoce, responde
con un guion genérico que no inventa cifras (devuelve el hueco), en vez
de fingir que sabe.

Las decisiones que dependen del estado (qué clientes quedaron en la
ruta, qué huecos ve el crítico) se calculan a partir del `contexto` que
le pasa cada nodo — el LLM real recibe esa misma información en prosa
dentro del prompt.
"""

from __future__ import annotations

from typing import Any, Callable

from .llm import LLM, UsoLLM
from .modelos import PasoRazonado

#: Cypher del salto 2: productos que corren en un servicio.
_Q_PRODUCTOS = (
    "MATCH (p:Producto)-[:SE_EJECUTA_EN]->(s:Servicio {{id:'{servicio}'}}) "
    "RETURN DISTINCT p.id AS producto_id, p.nombre AS producto"
)
#: Cypher del salto 3: clientes corporativos con esos productos.
_Q_CLIENTES_CORP = (
    "MATCH (c:Cliente {{segmento:'corporativo'}})-[:TIENE]->(p:Producto)"
    "-[:SE_EJECUTA_EN]->(s:Servicio {{id:'{servicio}'}}) "
    "RETURN DISTINCT c.id AS cliente_id, c.nombre AS cliente"
)
#: Igual pero sin filtrar por segmento (para "a qué clientes notificar").
_Q_CLIENTES_TODOS = (
    "MATCH (c:Cliente)-[:TIENE]->(p:Producto)-[:SE_EJECUTA_EN]->(s:Servicio {{id:'{servicio}'}}) "
    "RETURN DISTINCT c.id AS cliente_id, c.nombre AS cliente, c.segmento AS segmento"
)

Entrada = dict[str, Any] | Callable[[dict[str, Any]], dict[str, Any]]


# --------------------------------------------------------------------------
# Decisiones que dependen del estado
# --------------------------------------------------------------------------
def _accion_sobre_clientes_en_ruta(contexto: dict[str, Any]) -> dict[str, Any]:
    """`ejecutar_accion`: actuar sobre los clientes que **quedaron** en la
    ruta. Si el RBAC podó alguno, aquí ya no está: el agente no puede
    pedir la posición de un cliente que no vio."""
    clientes = list(contexto.get("clientes_en_ruta") or [])
    if not clientes:
        return {
            "razonamiento": "Todavía no hay clientes en la ruta recorrida. No hay nada sobre lo que actuar.",
            "decision": "Saltar la ejecución de acciones en esta iteración.",
            "node_ids": [],
            "metrica": "",
        }
    return {
        "razonamiento": (
            f"La ruta llegó a {len(clientes)} cliente(s) y la pregunta exige una cifra de exposición. "
            "La cifra tiene que venir de la acción gobernada, no de mi estimación."
        ),
        "decision": "Descubrir la acción que exponen esos clientes y ejecutarla para la métrica resuelta.",
        "node_ids": clientes,
        "metrica": contexto.get("metrica") or "exposicion_crediticia",
    }


def _veredicto_critico(contexto: dict[str, Any]) -> dict[str, Any]:
    """`nodo_critico`, fase de suficiencia.

    Se apoya en los huecos que el nodo calculó de forma determinista
    (sub-preguntas sin evidencia); el modelo simulado solo decide la
    directiva y a qué nodo volver.
    """
    huecos = list(contexto.get("huecos") or [])
    if not huecos:
        return {
            "razonamiento": (
                "Cada sub-pregunta del plan tiene evidencia con linaje y la ruta va de la base de datos "
                "hasta los clientes sin huecos."
            ),
            "decision": "Dar la ruta por completa y pasar a redactar la respuesta.",
            "is_complete": True,
            "huecos": [],
            "directiva_refinamiento": None,
            "nodo_destino": None,
        }
    destino = contexto.get("destino_sugerido") or "navegar_grafo"
    directiva = contexto.get("directiva_sugerida") or f"Cerrar estos huecos: {'; '.join(huecos)}"
    return {
        "razonamiento": (
            f"Quedan {len(huecos)} sub-pregunta(s) sin evidencia suficiente. "
            "Responder ahora sería completar con suposiciones."
        ),
        "decision": f"Emitir una directiva de refinamiento y volver a {destino}.",
        "is_complete": False,
        "huecos": huecos,
        "directiva_refinamiento": directiva,
        "nodo_destino": destino,
    }


def _verificacion(contexto: dict[str, Any]) -> dict[str, Any]:
    """`nodo_critico`, fase de verificación de la respuesta redactada."""
    sin_linaje = list(contexto.get("cifras_sin_linaje") or [])
    if not sin_linaje:
        return {
            "razonamiento": "Toda cifra de la respuesta aparece en la evidencia con su linaje.",
            "decision": "Aprobar la respuesta.",
            "respuesta_valida": True,
            "cifras_sin_linaje": [],
            "directiva_refinamiento": None,
        }
    return {
        "razonamiento": (
            f"La respuesta trae {len(sin_linaje)} cifra(s) que no están en la evidencia. "
            "Una cifra sin fuente no es auditable."
        ),
        "decision": "Rechazar la respuesta y pedir que se reescriba solo con cifras de la evidencia.",
        "respuesta_valida": False,
        "cifras_sin_linaje": sin_linaje,
        "directiva_refinamiento": (
            "Reescribe la respuesta usando únicamente cifras presentes en la evidencia, "
            f"y quita estas: {', '.join(sin_linaje)}"
        ),
    }


def _cita_de_fuente(linaje: str) -> str:
    """Saca la cita documental de un linaje, si la tiene.

    `"fuente: runbook_pagos.md#L24 (extraído del documento) | grafo infra"`
    -> `"runbook_pagos.md#L24"`. Si la dependencia no vino de un
    documento, devuelve cadena vacía y la advertencia no finge una
    fuente que no existe.
    """
    import re as _re

    m = _re.search(r"fuente:\s*([^\s|]+#L\d+)", str(linaje or ""))
    return m.group(1) if m else ""


def _redactar(contexto: dict[str, Any]) -> dict[str, Any]:
    """`responder`. Redacta a partir de la evidencia recogida — nunca de
    memoria. Si no hay cifras, lo dice en vez de inventarlas."""
    cifras = contexto.get("cifras") or []
    servicios = contexto.get("servicios_afectados") or []
    responsable = contexto.get("responsable") or {}
    politicas = contexto.get("politicas") or {}
    pregunta_tipo = contexto.get("guion", "generico")

    partes: list[str] = []
    hallazgos: list[dict[str, Any]] = []
    advertencias: list[str] = []

    if cifras:
        lineas = []
        for c in cifras:
            lineas.append(
                f"- {c['nombre']} ({c['cliente_id']}): {c['valor_formateado']} {c['unidad']} "
                f"[{c['evidencia_id']}]"
            )
            hallazgos.append(
                {"texto": f"{c['nombre']}: {c['valor_formateado']} {c['unidad']}",
                 "evidencia_ids": [c["evidencia_id"]]}
            )
        umbral = contexto.get("umbral_texto") or ""
        partes.append(
            (f"Clientes corporativos afectados{umbral}:\n" + "\n".join(lineas))
        )
    elif contexto.get("clientes_en_ruta"):
        partes.append(
            "Se identificaron clientes en la ruta, pero no se pudo obtener ninguna cifra de "
            "exposición: la acción gobernada que la calcula no está permitida para este rol. "
            "No se incluyen importes porque no hay evidencia con linaje que los respalde."
        )
    else:
        partes.append("La ruta recorrida no llegó a ningún cliente con evidencia suficiente.")

    if servicios:
        ids = ", ".join(s["id"] if isinstance(s, dict) else str(s) for s in servicios)
        partes.append(f"Servicios afectados según el dominio de infraestructura: {ids}.")
        for servicio in servicios:
            sid = servicio["id"] if isinstance(servicio, dict) else str(servicio)
            if sid == contexto.get("servicio_principal"):
                continue
            # Si la dependencia se extrajo de un documento, la advertencia
            # cita la línea: es la diferencia entre "el agente lo dijo" y
            # "el agente puede enseñar dónde está escrito".
            linaje = servicio.get("linaje", "") if isinstance(servicio, dict) else ""
            cita = _cita_de_fuente(linaje)
            advertencias.append(
                f"Hallazgo colateral: {sid} depende de la misma base de datos, "
                "así que la ventana de cambio también lo afecta"
                + (f" (según {cita})." if cita else ".")
            )

    if responsable:
        partes.append(
            f"Responsable técnico: {responsable.get('nombre')} — {responsable.get('rol')}"
            + (f" ({responsable.get('contacto')})" if responsable.get("contacto") else "")
            + "."
        )
        hallazgos.append(
            {"texto": f"Responsable: {responsable.get('nombre')}",
             "evidencia_ids": [responsable.get("evidencia_id")] if responsable.get("evidencia_id") else []}
        )

    if politicas.get("filtrados"):
        partes.append(
            "Políticas aplicadas: el servidor podó "
            f"{len(politicas['filtrados'])} nodo(s) de este resultado porque el rol "
            f"'{politicas.get('rol')}' no está autorizado a verlos."
        )
    if politicas.get("denegadas"):
        partes.append(
            "Acciones denegadas por política: " + "; ".join(politicas["denegadas"]) + "."
        )

    razonamientos = {
        "hilo_conductor": "Tengo la ruta completa de la base de datos a los clientes y el responsable, "
                          "cada dato con su linaje.",
        "tes_db": "La ruta va de TES-DB al servicio y de ahí a los productos y sus clientes.",
        "aprobacion_cambio": "Tengo quién aprueba el cambio y a qué clientes alcanza.",
        "generico": "Redacto solo con lo que hay en la evidencia.",
    }
    return {
        "razonamiento": razonamientos.get(pregunta_tipo, razonamientos["generico"]),
        "decision": "Redactar la respuesta citando la evidencia dato por dato.",
        "respuesta": "\n\n".join(partes),
        "hallazgos": hallazgos,
        "advertencias": advertencias,
    }


def _interpretar_b(contexto: dict[str, Any]) -> dict[str, Any]:
    """`interpretar` del Agente B: confirmar la skill y el objeto.

    Es común a las tres preguntas porque el Agente B no ve la pregunta
    original, solo la tarea acotada que le mandaron.
    """
    skill = contexto.get("skill_pedida") or "impacto_cambio"
    objetivo = contexto.get("objetivo_pedido") or ""
    if skill not in ("impacto_cambio", "responsable_servicio"):
        skill = "responsable_servicio" if "responsable" in str(skill).lower() else "impacto_cambio"
    return {
        "razonamiento": (
            f"La tarea pide {skill.replace('_', ' ')} sobre '{objetivo}', y eso lo cubre una de mis "
            "dos skills. No necesito nada más del agente que pregunta."
        ),
        "decision": f"Resolver con la skill {skill} sobre {objetivo}.",
        "skill": skill,
        "objetivo": objetivo,
    }


#: Pasos que no dependen de la pregunta (los del Agente B).
_BASE: dict[tuple[str, str], Entrada] = {
    ("interpretar", ""): _interpretar_b,
}


# --------------------------------------------------------------------------
# Guiones por pregunta
# --------------------------------------------------------------------------
def _guion_hilo_conductor() -> dict[tuple[str, str], Entrada]:
    return {
        ("planner", ""): {
            "razonamiento": (
                "La pregunta encadena infraestructura (qué depende de la base de datos de pagos) con "
                "negocio (qué clientes y con cuánta exposición). El servicio de pagos no es mío."
            ),
            "decision": "Descomponer en cinco sub-preguntas y marcar las de infraestructura como delegables.",
            "subpreguntas": [
                {"id": "S1", "pregunta": "¿Qué servicios dependen de la base de datos del servicio de pagos?",
                 "dominio": "infra", "estado": "pendiente"},
                {"id": "S2", "pregunta": "¿Qué productos se ejecutan en el servicio de pagos?",
                 "dominio": "negocio", "estado": "pendiente"},
                {"id": "S3", "pregunta": "¿Qué clientes corporativos tienen esos productos?",
                 "dominio": "negocio", "estado": "pendiente"},
                {"id": "S4", "pregunta": "¿Cuál es la exposición crediticia de esos clientes?",
                 "dominio": "semantica", "estado": "pendiente"},
                {"id": "S5", "pregunta": "¿Quién es el responsable técnico del servicio de pagos hoy?",
                 "dominio": "infra", "estado": "pendiente"},
            ],
            "nodos_frontera": ["pagos-core"],
        },
        ("resolver_contexto", ""): {
            "razonamiento": (
                "'Exposición crediticia' es una métrica gobernada y no sé su definición vigente. "
                "Fijarla antes de navegar evita responder con la definición equivocada."
            ),
            "decision": "Resolver exposicion_crediticia contra la capa semántica antes de tocar el grafo.",
            "metricas_a_resolver": ["exposicion_crediticia"],
            "conceptos": ["cliente corporativo"],
        },
        ("navegar_grafo", "iter1"): {
            "razonamiento": (
                "Entro por el servicio de pagos y recorro hacia productos y clientes corporativos. "
                "Lo que hay detrás de la base de datos no está en mi grafo."
            ),
            "decision": "Localizar pagos-core y recorrer SE_EJECUTA_EN y TIENE hacia atrás.",
            "texto_entrada": "pagos",
            "consultas": [
                _Q_PRODUCTOS.format(servicio="pagos-core"),
                _Q_CLIENTES_CORP.format(servicio="pagos-core"),
            ],
        },
        ("ejecutar_accion", "iter1"): _accion_sobre_clientes_en_ruta,
        ("delegar_a2a", "iter1"): {
            "razonamiento": (
                "pagos-core es un nodo frontera: mi grafo solo tiene su id y su dueño. "
                "El impacto de migrar su base de datos lo sabe el dominio de infraestructura."
            ),
            "decision": "Delegar impacto_cambio sobre PAY-DB-01 al agente de infraestructura.",
            "skill": "impacto_cambio",
            "parametro": "PAY-DB-01",
            "pregunta_parcial": "¿Qué servicios se verían afectados si se migra la base de datos PAY-DB-01?",
        },
        ("delegar_a2a", "iter2"): {
            "razonamiento": (
                "Falta el responsable técnico y ese dato tampoco vive en mi dominio. "
                "Es la segunda sub-pregunta que tengo que delegar."
            ),
            "decision": "Delegar responsable_servicio sobre pagos-core.",
            "skill": "responsable_servicio",
            "parametro": "pagos-core",
            "pregunta_parcial": "¿Quién es el responsable técnico del servicio pagos-core hoy?",
        },
        ("nodo_critico", "suficiencia"): _veredicto_critico,
        ("nodo_critico", "verificacion"): _verificacion,
        ("responder", ""): _redactar,
    }


def _guion_tes_db() -> dict[tuple[str, str], Entrada]:
    return {
        ("planner", ""): {
            "razonamiento": (
                "TES-DB es una base de datos: qué servicio depende de ella es de infraestructura. "
                "Qué productos quedan sin servicio sí es mío."
            ),
            "decision": "Separar la parte de infraestructura de la de negocio y delegar la primera.",
            "subpreguntas": [
                {"id": "S1", "pregunta": "¿Qué servicios dependen de TES-DB?", "dominio": "infra",
                 "estado": "pendiente"},
                {"id": "S2", "pregunta": "¿Qué productos se ejecutan en esos servicios?",
                 "dominio": "negocio", "estado": "pendiente"},
                {"id": "S3", "pregunta": "¿Qué clientes quedarían sin ese producto?",
                 "dominio": "negocio", "estado": "pendiente"},
            ],
            "nodos_frontera": ["TES-DB"],
        },
        ("resolver_contexto", ""): {
            "razonamiento": "La pregunta no pide ninguna métrica gobernada, solo relaciones del grafo.",
            "decision": "No resolver métricas; seguir a la navegación.",
            "metricas_a_resolver": [],
            "conceptos": ["producto"],
        },
        ("navegar_grafo", "iter1"): {
            "razonamiento": (
                "TES-DB no aparece en mi grafo de negocio: es de otro dominio. "
                "Sin saber qué servicio depende de ella no puedo recorrer nada."
            ),
            "decision": "Buscar TES-DB para confirmar que no es mío y delegar.",
            "texto_entrada": "TES-DB",
            "consultas": [],
        },
        ("navegar_grafo", "iter2"): lambda ctx: {
            "razonamiento": (
                "Infraestructura me dio el servicio afectado; ahora sí puedo recorrer hacia productos "
                "y clientes."
            ),
            "decision": "Recorrer SE_EJECUTA_EN y TIENE desde el servicio que me devolvieron.",
            "texto_entrada": (ctx.get("servicio_principal") or "tesoreria-api"),
            "consultas": [
                _Q_PRODUCTOS.format(servicio=ctx.get("servicio_principal") or "tesoreria-api"),
                _Q_CLIENTES_TODOS.format(servicio=ctx.get("servicio_principal") or "tesoreria-api"),
            ],
        },
        ("ejecutar_accion", "iter1"): {
            "razonamiento": "Todavía no hay clientes en la ruta y la pregunta no pide cifras.",
            "decision": "Saltar la ejecución de acciones.",
            "node_ids": [],
            "metrica": "",
        },
        ("ejecutar_accion", "iter2"): {
            "razonamiento": "La pregunta es de continuidad de servicio, no de importes.",
            "decision": "No ejecutar acciones: no hace falta ninguna cifra.",
            "node_ids": [],
            "metrica": "",
        },
        ("delegar_a2a", "iter1"): {
            "razonamiento": "TES-DB pertenece al dominio de infraestructura, que sabe qué depende de ella.",
            "decision": "Delegar impacto_cambio sobre TES-DB.",
            "skill": "impacto_cambio",
            "parametro": "TES-DB",
            "pregunta_parcial": "¿Qué servicios dejarían de funcionar si cae la base de datos TES-DB?",
        },
        ("nodo_critico", "suficiencia"): _veredicto_critico,
        ("nodo_critico", "verificacion"): _verificacion,
        ("responder", ""): _redactar,
    }


def _guion_aprobacion_cambio() -> dict[tuple[str, str], Entrada]:
    return {
        ("planner", ""): {
            "razonamiento": (
                "Quién aprueba un cambio y qué alcance tiene es de infraestructura; a qué clientes "
                "notificar es mío."
            ),
            "decision": "Delegar el cambio y su responsable, y recorrer después hacia los clientes.",
            "subpreguntas": [
                {"id": "S1", "pregunta": "¿A qué servicios alcanza el cambio CHG-2026-0917?",
                 "dominio": "infra", "estado": "pendiente"},
                {"id": "S2", "pregunta": "¿Quién es el responsable que debe aprobarlo?",
                 "dominio": "infra", "estado": "pendiente"},
                {"id": "S3", "pregunta": "¿Qué clientes usan productos que corren en esos servicios?",
                 "dominio": "negocio", "estado": "pendiente"},
            ],
            "nodos_frontera": ["CHG-2026-0917"],
        },
        ("resolver_contexto", ""): {
            "razonamiento": "La pregunta es de alcance y notificación; no hay métrica que fijar.",
            "decision": "Seguir sin resolver métricas.",
            "metricas_a_resolver": [],
            "conceptos": ["cliente"],
        },
        ("navegar_grafo", "iter1"): {
            "razonamiento": "El cambio es un nodo de infraestructura: en mi grafo no existe.",
            "decision": "Confirmar que el cambio no está en mi dominio y delegarlo.",
            "texto_entrada": "CHG-2026-0917",
            "consultas": [],
        },
        ("navegar_grafo", "iter2"): lambda ctx: {
            "razonamiento": "Ya sé a qué servicios alcanza el cambio; ahora busco a quién hay que avisar.",
            "decision": "Recorrer desde cada servicio afectado hacia productos y clientes.",
            "texto_entrada": ctx.get("servicio_principal") or "pagos-core",
            "consultas": [
                _Q_CLIENTES_TODOS.format(servicio=s)
                for s in (ctx.get("servicios_ids") or ["pagos-core"])
            ],
        },
        ("ejecutar_accion", "iter1"): {
            "razonamiento": "Sin clientes en la ruta todavía no hay acción que ejecutar.",
            "decision": "Saltar.",
            "node_ids": [],
            "metrica": "",
        },
        ("ejecutar_accion", "iter2"): {
            "razonamiento": "La pregunta pide a quién notificar, no cuánto expone cada cliente.",
            "decision": "No ejecutar acciones.",
            "node_ids": [],
            "metrica": "",
        },
        ("delegar_a2a", "iter1"): {
            "razonamiento": "El cambio y su responsable viven en el dominio de infraestructura.",
            "decision": "Delegar impacto_cambio sobre el cambio CHG-2026-0917.",
            "skill": "impacto_cambio",
            "parametro": "CHG-2026-0917",
            "pregunta_parcial": "¿A qué servicios alcanza el cambio CHG-2026-0917 y quién lo aprueba?",
        },
        ("delegar_a2a", "iter2"): lambda ctx: {
            "razonamiento": "Falta el nombre de quien aprueba; lo pido por la skill que lo cubre.",
            "decision": "Delegar responsable_servicio sobre el servicio principal afectado.",
            "skill": "responsable_servicio",
            "parametro": ctx.get("servicio_principal") or "pagos-core",
            "pregunta_parcial": "¿Quién es el responsable técnico que debe aprobar el cambio?",
        },
        ("nodo_critico", "suficiencia"): _veredicto_critico,
        ("nodo_critico", "verificacion"): _verificacion,
        ("responder", ""): _redactar,
    }


def _guion_generico() -> dict[tuple[str, str], Entrada]:
    """Para preguntas que el simulado no reconoce: no inventa nada."""
    return {
        ("planner", ""): {
            "razonamiento": "No tengo un guion para esta pregunta en modo simulado.",
            "decision": "Plantear una sub-pregunta genérica y dejar que el crítico detecte el hueco.",
            "subpreguntas": [
                {"id": "S1", "pregunta": "Pregunta no reconocida por el LLM simulado",
                 "dominio": "negocio", "estado": "pendiente"},
            ],
            "nodos_frontera": [],
        },
        ("resolver_contexto", ""): {
            "razonamiento": "Sin guion no sé qué métricas hacen falta.",
            "decision": "No resolver ninguna métrica.",
            "metricas_a_resolver": [],
            "conceptos": [],
        },
        ("navegar_grafo", ""): {
            "razonamiento": "Sin guion no puedo escribir un recorrido fiable.",
            "decision": "No ejecutar consultas.",
            "texto_entrada": "",
            "consultas": [],
        },
        ("ejecutar_accion", ""): {
            "razonamiento": "No hay ruta sobre la que actuar.",
            "decision": "Saltar.",
            "node_ids": [],
            "metrica": "",
        },
        ("delegar_a2a", ""): {
            "razonamiento": "No hay nodo frontera identificado.",
            "decision": "No delegar.",
            "skill": "",
            "parametro": "",
            "pregunta_parcial": "",
        },
        ("nodo_critico", "suficiencia"): _veredicto_critico,
        ("nodo_critico", "verificacion"): _verificacion,
        ("responder", ""): _redactar,
    }


#: Palabras que identifican cada pregunta del §5.
_DETECTORES: list[tuple[str, tuple[str, ...]]] = [
    ("aprobacion_cambio", ("chg-2026-0917", "aprobar")),
    ("tes_db", ("tes-db",)),
    ("hilo_conductor", ("pagos", "exposición crediticia", "exposicion crediticia")),
]

_GUIONES: dict[str, Callable[[], dict[tuple[str, str], Entrada]]] = {
    "hilo_conductor": _guion_hilo_conductor,
    "tes_db": _guion_tes_db,
    "aprobacion_cambio": _guion_aprobacion_cambio,
    "generico": _guion_generico,
}


def detectar_guion(query: str) -> str:
    """Qué pregunta del guion (§5) es esta."""
    q = (query or "").lower()
    for nombre, palabras in _DETECTORES:
        if any(p in q for p in palabras):
            return nombre
    return "generico"


class LLMFake(LLM):
    """LLM simulado. Ver docstring del módulo."""

    nombre = "fake"

    def __init__(self, query: str | None = None) -> None:
        self._guion_nombre = detectar_guion(query or "")
        self._tabla = {**_BASE, **_GUIONES[self._guion_nombre]()}

    def para_query(self, query: str) -> "LLMFake":
        """Devuelve un simulado atado al guion de esta pregunta."""
        return LLMFake(query)

    @property
    def guion(self) -> str:
        return self._guion_nombre

    def _buscar(self, paso: str, variante: str) -> Entrada:
        for clave in ((paso, variante), (paso, ""), (paso, "iter1")):
            if clave in self._tabla:
                return self._tabla[clave]
        raise KeyError(
            f"el LLM simulado no tiene respuesta para el paso '{paso}' (variante '{variante}') "
            f"del guion '{self._guion_nombre}'"
        )

    async def estructurado(self, modelo, *, sistema, humano, paso, variante="", contexto=None):
        entrada = self._buscar(paso, variante)
        datos = entrada(dict(contexto or {})) if callable(entrada) else dict(entrada)
        datos.setdefault("guion", self._guion_nombre)
        # Solo los campos que el modelo declara: los guiones comparten
        # claves auxiliares (como `guion`) que no todos los modelos tienen.
        permitidos = {k: v for k, v in datos.items() if k in modelo.model_fields}
        instancia = modelo.model_validate(permitidos)
        assert isinstance(instancia, PasoRazonado)
        return instancia, UsoLLM(modelo=self.nombre, tokens_in=0, tokens_out=0)
