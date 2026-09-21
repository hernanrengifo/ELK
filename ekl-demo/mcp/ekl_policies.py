"""
ekl_policies.py — carga de la gobernanza y de la capa semántica,
compartida por los dos servidores MCP (plan_demo.md §3.2, §3.3, §4.3,
§4.4).

Un único lugar donde se leen `data/policies.yaml`, `data/ontology.yaml` y
`data/semantic_layer.yaml`, con dos reglas importantes para la demo:

1. **Nada de RBAC hardcodeado.** Qué rol puede ver un `Cliente` con
   `sensibilidad: alta`, o quién puede ejecutar
   `retrieve_customer_position`, sale siempre del YAML. Es el punto del
   minuto 8-9 del guion: "la política vive en la capa, no en el prompt".

2. **Recarga en caliente por `mtime`.** El momento "wow" #1 del guion
   (§5, minuto 7-8) es editar `semantic_layer.yaml` en vivo y repetir la
   pregunta. Para que eso funcione sin reiniciar los servidores MCP, la
   caché se invalida cuando cambia la fecha de modificación del archivo.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

DATA_DIR = Path(os.environ.get("EKL_DATA_DIR") or (Path(__file__).resolve().parent.parent / "data"))

POLICIES_FILE = DATA_DIR / "policies.yaml"
ONTOLOGY_FILE = DATA_DIR / "ontology.yaml"
SEMANTIC_LAYER_FILE = DATA_DIR / "semantic_layer.yaml"

_cache: dict[Path, tuple[float, Any]] = {}


def _cargar_yaml(path: Path) -> dict[str, Any]:
    """Lee un YAML con caché invalidada por `mtime` (ver docstring)."""
    mtime = path.stat().st_mtime
    cacheado = _cache.get(path)
    if cacheado is not None and cacheado[0] == mtime:
        return cacheado[1]
    with open(path, encoding="utf-8") as f:
        datos = yaml.safe_load(f) or {}
    _cache[path] = (mtime, datos)
    return datos


def cargar_policies() -> dict[str, Any]:
    return _cargar_yaml(POLICIES_FILE)


def cargar_ontology() -> dict[str, Any]:
    return _cargar_yaml(ONTOLOGY_FILE)


def cargar_semantic_layer() -> dict[str, Any]:
    return _cargar_yaml(SEMANTIC_LAYER_FILE)


# --------------------------------------------------------------------------
# RBAC de nodos (policies.yaml -> node_policies)
# --------------------------------------------------------------------------
class DecisionPolitica:
    """Resultado de evaluar una política: si se permite, qué regla se
    aplicó y por qué. Los tres campos van tal cual al evento
    `policy_decision` del §4.8."""

    __slots__ = ("permitido", "regla", "justificacion", "efecto")

    def __init__(self, permitido: bool, regla: str, justificacion: str, efecto: str = "") -> None:
        self.permitido = permitido
        self.regla = regla
        self.justificacion = justificacion
        self.efecto = efecto

    def __repr__(self) -> str:  # pragma: no cover - ayuda al depurar
        return (
            f"DecisionPolitica(permitido={self.permitido}, regla={self.regla!r}, "
            f"efecto={self.efecto!r})"
        )


PERMITIDO = DecisionPolitica(True, "sin_politica_aplicable", "ninguna política restringe este nodo")


def evaluar_nodo(rol: str, label: str | None, propiedades: dict[str, Any]) -> DecisionPolitica:
    """¿Puede `rol` ver este nodo?

    Recorre `node_policies[<label>][<propiedad>][<valor>]` de
    `policies.yaml`. Si el valor de esa propiedad en el nodo tiene una
    regla y el rol no está en `permitido_para` (o está en
    `denegado_para`), el nodo se poda.
    """
    if not label:
        return PERMITIDO
    node_policies = cargar_policies().get("node_policies") or {}
    reglas_label = node_policies.get(label)
    if not reglas_label:
        return PERMITIDO

    for propiedad, valores in reglas_label.items():
        valor_nodo = propiedades.get(propiedad)
        if valor_nodo is None:
            continue
        regla = (valores or {}).get(str(valor_nodo))
        if not regla:
            continue
        permitidos = regla.get("permitido_para") or []
        denegados = regla.get("denegado_para") or []
        efecto = regla.get("efecto", "podar_nodo")
        if rol in denegados or (permitidos and rol not in permitidos):
            return DecisionPolitica(
                permitido=False,
                regla=f"node_policies.{label}.{propiedad}.{valor_nodo}",
                justificacion=(
                    f"el rol '{rol}' no está autorizado para nodos {label} con "
                    f"{propiedad}='{valor_nodo}' (permitido_para={permitidos or 'ninguno'}); "
                    f"efecto: {efecto}"
                ),
                efecto=efecto,
            )
    return PERMITIDO


# --------------------------------------------------------------------------
# RBAC de acciones (policies.yaml -> accion_policies, ontology.yaml -> acciones)
# --------------------------------------------------------------------------
def politica_de_accion(nombre: str) -> dict[str, Any]:
    """Política de una acción. `policies.yaml` manda; si la acción solo
    está declarada en `ontology.yaml`, se usa la de ahí (los dos lugares
    la declaran, §3.2)."""
    desde_policies = (cargar_policies().get("accion_policies") or {}).get(nombre)
    if desde_policies:
        return desde_policies
    for accion in cargar_ontology().get("acciones") or []:
        if accion.get("nombre") == nombre:
            return accion.get("politica") or {}
    return {}


def evaluar_accion(rol: str, nombre: str) -> DecisionPolitica:
    """¿Puede `rol` ejecutar la acción `nombre`? (plan §4.4: "verifica la
    política de la acción contra el rol del usuario; deniega con mensaje
    explicable")."""
    politica = politica_de_accion(nombre)
    requiere = politica.get("requiere_rol") or []
    if not requiere or rol in requiere:
        return DecisionPolitica(
            permitido=True,
            regla=f"accion_policies.{nombre}.requiere_rol",
            justificacion=(
                f"el rol '{rol}' está en requiere_rol={requiere}"
                if requiere
                else f"la acción '{nombre}' no exige rol"
            ),
        )
    return DecisionPolitica(
        permitido=False,
        regla=f"accion_policies.{nombre}.requiere_rol",
        justificacion=(
            f"la acción '{nombre}' requiere uno de los roles {requiere} y el usuario "
            f"tiene el rol '{rol}'"
        ),
        efecto=politica.get("efecto_denegado", "rechazar_con_mensaje_explicable"),
    )


def contrato_de_accion(nombre: str) -> dict[str, Any] | None:
    """Contrato público de una acción: entrada, salida y política.

    **Nunca incluye `backend`**: el §3.2 es explícito en que el agente
    recibe `nombre + contrato + política` y que es el MCP server quien
    resuelve dónde vive la acción.
    """
    for accion in cargar_ontology().get("acciones") or []:
        if accion.get("nombre") == nombre:
            return {
                "nombre": accion["nombre"],
                "expuesta_por": accion.get("expuesta_por"),
                "contrato": accion.get("contrato", {}),
                "politica": politica_de_accion(nombre),
            }
    return None
