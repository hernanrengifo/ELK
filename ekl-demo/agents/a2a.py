"""
a2a.py — subconjunto propio del protocolo A2A (plan_demo.md §4.6, §7).

**Por qué propio y no `a2a-sdk`.** El plan deja la decisión para F3
("implementación ligera propia del subconjunto necesario (agent card +
`tasks/send`) o el SDK `a2a-sdk` si está estable al momento de construir
— decidir en F3 y dejar constancia"). Se eligió la propia por tres
razones, en orden de peso:

1. **`a2a-sdk` ya no tiene `tasks/send`.** En la versión publicada al
   construir esto (1.1.2) los métodos JSON-RPC son `message/send`,
   `message/stream`, `tasks/get`, `tasks/cancel`, `tasks/resubscribe`…
   El método `tasks/send` de las primeras versiones de la especificación
   —el que pide el plan— ya no existe. Usar el SDK obligaría a cambiar
   el contrato que el plan fija.
2. **La demo enseña el sobre, no la librería.** El minuto 3-5 del guion
   es abrir el payload A2A y decir "miren qué le mandó: el id del
   servicio y la identidad del usuario; ni esquema, ni credenciales".
   Con ~120 líneas propias ese sobre es exactamente lo que se ve en
   pantalla; con el SDK habría envoltorios de por medio.
3. **Peso.** El SDK arrastra 12 dependencias más (protobuf,
   google-auth, grpc…) para una demo que corre en un portátil.

Qué se implementa del protocolo:

  - **Agent card** en `/.well-known/agent.json`, con la forma de A2A
    (`name`, `description`, `url`, `version`, `capabilities`,
    `defaultInputModes`/`defaultOutputModes`, `skills[]`).
  - **`tasks/send`** por JSON-RPC 2.0: `params = {id, sessionId, message:
    {role, parts[]}, metadata: {run_id, user_ctx}}` y respuesta
    `{id, status:{state}, artifacts:[{parts:[{type:"data", data}]}]}`.

Qué NO se implementa (y por qué no hace falta aquí): streaming
(`message/stream`, SSE de tarea), push notifications, `tasks/get` /
`tasks/cancel` (las tareas de la demo son síncronas y terminan en un
turno), y autenticación (los dos agentes corren en el mismo portátil).

Si más adelante se quiere el SDK, lo único que cambia es este archivo:
el resto de los agentes habla con `ClienteA2A` / `construir_tarea`.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field

A2A_VERSION = "0.2-subset"


# --------------------------------------------------------------------------
# Agent card
# --------------------------------------------------------------------------
class SkillCard(BaseModel):
    id: str
    name: str
    description: str
    tags: list[str] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)
    inputModes: list[str] = Field(default_factory=lambda: ["text", "data"])
    outputModes: list[str] = Field(default_factory=lambda: ["data"])


class Capabilities(BaseModel):
    streaming: bool = False
    pushNotifications: bool = False
    stateTransitionHistory: bool = False


class AgentCard(BaseModel):
    name: str
    description: str
    url: str
    version: str
    protocolVersion: str = A2A_VERSION
    capabilities: Capabilities = Field(default_factory=Capabilities)
    defaultInputModes: list[str] = Field(default_factory=lambda: ["text", "data"])
    defaultOutputModes: list[str] = Field(default_factory=lambda: ["data"])
    skills: list[SkillCard] = Field(default_factory=list)


# --------------------------------------------------------------------------
# tasks/send — JSON-RPC 2.0
# --------------------------------------------------------------------------
class Parte(BaseModel):
    type: Literal["text", "data"]
    text: str | None = None
    data: dict[str, Any] | None = None


class Mensaje(BaseModel):
    role: Literal["user", "agent"] = "user"
    parts: list[Parte] = Field(default_factory=list)


class ParamsTarea(BaseModel):
    """`params` de `tasks/send`.

    `metadata` es donde viaja el contexto mínimo del §4.6: el `run_id` de
    la ejecución y la identidad del usuario original, para que el agente
    receptor aplique su propio RBAC con ese rol. **No viaja nada más**:
    ni esquema del grafo, ni Cypher, ni credenciales."""

    id: str = Field(default_factory=lambda: f"task-{uuid.uuid4().hex[:10]}")
    sessionId: str | None = None
    message: Mensaje
    metadata: dict[str, Any] = Field(default_factory=dict)


class EstadoTarea(BaseModel):
    state: Literal["completed", "failed", "input-required"] = "completed"
    message: str | None = None


class Artefacto(BaseModel):
    name: str | None = None
    parts: list[Parte] = Field(default_factory=list)


class ResultadoTarea(BaseModel):
    id: str
    sessionId: str | None = None
    status: EstadoTarea = Field(default_factory=EstadoTarea)
    artifacts: list[Artefacto] = Field(default_factory=list)

    def datos(self) -> dict[str, Any]:
        """El contrato devuelto por el otro agente (la parte `data` del
        primer artefacto)."""
        for artefacto in self.artifacts:
            for parte in artefacto.parts:
                if parte.type == "data" and parte.data is not None:
                    return parte.data
        return {}


class PeticionJSONRPC(BaseModel):
    jsonrpc: Literal["2.0"] = "2.0"
    id: str | int = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    method: str
    params: dict[str, Any] = Field(default_factory=dict)


class ErrorJSONRPC(BaseModel):
    code: int
    message: str
    data: dict[str, Any] | None = None


class RespuestaJSONRPC(BaseModel):
    jsonrpc: Literal["2.0"] = "2.0"
    id: str | int | None = None
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


# Códigos JSON-RPC usados (los estándar; no se inventan códigos propios)
METODO_NO_ENCONTRADO = -32601
PARAMS_INVALIDOS = -32602
ERROR_INTERNO = -32603


def construir_tarea(
    *,
    skill: str,
    parametro: str,
    pregunta: str,
    run_id: str,
    user_ctx: dict[str, Any],
    session_id: str | None = None,
) -> ParamsTarea:
    """El sobre que el Agente A le manda al Agente B.

    Es deliberadamente escaso: la pregunta en palabras, la skill, el id
    del objeto, y en `metadata` el `run_id` y la identidad del usuario.
    Es lo que se abre en pantalla en el minuto 3-5 del guion.
    """
    return ParamsTarea(
        sessionId=session_id or run_id,
        message=Mensaje(
            role="user",
            parts=[
                Parte(type="text", text=pregunta),
                Parte(type="data", data={"skill": skill, "parametro": parametro}),
            ],
        ),
        metadata={"run_id": run_id, "user_ctx": dict(user_ctx)},
    )


#: Lo que explícitamente NO viaja en la petición. Se adjunta al evento
#: `a2a_request` para poder enseñarlo en la Sala de control (§4.8).
NO_SE_ENVIA = [
    "el esquema del grafo de negocio",
    "las consultas Cypher del agente A",
    "credenciales o tokens de acceso a datos",
    "la evidencia ya recogida por el agente A",
]


class ErrorA2A(RuntimeError):
    """El otro agente devolvió un error JSON-RPC o no se pudo contactar."""


class ClienteA2A:
    """Cliente del subconjunto: lee el agent card y manda `tasks/send`."""

    def __init__(self, base_url: str, *, timeout: float = 120.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def agent_card(self) -> AgentCard:
        async with httpx.AsyncClient(timeout=self.timeout) as cliente:
            respuesta = await cliente.get(f"{self.base_url}/.well-known/agent.json")
            respuesta.raise_for_status()
            return AgentCard.model_validate(respuesta.json())

    async def tasks_send(self, params: ParamsTarea) -> ResultadoTarea:
        peticion = PeticionJSONRPC(method="tasks/send", params=params.model_dump(exclude_none=True))
        async with httpx.AsyncClient(timeout=self.timeout) as cliente:
            respuesta = await cliente.post(f"{self.base_url}/a2a", json=peticion.model_dump())
            respuesta.raise_for_status()
            cuerpo = RespuestaJSONRPC.model_validate(respuesta.json())
        if cuerpo.error:
            raise ErrorA2A(f"{cuerpo.error.get('code')}: {cuerpo.error.get('message')}")
        if cuerpo.result is None:
            raise ErrorA2A("respuesta A2A sin result ni error")
        return ResultadoTarea.model_validate(cuerpo.result)
