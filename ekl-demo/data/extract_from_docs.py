#!/usr/bin/env python3
"""
extract_from_docs.py — estrato 2: extracción de relaciones desde
documentos (plan_demo.md §2 y §4.2).

Lee `data/runbook_pagos.md`, le pide al LLM las tripletas
`(servicio, DEPENDE_DE, base_de_datos)` que el texto afirma, las
**canonicaliza** contra los nodos que ya existen en el grafo con
coincidencia difusa, y escribe las aristas con la propiedad
`fuente: "runbook_pagos.md#L24"` — la línea concreta que lo dice.

Para qué sirve en la demo: es lo que convierte "el agente avisó de
`nomina-batch`" en "el agente avisó de `nomina-batch` **y puede citar la
línea 24 del runbook donde está escrito**". Un auditor puede abrir el
documento y verificarlo.

    LLM_MODEL=fake python data/extract_from_docs.py          # sin API key
    LLM_MODEL=claude-sonnet-5 python data/extract_from_docs.py
    python data/extract_from_docs.py --dry-run               # sin escribir

**Idempotente.** Se puede correr las veces que haga falta: una tripleta
que ya existe actualiza su `fuente` en vez de duplicar la arista, y dos
corridas seguidas dejan el archivo byte a byte igual. El script lo
comprueba él mismo y lo reporta.

**Dónde escribe.** En `data/rel_depende_de.csv`, que es la fuente de
verdad de la que `load_graph.py` construye el grafo en los dos backends.
Escribir solo en el grafo en memoria se perdería al terminar el proceso,
y escribir solo en Neo4j dejaría el modo `memory` sin linaje. Si hay un
backend de grafo disponible, además actualiza las aristas ahí mismo.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

DATA_DIR = Path(__file__).resolve().parent
ROOT_DIR = DATA_DIR.parent
for _ruta in (str(ROOT_DIR), str(DATA_DIR)):
    if _ruta not in sys.path:
        sys.path.insert(0, _ruta)

RUNBOOK = DATA_DIR / "runbook_pagos.md"
CSV_DEPENDE_DE = DATA_DIR / "rel_depende_de.csv"

#: Por debajo de esto, un nombre del documento no se acepta como
#: referencia a un nodo existente. Es deliberadamente alto: en una capa
#: de conocimiento gobernada, inventarse un nodo por un parecido lejano
#: es peor que no extraer la relación.
UMBRAL_SIMILITUD = 0.82


# --------------------------------------------------------------------------
# Contrato con el LLM
# --------------------------------------------------------------------------
class Tripleta(BaseModel):
    """Una dependencia afirmada por el documento."""

    servicio: str = Field(description="Nombre del servicio tal como aparece en el texto")
    destino: str = Field(description="Nombre de la base de datos o servicio del que depende")
    linea: int = Field(description="Número de línea del documento donde se afirma")
    cita: str = Field(default="", description="Fragmento literal que lo afirma, una frase")


class ExtraccionRelaciones(BaseModel):
    """Salida estructurada que se le pide al modelo."""

    razonamiento: str = Field(description="Qué buscaste en el documento, en una o dos frases")
    decision: str = Field(description="Qué extrajiste, en una frase")
    tripletas: list[Tripleta] = Field(default_factory=list)


SISTEMA = (
    "Eres un extractor de relaciones para una capa de conocimiento empresarial de un banco. "
    "Lees documentación operativa y extraes ÚNICAMENTE las dependencias que el texto afirma "
    "explícitamente.\n"
    "REGLAS:\n"
    "1. No infieras ni completes: si el documento no lo dice, no existe.\n"
    "2. Cada tripleta tiene que venir con el número de línea exacto donde se afirma y una cita "
    "literal corta.\n"
    "3. Interesan las relaciones de dependencia: un servicio que depende de una base de datos o "
    "de otro servicio.\n"
    "4. Usa los nombres tal como aparecen en el texto; la canonicalización se hace después."
)


def _prompt(texto_numerado: str) -> str:
    return (
        "Extrae las dependencias afirmadas en este runbook. El documento va numerado por línea; "
        "usa esos números.\n\n"
        f"{texto_numerado}\n\n"
        "Devuelve una tripleta por cada dependencia explícita."
    )


#: Lo que devuelve `LLM_MODEL=fake`. No es una respuesta inventada: son
#: las cuatro dependencias que el runbook afirma, con la línea donde las
#: dice. Sirve para que la extracción sea reproducible sin API key.
TRIPLETAS_CONOCIDAS = ExtraccionRelaciones(
    razonamiento=(
        "El runbook declara las dependencias en la sección 'Dependencias conocidas' y una más "
        "en el párrafo de notificaciones."
    ),
    decision="Extraer las cuatro dependencias explícitas con su línea.",
    tripletas=[
        Tripleta(servicio="pagos-core", destino="PAY-DB-01", linea=16,
                 cita="La base de datos principal de `pagos-core` es **PAY-DB-01**"),
        Tripleta(servicio="pagos-core", destino="PAY-DB-02", linea=19,
                 cita="`pagos-core` también escribe en **PAY-DB-02** como réplica de solo lectura"),
        Tripleta(servicio="nomina-batch", destino="PAY-DB-01", linea=24,
                 cita="**`nomina-batch` también depende de PAY-DB-01**"),
        Tripleta(servicio="notificaciones", destino="pagos-core", linea=33,
                 cita="`notificaciones` depende a su vez de `pagos-core`"),
    ],
)


# --------------------------------------------------------------------------
# Canonicalización
# --------------------------------------------------------------------------
def normalizar(texto: str) -> str:
    """Minúsculas, sin tildes, sin el ruido de Markdown y con los
    separadores unificados.

    El modelo devuelve lo que ve en el texto, y en el texto los nombres
    vienen entre backticks o en negrita. Los separadores (`-`, `_`,
    espacios) se colapsan a uno solo porque un modelo escribe igual de
    bien "PAY-DB-01" que "PAY DB 01", y las dos formas significan lo
    mismo.
    """
    sin_marcas = re.sub(r"[`*]", "", str(texto)).strip()
    sin_tildes = unicodedata.normalize("NFKD", sin_marcas)
    sin_tildes = "".join(c for c in sin_tildes if not unicodedata.combining(c))
    unificado = re.sub(r"[\s_\-]+", " ", sin_tildes)
    return unificado.lower().strip(" .,:;()")


def _digitos(texto: str) -> tuple[str, ...]:
    """Grupos de dígitos del nombre, sin ceros a la izquierda.

    `PAY-DB-01` y `PAY-DB-02` se parecen en un 0,95 y son bases de datos
    **distintas**: en un identificador, un dígito no es una errata, es la
    diferencia. Sin esta comprobación la coincidencia difusa mapeaba
    alegremente un `PAY-DB-3` inexistente a `PAY-DB-01`, que es
    justamente el error que una capa gobernada no se puede permitir.
    """
    return tuple(g.lstrip("0") or "0" for g in re.findall(r"\d+", texto))


@dataclass
class Candidato:
    id: str
    tipo: str
    nombres: list[str]


def catalogo_de_nodos() -> list[Candidato]:
    """Nodos contra los que se canonicaliza: los que ya existen.

    Se leen de los CSV y no del grafo para que la extracción funcione
    igual con cualquier backend y sin nada levantado.
    """
    def _filas(nombre: str) -> list[dict[str, str]]:
        with open(DATA_DIR / nombre, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    catalogo: list[Candidato] = []
    for fila in _filas("servicios.csv"):
        catalogo.append(Candidato(fila["id"], "Servicio", [fila["id"], fila.get("nombre", "")]))
    for fila in _filas("bases_datos.csv"):
        catalogo.append(Candidato(fila["id"], "BaseDatos", [fila["id"]]))
    return catalogo


def canonicalizar(texto: str, catalogo: list[Candidato]) -> tuple[Candidato | None, float]:
    """Mejor nodo existente para un nombre del documento, con su puntaje.

    Primero exacto sobre la forma normalizada; si no, coincidencia
    difusa por `SequenceMatcher` contra el id y el nombre. Devuelve
    `(None, puntaje)` si nada llega a `UMBRAL_SIMILITUD`: preferimos
    perder una relación a inventarnos un nodo.
    """
    aguja = normalizar(texto)
    if not aguja:
        return None, 0.0

    digitos_aguja = _digitos(aguja)
    mejor: Candidato | None = None
    mejor_puntaje = 0.0
    for candidato in catalogo:
        for nombre in candidato.nombres:
            if not nombre:
                continue
            objetivo = normalizar(nombre)
            if _digitos(objetivo) != digitos_aguja:
                continue  # distinto número = distinto objeto, por parecido que sea
            puntaje = 1.0 if objetivo == aguja else SequenceMatcher(None, aguja, objetivo).ratio()
            if puntaje > mejor_puntaje:
                mejor, mejor_puntaje = candidato, puntaje
    if mejor_puntaje < UMBRAL_SIMILITUD:
        return None, mejor_puntaje
    return mejor, mejor_puntaje


# --------------------------------------------------------------------------
# Escritura
# --------------------------------------------------------------------------
CAMPOS_CSV = ["servicio_id", "destino_id", "destino_tipo", "fuente"]


def leer_aristas() -> list[dict[str, str]]:
    with open(CSV_DEPENDE_DE, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def escribir_aristas(filas: list[dict[str, str]]) -> None:
    with open(CSV_DEPENDE_DE, "w", newline="", encoding="utf-8") as f:
        escritor = csv.DictWriter(f, fieldnames=CAMPOS_CSV)
        escritor.writeheader()
        for fila in filas:
            escritor.writerow({c: fila.get(c, "") for c in CAMPOS_CSV})


def fusionar(
    existentes: list[dict[str, str]], extraidas: list[dict[str, str]]
) -> tuple[list[dict[str, str]], list[str]]:
    """MERGE por `(servicio_id, destino_id)`.

    Si la arista ya está, se le actualiza la `fuente` con la cita del
    documento; si no, se añade. Nunca duplica, que es lo que hace el
    script idempotente.
    """
    por_clave = {(f["servicio_id"], f["destino_id"]): dict(f) for f in existentes}
    orden = [(f["servicio_id"], f["destino_id"]) for f in existentes]
    cambios: list[str] = []

    for arista in extraidas:
        clave = (arista["servicio_id"], arista["destino_id"])
        if clave in por_clave:
            antes = por_clave[clave].get("fuente", "")
            if antes != arista["fuente"]:
                por_clave[clave]["fuente"] = arista["fuente"]
                cambios.append(
                    f"linaje actualizado  {clave[0]} -> {clave[1]}  ({antes} → {arista['fuente']})"
                )
        else:
            por_clave[clave] = dict(arista)
            orden.append(clave)
            cambios.append(f"arista nueva        {clave[0]} -> {clave[1]}  ({arista['fuente']})")
    return [por_clave[c] for c in orden], cambios


def actualizar_grafo(extraidas: list[dict[str, str]]) -> str:
    """Escribe también en el backend de grafo, si hay uno disponible.

    Con `GRAPH_BACKEND=neo4j` esto es lo que deja el linaje en la base
    sin tener que recargar; con `memory` el grafo se reconstruye desde
    el CSV en cada arranque, así que basta con el CSV.
    """
    backend_nombre = os.environ.get("GRAPH_BACKEND", "memory").lower()
    if backend_nombre != "neo4j":
        return f"GRAPH_BACKEND={backend_nombre}: el grafo se reconstruye del CSV al arrancar"
    from graph_backend import get_backend
    from load_graph import DB_INFRA

    backend = get_backend()
    try:
        for arista in extraidas:
            backend.create_edge(
                DB_INFRA, "DEPENDE_DE", arista["servicio_id"], arista["destino_id"],
                {"destino_tipo": arista["destino_tipo"], "fuente": arista["fuente"]},
            )
    finally:
        backend.close()
    return f"{len(extraidas)} arista(s) actualizadas en Neo4j (base infra)"


# --------------------------------------------------------------------------
# Extracción
# --------------------------------------------------------------------------
def documento_numerado() -> str:
    lineas = RUNBOOK.read_text(encoding="utf-8").splitlines()
    return "\n".join(f"{i:>3}| {l}" for i, l in enumerate(lineas, start=1))


async def extraer(modelo: str | None = None) -> ExtraccionRelaciones:
    """Pide las tripletas al LLM. Con `fake`, las conocidas."""
    from agents.llm import MODELO_FALSO, construir_llm

    nombre = (modelo or os.environ.get("LLM_MODEL") or MODELO_FALSO).strip()
    if nombre.lower() == MODELO_FALSO:
        return TRIPLETAS_CONOCIDAS

    llm = construir_llm(nombre)
    salida, _uso = await llm.estructurado(
        ExtraccionRelaciones,
        sistema=SISTEMA,
        humano=_prompt(documento_numerado()),
        paso="extraer_relaciones",
    )
    return salida


def _cita_de_linea(numero: int) -> str:
    lineas = RUNBOOK.read_text(encoding="utf-8").splitlines()
    if 1 <= numero <= len(lineas):
        return lineas[numero - 1].strip()
    return ""


def procesar(extraccion: ExtraccionRelaciones, verboso: bool = True) -> tuple[list[dict[str, str]], list[str]]:
    """Canonicaliza las tripletas y las convierte en filas de arista."""
    catalogo = catalogo_de_nodos()
    aristas: list[dict[str, str]] = []
    descartadas: list[str] = []

    for tripleta in extraccion.tripletas:
        origen, p_origen = canonicalizar(tripleta.servicio, catalogo)
        destino, p_destino = canonicalizar(tripleta.destino, catalogo)

        if origen is None or destino is None:
            cual = tripleta.servicio if origen is None else tripleta.destino
            puntaje = p_origen if origen is None else p_destino
            descartadas.append(
                f"'{cual}' no coincide con ningún nodo existente (mejor puntaje {puntaje:.2f} "
                f"< {UMBRAL_SIMILITUD})"
            )
            continue
        if origen.tipo != "Servicio":
            descartadas.append(f"'{tripleta.servicio}' no es un Servicio ({origen.tipo})")
            continue

        aristas.append(
            {
                "servicio_id": origen.id,
                "destino_id": destino.id,
                "destino_tipo": destino.tipo,
                "fuente": f"runbook_pagos.md#L{tripleta.linea}",
            }
        )
        if verboso:
            exacto_o = "=" if p_origen == 1.0 else f"~{p_origen:.2f}"
            exacto_d = "=" if p_destino == 1.0 else f"~{p_destino:.2f}"
            print(
                f"  {tripleta.servicio!r} {exacto_o}> {origen.id}  DEPENDE_DE  "
                f"{tripleta.destino!r} {exacto_d}> {destino.id}   L{tripleta.linea}"
            )
    return aristas, descartadas


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Extrae dependencias del runbook y las escribe con su linaje (plan §4.2)"
    )
    parser.add_argument("--dry-run", action="store_true", help="no escribir nada, solo mostrar")
    parser.add_argument("--modelo", default=None, help="sobrescribe LLM_MODEL para esta corrida")
    args = parser.parse_args(argv)

    import asyncio

    modelo = args.modelo or os.environ.get("LLM_MODEL", "fake")
    print(f"Extrayendo de {RUNBOOK.relative_to(ROOT_DIR)} con LLM_MODEL={modelo}\n")

    extraccion = asyncio.run(extraer(args.modelo))
    print(f"  razonamiento: {extraccion.razonamiento}")
    print(f"  decision:     {extraccion.decision}\n")

    aristas, descartadas = procesar(extraccion)
    for aviso in descartadas:
        print(f"  ! descartada: {aviso}")

    if not aristas:
        print("\nNo se extrajo ninguna relación utilizable.")
        return 1

    existentes = leer_aristas()
    fusionadas, cambios = fusionar(existentes, aristas)

    print()
    if cambios:
        for cambio in cambios:
            print(f"  {cambio}")
    else:
        print("  sin cambios: el linaje del documento ya estaba escrito (idempotente)")

    if args.dry_run:
        print("\n--dry-run: no se escribió nada.")
        return 0

    escribir_aristas(fusionadas)
    print(f"\n  {CSV_DEPENDE_DE.relative_to(ROOT_DIR)}: {len(fusionadas)} aristas")
    print(f"  {actualizar_grafo(aristas)}")

    # Comprobación de idempotencia: volver a fusionar sobre lo ya escrito
    # no puede producir ningún cambio.
    _, cambios_segunda = fusionar(leer_aristas(), aristas)
    if cambios_segunda:
        print("\n  ! la segunda pasada produjo cambios: la extracción NO es idempotente")
        for cambio in cambios_segunda:
            print(f"    {cambio}")
        return 1
    print("  idempotencia verificada: una segunda pasada no cambia nada")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
