"""
cache_plan.py — caché del plan del planner (plan_demo.md §8).

Mitigación de latencia que el plan propone si la corrida se pasa de los
90 segundos: "cachear el plan del planner para la pregunta principal".
El planner es determinista para una misma pregunta y un mismo esquema,
así que su salida se puede reutilizar.

**Viene apagada.** Medio punto de la demo es enseñar al agente pensando:
si el plan sale de una caché, la Sala de control muestra un `thought`
instantáneo y se pierde justo lo que se quiere contar en el minuto 1-3.
Se enciende solo si hace falta:

    EKL_CACHE_PLAN=1 python agents/agent_a/server.py

La clave incluye la pregunta, el rol y una huella del esquema del grafo:
si cambia la ontología, el plan cacheado deja de valer solo.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
ARCHIVO = Path(os.environ.get("EKL_CACHE_PLAN_FILE") or (ROOT_DIR / ".cache" / "planes.json"))


def activada() -> bool:
    return (os.environ.get("EKL_CACHE_PLAN") or "").strip().lower() in ("1", "true", "yes", "si", "sí")


def clave(query: str, rol: str, esquema: dict[str, Any]) -> str:
    """Huella de la pregunta + el rol + el esquema del dominio."""
    huella_esquema = json.dumps(
        {
            "tipos": sorted(t.get("tipo", "") for t in esquema.get("tipos") or []),
            "aristas": sorted(a.get("nombre", "") for a in esquema.get("aristas") or []),
            "acciones": sorted(esquema.get("acciones") or []),
        },
        ensure_ascii=False,
    )
    crudo = json.dumps([" ".join((query or "").split()), rol, huella_esquema], ensure_ascii=False)
    return hashlib.sha256(crudo.encode("utf-8")).hexdigest()[:20]


def _leer() -> dict[str, Any]:
    if not ARCHIVO.exists():
        return {}
    try:
        return json.loads(ARCHIVO.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}  # una caché corrupta no puede tumbar una corrida


def obtener(k: str) -> dict[str, Any] | None:
    if not activada():
        return None
    return _leer().get(k)


def guardar(k: str, salida: dict[str, Any]) -> None:
    if not activada():
        return
    datos = _leer()
    datos[k] = salida
    try:
        ARCHIVO.parent.mkdir(parents=True, exist_ok=True)
        ARCHIVO.write_text(json.dumps(datos, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass  # sin caché se sigue funcionando, solo más lento


def limpiar() -> None:
    if ARCHIVO.exists():
        ARCHIVO.unlink()
