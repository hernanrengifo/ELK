"""
ekl_cypher.py — intérprete de un subconjunto de Cypher **de solo
lectura**, para `read_cypher` del servidor MCP de grafo
(plan_demo.md §4.3).

Por qué existe: el plan pide que `read_cypher` funcione contra Neo4j,
pero también que la demo corra sin Docker (`GRAPH_BACKEND=memory`, donde
el grafo es un `networkx` sin motor Cypher). En vez de tener dos caminos
distintos —Cypher nativo en un backend y otra cosa en el otro, con RBAC
duplicado— se ejecuta el mismo intérprete sobre la interfaz
`GraphBackend`, así el resultado y el podado por política son idénticos
en los dos backends.

Subconjunto soportado (suficiente para los recorridos de k saltos del
§4.5 y las consultas del guion):

    MATCH (c:Cliente {segmento:'corporativo'})-[:TIENE]->(p:Producto)
          -[:SE_EJECUTA_EN]->(s:Servicio {id:'pagos-core'})
    WHERE c.sensibilidad <> 'alta' AND p.tipo IN ['credito','cash_management']
    RETURN DISTINCT c, p.nombre AS producto
    LIMIT 50

  - un patrón lineal: nodos `(var:Label {prop:valor, ...})` encadenados
    con `-[:REL]->` o `<-[:REL]-` (cualquier número de saltos);
  - **variables de relación**: `-[r:DEPENDE_DE]->` permite devolver las
    propiedades de la arista (`RETURN r.fuente`), que es donde vive el
    linaje que escribe la extracción desde documentos;
  - `WHERE` con condiciones unidas por `AND` y los operadores
    `= <> != > >= < <= CONTAINS STARTS WITH ENDS WITH IN`;
  - `RETURN [DISTINCT] var | var.prop [AS alias] | *`;
  - `LIMIT n`.

Lo que **no** soporta (y rechaza con un mensaje que lo dice): escritura
de cualquier tipo, `OPTIONAL MATCH`, `WITH`, `UNWIND`, `CALL`,
agregaciones, `ORDER BY`, caminos de longitud variable (`[:REL*1..3]`) y
`OR` en el `WHERE`.

La garantía de solo lectura es doble: la consulta debe empezar por
`MATCH` y no puede contener ninguna palabra clave de escritura.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

#: Palabras que convierten una consulta en escritura o en algo que este
#: intérprete no puede garantizar como seguro.
PALABRAS_PROHIBIDAS = (
    "CREATE", "MERGE", "DELETE", "DETACH", "SET", "REMOVE", "DROP",
    "CALL", "LOAD", "FOREACH", "USING PERIODIC", "ALTER", "GRANT", "REVOKE",
)

#: Cláusulas de lectura válidas en Cypher que este subconjunto no implementa.
PALABRAS_NO_SOPORTADAS = ("OPTIONAL MATCH", "WITH", "UNWIND", "UNION", "ORDER BY")


class CypherNoSoportado(ValueError):
    """La consulta es Cypher válido pero está fuera del subconjunto."""


class CypherNoPermitido(ValueError):
    """La consulta intenta escribir (o algo que no se puede garantizar de
    solo lectura). `read_cypher` es de solo lectura por contrato."""


# --------------------------------------------------------------------------
# Modelo de la consulta
# --------------------------------------------------------------------------
@dataclass
class PatronNodo:
    var: str | None
    label: str | None
    props: dict[str, Any] = field(default_factory=dict)


@dataclass
class PatronRelacion:
    tipo: str
    direccion: str  # "out" | "in"
    #: Variable de la relación, si el patrón la declara
    #: (`-[r:DEPENDE_DE]->`). Sin ella la arista no se puede consultar;
    #: con ella, `r.fuente` da el linaje que escribió la extracción desde
    #: documentos (F6).
    var: str | None = None


@dataclass
class Condicion:
    var: str
    prop: str
    operador: str
    valor: Any


@dataclass
class ItemRetorno:
    var: str
    prop: str | None
    alias: str


@dataclass
class ConsultaLeida:
    nodos: list[PatronNodo]
    relaciones: list[PatronRelacion]
    condiciones: list[Condicion]
    retorno: list[ItemRetorno]          # vacío == RETURN *
    distinct: bool = False
    limite: int | None = None

    @property
    def labels_usados(self) -> list[str]:
        return [n.label for n in self.nodos if n.label]


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------
_RE_NODO = re.compile(
    r"\(\s*(?P<var>[A-Za-z_]\w*)?\s*(?::\s*(?P<label>[A-Za-z_]\w*))?\s*(?P<props>\{[^}]*\})?\s*\)"
)
_RE_REL_DER = re.compile(
    r"^-\s*\[\s*(?P<var>[A-Za-z_]\w*)?\s*:\s*(?P<tipo>[A-Za-z_]\w*)\s*\]\s*->"
)
_RE_REL_IZQ = re.compile(
    r"^<-\s*\[\s*(?P<var>[A-Za-z_]\w*)?\s*:\s*(?P<tipo>[A-Za-z_]\w*)\s*\]\s*-"
)
_RE_VAR_LEN = re.compile(r"\[\s*[A-Za-z_]*\s*:\s*[A-Za-z_]\w*\s*\*")

_OPERADORES = ("STARTS WITH", "ENDS WITH", "CONTAINS", "IN", "<>", "!=", ">=", "<=", "=", ">", "<")


def _sin_comillas(texto: str) -> str:
    """Reemplaza el contenido de los literales de texto por guiones bajos,
    para poder buscar palabras clave sin confundirlas con datos
    (`WHERE c.nombre = 'DELETE me'` no es una escritura)."""
    return re.sub(r"'[^']*'|\"[^\"]*\"", lambda m: "'" + "_" * (len(m.group(0)) - 2) + "'", texto)


def validar_solo_lectura(query: str) -> None:
    """Rechaza cualquier consulta que no sea un `MATCH ... RETURN ...`."""
    limpia = _sin_comillas(query).upper()
    if not limpia.strip().startswith("MATCH"):
        raise CypherNoPermitido(
            "read_cypher solo acepta consultas de lectura que empiecen por MATCH; "
            f"esta empieza por {limpia.strip().split(None, 1)[0] if limpia.strip() else '(vacío)'}"
        )
    for palabra in PALABRAS_PROHIBIDAS:
        if re.search(rf"\b{re.escape(palabra)}\b", limpia):
            raise CypherNoPermitido(
                f"read_cypher es de solo lectura: la consulta contiene '{palabra}'"
            )
    if "RETURN" not in limpia:
        raise CypherNoSoportado("la consulta debe tener una cláusula RETURN")
    for palabra in PALABRAS_NO_SOPORTADAS:
        if re.search(rf"\b{re.escape(palabra)}\b", limpia):
            raise CypherNoSoportado(
                f"'{palabra}' no está en el subconjunto de Cypher que soporta esta demo "
                "(ver docstring de mcp/ekl_cypher.py)"
            )


def _parsear_valor(texto: str) -> Any:
    texto = texto.strip()
    if texto.startswith("[") and texto.endswith("]"):
        interior = texto[1:-1].strip()
        if not interior:
            return []
        return [_parsear_valor(x) for x in _dividir_nivel_cero(interior, ",")]
    if (texto.startswith("'") and texto.endswith("'")) or (texto.startswith('"') and texto.endswith('"')):
        return texto[1:-1]
    bajo = texto.lower()
    if bajo in ("true", "false"):
        return bajo == "true"
    if bajo == "null":
        return None
    try:
        return int(texto)
    except ValueError:
        pass
    try:
        return float(texto)
    except ValueError:
        pass
    return texto


def _dividir_nivel_cero(texto: str, sep: str) -> list[str]:
    """Divide por `sep` ignorando lo que esté dentro de comillas o
    corchetes."""
    partes, actual, profundidad, comilla = [], [], 0, ""
    for ch in texto:
        if comilla:
            actual.append(ch)
            if ch == comilla:
                comilla = ""
            continue
        if ch in "'\"":
            comilla = ch
            actual.append(ch)
        elif ch in "[{(":
            profundidad += 1
            actual.append(ch)
        elif ch in "]})":
            profundidad -= 1
            actual.append(ch)
        elif ch == sep and profundidad == 0:
            partes.append("".join(actual))
            actual = []
        else:
            actual.append(ch)
    partes.append("".join(actual))
    return [p.strip() for p in partes if p.strip()]


def _parsear_props(texto: str | None) -> dict[str, Any]:
    if not texto:
        return {}
    interior = texto.strip()[1:-1].strip()
    if not interior:
        return {}
    props: dict[str, Any] = {}
    for par in _dividir_nivel_cero(interior, ","):
        if ":" not in par:
            raise CypherNoSoportado(f"propiedad mal formada en el patrón: {par!r}")
        clave, valor = par.split(":", 1)
        props[clave.strip().strip("`")] = _parsear_valor(valor)
    return props


def _partir_clausulas(query: str) -> tuple[str, str | None, str, int | None, bool]:
    """(patrón, where, retorno, limite, distinct), respetando comillas."""
    plano = _sin_comillas(query)

    def _buscar(palabra: str, desde: int = 0) -> int:
        m = re.search(rf"\b{palabra}\b", plano[desde:], re.IGNORECASE)
        return desde + m.start() if m else -1

    i_match = _buscar("MATCH")
    i_where = _buscar("WHERE")
    i_return = _buscar("RETURN")
    i_limit = _buscar("LIMIT", i_return)

    fin_patron = i_where if i_where != -1 else i_return
    patron = query[i_match + len("MATCH") : fin_patron].strip()
    where = query[i_where + len("WHERE") : i_return].strip() if i_where != -1 else None

    fin_retorno = i_limit if i_limit != -1 else len(query)
    retorno = query[i_return + len("RETURN") : fin_retorno].strip()
    limite = None
    if i_limit != -1:
        resto = query[i_limit + len("LIMIT") :].strip().rstrip(";").strip()
        try:
            limite = int(resto)
        except ValueError as exc:
            raise CypherNoSoportado(f"LIMIT debe ser un entero, no {resto!r}") from exc

    distinct = False
    if re.match(r"(?i)^DISTINCT\b", retorno):
        distinct = True
        retorno = retorno[len("DISTINCT") :].strip()

    return patron, where, retorno.rstrip(";").strip(), limite, distinct


def parsear(query: str) -> ConsultaLeida:
    """Texto Cypher -> `ConsultaLeida`. Valida solo-lectura primero."""
    query = (query or "").strip()
    if not query:
        raise CypherNoSoportado("consulta vacía")
    validar_solo_lectura(query)

    patron_txt, where_txt, retorno_txt, limite, distinct = _partir_clausulas(query)

    if _RE_VAR_LEN.search(_sin_comillas(patron_txt)):
        raise CypherNoSoportado(
            "los caminos de longitud variable ([:REL*1..3]) no están soportados: "
            "escribe los saltos explícitamente"
        )
    if "," in _dividir_nivel_cero(patron_txt, ",")[0:1] and len(_dividir_nivel_cero(patron_txt, ",")) > 1:
        raise CypherNoSoportado("solo se soporta un patrón lineal por MATCH (sin comas)")

    nodos, relaciones = _parsear_patron(patron_txt)
    condiciones = _parsear_where(where_txt) if where_txt else []
    retorno = _parsear_retorno(retorno_txt, nodos)

    vars_declaradas = {n.var for n in nodos if n.var} | {
        r.var for r in relaciones if r.var
    }
    for cond in condiciones:
        if cond.var not in vars_declaradas:
            raise CypherNoSoportado(f"la variable '{cond.var}' del WHERE no está en el MATCH")
    for item in retorno:
        if item.var not in vars_declaradas:
            raise CypherNoSoportado(f"la variable '{item.var}' del RETURN no está en el MATCH")

    return ConsultaLeida(nodos, relaciones, condiciones, retorno, distinct, limite)


def _parsear_patron(texto: str) -> tuple[list[PatronNodo], list[PatronRelacion]]:
    nodos: list[PatronNodo] = []
    relaciones: list[PatronRelacion] = []
    pos = 0
    texto = texto.strip()

    m = _RE_NODO.match(texto, pos)
    if not m:
        raise CypherNoSoportado(f"el patrón debe empezar con un nodo `(var:Label)`: {texto!r}")
    nodos.append(PatronNodo(m.group("var"), m.group("label"), _parsear_props(m.group("props"))))
    pos = m.end()

    while pos < len(texto):
        resto = texto[pos:].lstrip()
        pos += len(texto[pos:]) - len(resto)
        if not resto:
            break
        m_der = _RE_REL_DER.match(resto)
        m_izq = _RE_REL_IZQ.match(resto)
        if m_der:
            relaciones.append(PatronRelacion(m_der.group("tipo"), "out", m_der.group("var")))
            pos += m_der.end()
        elif m_izq:
            relaciones.append(PatronRelacion(m_izq.group("tipo"), "in", m_izq.group("var")))
            pos += m_izq.end()
        else:
            raise CypherNoSoportado(
                f"no se entiende la relación en el patrón cerca de: {resto[:40]!r} "
                "(formatos soportados: -[:REL]-> y <-[:REL]-)"
            )
        resto = texto[pos:].lstrip()
        pos += len(texto[pos:]) - len(resto)
        m_nodo = _RE_NODO.match(texto, pos)
        if not m_nodo:
            raise CypherNoSoportado(f"falta el nodo destino en el patrón cerca de: {texto[pos:][:40]!r}")
        nodos.append(
            PatronNodo(m_nodo.group("var"), m_nodo.group("label"), _parsear_props(m_nodo.group("props")))
        )
        pos = m_nodo.end()

    return nodos, relaciones


def _parsear_where(texto: str) -> list[Condicion]:
    plano = _sin_comillas(texto)
    if re.search(r"\bOR\b", plano, re.IGNORECASE):
        raise CypherNoSoportado("el WHERE solo soporta condiciones unidas por AND")
    condiciones: list[Condicion] = []
    trozos: list[str] = []
    ultimo = 0
    for m in re.finditer(r"\bAND\b", plano, re.IGNORECASE):
        trozos.append(texto[ultimo : m.start()])
        ultimo = m.end()
    trozos.append(texto[ultimo:])

    for trozo in trozos:
        trozo = trozo.strip()
        if not trozo:
            continue
        plano_trozo = _sin_comillas(trozo).upper()
        operador = None
        idx = -1
        for op in _OPERADORES:
            pos = plano_trozo.find(op)
            if pos != -1 and (idx == -1 or pos < idx):
                idx, operador = pos, op
        if operador is None:
            raise CypherNoSoportado(f"condición no soportada en el WHERE: {trozo!r}")
        izq = trozo[:idx].strip()
        der = trozo[idx + len(operador) :].strip()
        if "." not in izq:
            raise CypherNoSoportado(
                f"el lado izquierdo de una condición debe ser `var.propiedad`: {izq!r}"
            )
        var, prop = izq.split(".", 1)
        condiciones.append(
            Condicion(var.strip(), prop.strip().strip("`"), operador, _parsear_valor(der))
        )
    return condiciones


def _parsear_retorno(texto: str, nodos: list[PatronNodo]) -> list[ItemRetorno]:
    if texto.strip() == "*":
        return [ItemRetorno(n.var, None, n.var) for n in nodos if n.var]
    items: list[ItemRetorno] = []
    for trozo in _dividir_nivel_cero(texto, ","):
        alias = None
        m_alias = re.search(r"\bAS\b", _sin_comillas(trozo), re.IGNORECASE)
        if m_alias:
            alias = trozo[m_alias.end() :].strip()
            trozo = trozo[: m_alias.start()].strip()
        trozo = trozo.strip()
        if "(" in trozo:
            raise CypherNoSoportado(
                f"las funciones y agregaciones no están soportadas en el RETURN: {trozo!r}"
            )
        if "." in trozo:
            var, prop = trozo.split(".", 1)
            var, prop = var.strip(), prop.strip().strip("`")
            items.append(ItemRetorno(var, prop, alias or f"{var}.{prop}"))
        else:
            items.append(ItemRetorno(trozo, None, alias or trozo))
    return items


# --------------------------------------------------------------------------
# Ejecución sobre GraphBackend
# --------------------------------------------------------------------------
def _coincide_props(nodo: dict[str, Any], props: dict[str, Any]) -> bool:
    for clave, esperado in props.items():
        if str(nodo.get(clave)) != str(esperado):
            return False
    return True


def _coincide_label(nodo: dict[str, Any], label: str | None) -> bool:
    return label is None or nodo.get("_label") == label


def _evaluar_condicion(nodo: dict[str, Any], cond: Condicion) -> bool:
    valor = nodo.get(cond.prop)
    esperado = cond.valor
    if cond.operador in ("=",):
        return str(valor) == str(esperado)
    if cond.operador in ("<>", "!="):
        return str(valor) != str(esperado)
    if cond.operador == "IN":
        return any(str(valor) == str(v) for v in (esperado or []))
    if cond.operador == "CONTAINS":
        return str(esperado).lower() in str(valor).lower()
    if cond.operador == "STARTS WITH":
        return str(valor).lower().startswith(str(esperado).lower())
    if cond.operador == "ENDS WITH":
        return str(valor).lower().endswith(str(esperado).lower())
    # comparaciones numéricas
    try:
        izq, der = float(valor), float(esperado)
    except (TypeError, ValueError):
        return False
    return {
        ">": izq > der,
        ">=": izq >= der,
        "<": izq < der,
        "<=": izq <= der,
    }[cond.operador]


def ejecutar(consulta: ConsultaLeida, backend, db: str) -> list[dict[str, dict[str, Any]]]:
    """Ejecuta el patrón y devuelve una lista de *bindings*: cada fila es
    `{nombre_de_variable: nodo}`.

    El servidor MCP aplica el RBAC sobre estos bindings (antes de
    proyectar el RETURN), porque la política se define sobre nodos —
    `podar_nodo` — y no sobre columnas.
    """
    primero = consulta.nodos[0]
    candidatos = [
        n
        for n in backend.list_nodes(db, primero.label)
        if _coincide_label(n, primero.label) and _coincide_props(n, primero.props)
    ]

    filas: list[dict[str, dict[str, Any]]] = []
    for nodo in candidatos:
        filas.append({(primero.var or "_n0"): nodo})

    for i, relacion in enumerate(consulta.relaciones):
        patron_destino = consulta.nodos[i + 1]
        var_origen = consulta.nodos[i].var or f"_n{i}"
        var_destino = patron_destino.var or f"_n{i + 1}"
        nuevas: list[dict[str, dict[str, Any]]] = []
        for fila in filas:
            origen = fila[var_origen]
            if relacion.var:
                # El patrón nombró la relación: hay que traer también sus
                # propiedades para poder devolver `r.fuente` y compañía.
                vecinos = backend.neighbors_with_edge(
                    db, origen["id"], relacion.tipo, direction=relacion.direccion
                )
            else:
                vecinos = [
                    (v, {})
                    for v in backend.neighbors(
                        db, origen["id"], relacion.tipo, direction=relacion.direccion
                    )
                ]
            for vecino, props_arista in vecinos:
                if not _coincide_label(vecino, patron_destino.label):
                    continue
                if not _coincide_props(vecino, patron_destino.props):
                    continue
                nueva = dict(fila)
                nueva[var_destino] = vecino
                if relacion.var:
                    # La arista se guarda como un "nodo" más del binding,
                    # marcado con `_arista`, para que el RETURN y el WHERE
                    # la traten igual que a un nodo.
                    nueva[relacion.var] = {**props_arista, "_arista": relacion.tipo}
                nuevas.append(nueva)
        filas = nuevas

    for cond in consulta.condiciones:
        filas = [f for f in filas if cond.var in f and _evaluar_condicion(f[cond.var], cond)]

    return filas


def proyectar(consulta: ConsultaLeida, filas: list[dict[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    """Aplica RETURN / DISTINCT / LIMIT sobre los bindings ya filtrados
    por política."""
    proyectadas: list[dict[str, Any]] = []
    for fila in filas:
        registro: dict[str, Any] = {}
        for item in consulta.retorno:
            nodo = fila.get(item.var)
            if nodo is None:
                registro[item.alias] = None
            elif item.prop is None:
                if "_arista" in nodo:
                    registro[item.alias] = {
                        k: v for k, v in nodo.items() if k != "_arista"
                    } | {"_relacion": nodo["_arista"]}
                else:
                    registro[item.alias] = {
                        k: v for k, v in nodo.items() if k != "_label"
                    } | {"_tipo": nodo.get("_label")}
            else:
                registro[item.alias] = nodo.get(item.prop)
        proyectadas.append(registro)

    if consulta.distinct:
        vistas, unicas = set(), []
        for registro in proyectadas:
            clave = repr(sorted(registro.items(), key=lambda kv: kv[0]))
            if clave not in vistas:
                vistas.add(clave)
                unicas.append(registro)
        proyectadas = unicas

    if consulta.limite is not None:
        proyectadas = proyectadas[: consulta.limite]
    return proyectadas
