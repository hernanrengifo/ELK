"""
tests/test_agents.py — reglas duras de los agentes (F3 + F4).

`scripts/demo_smoke.py` comprueba que la demo **funciona**; esto
comprueba que **no se puede romper**: las reglas que el plan pone como
innegociables y que en una demo en vivo no se pueden verificar a ojo.

  1. `max_iterations = 6` y una directiva de refinamiento repetida corta
     el bucle (§8, riesgo "nodo crítico entra en bucle").
  2. Ninguna cifra llega a la respuesta sin estar en `evidence[]` con
     linaje, y el **nodo crítico la rechaza** si el redactor lo intenta.
  3. El Agente B no expone su Cypher ni su esquema en el contrato A2A
     (§4.6).
  4. `tasks/send` exige `run_id` y rechaza métodos que no son suyos.

Corre sin API key ni Docker:

    LLM_MODEL=fake pytest tests/test_agents.py -v
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import anyio
import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent
for _ruta in (str(ROOT_DIR), str(ROOT_DIR / "mcp"), str(ROOT_DIR / "data")):
    if _ruta not in sys.path:
        sys.path.insert(0, _ruta)

os.environ.setdefault("GRAPH_BACKEND", "memory")
os.environ.setdefault("EVENT_BUS", "memory")
os.environ.setdefault("LLM_MODEL", "fake")

from agents.agent_a.verificacion import (  # noqa: E402
    UMBRAL_CIFRA,
    cifras_del_texto,
    cifras_sin_linaje,
    evidencias_faltantes,
)
from agents.llm_fake import LLMFake  # noqa: E402
from agents.modelos import Evidencia  # noqa: E402

PREGUNTA = (
    "¿Qué clientes corporativos con exposición crediticia mayor a 5.000 millones se verían "
    "afectados si migramos la base de datos del servicio de pagos, y quién es el responsable?"
)
#: Puerto sin nadie escuchando: la delegación A2A falla rápido y de forma
#: predecible, que es justo lo que estos tests quieren provocar.
SIN_AGENTE_B = "http://127.0.0.1:59999"


# --------------------------------------------------------------------------
# 2. Ninguna cifra sin linaje (comprobación determinista)
# --------------------------------------------------------------------------
def test_reconoce_las_formas_de_cifra_del_espanol():
    encontradas = dict(cifras_del_texto("5.300 millones, 6,3 mil millones y 1.234.567.890"))
    assert encontradas["5.300 millones"] == pytest.approx(5_300_000_000)
    assert encontradas["6,3 mil millones"] == pytest.approx(6_300_000_000)
    assert encontradas["1.234.567.890"] == pytest.approx(1_234_567_890)


def test_no_confunde_ids_fechas_ni_conteos_con_cifras():
    texto = "3 clientes de CLI01 y CLI05 en PAY-DB-01, cambio CHG-2026-0917, corte 2026-09-15, v2"
    assert cifras_del_texto(texto) == []


def test_una_cifra_que_no_esta_en_la_evidencia_se_detecta():
    evidencia = [
        Evidencia(id="E1", tipo="cifra", descripcion="exposición CLI02", valor=6_300_000_000,
                  unidad="COP", linaje="posiciones.csv", origen="mcp_actions").model_dump()
    ]
    assert cifras_sin_linaje("La exposición es de 6.300 millones COP", evidencia) == []
    assert cifras_sin_linaje("La exposición es de 9.100 millones COP", evidencia) == ["9.100 millones"]


def test_el_umbral_de_la_pregunta_no_cuenta_como_cifra_inventada():
    """Repetir el umbral del enunciado no es afirmar un dato.

    Con el modelo real, una respuesta correcta decía "exposición
    superior a 5.000 millones" —el umbral que venía en la pregunta— y el
    nodo crítico la rechazaba por cifra sin linaje.
    """
    evidencia = [
        Evidencia(id="E1", tipo="cifra", descripcion="x", valor=6_300_000_000,
                  unidad="COP", linaje="posiciones.csv", origen="mcp_actions").model_dump()
    ]
    pregunta = "¿Qué clientes tienen exposición crediticia mayor a 5.000 millones?"
    respuesta = "Un cliente supera los 5.000 millones: 6.300 millones COP."
    assert cifras_sin_linaje(respuesta, evidencia, pregunta) == []
    # Sin la pregunta, el umbral sí se marcaría: es lo que pasaba antes.
    assert cifras_sin_linaje(respuesta, evidencia) == ["5.000 millones"]
    # Y una cifra que no está ni en la pregunta ni en la evidencia sigue cayendo.
    assert cifras_sin_linaje("son 9.100 millones", evidencia, pregunta) == ["9.100 millones"]


def test_una_evidencia_sin_linaje_no_respalda_nada():
    """El modelo `Evidencia` no deja construir un dato sin fuente."""
    with pytest.raises(ValueError):
        Evidencia(id="E1", tipo="cifra", descripcion="x", valor=1.0, linaje="  ", origen="mcp_actions")


def test_un_hallazgo_que_cita_evidencia_inexistente_se_detecta():
    evidencia = [
        Evidencia(id="E1", tipo="cifra", descripcion="x", valor=5e9, linaje="csv",
                  origen="mcp_actions").model_dump()
    ]
    assert evidencias_faltantes([{"texto": "a", "evidencia_ids": ["E1"]}], evidencia) == []
    assert evidencias_faltantes([{"texto": "a", "evidencia_ids": ["E9"]}], evidencia) == ["E9"]


def test_el_umbral_solo_exige_linaje_a_cifras_de_negocio():
    assert cifras_del_texto(f"{int(UMBRAL_CIFRA) - 1}") == []
    assert cifras_del_texto(f"{int(UMBRAL_CIFRA)}")


# --------------------------------------------------------------------------
# LLMs de prueba: el simulado con un paso cambiado
# --------------------------------------------------------------------------
class LLMCriticoTerco(LLMFake):
    """Un crítico que nunca se da por satisfecho y siempre pide lo mismo.

    Sirve para comprobar que el bucle se corta por la regla de la
    directiva repetida y no por agotar `max_iterations`.
    """

    async def estructurado(self, modelo, *, sistema, humano, paso, variante="", contexto=None):
        if paso == "nodo_critico" and variante == "suficiencia":
            return modelo.model_validate(
                {
                    "razonamiento": "Sigo viendo el mismo hueco.",
                    "decision": "Pedir otra vez lo mismo.",
                    "is_complete": False,
                    "huecos": ["falta algo que no sé pedir de otra forma"],
                    "directiva_refinamiento": "vuelve a intentarlo igual",
                    "nodo_destino": "navegar_grafo",
                }
            ), await _uso_falso()
        return await super().estructurado(
            modelo, sistema=sistema, humano=humano, paso=paso, variante=variante, contexto=contexto
        )


class LLMQueInventaCifras(LLMFake):
    """Un redactor que se saca una cifra de la manga."""

    async def estructurado(self, modelo, *, sistema, humano, paso, variante="", contexto=None):
        if paso == "responder":
            return modelo.model_validate(
                {
                    "razonamiento": "Redacto la respuesta.",
                    "decision": "Dar una cifra redonda.",
                    "respuesta": "La exposición conjunta asciende a 99.999 millones de COP.",
                    "hallazgos": [],
                    "advertencias": [],
                }
            ), await _uso_falso()
        return await super().estructurado(
            modelo, sistema=sistema, humano=humano, paso=paso, variante=variante, contexto=contexto
        )


async def _uso_falso():
    from agents.llm import UsoLLM

    return UsoLLM(modelo="fake", tokens_in=0, tokens_out=0)


def _correr(llm, run_id: str, rol: str = "riesgo") -> dict[str, Any]:
    """Corre el grafo del Agente A con un LLM concreto, sin Agente B."""
    from agents.agent_a.grafo import MAX_ITERATIONS, construir_grafo
    from agents.eventos import ContextoCorrida
    from agents.mcp_cliente import ClienteMCP

    async def _run():
        ctx = ContextoCorrida(run_id=run_id, user_ctx={"usuario": "test", "rol": rol},
                              source="agent_a", modelo="fake")
        async with ClienteMCP(ctx, grafo_base="negocio", con_acciones=True) as mcp:
            grafo = construir_grafo(llm, ctx, mcp, agent_b_url=SIN_AGENTE_B)
            return await grafo.ainvoke(
                {
                    "query": PREGUNTA, "user_ctx": ctx.user_ctx, "run_id": run_id,
                    "plan": [], "visited_nodes": [], "evidence": [], "pending_gaps": [],
                    "is_complete": False, "iteration": 0, "max_iterations": MAX_ITERATIONS,
                    "audit_trail": [],
                },
                config={"recursion_limit": 80},
            )

    return anyio.run(_run)


# --------------------------------------------------------------------------
# 1. El bucle de refinamiento siempre termina
# --------------------------------------------------------------------------
def test_max_iterations_es_seis():
    from agents.agent_a.grafo import MAX_ITERATIONS

    assert MAX_ITERATIONS == 6


def test_una_directiva_repetida_corta_el_bucle():
    estado = _correr(LLMCriticoTerco(PREGUNTA), "test-terco")
    assert estado.get("motivo_corte"), "el bucle debía cortarse con un motivo explícito"
    assert "idéntica a la anterior" in estado["motivo_corte"]
    # Se corta por la regla de la directiva repetida, antes de agotar las 6.
    assert estado["iteration"] < estado["max_iterations"]
    assert estado.get("respuesta"), "aun cortando, tiene que entregar la respuesta que tenga"


def test_el_bucle_nunca_supera_max_iterations():
    estado = _correr(LLMCriticoTerco(PREGUNTA), "test-tope")
    assert estado["iteration"] <= estado["max_iterations"]


# --------------------------------------------------------------------------
# 2. El nodo crítico rechaza una respuesta con cifras sin linaje
# --------------------------------------------------------------------------
def test_el_critico_rechaza_una_cifra_inventada():
    from observability.trace_store import TraceStore

    run_id = "test-cifra-inventada"
    estado = _correr(LLMQueInventaCifras(PREGUNTA), run_id)
    respuesta = estado.get("respuesta") or {}

    # O la reescribió sin la cifra, o la entregó marcada como no validada.
    if respuesta.get("validada") is False:
        assert respuesta.get("cifras_sin_linaje"), "debía decir qué cifras no pudo respaldar"
    assert "99.999 millones" not in (respuesta.get("respuesta") or "") or (
        respuesta.get("validada") is False
    ), "una cifra inventada no puede salir como respuesta válida"

    eventos = TraceStore().read(run_id)
    veredictos = [e for e in eventos if e.kind == "critic_verdict"
                  and e.payload.get("fase") == "verificacion"]
    assert veredictos, "el crítico tenía que verificar la respuesta redactada"
    assert any(v.payload.get("respuesta_valida") is False for v in veredictos)
    assert any(
        e.kind == "refinement" and e.payload.get("fase") == "verificacion" for e in eventos
    ), "el rechazo tenía que emitir un refinement"


def test_cada_paso_emite_un_thought_con_razonamiento_y_decision():
    from observability.trace_store import TraceStore

    run_id = "test-thoughts"
    _correr(LLMFake(PREGUNTA), run_id)
    thoughts = [e for e in TraceStore().read(run_id) if e.kind == "thought"]

    pasos = {t.step for t in thoughts}
    assert {"planner", "resolver_contexto", "navegar_grafo", "ejecutar_accion",
            "delegar_a2a", "nodo_critico", "responder"} <= pasos, f"faltan pasos: {pasos}"
    for t in thoughts:
        assert t.payload.get("razonamiento"), f"thought sin razonamiento en {t.step}"
        assert t.payload.get("decision"), f"thought sin decision en {t.step}"
        # ≤ 2 frases (§8): se cuenta el final de frase, no las comas.
        assert t.payload["razonamiento"].count(".") <= 3


def test_la_delegacion_caida_se_convierte_en_hueco_no_en_crash():
    """Sin Agente B al otro lado, la corrida termina igual y lo dice."""
    estado = _correr(LLMFake(PREGUNTA), "test-sin-b")
    assert estado.get("respuesta"), "tenía que responder aunque la delegación fallara"
    assert estado.get("pending_gaps"), "y dejar constancia del hueco"


# --------------------------------------------------------------------------
# Parámetros que se le mandan al modelo real
# --------------------------------------------------------------------------
def _cuerpo_de_la_peticion(entorno: dict[str, str]) -> dict[str, Any]:
    """Qué JSON sale de verdad hacia la API, sin necesitar una API key.

    Se levanta un servidor que captura el cuerpo y responde con un error:
    solo interesa lo que se envía. Comprobar el atributo del cliente no
    bastaría — lo que rechaza la API es el payload.
    """
    import http.server
    import json as _json
    import socketserver
    import threading

    capturado: dict[str, Any] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            n = int(self.headers.get("content-length", 0))
            capturado["body"] = _json.loads(self.rfile.read(n) or b"{}")
            self.send_response(500)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"type":"error","error":{"type":"api_error","message":"captura"}}')

        def log_message(self, *a):  # silencio
            pass

    with socketserver.TCPServer(("127.0.0.1", 0), Handler) as servidor:
        puerto = servidor.server_address[1]
        hilo = threading.Thread(target=servidor.serve_forever, daemon=True)
        hilo.start()
        previo = {k: os.environ.get(k) for k in
                  ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "EKL_LLM_TEMPERATURE")}
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-prueba"
        os.environ["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{puerto}"
        os.environ.pop("EKL_LLM_TEMPERATURE", None)
        os.environ.update(entorno)
        try:
            from agents.llm import LLMAnthropic
            from agents.modelos import SalidaPlanner

            async def _pedir():
                llm = LLMAnthropic("claude-sonnet-5")
                try:
                    await llm.estructurado(SalidaPlanner, sistema="s", humano="h", paso="planner")
                except Exception:
                    pass  # la respuesta es un error a propósito

            anyio.run(_pedir)
        finally:
            servidor.shutdown()
            for k, v in previo.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    return capturado.get("body", {})


def test_no_se_manda_temperature_por_defecto():
    """`claude-sonnet-5` responde 400 si recibe `temperature`.

    Mandarlo tumbaba la corrida entera en el primer paso del planner:
    `invalid_request_error: temperature is deprecated for this model`.
    """
    cuerpo = _cuerpo_de_la_peticion({})
    assert cuerpo, "no se capturó ninguna petición"
    assert "temperature" not in cuerpo, (
        "los modelos nuevos rechazan `temperature`: no puede viajar por defecto"
    )
    assert cuerpo["model"] == "claude-sonnet-5"
    assert cuerpo["max_tokens"] > 0


def test_temperature_viaja_solo_si_se_pide():
    """Para un modelo antiguo donde sí sirva, se puede fijar a propósito."""
    cuerpo = _cuerpo_de_la_peticion({"EKL_LLM_TEMPERATURE": "0"})
    assert cuerpo.get("temperature") == 0


def test_un_valor_invalido_no_tumba_el_arranque():
    """En mitad de una demo es mejor el valor por defecto que no arrancar."""
    cuerpo = _cuerpo_de_la_peticion({"EKL_LLM_TEMPERATURE": "cero"})
    assert "temperature" not in cuerpo


# --------------------------------------------------------------------------
# 3. El Agente B no expone su Cypher ni su esquema
# --------------------------------------------------------------------------
def _contrato_b(skill: str, objetivo: str, rol: str = "riesgo") -> dict[str, Any]:
    from agents.agent_b.grafo import construir_grafo, contrato_de_salida
    from agents.eventos import ContextoCorrida
    from agents.mcp_cliente import ClienteMCP

    async def _run():
        ctx = ContextoCorrida(run_id=f"test-b-{skill}", user_ctx={"rol": rol}, source="agent_b",
                              modelo="fake")
        async with ClienteMCP(ctx, grafo_base="infra", con_acciones=False) as mcp:
            grafo = construir_grafo(LLMFake(), ctx, mcp)
            estado = await grafo.ainvoke(
                {"pregunta": f"{skill} sobre {objetivo}", "skill_pedida": skill,
                 "objetivo_pedido": objetivo}
            )
        return contrato_de_salida(estado)

    return anyio.run(_run)


def test_el_contrato_de_b_trae_lo_acordado():
    contrato = _contrato_b("impacto_cambio", "PAY-DB-01")
    ids = {s["id"] for s in contrato["servicios_afectados"]}
    assert ids == {"pagos-core", "nomina-batch"}
    assert contrato["linaje"], "el contrato tiene que venir con linaje"

    responsable = _contrato_b("responsable_servicio", "pagos-core")["responsable"]
    assert responsable["nombre"] == "Laura Gómez"


@pytest.mark.parametrize(
    ("skill", "objetivo"),
    [("impacto_cambio", "PAY-DB-01"), ("responsable_servicio", "pagos-core")],
)
def test_el_contrato_de_b_no_filtra_cypher_ni_esquema(skill, objetivo):
    crudo = json.dumps(_contrato_b(skill, objetivo), ensure_ascii=False)
    for filtracion in ("MATCH ", "RETURN ", "DEPENDE_DE", "ES_RESPONSABLE", "read_cypher"):
        assert filtracion not in crudo, f"el contrato filtró '{filtracion}': {crudo}"


def test_el_contrato_de_b_solo_tiene_las_claves_acordadas():
    contrato = _contrato_b("impacto_cambio", "PAY-DB-01")
    permitidas = {"skill", "objetivo", "servicios_afectados", "responsable", "linaje",
                  "detalle", "nota"}
    assert set(contrato) <= permitidas, f"claves de más: {set(contrato) - permitidas}"


# --------------------------------------------------------------------------
# 4. El servidor A2A: agent card y `tasks/send`
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def cliente_b():
    from fastapi.testclient import TestClient

    from agents.agent_b.server import app

    with TestClient(app) as cliente:
        yield cliente


def test_el_agent_card_publica_las_dos_skills(cliente_b):
    card = cliente_b.get("/.well-known/agent.json").json()
    assert {s["id"] for s in card["skills"]} == {"impacto_cambio", "responsable_servicio"}
    assert card["url"] and card["version"]
    # El card describe qué hace, no cómo: no puede nombrar su modelo de datos.
    crudo = json.dumps(card, ensure_ascii=False)
    assert "Cypher" not in crudo and "DEPENDE_DE" not in crudo


def test_tasks_send_exige_run_id(cliente_b):
    respuesta = cliente_b.post("/a2a", json={
        "jsonrpc": "2.0", "id": "1", "method": "tasks/send",
        "params": {"message": {"role": "user", "parts": []}, "metadata": {}},
    }).json()
    assert respuesta["error"]["code"] == -32602
    assert "run_id" in respuesta["error"]["message"]


def test_tasks_send_rechaza_otros_metodos(cliente_b):
    respuesta = cliente_b.post("/a2a", json={
        "jsonrpc": "2.0", "id": "1", "method": "message/send", "params": {},
    }).json()
    assert respuesta["error"]["code"] == -32601
    assert respuesta["error"]["data"]["soportados"] == ["tasks/send"]


def test_tasks_send_devuelve_el_contrato_y_propaga_el_run_id(cliente_b):
    from observability.trace_store import TraceStore

    run_id = "test-a2a-propagacion"
    respuesta = cliente_b.post("/a2a", json={
        "jsonrpc": "2.0", "id": "1", "method": "tasks/send",
        "params": {
            "id": "task-x",
            "message": {"role": "user", "parts": [
                {"type": "text", "text": "¿Qué depende de PAY-DB-01?"},
                {"type": "data", "data": {"skill": "impacto_cambio", "parametro": "PAY-DB-01"}},
            ]},
            "metadata": {"run_id": run_id, "user_ctx": {"usuario": "ana", "rol": "riesgo"}},
        },
    }).json()

    datos = respuesta["result"]["artifacts"][0]["parts"][0]["data"]
    assert {s["id"] for s in datos["servicios_afectados"]} == {"pagos-core", "nomina-batch"}

    eventos = TraceStore().read(run_id)
    assert {e.source for e in eventos} >= {"agent_b", "mcp_graph"}, (
        "los eventos de B y de su servidor MCP tienen que colgar del run_id recibido"
    )
    assert any(e.kind == "a2a_response" for e in eventos)
    assert all(e.meta.user_role == "riesgo" for e in eventos if e.source == "agent_b")


def test_la_bitacora_evita_repetir_una_consulta_ya_hecha():
    """Una consulta que ya está en la bitácora no se vuelve a ejecutar.

    Medido con el modelo real, 41 de las 64 llamadas de la pregunta
    principal eran repeticiones exactas: el prompt enseñaba la evidencia
    pero no qué se había consultado.
    """
    from agents.agent_a.grafo import _registrar, _ya_ejecutada, _bitacora_para_prompt

    bitacora: list[str] = []
    _registrar(bitacora, "read_cypher(MATCH (c:Cliente) RETURN c)", "3 fila(s)")
    assert _ya_ejecutada(bitacora, "read_cypher(MATCH (c:Cliente) RETURN c)")
    assert not _ya_ejecutada(bitacora, "read_cypher(MATCH (s:Servicio) RETURN s)")

    # registrar dos veces lo mismo no duplica la línea
    _registrar(bitacora, "read_cypher(MATCH (c:Cliente) RETURN c)", "3 fila(s)")
    assert len(bitacora) == 1

    texto = _bitacora_para_prompt(bitacora)
    assert "3 fila(s)" in texto
    assert _bitacora_para_prompt([]).strip() == "(ninguna todavía)"


def test_la_bitacora_del_critico_incluye_los_valores():
    """El crítico tiene que ver las cifras, no solo que se consultaron."""
    from agents.agent_a.grafo import _evidencia_para_prompt

    texto = _evidencia_para_prompt([
        {"id": "E1", "tipo": "cifra", "descripcion": "exposicion_crediticia de CLI01",
         "valor": 5_300_000_000.0, "unidad": "COP"},
    ])
    assert "5,300,000,000" in texto and "COP" in texto


def test_el_texto_no_nombra_los_nodos_podados():
    """El párrafo dice cuántos se filtraron, no cuáles.

    Un control de acceso que nombra el id de lo que te escondió ya filtró
    lo que protegía; el id vive en `politicas_aplicadas` y en la traza,
    que es lo que mira quien audita (docs/demo_decisions.md §9).
    """
    run_id = "test-podados-sin-id"
    estado = _correr(LLMFake(PREGUNTA), run_id, rol="analista_junior")
    respuesta = estado.get("respuesta") or {}

    prosa = " ".join(
        [respuesta.get("respuesta") or ""]
        + list(respuesta.get("advertencias") or [])
        + [h.get("texto", "") for h in respuesta.get("hallazgos") or []]
    )
    podados = (respuesta.get("politicas_aplicadas") or {}).get("nodos_podados") or []
    assert podados, "el escenario junior tenía que podar algo"
    for nodo in podados:
        assert nodo not in prosa, f"el texto nombra el nodo podado {nodo}"


def test_impacto_cambio_resuelve_quien_aprueba():
    """Un `CHG-...` tiene que decir quién lo aprueba.

    No está en la ficha del cambio: es la persona responsable del
    servicio que el cambio afecta. Sin esto la respuesta admitía no
    saberlo, con el dato a un salto de distancia en el grafo.
    """
    contrato = _contrato_b("impacto_cambio", "CHG-2026-0917")
    responsable = contrato.get("responsable") or {}
    assert responsable.get("nombre"), f"sin responsable: {contrato}"
    assert any(
        "responsable del servicio" in l or "quien aprueba" in l
        for l in contrato.get("linaje") or []
    ), contrato.get("linaje")
