#!/usr/bin/env python3
"""
capturar_pantallas.py — capturas de la UI para `docs/screenshots/`.

Conduce un Chrome headless por CDP (protocolo de depuración) para
fotografiar cada pestaña de la UI y la Sala de control. Se deja como
script y no como capturas sueltas para poder **regenerarlas** cuando la
interfaz cambie, en vez de arrastrar imágenes que envejecen.

    # con la pila levantada (agentes, trace_store y Streamlit):
    python scripts/capturar_pantallas.py

Requisitos: Google Chrome instalado y `websockets` (ya viene con
langgraph). No usa Selenium ni Playwright a propósito: son descargas
grandes para hacer siete fotos.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import websockets

ROOT_DIR = Path(__file__).resolve().parent.parent
DESTINO = ROOT_DIR / "docs" / "screenshots"

CHROME = os.environ.get(
    "CHROME_BIN", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
)
PUERTO_CDP = int(os.environ.get("CDP_PORT", "9333"))
UI_URL = os.environ.get("UI_URL", "http://127.0.0.1:8501")
CONTROL_URL = os.environ.get("TRACE_STORE_URL", "http://127.0.0.1:8020") + "/control"

ANCHO, ALTO = 1500, 980

#: Las seis pestañas del §4.7, en su orden.
PESTANAS = [
    ("Respuesta", "01_respuesta"),
    ("Ruta", "02_ruta"),
    ("Evidencia y linaje", "03_evidencia"),
    ("Auditoría", "04_auditoria"),
    ("Capa semántica", "05_capa_semantica"),
    ("Sala de control", "06_sala_de_control"),
]

JS_CLIC_TEXTO = """
(() => {
  const objetivo = %s;
  const candidatos = [...document.querySelectorAll('button, [role="tab"]')];
  const el = candidatos.find(e => (e.innerText || '').trim() === objetivo);
  if (!el) return 'no encontrado';
  el.click();
  return 'ok';
})()
"""

JS_LISTO = """
(() => {
  const corriendo = document.querySelector('[data-testid="stStatusWidget"]');
  return corriendo ? 'ocupado' : 'listo';
})()
"""


class Chrome:
    """Chrome headless conducido por CDP, con lo justo para capturar."""

    def __init__(self) -> None:
        self.perfil = tempfile.mkdtemp(prefix="ekl-chrome-")
        self.proceso: subprocess.Popen | None = None
        self.ws: websockets.WebSocketClientProtocol | None = None
        self._id = 0

    async def __aenter__(self) -> "Chrome":
        if not Path(CHROME).exists():
            raise RuntimeError(f"no encuentro Chrome en {CHROME} (define CHROME_BIN)")
        self.proceso = subprocess.Popen(
            [
                CHROME, "--headless=new", f"--remote-debugging-port={PUERTO_CDP}",
                f"--user-data-dir={self.perfil}", "--no-first-run", "--no-default-browser-check",
                f"--window-size={ANCHO},{ALTO}", "--hide-scrollbars",
                "--disable-gpu", "--force-color-profile=srgb", "about:blank",
            ],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        ws_url = None
        limite = time.time() + 30
        while time.time() < limite and ws_url is None:
            try:
                objetivos = httpx.get(f"http://127.0.0.1:{PUERTO_CDP}/json", timeout=1).json()
                pagina = next((o for o in objetivos if o.get("type") == "page"), None)
                if pagina:
                    ws_url = pagina["webSocketDebuggerUrl"]
            except (httpx.HTTPError, StopIteration, KeyError):
                await asyncio.sleep(0.4)
        if ws_url is None:
            raise RuntimeError("Chrome no expuso una pestaña por CDP")

        self.ws = await websockets.connect(ws_url, max_size=64 * 1024 * 1024)
        await self.cmd("Page.enable")
        await self.cmd("Runtime.enable")
        await self.cmd("Emulation.setDeviceMetricsOverride",
                       {"width": ANCHO, "height": ALTO, "deviceScaleFactor": 2, "mobile": False})
        return self

    async def __aexit__(self, *exc) -> None:
        if self.ws:
            await self.ws.close()
        if self.proceso:
            self.proceso.terminate()
            try:
                self.proceso.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover
                self.proceso.kill()
        shutil.rmtree(self.perfil, ignore_errors=True)

    async def cmd(self, metodo: str, params: dict | None = None) -> dict:
        self._id += 1
        mensaje_id = self._id
        await self.ws.send(json.dumps({"id": mensaje_id, "method": metodo,
                                       "params": params or {}}))
        while True:
            respuesta = json.loads(await self.ws.recv())
            if respuesta.get("id") == mensaje_id:
                if "error" in respuesta:
                    raise RuntimeError(f"{metodo}: {respuesta['error']}")
                return respuesta.get("result", {})

    async def ir_a(self, url: str, espera: float = 6.0) -> None:
        await self.cmd("Page.navigate", {"url": url})
        await asyncio.sleep(espera)

    async def evaluar(self, expresion: str) -> str:
        r = await self.cmd("Runtime.evaluate",
                           {"expression": expresion, "returnByValue": True, "awaitPromise": True})
        return str((r.get("result") or {}).get("value"))

    async def clic_en_texto(self, texto: str) -> bool:
        return await self.evaluar(JS_CLIC_TEXTO % json.dumps(texto)) == "ok"

    async def esperar_streamlit(self, limite_s: float = 90) -> None:
        """Espera a que Streamlit deje de estar 'RUNNING'."""
        limite = time.time() + limite_s
        while time.time() < limite:
            if await self.evaluar(JS_LISTO) == "listo":
                await asyncio.sleep(1.2)
                return
            await asyncio.sleep(0.5)

    async def capturar(self, destino: Path) -> None:
        r = await self.cmd("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False})
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_bytes(base64.b64decode(r["data"]))
        print(f"  ✓ {destino.relative_to(ROOT_DIR)}  ({destino.stat().st_size // 1024} KB)")


def _comprobar_servicios() -> list[str]:
    faltan = []
    for nombre, url in (("Streamlit", UI_URL),
                        ("trace_store", CONTROL_URL.replace("/control", "/health")),
                        ("Agente A", "http://127.0.0.1:8001/health"),
                        ("Agente B", "http://127.0.0.1:8002/health")):
        try:
            httpx.get(url, timeout=2)
        except httpx.HTTPError:
            faltan.append(f"{nombre} ({url})")
    return faltan


async def principal() -> int:
    faltan = _comprobar_servicios()
    if faltan:
        print("Faltan servicios por levantar:\n  - " + "\n  - ".join(faltan))
        print("\nLevántalos antes de capturar (ver README, 'La demo completa').")
        return 2

    async with Chrome() as chrome:
        print(f"Capturando la UI de {UI_URL}")
        await chrome.ir_a(UI_URL, espera=8)
        await chrome.esperar_streamlit()

        if not await chrome.clic_en_texto("Ejecutar"):
            print("  ! no encontré el botón Ejecutar")
            return 1
        await chrome.esperar_streamlit()

        for etiqueta, archivo in PESTANAS:
            if not await chrome.clic_en_texto(etiqueta):
                print(f"  ! no encontré la pestaña {etiqueta!r}")
                continue
            # La Sala de control es un iframe con su propio arranque.
            await asyncio.sleep(4.5 if etiqueta == "Sala de control" else 2.0)
            await chrome.esperar_streamlit()
            await chrome.capturar(DESTINO / f"{archivo}.png")

        print(f"\nCapturando la Sala de control suelta en {CONTROL_URL}")
        await chrome.ir_a(CONTROL_URL, espera=6)
        await chrome.capturar(DESTINO / "07_sala_de_control_pantalla_completa.png")

        # Comparación de dos corridas (el minuto 9-10 del guion).
        await chrome.evaluar(
            "(() => { const b = document.getElementById('btnComparar'); if (b) b.click();"
            " const s = document.getElementById('selRunB');"
            " if (s) { const op = [...s.options].find(o => o.value.includes('junior'));"
            " if (op) { s.value = op.value; s.dispatchEvent(new Event('change')); } }"
            " return 'ok'; })()"
        )
        await asyncio.sleep(2.5)
        await chrome.capturar(DESTINO / "08_comparacion_de_corridas.png")

    print(f"\nListo: {len(list(DESTINO.glob('*.png')))} capturas en {DESTINO.relative_to(ROOT_DIR)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(principal()))
