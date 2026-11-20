# Loop State — Exp-brazo

## Tarea
Construir el sistema completo de interacción robot-humano (percepción de 4
gestos, decisión, actuación Mock/Dobot, dashboard local, supervisor,
troubleshooting, scripts idempotentes), con foco inmediato en dashboard
debuggeado + reconocimiento de gestos. Dobot real (fase 6) documentado desde
el SDK oficial y marcado SIN VALIDAR hasta prueba física.

## Criterios de aceptación
- Sistema operativo con cámara desconectada (lo informa y degrada).
- Ningún comportamiento excede límites articulares/velocidad/aceleración.
- Repetir un event_id no mueve el robot dos veces.
- Todo se reconfigura y diagnostica desde el dashboard sin tocar código.
- Todo offline: sin CDN ni descargas en runtime.
- start.sh idempotente, no-dev, con troubleshooting, lanza si todo está bien.
- Movimientos previsualizables/analizables antes de ejecutar (mínimo SI, NO, HOLA).

## Presupuesto
max_iteraciones: 8

## Bitácora
### Iteración 0 — Plan aprobado
- Plan refinado por fases 0..6.
- SDK verificado: Dobot-Arm/TCP-IP-Python-V4 (CR/E6/Nova).
- Puertos 29999/30003/30004, MovJ(joint), RelJointMovJ, EnableRobot,
  GetErrorID, RobotMode, GetAngle, RunScript.
- Decisión: modo stream con sincronización por CurrentCommandId; RunScript
  documentado como alternativa. Finalización firmware-dependiente SIN VALIDAR.
- Repo vacío: aplicar Caso B (bootstrap AGENTS.md + docs/).
