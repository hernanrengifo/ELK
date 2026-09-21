"""
grafo_svg.py — dibujo del grafo recorrido para la pestaña Ruta.

**Por qué no `streamlit-agraph`.** El plan admite `pyvis` o
`streamlit-agraph` (§4.7) y se intentó con el segundo, pero dentro de
una pestaña de Streamlit no sirve:

  * Streamlit renderiza **todas** las pestañas y oculta las inactivas con
    CSS. El componente monta con el canvas a tamaño cero, vis.js encuadra
    la vista (`stabilization.fit`) sobre ese cero, y cuando la pestaña se
    muestra el grafo queda comprimido en una esquina. Ni un `resize` de
    la ventana lo recupera, porque el ajuste ya ocurrió.
  * Además, su `Config.__init__` hace `self.__dict__.update(**kwargs)`,
    así que todos los parámetros de layout acaban también como claves de
    primer nivel del objeto de opciones; vis.js las rechaza y descarta el
    objeto entero ("Errors have been found in the supplied options
    object").

Lo que se recorre es un DAG por capas (cliente → producto → servicio →
base de datos / persona), así que dibujarlo es calcular niveles y pintar
un SVG. Sale determinista, se ve igual siempre —importante para una demo
cronometrada— y permite usar exactamente los mismos colores que la Sala
de control. La opción de `streamlit-agraph` sigue disponible con un
interruptor en la propia pestaña, por si una versión futura lo arregla.
"""

from __future__ import annotations

from typing import Any

COLOR = {
    "negocio": "#4a9eff",
    "frontera": "#8b93a7",
    "a2a": "#f5a524",
}
FONDO = "#0f1219"
TEXTO = "#e6e9f0"
TENUE = "#8b93a7"
COLOR_ARISTA = "#3d465c"

ANCHO_NIVEL = 215
ALTO_FILA = 86
MARGEN_X = 95
MARGEN_Y = 52
ALTO_CAJA = 34
#: Ancho de la caja según el texto: un id como `nomina-batch` no cabe en
#: un círculo de radio fijo, y recortarlo perdería justo el nodo que la
#: demo quiere señalar.
ANCHO_CAJA_MIN = 62
ANCHO_POR_CARACTER = 7.3


def _niveles(nodos: list[str], aristas: list[tuple[str, str, str]]) -> dict[str, int]:
    """Nivel de cada nodo = camino más largo desde cualquier origen.

    Es lo que coloca los clientes a la izquierda y la base de datos a la
    derecha, con `nomina-batch` como segunda entrada a la misma base —
    que es justo el hallazgo colateral que la demo quiere enseñar.
    """
    salientes: dict[str, list[str]] = {n: [] for n in nodos}
    entrantes: dict[str, int] = {n: 0 for n in nodos}
    for origen, destino, _ in aristas:
        if origen in salientes and destino in entrantes:
            salientes[origen].append(destino)
            entrantes[destino] += 1

    nivel = {n: 0 for n in nodos}
    cola = [n for n in nodos if entrantes[n] == 0]
    pendiente = dict(entrantes)
    vistos = 0
    while cola:
        actual = cola.pop(0)
        vistos += 1
        for siguiente in salientes[actual]:
            nivel[siguiente] = max(nivel[siguiente], nivel[actual] + 1)
            pendiente[siguiente] -= 1
            if pendiente[siguiente] == 0:
                cola.append(siguiente)
    if vistos < len(nodos):
        # Hay un ciclo (no debería, pero un grafo real puede tenerlo):
        # se deja a los nodos no ordenados en su nivel actual en vez de
        # colgarse.
        pass
    return nivel


def _ancho_caja(texto: str) -> float:
    return max(ANCHO_CAJA_MIN, len(texto) * ANCHO_POR_CARACTER + 16)


def _posiciones(
    nodos: list[dict[str, Any]], aristas: list[tuple[str, str, str]]
) -> tuple[dict[str, tuple[int, int]], int, int]:
    ids = [n["id"] for n in nodos]
    nivel = _niveles(ids, aristas)
    por_nivel: dict[int, list[str]] = {}
    for node_id in ids:
        por_nivel.setdefault(nivel[node_id], []).append(node_id)

    max_nivel = max(por_nivel) if por_nivel else 0
    max_filas = max((len(v) for v in por_nivel.values()), default=1)
    ancho = MARGEN_X * 2 + ANCHO_NIVEL * max_nivel
    alto = MARGEN_Y * 2 + ALTO_FILA * max(max_filas - 1, 0)

    pos: dict[str, tuple[int, int]] = {}
    for nv, miembros in por_nivel.items():
        x = MARGEN_X + ANCHO_NIVEL * nv
        sobra = (alto - MARGEN_Y * 2 - ALTO_FILA * (len(miembros) - 1)) // 2
        for i, node_id in enumerate(miembros):
            pos[node_id] = (x, MARGEN_Y + sobra + ALTO_FILA * i)
    return pos, ancho, alto


def _escapar(texto: str) -> str:
    return (
        str(texto)
        .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def alto_de(nodos: list[dict[str, Any]], aristas: list[tuple[str, str, str]]) -> int:
    """Alto en píxeles que ocupará el dibujo.

    Lo necesita `st.components.v1.html`, que exige una altura fija: si se
    queda corta, el grafo se ve recortado a una franja.
    """
    if not nodos:
        return 80
    _, _, alto = _posiciones(nodos, aristas)
    return alto


def dibujar(
    nodos: list[dict[str, Any]], aristas: list[tuple[str, str, str]]
) -> str:
    """SVG del grafo recorrido, listo para `st.components.v1.html`."""
    if not nodos:
        return "<p style='color:#8b93a7'>La corrida no recorrió ningún nodo.</p>"

    pos, ancho, alto = _posiciones(nodos, aristas)
    partes: list[str] = [
        f'<svg viewBox="0 0 {ancho} {alto}" width="100%" height="{alto}" '
        f'xmlns="http://www.w3.org/2000/svg" style="background:{FONDO};border-radius:10px">',
        f'<defs><marker id="punta" markerWidth="9" markerHeight="7" refX="9" refY="3.5" '
        f'orient="auto"><polygon points="0 0, 9 3.5, 0 7" fill="{COLOR_ARISTA}"/></marker></defs>',
    ]

    for origen, destino, tipo in aristas:
        if origen not in pos or destino not in pos:
            continue
        x1, y1 = pos[origen]
        x2, y2 = pos[destino]
        # Se recorta la línea en el borde de cada caja para que la punta
        # no quede debajo del nodo.
        media_a = _ancho_caja(origen) / 2
        media_b = _ancho_caja(destino) / 2
        dx, dy = x2 - x1, y2 - y1
        largo = max((dx * dx + dy * dy) ** 0.5, 1)
        ux, uy = dx / largo, dy / largo
        ax, ay = x1 + ux * (media_a + 4), y1 + uy * (media_a + 4)
        bx, by = x2 - ux * (media_b + 9), y2 - uy * (media_b + 9)
        mx, my = (ax + bx) / 2, (ay + by) / 2 - 9
        partes.append(
            f'<line x1="{ax:.0f}" y1="{ay:.0f}" x2="{bx:.0f}" y2="{by:.0f}" '
            f'stroke="{COLOR_ARISTA}" stroke-width="1.6" marker-end="url(#punta)"/>'
            f'<text x="{mx:.0f}" y="{my:.0f}" fill="{TENUE}" font-size="9" '
            f'text-anchor="middle" font-family="system-ui,sans-serif">{_escapar(tipo)}</text>'
        )

    for nodo in nodos:
        x, y = pos[nodo["id"]]
        color = COLOR.get(nodo["origen"], TENUE)
        ancho_caja = _ancho_caja(nodo["id"])
        # El nombre solo se pinta si aporta algo sobre el id (para
        # `CLI01` sí —"Textiles Andinos"—, para `pagos-core` no).
        nombre = nodo["etiqueta"] if nodo["etiqueta"] != nodo["id"] else ""
        if len(nombre) > 20:
            nombre = nombre[:19] + "…"
        partes.append(
            f'<g><title>{_escapar(nodo["tipo"])} · {_escapar(nodo["id"])}'
            f' · {_escapar(nodo["etiqueta"])}</title>'
            f'<rect x="{x - ancho_caja / 2:.0f}" y="{y - ALTO_CAJA / 2:.0f}" '
            f'width="{ancho_caja:.0f}" height="{ALTO_CAJA}" rx="8" '
            f'fill="{color}" fill-opacity="0.15" stroke="{color}" stroke-width="1.8"/>'
            f'<text x="{x}" y="{y + 4}" fill="{TEXTO}" font-size="10.5" text-anchor="middle" '
            f'font-family="system-ui,sans-serif" font-weight="600">{_escapar(nodo["id"])}</text>'
            f'<text x="{x}" y="{y - ALTO_CAJA / 2 - 6:.0f}" fill="{color}" font-size="8.5" '
            f'text-anchor="middle" font-family="system-ui,sans-serif" '
            f'opacity="0.9">{_escapar(nodo["tipo"])}</text>'
            + (
                f'<text x="{x}" y="{y + ALTO_CAJA / 2 + 13:.0f}" fill="{TENUE}" font-size="9" '
                f'text-anchor="middle" font-family="system-ui,sans-serif">'
                f'{_escapar(nombre)}</text>'
                if nombre else ""
            )
            + "</g>"
        )

    partes.append("</svg>")
    return "".join(partes)
