"""
verificacion.py — la regla dura del Agente A: **ninguna cifra sin
linaje**.

El plan lo pone como mitigación de riesgo (§8): "el prompt del planner
prohíbe inventar cifras; toda cifra debe venir de `evidence[]` con
linaje; el nodo crítico rechaza respuestas con cifras sin fuente".

Aquí está la comprobación, y es **determinista**: no se le pregunta al
modelo si se portó bien, se revisa el texto. El nodo crítico usa el
resultado para aprobar o rechazar la respuesta redactada.

Cómo se decide qué es "una cifra":

  - Se buscan números en el texto, incluidas las formas del español
    ("5.300 millones", "6.3 mil millones", "1.234.567,89").
  - Solo se exigen con linaje los de **magnitud monetaria**
    (≥ `UMBRAL_CIFRA`, mil millones por defecto en una demo cuyos
    importes son miles de millones de COP). Los números pequeños —"3
    clientes", "2 servicios", una versión de métrica, un año— no son
    cifras de negocio y exigirles linaje daría falsos positivos
    constantes.
  - Los identificadores con letras (`PAY-DB-01`, `CLI01`,
    `CHG-2026-0917`) y las fechas ISO no son cifras.
  - Una cifra está respaldada si coincide con el valor de alguna
    evidencia con linaje, con tolerancia relativa (para que "5.300
    millones" valide un valor de 5_300_000_000).
"""

from __future__ import annotations

import re
from typing import Any, Iterable

#: Por debajo de esto no se considera cifra de negocio (ver docstring).
UMBRAL_CIFRA = 1_000_000_000.0

#: Tolerancia relativa al comparar una cifra del texto con la evidencia.
#: Cubre el redondeo de "5,3 mil millones" frente a 5_300_000_000.
TOLERANCIA = 0.02

_MULTIPLICADORES = {
    "billon": 1e12, "billones": 1e12, "billón": 1e12,
    "mil millones": 1e9, "millardo": 1e9, "millardos": 1e9,
    "millon": 1e6, "millones": 1e6, "millón": 1e6,
    "mil": 1e3,
}

#: Número + (opcional) escala en palabras. Se exige que el número no
#: venga pegado a letras, para no capturar `CLI01` ni `PAY-DB-01`.
_RE_CIFRA = re.compile(
    r"(?<![\w\-/])(\d{1,3}(?:[.,]\d{3})*(?:[.,]\d+)?|\d+(?:[.,]\d+)?)"
    r"\s*(mil\s+millones|millardos?|millones|millón|millon|billones|billón|billon|mil)?"
    r"(?![\w\-/])",
    re.IGNORECASE,
)

_RE_FECHA_ISO = re.compile(r"\d{4}-\d{2}-\d{2}")


def _a_numero(literal: str) -> float | None:
    """'5.300' -> 5300.0, '1.234.567,89' -> 1234567.89, '6.3' -> 6.3.

    El español usa el punto como separador de miles y la coma como
    decimal, pero los modelos mezclan las dos convenciones; se resuelve
    mirando la forma del literal, no asumiendo una.
    """
    texto = literal.strip()
    if not texto:
        return None
    tiene_punto, tiene_coma = "." in texto, "," in texto
    if tiene_punto and tiene_coma:
        # El último separador que aparece es el decimal.
        decimal = "," if texto.rfind(",") > texto.rfind(".") else "."
        miles = "." if decimal == "," else ","
        texto = texto.replace(miles, "").replace(decimal, ".")
    elif tiene_punto or tiene_coma:
        sep = "." if tiene_punto else ","
        partes = texto.split(sep)
        # Grupos de tres cifras en todas las partes salvo la primera ->
        # es separador de miles (5.300, 1.234.567).
        if len(partes) > 1 and all(len(p) == 3 for p in partes[1:]):
            texto = texto.replace(sep, "")
        else:
            texto = texto.replace(sep, ".")
    try:
        return float(texto)
    except ValueError:
        return None


def cifras_del_texto(texto: str) -> list[tuple[str, float]]:
    """Cifras de magnitud monetaria encontradas en el texto."""
    if not texto:
        return []
    limpio = _RE_FECHA_ISO.sub(" ", texto)
    encontradas: list[tuple[str, float]] = []
    for coincidencia in _RE_CIFRA.finditer(limpio):
        literal, escala = coincidencia.group(1), (coincidencia.group(2) or "").strip().lower()
        valor = _a_numero(literal)
        if valor is None:
            continue
        if escala:
            valor *= _MULTIPLICADORES.get(escala, 1.0)
        if abs(valor) >= UMBRAL_CIFRA:
            encontradas.append((coincidencia.group(0).strip(), valor))
    return encontradas


def valores_respaldados(evidencias: Iterable[dict[str, Any]]) -> list[float]:
    """Valores numéricos de las evidencias que tienen linaje."""
    valores: list[float] = []
    for evidencia in evidencias or []:
        if not str(evidencia.get("linaje") or "").strip():
            continue
        valor = evidencia.get("valor")
        if isinstance(valor, (int, float)):
            valores.append(float(valor))
    return valores


def cifras_sin_linaje(
    texto: str,
    evidencias: Iterable[dict[str, Any]],
    pregunta: str = "",
) -> list[str]:
    """Las cifras del texto que **no** están respaldadas por evidencia.

    Es lo que el nodo crítico usa para rechazar una respuesta.

    `pregunta` es la del usuario, y sus cifras no cuentan: si preguntas
    "clientes con exposición mayor a 5.000 millones" y la respuesta dice
    "superior a 5.000 millones", el agente está repitiendo tu umbral, no
    afirmando un dato. Exigirle linaje a eso rechazaba respuestas
    correctas.
    """
    respaldadas = valores_respaldados(evidencias)
    del_enunciado = {valor for _literal, valor in cifras_del_texto(pregunta)}
    huerfanas: list[str] = []
    for literal, valor in cifras_del_texto(texto):
        if any(_coinciden(valor, v) for v in respaldadas):
            continue
        if any(_coinciden(valor, v) for v in del_enunciado):
            continue
        if literal not in huerfanas:
            huerfanas.append(literal)
    return huerfanas


def _coinciden(a: float, b: float) -> bool:
    if b == 0:
        return abs(a) < 1e-9
    return abs(a - b) / abs(b) <= TOLERANCIA


def evidencias_faltantes(hallazgos: Iterable[dict[str, Any]], evidencias: Iterable[dict[str, Any]]) -> list[str]:
    """Ids de evidencia citados por un hallazgo que no existen (o no
    tienen linaje). Complementa la revisión del texto: cada afirmación
    de la respuesta debe apuntar a evidencia real."""
    validas = {
        e.get("id")
        for e in (evidencias or [])
        if str(e.get("linaje") or "").strip()
    }
    faltan: list[str] = []
    for hallazgo in hallazgos or []:
        for eid in hallazgo.get("evidencia_ids") or []:
            if eid not in validas and eid not in faltan:
                faltan.append(eid)
    return faltan
