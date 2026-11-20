# Sesión 2026-11-20 — Bootstrap documental

- **Fecha**: 2026-11-20
- **Tipo**: Bootstrap Caso B (repo vacío, sin AGENTS.md ni docs/ previos)
- **Alcance**: solo estructura, metadatos, configuración semilla y
  documentación. Sin lógica de percepción ni dashboard.

## Resumen

Se inicializa la documentación y el esqueleto del proyecto "Exp-brazo":
robot de feria offline que reacciona a cuatro gestos (HOLA, SI, NO,
SEIS_SIETE) y se opera desde un dashboard local. Se crean `AGENTS.md`,
`README.md`, requirements, configuración de ruff/pytest, `.gitignore`,
documentación de sesiones/ADR/formato, el paquete `exp_brazo/` y la
configuración semilla (`config/config.yaml`, `config/behaviors.yaml`).

## Decisiones

- **Stack offline**: FastAPI + uvicorn, OpenCV, MediaPipe **Tasks API**
  (modelos `.task` vendorizados), pydantic v2, PyYAML, numpy, frontend
  vanilla sin CDN. Nada de red externa en runtime.
- **MediaPipe Tasks API**: se usan `mediapipe.tasks.python.vision`
  (Pose/Hand Landmarker) con modelos `.task` vendorizados en
  `assets/models/`, sin descargas en runtime. La decisión original de
  usar soluciones legacy (`mp.solutions`) fue revertida por la migración
  documentada en ADR-004 (NumPy 2 + OpenCV 4.12).
- **Modo stream vs RunScript** (ver ADR-002): se elige modo stream (pasos
  YAML -> `MovJ` joint con sincronización por feedback del puerto 30004)
  para poder editar deltas en caliente desde el dashboard. `RunScript`
  queda documentado como alternativa.
- **Cuatro capas independientes** (ver ADR-001): percepción, decisión,
  actuación y supervisión, para que una pueda fallar sin matar a las otras.

## Pendientes

- Fase 1: contratos, config store y validación.
- Fase 2: percepción de gestos.
- Fase 3: máquina de estados / decisión.
- Fase 4: adaptador mock y ejecución de comportamientos.
- Fase 5: dashboard y supervisor.
- Fase 6: adaptador Dobot real, con todos los valores **SIN VALIDAR** hasta
  prueba física supervisada.
