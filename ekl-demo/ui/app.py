#!/usr/bin/env python3
"""
app.py — interfaz de la demo EKL en Streamlit (plan_demo.md §4.7).

Layout en dos columnas: a la izquierda el rol, la pregunta y el botón
Ejecutar; a la derecha las seis pestañas del plan.

    streamlit run ui/app.py --server.port 8501

Necesita el Agente A en el 8001 y el Agente B en el 8002 (la UI no los
levanta: en la demo se arrancan aparte para que se vean como procesos
distintos, que es medio punto del §2). La pestaña Sala de control embebe
la página que sirve el trace_store en el 8020; también se puede abrir
suelta en http://localhost:8020/control para proyectarla en una segunda
pantalla.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx
import streamlit as st
import yaml

ROOT_DIR = Path(__file__).resolve().parent.parent
for _ruta in (str(ROOT_DIR), str(ROOT_DIR / "mcp"), str(ROOT_DIR / "data")):
    if _ruta not in sys.path:
        sys.path.insert(0, _ruta)

from ui.datos_ruta import aristas_de_negocio, construir_grafo  # noqa: E402
from ui.grafo_svg import alto_de as alto_ruta, dibujar as dibujar_ruta  # noqa: E402

AGENT_A_URL = os.environ.get("AGENT_A_URL", "http://127.0.0.1:8001")
AGENT_B_URL = os.environ.get("AGENT_B_URL", "http://127.0.0.1:8002")
TRACE_URL = os.environ.get("TRACE_STORE_URL", "http://localhost:8020")

AUDIT_FILE = Path(os.environ.get("EKL_AUDIT_FILE") or (ROOT_DIR / "audit.jsonl"))
SEMANTIC_FILE = ROOT_DIR / "data" / "semantic_layer.yaml"

ROLES = ["direccion", "riesgo", "analista_junior"]

#: La pregunta hilo conductor (§1) y las dos alternativas del §5.
PREGUNTAS = {
    "Hilo conductor — migración de la BD de pagos": (
        "¿Qué clientes corporativos con exposición crediticia mayor a 5.000 millones se verían "
        "afectados si migramos la base de datos del servicio de pagos el próximo fin de semana, "
        "y quién es el responsable técnico de ese servicio hoy?"
    ),
    "Alternativa 1 — caída de TES-DB": "¿Qué productos quedarían sin servicio si cae TES-DB?",
    "Alternativa 2 — aprobación de CHG-2026-0917": (
        "¿Quién debe aprobar el cambio CHG-2026-0917 y a qué clientes hay que notificar?"
    ),
}

#: Colores de la pestaña Ruta. El ámbar es el de las flechas A2A de la
#: Sala de control, a propósito: es el mismo concepto en las dos pantallas.
COLOR_NODO = {
    "negocio": "#4a9eff",
    "frontera": "#8b93a7",
    "a2a": "#f5a524",
}
ETIQUETA_ORIGEN = {
    "negocio": "grafo de negocio (dominio del Agente A)",
    "frontera": "nodo frontera (referencia a otro dominio)",
    "a2a": "vino por A2A (lo contó el Agente B)",
}


def config_grafo(alto: int = 520, ancho: int = 1000):
    """Config de `streamlit-agraph` saneada.

    `Config.__init__` hace `self.__dict__.update(**kwargs)`, así que
    todos los parámetros de layout terminan **además** como claves de
    primer nivel del objeto de opciones. vis.js no las conoce, rechaza
    el objeto entero ("Errors have been found in the supplied options
    object") y el grafo acaba apelotonado en una esquina. Se construye
    la Config y se le quitan las claves que sobran, dejando solo lo que
    vis.js entiende: `height`, `width`, `physics`, `layout`, `edges`.

    Layout jerárquico de izquierda a derecha porque lo que se dibuja
    **es una ruta** (cliente → producto → servicio → base de datos /
    persona): con el layout por fuerzas los nodos se amontonan y no se
    lee el camino.

    `physics` queda **encendido** aunque el layout sea jerárquico: el
    encuadre de la vista (`stabilization.fit`) solo ocurre al estabilizar,
    y con la física apagada vis.js coloca los nodos pero deja la cámara
    donde estaba, así que el grafo se queda fuera de pantalla. El
    componente de Streamlit no expone `network.fit()`, así que esta es la
    forma de conseguirlo.
    """
    from streamlit_agraph import Config

    cfg = Config(
        width=ancho, height=alto, directed=True, physics=True, hierarchical=True,
        direction="LR", sortMethod="directed",
        levelSeparation=220, nodeSpacing=120, treeSpacing=160,
    )
    validas = {"height", "width", "physics", "layout", "edges", "nodes", "interaction"}
    for clave in list(cfg.__dict__):
        if clave not in validas:
            del cfg.__dict__[clave]
    return cfg


# --------------------------------------------------------------------------
# Servicios
# --------------------------------------------------------------------------
def salud(url: str, ruta: str = "/health") -> dict[str, Any] | None:
    try:
        r = httpx.get(f"{url}{ruta}", timeout=1.5)
        return r.json() if r.status_code == 200 else None
    except httpx.HTTPError:
        return None


def preguntar(query: str, rol: str) -> dict[str, Any]:
    r = httpx.post(
        f"{AGENT_A_URL}/ask",
        json={"query": query, "user_role": rol},
        timeout=float(os.environ.get("UI_TIMEOUT", "300")),
    )
    r.raise_for_status()
    return r.json()


def eventos_de(run_id: str) -> list[dict[str, Any]]:
    """Eventos de una corrida, del trace_store si está, y si no del
    `trace.jsonl` local (para que la UI siga sirviendo aunque la Sala de
    control no esté levantada)."""
    try:
        r = httpx.get(f"{TRACE_URL}/runs/{run_id}", timeout=5)
        if r.status_code == 200:
            return r.json().get("eventos", [])
    except httpx.HTTPError:
        pass
    from observability.trace_store import TraceStore

    return [e.model_dump(mode="json") for e in TraceStore().read(run_id)]


@st.cache_data(show_spinner=False)
def aristas_negocio_cacheadas(visitados: tuple[str, ...], run_id: str, rol: str):
    return asyncio.run(
        aristas_de_negocio(set(visitados), run_id, {"usuario": f"ui_{rol}", "rol": rol})
    )


# --------------------------------------------------------------------------
# Pestañas
# --------------------------------------------------------------------------
def pestana_respuesta(res: dict[str, Any]) -> None:
    if res.get("error"):
        st.error(f"La corrida falló: {res['error']}")
        return

    st.markdown(f"#### {res.get('query', '')}")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Rol", res.get("user_role", "—"))
    c2.metric("Iteraciones", res.get("iteraciones", "—"))
    c3.metric("Nodos en la ruta", len(res.get("ruta_nodos") or []))
    c4.metric("Duración", f"{(res.get('duracion_ms') or 0)/1000:.1f} s")

    st.markdown("### Respuesta")
    st.markdown(res.get("respuesta") or "_(sin respuesta)_")

    for advertencia in res.get("advertencias") or []:
        st.warning(advertencia, icon="⚠️")

    politicas = res.get("politicas_aplicadas") or {}
    podados = politicas.get("nodos_podados") or []
    denegadas = politicas.get("acciones_denegadas") or []
    if podados or denegadas:
        with st.container(border=True):
            st.markdown("**Políticas aplicadas por el servidor** (no por el modelo)")
            if podados:
                st.markdown(
                    f"- Nodos podados para el rol `{politicas.get('rol')}`: "
                    + ", ".join(f"`{n}`" for n in podados)
                )
            for mensaje in denegadas:
                st.markdown(f"- Acción denegada: {mensaje}")

    if res.get("motivo_corte"):
        st.info(f"El bucle de refinamiento se cortó: {res['motivo_corte']}", icon="✂️")
    if res.get("huecos_abiertos"):
        st.info("Huecos que quedaron abiertos: " + "; ".join(res["huecos_abiertos"]), icon="🕳️")
    if res.get("cifras_sin_linaje"):
        st.error(
            "El nodo crítico no pudo respaldar estas cifras: "
            + ", ".join(res["cifras_sin_linaje"])
        )

    with st.expander("Plan del agente (sub-preguntas)"):
        for sub in res.get("plan") or []:
            st.markdown(f"- `{sub['id']}` **{sub['estado']}** · {sub['pregunta']}  _({sub['dominio']})_")


def pestana_ruta(res: dict[str, Any], eventos: list[dict[str, Any]]) -> None:
    ruta = res.get("ruta_nodos") or []
    if not ruta:
        st.info("La corrida no recorrió ningún nodo.")
        return

    # Los nombres legibles los publica el agente; derivarlos de la
    # descripción de la evidencia daba el nombre de la métrica, no el del
    # cliente.
    nombres = dict(res.get("nombres_nodos") or {})
    if not nombres:
        for evento in eventos:
            if evento.get("kind") == "answer":
                nombres.update((evento.get("payload") or {}).get("nombres_nodos") or {})
    with st.spinner("Reconstruyendo el grafo recorrido…"):
        aristas_neg = aristas_negocio_cacheadas(
            tuple(sorted(ruta)), res.get("run_id", ""), res.get("user_role", "riesgo")
        )
    nodos, aristas = construir_grafo(ruta, aristas_neg, eventos, nombres)

    leyenda = st.columns(3)
    for col, (origen, etiqueta) in zip(leyenda, ETIQUETA_ORIGEN.items()):
        col.markdown(
            f"<span style='color:{COLOR_NODO[origen]};font-size:20px'>●</span> {etiqueta}",
            unsafe_allow_html=True,
        )

    con_agraph = st.toggle(
        "Dibujar con streamlit-agraph",
        value=False,
        help="Alternativa del plan (§4.7). Dentro de una pestaña de Streamlit el componente "
             "monta oculto y vis.js encuadra sobre un canvas de tamaño cero, así que el grafo "
             "sale comprimido en una esquina; por eso el dibujo propio es el que va por defecto. "
             "Ver ui/grafo_svg.py y STATUS.md.",
    )

    if con_agraph:
        from streamlit_agraph import Edge, Node, agraph

        agraph(
            nodes=[
                Node(
                    id=n["id"],
                    label=n["etiqueta"] if len(n["etiqueta"]) < 26 else n["id"],
                    size=26 if n["origen"] == "a2a" else 22,
                    color=COLOR_NODO[n["origen"]],
                    title=f"{n['tipo']} · {n['id']} · {ETIQUETA_ORIGEN[n['origen']]}",
                    shape="box" if n["tipo"] in ("Servicio", "BaseDatos") else "dot",
                )
                for n in nodos
            ],
            edges=[
                Edge(source=o, target=d, label=t, color="#5d6478",
                     font={"size": 9, "color": "#8b93a7"})
                for o, d, t in aristas
            ],
            config=config_grafo(),
        )
    else:
        st.components.v1.html(
            '<div style="background:#0f1219;border-radius:10px;padding:8px">'
            + dibujar_ruta(nodos, aristas)
            + "</div>",
            height=alto_ruta(nodos, aristas) + 40,
            scrolling=False,
        )

    st.caption(
        f"{len(nodos)} nodos y {len(aristas)} aristas. Las aristas de negocio se leen del mismo "
        "servidor MCP que usó el agente, **con tu rol**: si la política podó un nodo, aquí "
        "tampoco está. Las de infraestructura salen del contrato que devolvió el Agente B — la "
        "UI no consulta ese grafo, igual que no lo hace el Agente A."
    )


def pestana_evidencia(res: dict[str, Any]) -> None:
    evidencia = res.get("evidencia") or []
    if not evidencia:
        st.info("La corrida no recogió evidencia.")
        return
    import pandas as pd

    df = pd.DataFrame(
        [
            {
                "id": e["id"],
                "tipo": e["tipo"],
                "nodo": e.get("nodo") or "—",
                "dato": e["descripcion"],
                "valor": f"{e['valor']:,.0f} {e.get('unidad') or ''}".replace(",", ".")
                if isinstance(e.get("valor"), (int, float)) else "—",
                "linaje": e["linaje"],
            }
            for e in evidencia
        ]
    )
    st.dataframe(
        df, width="stretch", hide_index=True,
        column_config={
            "id": st.column_config.TextColumn(width="small"),
            "tipo": st.column_config.TextColumn(width="small"),
            "nodo": st.column_config.TextColumn(width="small"),
            "dato": st.column_config.TextColumn(width="medium"),
            "valor": st.column_config.TextColumn(width="small"),
            # El linaje es lo que la pestaña existe para enseñar.
            "linaje": st.column_config.TextColumn(width="large"),
        },
    )
    st.caption(
        "Cada cifra de la respuesta tiene que estar en esta tabla con su linaje. "
        "El nodo crítico rechaza la respuesta si aparece una que no esté — es una "
        "comprobación determinista, no una opinión del modelo."
    )

    hallazgos = res.get("hallazgos") or []
    if hallazgos:
        st.markdown("##### Hallazgos y la evidencia que los respalda")
        for h in hallazgos:
            ids = ", ".join(f"`{i}`" for i in h.get("evidencia_ids") or []) or "_sin citar_"
            st.markdown(f"- {h['texto']} → {ids}")


def pestana_auditoria() -> None:
    st.caption(f"`{AUDIT_FILE}` — cada llamada MCP, con lo que devolvió y lo que filtró (§4.3).")
    col1, col2 = st.columns([1, 3])
    n = col1.number_input("Últimas líneas", 5, 500, 40, step=5)
    if col2.button("Refrescar", width="stretch"):
        st.rerun()

    if not AUDIT_FILE.exists():
        st.info("Todavía no hay `audit.jsonl`. Lanza una pregunta.")
        return

    lineas = [l for l in AUDIT_FILE.read_text(encoding="utf-8").splitlines() if l.strip()]
    registros = []
    for linea in lineas[-int(n):]:
        try:
            registros.append(json.loads(linea))
        except json.JSONDecodeError:
            continue
    if not registros:
        st.info("La auditoría está vacía.")
        return

    import pandas as pd

    def _estado(registro: dict[str, Any]) -> str:
        """Una denegación por política no es un fallo.

        En `audit.jsonl` las dos cosas llegan como `estado: error` (el
        servidor MCP rechaza la llamada), pero en una demo de gobernanza
        confundirlas es justo lo contrario de lo que se quiere enseñar:
        una es el sistema funcionando y la otra es el sistema roto.
        """
        if registro.get("estado") != "error":
            return registro.get("estado", "")
        denegada = any(
            d.get("resultado") == "denegado"
            for d in registro.get("politicas_aplicadas") or []
        )
        return "denegado por política" if denegada else "error"

    df = pd.DataFrame(
        [
            {
                "hora": r["timestamp"][11:19],
                "user": r.get("user"),
                "rol": r.get("rol"),
                "servidor": r.get("servidor"),
                "tool": r.get("tool"),
                "estado": _estado(r),
                "devueltos": r.get("nodos_devueltos"),
                "filtrados": ", ".join(r.get("filtrados") or []) or "—",
                "ms": r.get("duracion_ms"),
                "run_id": r.get("run_id"),
            }
            for r in reversed(registros)
        ]
    )
    st.dataframe(df, width="stretch", hide_index=True, height=440)

    estados = [_estado(r) for r in registros]
    filtrados = sum(len(r.get("filtrados") or []) for r in registros)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Llamadas", len(registros))
    c2.metric("Nodos filtrados", filtrados)
    c3.metric("Denegadas por política", estados.count("denegado por política"))
    c4.metric("Errores", estados.count("error"))


def pestana_capa_semantica() -> None:
    st.caption(
        "El momento **wow #1** del guion: cambia aquí la definición de una métrica, guarda, "
        "y repite la pregunta. Los servidores MCP releen este archivo por fecha de "
        "modificación, así que la nueva definición entra sin reiniciar nada."
    )
    texto_actual = SEMANTIC_FILE.read_text(encoding="utf-8")
    mtime = SEMANTIC_FILE.stat().st_mtime
    clave = "editor_semantica"
    clave_mtime = "editor_semantica_mtime"
    clave_disco = "editor_semantica_disco"

    # El editor se refresca si el archivo cambió en disco **y** no hay
    # cambios sin guardar. Sin esto, tras un `demo_reset.sh` (o después
    # de revertir la métrica a mano) el editor seguiría mostrando lo
    # viejo y Guardar lo volvería a escribir, deshaciendo el cambio sin
    # avisar — justo en mitad de la demo.
    # "Descartar cambios" pide el reinicio con una bandera y **no**
    # escribe la clave del widget desde su propio handler: Streamlit
    # prohíbe modificar `session_state[clave]` una vez que el widget con
    # esa clave ya se instanció en la misma pasada, y lanza
    # `StreamlitWidgetAlreadyInstantiatedError` en la cara del
    # presentador. La bandera se consume aquí arriba, antes de crearlo.
    if st.session_state.pop("descartar_semantica", False):
        st.session_state.pop(clave, None)

    if clave not in st.session_state:
        st.session_state[clave] = texto_actual
        st.session_state[clave_disco] = texto_actual
        st.session_state[clave_mtime] = mtime
    elif mtime != st.session_state.get(clave_mtime):
        sin_editar = st.session_state[clave] == st.session_state.get(clave_disco)
        if sin_editar:
            st.session_state[clave] = texto_actual
            st.session_state[clave_disco] = texto_actual
            st.session_state[clave_mtime] = mtime
        else:
            st.warning(
                "El archivo cambió en disco y tienes cambios sin guardar. "
                "**Descartar cambios** recarga lo que hay en disco.",
                icon="⚠️",
            )

    editado = st.text_area("data/semantic_layer.yaml", key=clave, height=420)

    col1, col2, col3 = st.columns([1, 1, 2])
    if col1.button("Guardar", type="primary", width="stretch"):
        try:
            datos = yaml.safe_load(editado)
        except yaml.YAMLError as exc:
            st.error(f"YAML inválido, no se guardó: {exc}")
        else:
            if not isinstance(datos, dict) or "metricas" not in datos:
                st.error("El archivo tiene que tener una clave `metricas`. No se guardó.")
            else:
                SEMANTIC_FILE.write_text(editado, encoding="utf-8")
                # Claves auxiliares, no la del widget: estas sí se pueden
                # tocar aquí.
                st.session_state[clave_disco] = editado
                st.session_state[clave_mtime] = SEMANTIC_FILE.stat().st_mtime
                st.session_state["semantica_guardada"] = True
                st.success(
                    "Guardado. La próxima llamada a `resolve_metric` o "
                    "`retrieve_customer_position` ya usa esta definición."
                )
    if col2.button("Descartar cambios", width="stretch"):
        st.session_state["descartar_semantica"] = True
        st.rerun()

    # Las versiones se leen **del disco**, no del editor: es lo que los
    # servidores MCP van a usar en la próxima llamada. Si el editor tiene
    # algo distinto sin guardar, se avisa.
    try:
        vigentes = (yaml.safe_load(texto_actual) or {}).get("metricas") or {}
        col3.markdown("**Vigente en disco**: " + " · ".join(
            f"`{n}` v{m.get('version')}" for n, m in vigentes.items()))
        if editado != texto_actual:
            col3.caption("El editor tiene cambios sin guardar.")
    except yaml.YAMLError:
        col3.warning("El archivo en disco no es YAML válido")

    if st.session_state.get("semantica_guardada"):
        st.info(
            "Ahora vuelve a pulsar **Ejecutar** con la misma pregunta: la lista de clientes "
            "cambia sin haber tocado ni el prompt ni el código.",
            icon="↩️",
        )


def pestana_sala_de_control(run_id: str | None) -> None:
    disponible = salud(TRACE_URL) is not None
    if not disponible:
        st.warning(
            f"El trace_store no responde en {TRACE_URL}. Arráncalo con "
            "`python observability/trace_store.py` para ver la Sala de control.",
            icon="🔌",
        )
        return
    st.caption(
        f"Embebida desde {TRACE_URL}/control. Para proyectarla en una segunda pantalla, "
        f"ábrela suelta: [{TRACE_URL}/control]({TRACE_URL}/control)"
    )
    st.components.v1.iframe(f"{TRACE_URL}/control", height=760, scrolling=True)


# --------------------------------------------------------------------------
# App
# --------------------------------------------------------------------------
def main() -> None:
    st.set_page_config(page_title="EKL — Demo", page_icon="🕸️", layout="wide")
    st.markdown(
        "<style>.block-container{padding-top:2.2rem}</style>", unsafe_allow_html=True
    )

    izquierda, derecha = st.columns([1, 2.6], gap="large")

    with izquierda:
        st.markdown("## EKL")
        st.caption("Enterprise Knowledge Layer — demo")

        rol = st.radio(
            "Rol del usuario", ROLES,
            index=ROLES.index(os.environ.get("DEMO_USER_ROLE", "riesgo")),
            help="La política la aplica el servidor MCP, no el modelo. Cambiar de rol cambia "
                 "lo que el agente puede ver y hacer.",
        )

        etiqueta = st.selectbox("Pregunta", list(PREGUNTAS))
        query = st.text_area("Texto de la pregunta", value=PREGUNTAS[etiqueta], height=150,
                             key=f"q_{etiqueta}", label_visibility="collapsed")

        ejecutar = st.button("Ejecutar", type="primary", width="stretch")

        st.divider()
        st.markdown("###### Servicios")
        for nombre, url, ruta in (
            ("Agente A", AGENT_A_URL, "/health"),
            ("Agente B", AGENT_B_URL, "/health"),
            ("trace_store", TRACE_URL, "/health"),
        ):
            info = salud(url, ruta)
            st.markdown(
                f"{'🟢' if info else '🔴'} **{nombre}** · `{url}`"
                + (f" · `{info.get('modelo','')}`" if info and info.get("modelo") else "")
            )
        if salud(AGENT_A_URL) is None:
            st.info(
                "Arranca los agentes:\n```\npython agents/agent_b/server.py &\n"
                "python agents/agent_a/server.py &\n```",
                icon="▶️",
            )

        if st.session_state.get("resultado", {}).get("run_id"):
            st.divider()
            st.markdown("###### Última corrida")
            st.code(st.session_state["resultado"]["run_id"], language=None)

    if ejecutar:
        if salud(AGENT_A_URL) is None:
            st.session_state["resultado"] = {"error": f"el Agente A no responde en {AGENT_A_URL}"}
            st.session_state["eventos"] = []
        else:
            with derecha:
                with st.spinner("El agente está trabajando… (mira la Sala de control)"):
                    try:
                        resultado = preguntar(query, rol)
                    except httpx.HTTPError as exc:
                        resultado = {"error": f"{type(exc).__name__}: {exc}"}
            st.session_state["resultado"] = resultado
            st.session_state["eventos"] = eventos_de(resultado.get("run_id", "")) \
                if resultado.get("run_id") else []
            st.session_state.pop("semantica_guardada", None)
            aristas_negocio_cacheadas.clear()

    with derecha:
        resultado = st.session_state.get("resultado")
        eventos = st.session_state.get("eventos") or []

        pestanas = st.tabs([
            "Respuesta", "Ruta", "Evidencia y linaje", "Auditoría",
            "Capa semántica", "Sala de control",
        ])
        with pestanas[0]:
            if resultado:
                pestana_respuesta(resultado)
            else:
                st.info("Elige un rol y una pregunta, y pulsa **Ejecutar**.", icon="👈")
        with pestanas[1]:
            if resultado and not resultado.get("error"):
                pestana_ruta(resultado, eventos)
            else:
                st.info("Ejecuta una pregunta para ver el grafo recorrido.")
        with pestanas[2]:
            if resultado and not resultado.get("error"):
                pestana_evidencia(resultado)
            else:
                st.info("Ejecuta una pregunta para ver la evidencia.")
        with pestanas[3]:
            pestana_auditoria()
        with pestanas[4]:
            pestana_capa_semantica()
        with pestanas[5]:
            pestana_sala_de_control((resultado or {}).get("run_id"))


if __name__ == "__main__":
    main()
