# Exp-brazo — AGENTS.md

Fuente de verdad del repositorio. Si algo en este archivo contradice al
código real, el código gana y este archivo debe actualizarse en la misma
sesión.

## Propósito

Robot de feria **offline**. Un brazo (Dobot o mock) reacciona a gestos
capturados por cámara local (HOLA, SI, NO, SEIS_SIETE) y se opera desde un
dashboard web local. No hay dependencia de internet en runtime: sin CDN,
sin descargas de modelos, sin telemetría.

## Stack real

- Python 3.11+ (probado sobre 3.12).
- FastAPI **0.139** / Starlette **1.3.1** + uvicorn (dashboard y API local).
- OpenCV **contrib 4.12.0.88** (captura de cámara).
- MediaPipe **Tasks API 0.10.35** (`mediapipe.tasks.python.vision`, no
  legacy): modelos `.task` vendorizados en `assets/models/` y leídos en
  runtime; **no se descargan modelos** en runtime (ver ADR-004).
- pydantic v2 (contratos y validación de configuración).
- PyYAML (config y catálogo de comportamientos).
- numpy **2.2.6**.
- Frontend vanilla (HTML/CSS/JS servido localmente, sin CDN).

## Comandos reales

```bash
./install.sh              # crea/actualiza .venv e instala wheels locales/requirements
./start.sh --check        # preflight: valida entorno, config, cámara y puertos
./start.sh                # arranca el sistema normal
./start.sh --mock         # arranca con adaptador de robot mock (sin brazo físico)
python -m pytest -q       # tests (sin cámara ni robot reales)
```

## Estructura de carpetas

```
exp_brazo/
  contracts/        # modelos pydantic, event_id, esquemas de config
  config_store/     # carga, versionado y aplicación de configuración
  behaviors/        # catálogo y ejecución de pasos
  state_machine/    # decisión / estados
  robot/            # adaptador de actuación (mock y Dobot)
  perception/       # captura y detección de gestos
  supervisor/       # heartbeat, reinicio, tareas de fondo
  troubleshooting/  # diagnóstico y mensajes al operador
  preflight/        # chequeos previos al arranque (--check)
  dashboard/        # API + frontend local
config/             # config.yaml y behaviors.yaml (semilla versionada)
config_versions/    # versiones aplicadas/guardadas en runtime
wheels/             # wheels offline para install.sh
tests/              # pytest, sin hardware
docs/
  sessions/         # bitácora de sesiones
  adr/              # Architecture Decision Records
  format/           # convenciones de estilo
state/              # estado persistido en runtime
```

## Principios

1. **Cuatro capas independientes**: percepción, decisión, actuación y
   supervisión. Cada una puede fallar o reiniciarse sin matar a las otras.
2. **Idempotencia por `event_id`**: repetir un evento no vuelve a mover el
   robot. El mismo `event_id` se procesa una sola vez.
3. **Límites duros dentro del adaptador**: los topes de articulación,
   velocidad y aceleración se aplican en el adaptador de robot, no solo en
   la UI ni en la config.
4. **Offline**: nada de red externa en runtime. Dependencias y modelos
   vienen instalados/embebidos.

## Aviso importante: SIN VALIDAR

Deltas, poses de home, límites articulares y umbrales de gestos son valores
**SIN VALIDAR** contra el brazo físico. No ejecutar movimientos en hardware
real sin revisión y prueba supervisada. El paro de emergencia físico es la
única garantía de seguridad; la pausa de software es complementaria.

Los límites mutables (articulares, velocidad, aceleración) solo se pueden
**achicar** desde la API, nunca ampliarse más allá del envelope físico
inmutable (`PHYSICAL_JOINT_ENVELOPE`).

## Convenciones

- **Conventional Commits** (`feat(scope): ...`, `fix(scope): ...`).
- **ruff**, `line-length = 100` (ver `pyproject.toml`).
- Mensajes de error al operador **en español**, claros y accionables.
- Marcar supuestos con `TODO` / `SIN VALIDAR` cuando corresponda.
- Tests sin cámara ni robot reales (usar mocks/fakes).

## Estado — fase 6 (Dobot real)

- SDK de referencia: **Dobot-Arm/TCP-IP-Python-V4** (CR/E6/Nova).
- Puertos: **29999** (dashboard), **30003** (movimiento), **30004**
  (feedback).
- Comandos usados: `EnableRobot`, `DisableRobot`, `ClearError`,
  `RobotMode`, `GetErrorID`, `GetAngle`, `MovJ` (joint), `Stop`, `Pause`,
  `Continue`, `RunScript`.
- Finalización de movimiento por **feedback del puerto 30004**
  (`RobotMode` + `CurrentCommandId`). El comportamiento exacto es
  **firmware-dependiente y SIN VALIDAR**.
- Modo elegido: **stream** (ver ADR-002). `RunScript` queda documentado
  como alternativa.
- El dashboard **exige token de acceso** y por defecto escucha **solo en
  loopback** (`127.0.0.1`). Para exponerlo en LAN se usa
  `--allow-remote` **y** la variable `EXP_BRAZO_TOKEN` (mínimo 24
  caracteres); sin ambas, el arranque se aborta.
