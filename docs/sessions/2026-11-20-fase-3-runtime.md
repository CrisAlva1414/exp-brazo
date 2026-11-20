# Sesión 2026-11-20 — Fase 3: runtime, supervisor, troubleshooting y preflight

- **Fecha**: 2026-11-20
- **Tipo**: implementación (builder)
- **Alcance**: unión de las capas existentes en un `Runtime`, supervisor con
  degradación y modo ambiental, diagnóstico guiado y preflight ejecutable.
  Sin dashboard web ni `DobotAdapter` real.

## Resumen

Se agregaron `logging_setup`, `supervisor`, `troubleshooting`, `preflight` y
`runtime`, junto con sus pruebas. El runtime carga configuración y catálogo,
construye adaptador/controlador/pipeline/supervisor, aplica cambios de
configuración en caliente y expone comandos, eventos, exportación, replay y
grabación. El preflight puede ejecutarse con
`python -m exp_brazo.preflight [--json]`.

## Decisiones

- **force_adapter** sólo modifica la configuración en memoria; no se guarda
  automáticamente en disco.
- **Validación de comportamientos en caliente**: se valida el catálogo
  actual contra los nuevos límites/velocidades antes de persistir. Si falla,
  no se guarda nada.
- **Rollback y versión**: el runtime usa el `ConfigStore` existente; el
  directorio de versiones se deriva como hermano de `config/` para que las
  pruebas con `tmp_path` no contaminen el repositorio.
- **Supervisor testeable**: `force_check()` ejecuta un ciclo con el `clock`
  inyectado; el hilo daemon sólo aporta el ciclo real cada 0.5 s.
- **Diagnóstico acotado**: cada chequeo corre con timeout independiente. Si
  `cv2`/`mediapipe` faltan, se reporta acción (`./install.sh`) sin bloquear
  el arranque.
- **Preflight degradado**: cámara y dependencias opcionales generan warnings;
  `can_start` depende sólo de dependencias críticas, configuración, catálogo
  y puerto libre.
- Se agregó el comportamiento semilla `ambiental` (deshabilitado por defecto)
  para que el supervisor tenga una acción suave en modo ambiental.

## Pendientes

- Fase siguiente: `DobotAdapter` real y dashboard web.
- Validar en hardware real todos los deltas, poses y umbrales marcados como
  **SIN VALIDAR**.
