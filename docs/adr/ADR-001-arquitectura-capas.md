# ADR-001 — Arquitectura en cuatro capas independientes

- **Estado**: aceptado
- **Fecha**: 2026-11-20

## Contexto

El sistema interactúa con personas en una feria: captura gestos con cámara,
decide qué hacer, mueve un brazo y se supervisa a sí mismo. La cámara, el
brazo y el dashboard pueden fallar o desconectarse de forma independiente.

## Decisión

Separar el sistema en cuatro capas independientes:

1. **Percepción**: captura y detección de gestos.
2. **Decisión**: máquina de estados, anti-repetición e idempotencia.
3. **Actuación**: adaptador de robot (mock y Dobot), con límites duros.
4. **Supervisión**: heartbeat, reinicio y tareas de fondo.

Cada capa se comunica mediante contratos explícitos y puede reiniciarse sin
requerir el reinicio de las demás.

## Motivo

Cada capa puede morir sin matar a las otras. Si la cámara se cae, el
dashboard y el supervisor siguen funcionando y el operador ve el
diagnóstico. Si el brazo se desconecta, la percepción y la decisión siguen
corriendo y la actuación se degrada de forma controlada. Esto es clave en
un entorno de feria donde no se puede reiniciar todo ante cada fallo.

## Consecuencias

- Más piezas móviles y contratos que mantener, pero fallos aislados.
- La supervisión puede reiniciar una sola capa.
- Los límites de seguridad viven en la actuación, no en la UI.
- Mayor necesidad de pruebas con mocks entre capas.

## Alternativas descartadas

- **Monolito**: un único proceso con todo acoplado. Se descarta porque
  cualquier fallo de cámara o robot derriba el sistema completo, y porque
  impide degradar de forma granular en un entorno sin supervisión técnica
  permanente.
