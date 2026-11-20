# Modelos MediaPipe vendorizados

Modelos **oficiales de MediaPipe** (Google) descargados una sola vez y
versionados en el repositorio para que el sistema funcione **100 % offline**
en runtime. No se descargan modelos al arrancar.

- **Fecha de descarga**: 2026-11-20
- **Formato**: MediaPipe Tasks API (`.task`, float16)
- **Uso en runtime**: solo lectura desde `assets/models/`; sin red.

## Archivos

| Archivo | Tamaño | Uso | URL oficial |
| --- | --- | --- | --- |
| `pose_landmarker_lite.task` | 5 777 746 bytes (~5,6 MB) | Pose, `pose_model_complexity: 0` | https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_lite/float16/latest/pose_landmarker_lite.task |
| `pose_landmarker_full.task` | 9 398 198 bytes (~9,0 MB) | Pose, `pose_model_complexity: 1` | https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_full/float16/latest/pose_landmarker_full.task |
| `hand_landmarker.task` | 7 819 105 bytes (~7,5 MB) | Manos (ambos niveles de complejidad) | https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/latest/hand_landmarker.task |

**Total**: 22 995 049 bytes (~23 MB) para los 3 modelos.

## Reposición

Si faltan, en una máquina **con internet**:

```bash
./install.sh --fetch-models
```

El script es idempotente: no sobrescribe archivos ya presentes.
