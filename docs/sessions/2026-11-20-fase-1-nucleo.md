# Sesión 2026-11-20 — Fase 1: núcleo de dominio, MockAdapter y tests

- **Fecha**: 2026-11-20
- **Tipo**: implementación (builder)
- **Alcance**: contratos pydantic, config store versionado, catálogo de
  comportamientos, motor de decisión, controlador y adaptador mock. Sin
  percepción, dashboard ni Dobot real.

## Resumen

Se implementó el núcleo de dominio de Exp-brazo y se agregó la suite de
pruebas de Fase 1. La configuración real (`config/config.yaml`) y el
catálogo (`config/behaviors.yaml`) cargan sin cambios. Los tests no tocan
los archivos reales: usan copias temporales cuando necesitan escribir.

## Decisiones

- **Archivo de versión inicial**: `ConfigStore.load()` archiva la versión
  cargada como `config_versions/v{version:04d}.yaml` si no existe. Esto
  permite que `rollback()` tenga una versión anterior incluso después del
  primer `save()`, manteniendo el historial append-only.
- **Historial del mock**: `MockAdapter.history[n]["steps"]` guarda la
  cantidad de pasos ejecutados (no la lista de deltas).
- **Modo asíncrono**: `submit_event()` devuelve el registro con
  `result="queued"`; el worker registra el resultado final `"executed"` con
  su duración. Con `execute_inline=True` devuelve el registro final.
- **Fallo inyectado**: `clear_fault()` limpia el estado `FAULT`, pero no
  desactiva `fail_on_execute`; un test que quiera recuperar la ejecución
  debe apagar ese flag explícitamente.
- **Home del mock**: duración nominal de 500 ms escalada por `time_scale`.

## Pendientes

- Fase 2: percepción de gestos y cámara.
- Fase 5: dashboard y supervisor.
- Fase 6: adaptador Dobot real, con valores **SIN VALIDAR** hasta prueba
  física supervisada.
