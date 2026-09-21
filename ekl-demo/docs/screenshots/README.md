# Capturas de la UI

Las seis pestañas del §4.7 y la Sala de control del §4.8, tomadas con
`LLM_MODEL=fake` sobre la pregunta hilo conductor.

| Archivo | Qué muestra |
|---------|-------------|
| `01_respuesta.png` | Respuesta con los 3 clientes, sus cifras, el responsable y la advertencia colateral sobre `nomina-batch`. |
| `02_ruta.png` | Grafo recorrido. Azul = grafo de negocio, ámbar = vino por A2A. Se ve `nomina-batch` como segunda entrada a `PAY-DB-01`. |
| `03_evidencia.png` | Tabla de evidencia con el linaje de cada dato, y los hallazgos atados a su id de evidencia. |
| `04_auditoria.png` | Tail de `audit.jsonl`: cada llamada MCP con lo que devolvió y lo que filtró. Distingue denegaciones por política de errores. |
| `05_capa_semantica.png` | Editor de `semantic_layer.yaml` — el momento "wow" #1. |
| `06_sala_de_control.png` | La Sala de control embebida en su pestaña. |
| `07_sala_de_control_pantalla_completa.png` | La Sala de control suelta (`:8020/control`), como se proyecta en la segunda pantalla: las cuatro zonas del §4.8. |
| `08_comparacion_de_corridas.png` | Comparación `riesgo` vs `analista_junior`: en ámbar, lo que cambia. Es el minuto 9-10 del guion. |

## Regenerarlas

No se editan a mano. Con la pila levantada (ver README, "La demo completa"):

```bash
python scripts/capturar_pantallas.py
```

Conduce un Chrome headless por CDP: abre la UI, lanza la pregunta,
recorre las pestañas y fotografía cada una. Necesita Google Chrome
(`CHROME_BIN` si está en otra ruta).
