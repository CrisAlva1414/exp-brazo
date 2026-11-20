# Convenciones de formato y estilo

## Python

- **ruff** como linter/formatter, `line-length = 100`, `target py311`,
  reglas `E, F, I, UP, B` (ver `pyproject.toml`).
- **Type hints** en APIs públicas (funciones y métodos expuestos entre
  capas y hacia el dashboard).
- Docstrings breves en módulos y APIs públicas.

## Mensajes

- Todo mensaje dirigido al **operador** va **en español**, claro y
  accionable (qué pasó y qué hacer).
- Los logs internos pueden ser técnicos, pero nunca deben incluir secretos
  ni PII completa.

## Marcadores

- Usar `TODO` cuando falta implementación conocida.
- Usar `SIN VALIDAR` cuando un valor o comportamiento no fue probado contra
  hardware real (deltas, home, límites, umbrales de gestos).

## Tests

- Tests con **pytest**, sin cámara ni robot reales: usar mocks/fakes.
- `testpaths = ["tests"]` únicamente (sin `addopts`/`timeout` globales).
- Un test que falla por lógica correcta no se "arregla" debilitando la
  aserción.

## Percepción — tuning de gestos (SIN VALIDAR)

Las ventanas de detección (`window_ms`) y los `hold_ms` se ampliaron para
cámaras lentas (~5 FPS reales medidos): HOLA 2500/300, SI 2000/250,
NO 2000/250, SEIS_SIETE 3000/400. `min_direction_changes`,
`min_amplitude_ratio` y los cooldowns no se modificaron.

Estos valores siguen **SIN VALIDAR** con personas reales y hardware; son un
punto de partida ajustado al FPS bajo observado. Requieren prueba supervisada
antes de darlos por buenos.
