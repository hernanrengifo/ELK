"""
modelos.py — estado del Agente A y contratos de salida de cada nodo
(plan_demo.md §4.5).

Dos familias de modelos:

1. **`AgentState`** — el estado del grafo LangGraph, con exactamente los
   campos del §4.5: `query, user_ctx, plan[], visited_nodes[],
   evidence[], pending_gaps[], is_complete, iteration,
   max_iterations=6, audit_trail[]`.

2. **Salidas estructuradas por nodo** — todas heredan de `PasoRazonado`,
   que fija los dos campos que el plan exige pedirle al modelo en cada
   paso: `{razonamiento (≤ 2 frases), decision}`. De ahí sale el evento
   `thought` de la Sala de control, sin exponer el prompt completo
   (§4.8, payloads por tipo).

La regla de "ninguna cifra sin linaje" se apoya en `Evidencia`: toda
cifra que aparezca en la respuesta tiene que existir aquí, con su
`linaje`, y el nodo crítico lo verifica (`verificar_cifras`).
"""

from __future__ import annotations

import ast
import json
import re
from typing import Annotated, Any, Literal, TypedDict, get_args, get_origin

from pydantic import BaseModel, Field, field_validator, model_validator

#: Tope de frases del campo `razonamiento` (§8, tabla de riesgos: "pedir
#: salida estructurada {razonamiento (≤ 2 frases), decision} por paso").
MAX_FRASES_RAZONAMIENTO = 2

NodoDestino = Literal["navegar_grafo", "ejecutar_accion", "delegar_a2a", "responder"]


def _recortar_a_frases(texto: str, maximo: int = MAX_FRASES_RAZONAMIENTO) -> str:
    """Deja como mucho `maximo` frases.

    Se recorta en vez de rechazar: un modelo real que se pase de largo no
    debe tumbar la corrida en mitad de la demo, pero la tarjeta de la
    Sala de control tiene que seguir cabiendo en pantalla.
    """
    texto = " ".join((texto or "").split())
    if not texto:
        return texto
    frases = re.findall(r"[^.!?]+[.!?]?", texto)
    frases = [f.strip() for f in frases if f.strip()]
    if len(frases) <= maximo:
        return texto
    return " ".join(frases[:maximo])


def _es_campo_de_lista(anotacion: Any) -> bool:
    """¿El campo declara una lista? (también dentro de un `X | None`)."""
    if get_origin(anotacion) is list:
        return True
    return any(get_origin(a) is list for a in get_args(anotacion))


def _a_lista(texto: str) -> list[Any]:
    """Convierte a lista lo que el modelo mandó como texto.

    Los modelos serializan de vez en cuando un campo de lista como una
    cadena con la lista dentro: `"['falta el responsable', 'sin cifras']"`.
    Se intenta JSON y después literal de Python (que cubre las comillas
    simples); si no es ninguna de las dos, se trata como un único
    elemento, que es lo que quiso decir.
    """
    limpio = texto.strip()
    if not limpio:
        return []
    for parsear in (json.loads, ast.literal_eval):
        try:
            valor = parsear(limpio)
        except (ValueError, SyntaxError):
            continue
        if isinstance(valor, (list, tuple)):
            return list(valor)
    return [texto]


class ModeloTolerante(BaseModel):
    """Base de todo lo que el LLM rellena.

    Acepta que un campo de lista llegue como cadena y lo convierte. No es
    laxitud gratuita: `claude-sonnet-5` devolvió `huecos` como
    `"['...', '...']"` en el nodo crítico y la corrida entera se caía con
    un `validation error` de Pydantic a mitad del recorrido. El contrato
    que importa —qué campos hay y de qué tipo— no se relaja: solo se
    normaliza la forma en que llegan.
    """

    @model_validator(mode="before")
    @classmethod
    def _normalizar_listas(cls, datos: Any) -> Any:
        if not isinstance(datos, dict):
            return datos
        for nombre, campo in cls.model_fields.items():
            valor = datos.get(nombre)
            if isinstance(valor, str) and _es_campo_de_lista(campo.annotation):
                datos[nombre] = _a_lista(valor)
        return datos


class PasoRazonado(ModeloTolerante):
    """Lo que se le pide al LLM en **todos** los pasos de ambos agentes."""

    razonamiento: str = Field(
        description="Por qué haces esto, en máximo dos frases. Sin repetir la pregunta."
    )
    decision: str = Field(description="Qué decides hacer ahora, en una frase.")

    @field_validator("razonamiento")
    @classmethod
    def _max_dos_frases(cls, v: str) -> str:
        return _recortar_a_frases(v)

    @field_validator("decision")
    @classmethod
    def _decision_en_una_linea(cls, v: str) -> str:
        return " ".join((v or "").split())


# --------------------------------------------------------------------------
# Piezas del estado
# --------------------------------------------------------------------------
class SubPregunta(ModeloTolerante):
    """Una entrada del `plan[]`. El §4.8 pide que el evento `plan` muestre
    su estado cambiando en vivo."""

    id: str
    pregunta: str
    dominio: Literal["negocio", "infra", "semantica"] = "negocio"
    estado: Literal["pendiente", "en_curso", "resuelta", "delegada"] = "pendiente"


class Evidencia(BaseModel):
    """Un dato con su origen. **Sin `linaje` no es evidencia.**"""

    id: str
    tipo: Literal["nodo", "cifra", "definicion", "a2a"]
    descripcion: str
    valor: float | None = None
    unidad: str | None = None
    linaje: str
    origen: str = Field(description="Quién lo trajo: mcp_graph, mcp_actions, agent_b…")
    nodo: str | None = Field(
        default=None, description="Nodo del grafo al que pertenece el dato, si aplica."
    )

    @field_validator("linaje")
    @classmethod
    def _linaje_obligatorio(cls, v: str) -> str:
        if not (v or "").strip():
            raise ValueError("una evidencia sin linaje no sirve: no se puede auditar")
        return v


class Hallazgo(ModeloTolerante):
    """Una afirmación de la respuesta final, atada a su evidencia."""

    texto: str
    evidencia_ids: list[str] = Field(default_factory=list)


class AgentState(TypedDict, total=False):
    """Estado del grafo del Agente A — los campos del §4.5, literales."""

    query: str
    user_ctx: dict[str, Any]
    plan: list[dict[str, Any]]
    visited_nodes: list[str]
    evidence: list[dict[str, Any]]
    pending_gaps: list[str]
    is_complete: bool
    iteration: int
    max_iterations: int
    audit_trail: list[dict[str, Any]]

    # --- campos de trabajo (no son parte del contrato del §4.5) ---
    run_id: str
    esquema: dict[str, Any]
    definiciones: dict[str, Any]
    servicios_frontera: list[str]
    respuesta: dict[str, Any] | None
    directiva_refinamiento: str | None
    directivas_previas: list[str]
    proximo_nodo: str
    motivo_corte: str | None
    #: Nodos que el servidor MCP podó por RBAC durante la corrida: la
    #: respuesta tiene que decir qué se filtró y por qué (§4.5, nodo 7).
    nodos_podados: list[str]
    #: Mensajes explicables de las acciones denegadas por política.
    acciones_denegadas: list[str]
    #: Contratos devueltos por el otro agente (A2A), en orden.
    respuestas_a2a: list[dict[str, Any]]
    #: Contratos de acción descubiertos con `list_node_actions`.
    contratos_accion: dict[str, Any]
    #: Cuántas veces se ha reescrito la respuesta tras un rechazo del crítico.
    intentos_respuesta: int
    #: id de nodo -> nombre legible, para que la respuesta hable de
    #: "Textiles Andinos S.A.S." y no de "CLI01".
    nombres_nodos: dict[str, str]
    #: Bitácora de llamadas ya ejecutadas en esta corrida, con un resumen
    #: de lo que devolvió cada una. Los prompts la enseñan para que el
    #: modelo no vuelva a pedir lo mismo: sin ella, medido con el modelo
    #: real, 41 de 64 llamadas de la pregunta principal eran repeticiones
    #: exactas, y cada repetición arrastra una iteración entera.
    consultas_hechas: list[str]


# --------------------------------------------------------------------------
# Salidas estructuradas, una por nodo
# --------------------------------------------------------------------------
class SalidaPlanner(PasoRazonado):
    subpreguntas: list[SubPregunta] = Field(
        description="Descomposición de la pregunta en sub-preguntas, marcando el dominio de cada una."
    )
    nodos_frontera: list[str] = Field(
        default_factory=list,
        description="Ids de nodos que pertenecen a otro dominio y habrá que delegar por A2A.",
    )


class SalidaContexto(PasoRazonado):
    metricas_a_resolver: list[str] = Field(
        default_factory=list,
        description="Nombres de métricas de negocio cuya definición hay que fijar antes de navegar.",
    )
    conceptos: list[str] = Field(
        default_factory=list,
        description="Conceptos del dominio que se resuelven contra la ontología (p.ej. 'cliente corporativo').",
    )


class SalidaNavegacion(PasoRazonado):
    texto_entrada: str = Field(
        default="", description="Texto para find_entry_nodes: por dónde entrar al grafo."
    )
    consultas: list[str] = Field(
        default_factory=list, description="Consultas Cypher de solo lectura para recorrer k saltos."
    )


class SalidaAccion(PasoRazonado):
    node_ids: list[str] = Field(
        default_factory=list,
        description="Nodos cuya acción hay que ejecutar (se descubre con list_node_actions).",
    )
    metrica: str = Field(default="", description="Métrica a calcular con la acción.")


class SalidaDelegacion(PasoRazonado):
    skill: str = Field(default="", description="Skill del agent card del otro agente.")
    parametro: str = Field(default="", description="Id del servicio o de la base de datos.")
    pregunta_parcial: str = Field(
        default="", description="La sub-pregunta en palabras, sin contexto de más."
    )


class SalidaCritico(PasoRazonado):
    is_complete: bool = Field(description="¿Cada sub-pregunta del plan tiene evidencia suficiente?")
    huecos: list[str] = Field(default_factory=list, description="Qué falta, en concreto.")
    directiva_refinamiento: str | None = Field(
        default=None, description="Qué hacer para cerrar el hueco. Nula si está completo."
    )
    nodo_destino: NodoDestino | None = Field(
        default=None, description="A qué nodo volver para aplicar la directiva."
    )


class SalidaVerificacion(PasoRazonado):
    respuesta_valida: bool = Field(description="¿Toda cifra de la respuesta está en evidence[] con linaje?")
    cifras_sin_linaje: list[str] = Field(default_factory=list)
    directiva_refinamiento: str | None = None


class SalidaRespuesta(PasoRazonado):
    respuesta: str = Field(description="La respuesta al usuario, en prosa. Toda cifra debe venir de la evidencia.")
    hallazgos: list[Hallazgo] = Field(default_factory=list)
    advertencias: list[str] = Field(
        default_factory=list, description="Hallazgos colaterales relevantes que no se preguntaron."
    )


# --- Agente B ---------------------------------------------------------
class SalidaInterpretacionB(PasoRazonado):
    skill: Literal["impacto_cambio", "responsable_servicio"] = Field(
        description="Qué skill del agent card resuelve esta tarea."
    )
    objetivo: str = Field(description="Id del servicio o de la base de datos sobre el que actuar.")

