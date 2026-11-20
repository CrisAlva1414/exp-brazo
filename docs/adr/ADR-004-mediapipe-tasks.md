# ADR-004 — Migración a MediaPipe Tasks API (offline real)

- **Estado**: aceptado
- **Fecha**: 2026-11-20

## Contexto

El stack original tenía un conflicto de versiones irresoluble con el
resolver de pip:

- **MediaPipe legacy 0.10.20** (soluciones `mp.solutions`) exigía
  `NumPy<2`.
- **OpenCV 4.12** exige `NumPy>=2`.
- **OpenCV 4.10**, que satisfacía a ambos, estaba afectado por
  **CVE-2025-53644**.

Es decir: o se quedaba un NumPy viejo con un OpenCV vulnerable, o se
rompía la compatibilidad con MediaPipe legacy.

## Decisión

Migrar a:

- **MediaPipe Tasks API 0.10.35** (`mediapipe.tasks.python.vision`),
  eliminando cualquier uso de `mp.solutions`.
- **NumPy 2.2.6**.
- **OpenCV contrib 4.12.0.88** (`opencv-contrib-python`), sin la CVE.
- **Modelos `.task` vendorizados** en `assets/models/`
  (`pose_landmarker_lite.task`, `pose_landmarker_full.task`,
  `hand_landmarker.task`), descargados una sola vez con
  `./install.sh --fetch-models` y versionados en el repo.

## Consecuencias

- **Offline real**: en runtime no se descarga nada (ni dependencias ni
  modelos); los tres modelos se leen en solo lectura desde
  `assets/models/`.
- **23 MB de modelos** agregados al repositorio (~5,6 + ~9,0 + ~7,5 MB).
- Se **elimina `mp.solutions`** del código; el motor de percepción usa
  `mediapipe.tasks.python.vision` (PoseLandmarker / HandLandmarker).
- Los tests que necesitan `mediapipe`/`numpy` usan
  `pytest.importorskip(...)`: la suite corre completa en el `.venv` y se
  saltea la integración en entornos sin esas dependencias.
- La suite pasa **182 tests** en `.venv` y **181 passed + 1 skipped** en
  el Python del sistema.

## Alternativas descartadas

- **Quedarse en OpenCV 4.11** (o 4.10): mantiene la exposición a
  CVE-2025-53644 sin una razón de bloqueo que lo justifique.
- **Instalar OpenCV 4.12 con `--no-deps` sobre NumPy 1.26**: el resolver
  queda inconsistente, `pip check` falla y cualquier instalación futura
  puede romper el entorno de forma no reproducible. Se prefirió una
  migración limpia y verificable.

## Nota

La migración no cambia el contrato de gestos ni la lógica de decisión;
solo la forma de obtener landmarks. Los umbrales de gestos siguen
**SIN VALIDAR** contra personas reales.
