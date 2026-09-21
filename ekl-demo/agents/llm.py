"""
llm.py — fábrica del modelo de lenguaje de los agentes.

El plan pide **LLM-agnosticism por configuración** (§"Decisiones
tomadas"): el modelo se lee de `LLM_MODEL` y cambiarlo no toca ni el
código ni los prompts.

    LLM_MODEL=claude-sonnet-5   -> Claude vía `langchain-anthropic`
    LLM_MODEL=fake              -> LLM simulado (agents/llm_fake.py),
                                   sin API key, determinista

Todos los pasos de los dos agentes piden **la misma forma de salida**:
un modelo Pydantic que hereda de `PasoRazonado`, es decir que siempre
trae `{razonamiento (≤ 2 frases), decision}` además de los campos
propios del paso. De ahí sale el evento `thought` de la Sala de control
sin tener que exponer el prompt (§4.8).

`estructurado()` devuelve `(instancia, uso)`, donde `uso` trae los
tokens de entrada y salida para el `meta` del evento — la zona
"Gobernanza en vivo" del §4.8 muestra tokens y costo estimado.
"""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, TypeVar

from pydantic import BaseModel

from .modelos import PasoRazonado

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=PasoRazonado)

#: Valor de `LLM_MODEL` que activa el LLM simulado.
MODELO_FALSO = "fake"

#: Cómo se le pide al modelo la salida estructurada. `json_schema` acota
#: la respuesta en la propia API y es menos propenso a que se cuele
#: sintaxis de la llamada dentro de un campo de texto.
METODO_ESTRUCTURADO = os.environ.get("EKL_LLM_STRUCTURED_METHOD", "json_schema")

#: Intentos por paso antes de darse por vencido. Una salida mal formada
#: es un tropiezo del modelo, no un error de la corrida.
INTENTOS_ESTRUCTURADO = 3
#: Segundos que esperamos una respuesta HTTP antes de cortar y reintentar.
#: Sin esto, una conexión que muere en silencio (portátil suspendido, wifi
#: que cambia de red) deja el socket abierto y la demo colgada para
#: siempre: lo vimos, cuatro horas parada sin un solo byte ni un error.
TIMEOUT_PETICION = 60.0
#: Reintentos de transporte del propio SDK, por encima de los nuestros.
REINTENTOS_TRANSPORTE = 2


def _decimal_env(nombre: str) -> float | None:
    """Lee un float de una variable de entorno, o `None` si no está.

    Un valor mal escrito se ignora con un aviso en vez de tumbar el
    arranque: en mitad de una demo es preferible correr con el valor por
    defecto que no arrancar.
    """
    crudo = (os.environ.get(nombre) or "").strip()
    if not crudo:
        return None
    try:
        return float(crudo)
    except ValueError:
        logger.warning("%s=%r no es un número; se ignora", nombre, crudo)
        return None


def _entero_env(nombre: str, por_defecto: int) -> int:
    crudo = (os.environ.get(nombre) or "").strip()
    if not crudo:
        return por_defecto
    try:
        return int(crudo)
    except ValueError:
        logger.warning("%s=%r no es un entero; se usa %s", nombre, crudo, por_defecto)
        return por_defecto


@dataclass
class UsoLLM:
    tokens_in: int | None = None
    tokens_out: int | None = None
    modelo: str | None = None


class LLM(ABC):
    """Interfaz mínima que usan los nodos de los dos agentes."""

    nombre: str

    @abstractmethod
    async def estructurado(
        self,
        modelo: type[T],
        *,
        sistema: str,
        humano: str,
        paso: str,
        variante: str = "",
        contexto: dict[str, Any] | None = None,
    ) -> tuple[T, UsoLLM]:
        """Pide al modelo una instancia de `modelo`.

        `paso` y `variante` identifican el punto del grafo desde el que
        se llama; el LLM real los ignora y el simulado los usa para
        escoger su respuesta.

        `contexto` es el mismo estado que el nodo ya puso en prosa dentro
        de `humano` (los clientes que quedaron en la ruta, los huecos que
        detectó el crítico…), pero en estructura. El LLM real lo ignora
        —lo lee del prompt— y el simulado lo usa para decidir sin tener
        que analizar texto.
        """


class LLMAnthropic(LLM):
    """Claude vía `langchain-anthropic`, con salida estructurada.

    **No se manda `temperature` salvo que se pida.** Los modelos nuevos
    lo rechazan —`claude-sonnet-5` devuelve
    `400 invalid_request_error: temperature is deprecated for this
    model`— y la corrida entera se cae en el primer paso. Enviarlo solo
    cuando alguien lo configura a propósito funciona con los dos: los
    modelos que lo aceptan usan su valor por defecto y los que no, no lo
    ven.

    Si necesitas fijarlo (por ejemplo para un modelo antiguo donde
    `temperature=0` daba corridas repetibles):

        EKL_LLM_TEMPERATURE=0 LLM_MODEL=<modelo-antiguo> ...

    Para determinismo de verdad, lo que hay es `LLM_MODEL=fake`.
    """

    def __init__(
        self,
        modelo: str,
        *,
        temperatura: float | None = None,
        max_tokens: int | None = None,
    ) -> None:
        from langchain_anthropic import ChatAnthropic

        self.nombre = modelo
        opciones: dict[str, Any] = {
            "model": modelo,
            "max_tokens": max_tokens if max_tokens is not None else _entero_env(
                "EKL_LLM_MAX_TOKENS", 2048
            ),
        }
        espera = _decimal_env("EKL_LLM_TIMEOUT")
        opciones["timeout"] = espera if espera is not None else TIMEOUT_PETICION
        opciones["max_retries"] = _entero_env("EKL_LLM_REINTENTOS", REINTENTOS_TRANSPORTE)
        temperatura = temperatura if temperatura is not None else _decimal_env("EKL_LLM_TEMPERATURE")
        if temperatura is not None:
            opciones["temperature"] = temperatura
        self._chat = ChatAnthropic(**opciones)

    async def estructurado(self, modelo, *, sistema, humano, paso, variante="", contexto=None):
        # `contexto` no se usa aquí: el modelo real recibe esa misma
        # información redactada dentro de `humano`.
        from langchain_core.messages import HumanMessage, SystemMessage

        # include_raw=True para quedarnos con el uso de tokens del mensaje
        # crudo además del objeto ya validado.
        cadena = self._chat.with_structured_output(
            modelo, include_raw=True, method=METODO_ESTRUCTURADO
        )
        mensajes = [SystemMessage(content=sistema), HumanMessage(content=humano)]

        uso = UsoLLM(modelo=self.nombre)
        ultimo_error: Any = None
        for intento in range(1, INTENTOS_ESTRUCTURADO + 1):
            salida = await cadena.ainvoke(mensajes)
            parseado = salida.get("parsed") if isinstance(salida, dict) else salida

            crudo = salida.get("raw") if isinstance(salida, dict) else None
            metadatos: dict[str, Any] = getattr(crudo, "usage_metadata", None) or {}
            uso.tokens_in = (uso.tokens_in or 0) + (metadatos.get("input_tokens") or 0)
            uso.tokens_out = (uso.tokens_out or 0) + (metadatos.get("output_tokens") or 0)

            if parseado is not None:
                return parseado, uso

            # Pasa: el modelo devuelve de vez en cuando una llamada mal
            # formada (se ha visto sintaxis `</invoke>` metida dentro de
            # un campo de texto, con el resto de campos perdidos). Un
            # reintento lo arregla, y en una demo en vivo es la
            # diferencia entre seguir y quedarse tirado.
            ultimo_error = salida.get("parsing_error") if isinstance(salida, dict) else None
            logger.warning(
                "salida inválida del modelo %s en el paso '%s' (intento %s de %s): %s",
                self.nombre, paso, intento, INTENTOS_ESTRUCTURADO, str(ultimo_error)[:200],
            )
            if intento < INTENTOS_ESTRUCTURADO:
                mensajes = [
                    SystemMessage(content=sistema),
                    HumanMessage(content=humano),
                    HumanMessage(
                        content=(
                            "La respuesta anterior no cumplió el esquema. Devuélvela otra vez "
                            "rellenando TODOS los campos obligatorios, cada uno en su argumento "
                            "y con su tipo (las listas como listas, no como texto)."
                        )
                    ),
                ]

        raise RuntimeError(
            f"el modelo {self.nombre} no devolvió una salida válida en el paso '{paso}' "
            f"tras {INTENTOS_ESTRUCTURADO} intentos: {ultimo_error}"
        )


#: Variables que permiten usar un modelo distinto en los pasos donde el
#: §8 dice que se puede (planner y crítico), sin tocar el resto. Es la
#: mitigación de latencia del plan: si la corrida se pasa de 90 s, se
#: bajan esos dos a un modelo rápido y los pasos que redactan la
#: respuesta se quedan con el bueno.
VARIABLE_POR_PASO = {
    "planner": "LLM_MODEL_PLANNER",
    "nodo_critico": "LLM_MODEL_CRITICO",
}


def modelo_para(paso: str | None = None) -> str:
    """Qué modelo usar en un paso. `LLM_MODEL` es el valor de base."""
    base = os.environ.get("LLM_MODEL") or MODELO_FALSO
    variable = VARIABLE_POR_PASO.get(paso or "")
    if variable:
        return (os.environ.get(variable) or base).strip()
    return base.strip()


def construir_llm(modelo: str | None = None, *, paso: str | None = None) -> LLM:
    """Fábrica. `modelo` por defecto viene de `LLM_MODEL`, o de la
    variable propia del paso si la hay (ver `VARIABLE_POR_PASO`)."""
    nombre = (modelo or modelo_para(paso)).strip()
    if nombre.lower() == MODELO_FALSO:
        from .llm_fake import LLMFake

        return LLMFake()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            f"LLM_MODEL={nombre} necesita ANTHROPIC_API_KEY. "
            f"Para probar el flujo sin API key: LLM_MODEL={MODELO_FALSO}"
        )
    return LLMAnthropic(nombre)


def es_falso(llm: LLM) -> bool:
    return llm.nombre == MODELO_FALSO
