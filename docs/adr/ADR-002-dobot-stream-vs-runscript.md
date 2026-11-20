# ADR-002 — Dobot: modo stream vs RunScript

- **Estado**: aceptado (comportamiento firmware-dependiente, **SIN VALIDAR**)
- **Fecha**: 2026-11-20

## Contexto

El modo real usa el SDK **Dobot-Arm/TCP-IP-Python-V4** (CR/E6/Nova) sobre
los puertos 29999 (dashboard), 30003 (movimiento) y 30004 (feedback). Hay
dos formas de ejecutar los comportamientos definidos como pasos en YAML:

1. **Stream**: el sistema envía `MovJ` por articulación paso a paso y
   sincroniza la finalización con el feedback del puerto 30004.
2. **RunScript**: se carga un script precompilado en el controlador.

## Decisión

Elegir **modo stream**: los pasos del YAML se traducen a `MovJ` (joint) y
la sincronización se hace por feedback (`RobotMode` + `CurrentCommandId`
del puerto 30004).

## Motivo

El dashboard debe poder editar deltas en caliente y previsualizar
movimientos sin recompilar nada en el controlador. `RunScript` exige
programar en DobotStudio Pro, lo que rompe el flujo de configuración desde
el dashboard y hace que el operador dependa de una herramienta externa.

## Consecuencias

- Los deltas viven en YAML y se pueden validar/editar desde la UI.
- La finalización de movimiento depende del firmware: el significado exacto
  de `RobotMode` y `CurrentCommandId` es **firmware-dependiente y SIN
  VALIDAR**.
- **Riesgo de corte de cable documentado**: ante un corte de comunicación
  el brazo queda en la última pose. Al reconectar, el sistema fuerza el
  regreso a home antes de aceptar nuevos movimientos.
- Se requiere manejo explícito de timeout y reconexión.

## Alternativas descartadas

- **RunScript**: requiere programar en DobotStudio Pro y no permite editar
  deltas en caliente desde el dashboard.

## Nota

Todos los movimientos, deltas, límites y poses de home asociados están
**SIN VALIDAR** contra hardware real y no deben ejecutarse sin una prueba
física supervisada.
