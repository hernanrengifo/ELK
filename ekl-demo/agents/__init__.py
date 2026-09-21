"""
agents/ — los dos agentes de la demo EKL (plan_demo.md §4.5 y §4.6).

    agent_a/   Orquestador de Negocio (LangGraph, 7 nodos, puerto 8001)
    agent_b/   Agente de Infraestructura (A2A server, puerto 8002)

    modelos.py      AgentState y las salidas estructuradas de cada nodo
    llm.py          fábrica del LLM por `LLM_MODEL` (real o simulado)
    llm_fake.py     LLM simulado (`LLM_MODEL=fake`), sin API key
    eventos.py      emisión de eventos al bus de F2b (esquema existente)
    mcp_cliente.py  cliente MCP instrumentado (propaga run_id y user_ctx)
    a2a.py          subconjunto propio del protocolo A2A (agent card +
                    `tasks/send`), ver STATUS.md para el porqué

Ningún módulo de aquí define un esquema de evento nuevo: todos usan
`observability/events.py` tal como quedó en la sesión 2.
"""
