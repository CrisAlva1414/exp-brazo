#!/usr/bin/env bash
# Instalador idempotente de Exp-brazo.
#
# No usa sudo, no modifica el sistema y no descarga nada si ya está
# instalado. Pensado para una persona no desarrolladora en un mini PC
# Linux: crea un entorno virtual local, instala dependencias y deja la
# configuración lista para arrancar.
set -uo pipefail
umask 077

if [ -t 1 ]; then
    RED=$'\033[31m'
    GREEN=$'\033[32m'
    YELLOW=$'\033[33m'
    BLUE=$'\033[34m'
    BOLD=$'\033[1m'
    RESET=$'\033[0m'
else
    RED=''
    GREEN=''
    YELLOW=''
    BLUE=''
    BOLD=''
    RESET=''
fi

info() { printf '%s[INFO]%s %s\n' "$BLUE" "$RESET" "$*"; }
ok() { printf '%s[ OK ]%s %s\n' "$GREEN" "$RESET" "$*"; }
warn() { printf '%s[AVISO]%s %s\n' "$YELLOW" "$RESET" "$*"; }
err() { printf '%s[ERROR]%s %s\n' "$RED" "$RESET" "$*" >&2; }

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
ROOT="$SCRIPT_DIR"
cd "$ROOT" || {
    err "No se pudo entrar a la carpeta del proyecto: $ROOT"
    exit 1
}

usage() {
    cat <<'EOF'
Instalador de Exp-brazo (idempotente, sin sudo)

Uso:
  ./install.sh                 Prepara .venv y dependencias.
  ./install.sh --check         Solo diagnostica; no crea ni modifica nada.
  ./install.sh --offline       Instala solo con las wheels de wheels/.
  ./install.sh --fetch-wheels  Descarga wheels a wheels/ (requiere internet).
  ./install.sh --fetch-models  Descarga los modelos .task a assets/models/ (requiere internet).
  ./install.sh --help          Muestra esta ayuda.

Notas:
  - No sobrescribe config/config.yaml ni config/behaviors.yaml.
  - Si faltan, los restaura desde config/defaults/.
  - No pide permisos de administrador ni instala paquetes del sistema.
  - Los modelos .task se usan offline; el runtime no descarga nada.
EOF
}

CHECK_ONLY=0
OFFLINE=0
FETCH_WHEELS=0
FETCH_MODELS=0

while [ "$#" -gt 0 ]; do
    case "$1" in
        --check)
            CHECK_ONLY=1
            ;;
        --offline)
            OFFLINE=1
            ;;
        --fetch-wheels)
            FETCH_WHEELS=1
            ;;
        --fetch-models)
            FETCH_MODELS=1
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            err "Opción no reconocida: $1"
            printf '\n'
            usage
            exit 2
            ;;
    esac
    shift
done

printf '\n%s============================================================%s\n' "$BOLD" "$RESET"
printf '%s  Exp-brazo · Instalación local%s\n' "$BOLD" "$RESET"
printf '%s============================================================%s\n' "$BOLD" "$RESET"

if [ "$CHECK_ONLY" -eq 1 ]; then
    info "Modo chequeo: no se crea ni modifica nada (tampoco se usa internet)."
else
    info "Esto va a preparar un entorno Python local en .venv, instalar las"
    info "dependencias del proyecto y restaurar la configuración si falta."
    info "No usa sudo, no instala paquetes del sistema y no sobrescribe tu config."
fi

# ---------------------------------------------------------------------------
# 1. Python del sistema
# ---------------------------------------------------------------------------
if ! command -v python3 >/dev/null 2>&1; then
    err "No se encontró 'python3' en este equipo."
    err "Instalá Python 3.11 o superior (por ejemplo, desde el gestor de"
    err "paquetes de tu distribución) y volvé a ejecutar ./install.sh."
    exit 1
fi

PYTHON_VERSION="$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:3])))' 2>/dev/null)"
if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    err "Se requiere Python 3.11 o superior. Detectado: ${PYTHON_VERSION:-desconocido}."
    err "Actualizá Python y volvé a ejecutar ./install.sh."
    exit 1
fi
ok "Python del sistema: ${PYTHON_VERSION:-desconocido}"

# ---------------------------------------------------------------------------
# 2. Modo --check: diagnóstico seguro, sin tocar nada
# ---------------------------------------------------------------------------
if [ "$CHECK_ONLY" -eq 1 ]; then
    info "Revisando el entorno (sin modificar archivos ni instalar nada)..."

    if [ -x "$ROOT/.venv/bin/python" ]; then
        PYCHECK="$ROOT/.venv/bin/python"
        ok "Entorno virtual: .venv presente"
    else
        PYCHECK="python3"
        warn "Entorno virtual: .venv no existe (se crearía con ./install.sh)"
    fi

    critical_missing=0
    for module in fastapi uvicorn pydantic yaml numpy; do
        if "$PYCHECK" -c "import $module" >/dev/null 2>&1; then
            ok "Dependencia crítica '$module': instalada"
        else
            warn "Dependencia crítica '$module': FALTA"
            critical_missing=1
        fi
    done

    for module in cv2 mediapipe; do
        if "$PYCHECK" -c "import $module" >/dev/null 2>&1; then
            ok "Dependencia opcional '$module': instalada"
        else
            warn "Dependencia opcional '$module': falta (percepción deshabilitada)"
        fi
    done

    wheel_count=0
    if [ -d "$ROOT/wheels" ]; then
        wheel_count="$(find "$ROOT/wheels" -maxdepth 1 -type f -name '*.whl' 2>/dev/null | wc -l | tr -d ' ')"
    fi
    if [ "$wheel_count" -gt 0 ]; then
        ok "Wheels offline disponibles: $wheel_count"
    else
        warn "Wheels offline: no hay archivos .whl en wheels/"
    fi

    camera_found=0
    camera_list=""
    for device in /dev/video*; do
        [ -e "$device" ] || continue
        camera_found=1
        camera_list="$camera_list $device"
    done
    if [ "$camera_found" -eq 1 ]; then
        ok "Cámara: dispositivos detectados$camera_list"
    else
        warn "Cámara: no se detectaron dispositivos /dev/video*"
    fi

    config_ok=1
    for relative in config/config.yaml config/behaviors.yaml; do
        if [ -f "$ROOT/$relative" ]; then
            ok "Configuración: $relative presente"
        else
            warn "Configuración: falta $relative (se restauraría con ./install.sh)"
            config_ok=0
        fi
    done

    models_ok=1
    for model in pose_landmarker_lite.task pose_landmarker_full.task hand_landmarker.task; do
        if [ -s "$ROOT/assets/models/$model" ]; then
            ok "Modelo MediaPipe: $model presente"
        else
            warn "Modelo MediaPipe: falta assets/models/$model (se descargaría con ./install.sh --fetch-models)"
            models_ok=0
        fi
    done

    if [ "$config_ok" -eq 0 ]; then
        info "Para restaurar los archivos faltantes: ./install.sh"
    fi
    if [ "$models_ok" -eq 0 ]; then
        info "Para descargar los modelos: ./install.sh --fetch-models (requiere internet)"
    fi

    if [ "$critical_missing" -ne 0 ]; then
        err "Faltan dependencias críticas. Ejecutá ./install.sh y volvé a probar."
        exit 1
    fi

    ok "Diagnóstico completado: las dependencias críticas están presentes."
    info "No se modificó ningún archivo. Para instalar: ./install.sh"
    exit 0
fi

# ---------------------------------------------------------------------------
# 3. Carpetas de trabajo (idempotente)
# ---------------------------------------------------------------------------
for directory in logs recordings config_versions state; do
    if [ -d "$ROOT/$directory" ]; then
        info "Carpeta $directory: ya existe"
    else
        if mkdir -p "$ROOT/$directory"; then
            ok "Carpeta $directory: creada"
        else
            err "No se pudo crear la carpeta $directory."
            exit 1
        fi
    fi
done

# ---------------------------------------------------------------------------
# 4. Restaurar configuración solo si falta (nunca sobrescribir)
# ---------------------------------------------------------------------------
restore_config() {
    local destination="$1"
    local source="$2"

    if [ -f "$ROOT/$destination" ]; then
        info "Configuración $destination: ya existe (no se toca)"
        return 0
    fi

    if [ ! -f "$ROOT/$source" ]; then
        err "No existe $source; no se puede restaurar $destination."
        return 1
    fi

    mkdir -p "$(dirname "$ROOT/$destination")" || return 1

    if cp "$ROOT/$source" "$ROOT/$destination"; then
        ok "Configuración $destination: restaurada desde $source"
        return 0
    fi

    err "No se pudo copiar $source a $destination."
    return 1
}

restore_config "config/config.yaml" "config/defaults/config.yaml" || exit 1
restore_config "config/behaviors.yaml" "config/defaults/behaviors.yaml" || exit 1

# ---------------------------------------------------------------------------
# 5. Entorno virtual
# ---------------------------------------------------------------------------
if [ ! -x "$ROOT/.venv/bin/python" ]; then
    if [ -d "$ROOT/.venv" ]; then
        warn "El entorno .venv está incompleto; se intentará reparar."
    else
        info "Creando entorno virtual .venv..."
    fi

    if ! python3 -m venv "$ROOT/.venv"; then
        err "No se pudo crear el entorno virtual .venv."
        err "En Debian/Ubuntu suele faltar el paquete python3-venv"
        err "(o python3.12-venv). Pedí su instalación y volvé a ejecutar"
        err "./install.sh. Este script no instala paquetes del sistema ni usa sudo."
        exit 1
    fi
    ok "Entorno virtual .venv listo."
else
    ok "Entorno virtual .venv: ya existe (se reutiliza)."
fi

PY="$ROOT/.venv/bin/python"
if [ ! -x "$PY" ]; then
    err "El entorno virtual no tiene un intérprete utilizable en $PY."
    exit 1
fi

if [ ! -f "$ROOT/requirements.txt" ]; then
    err "Falta requirements.txt; no se pueden instalar las dependencias."
    exit 1
fi

# ---------------------------------------------------------------------------
# 6. Wheels opcionales para instalación offline
# ---------------------------------------------------------------------------
if [ "$FETCH_WHEELS" -eq 1 ]; then
    info "Descargando wheels a wheels/ (requiere internet)..."
    mkdir -p "$ROOT/wheels" || {
        err "No se pudo crear wheels/."
        exit 1
    }
    if "$PY" -m pip download -r "$ROOT/requirements.txt" -d "$ROOT/wheels"; then
        ok "Wheels descargadas en wheels/."
    else
        warn "No se pudieron descargar todas las wheels (¿sin internet?)."
        warn "Se continúa: si ya había wheels, se usarán; si no, se intentará online."
    fi
fi

# ---------------------------------------------------------------------------
# 6b. Modelos MediaPipe vendorizados (offline en runtime)
# ---------------------------------------------------------------------------
MODELS_DIR="$ROOT/assets/models"
MODEL_NAMES=(pose_landmarker_lite.task pose_landmarker_full.task hand_landmarker.task)
MODEL_URLS=(
    "https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_lite/float16/latest/pose_landmarker_lite.task"
    "https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_full/float16/latest/pose_landmarker_full.task"
    "https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/latest/hand_landmarker.task"
)

download_model() {
    local name="$1"
    local url="$2"
    local destination="$MODELS_DIR/$name"
    local partial="$destination.part"

    if [ -s "$destination" ]; then
        info "Modelo $name: ya existe (no se sobrescribe)"
        return 0
    fi

    if command -v curl >/dev/null 2>&1; then
        if curl -fL --retry 3 -o "$partial" "$url"; then
            :
        else
            rm -f "$partial"
            warn "No se pudo descargar $name con curl."
            return 1
        fi
    elif command -v wget >/dev/null 2>&1; then
        if wget -O "$partial" "$url"; then
            :
        else
            rm -f "$partial"
            warn "No se pudo descargar $name con wget."
            return 1
        fi
    else
        warn "No hay curl ni wget para descargar $name."
        return 1
    fi

    local size
    size="$(wc -c <"$partial" 2>/dev/null | tr -d ' ')"
    if [ -z "$size" ] || [ "$size" -lt 1000000 ]; then
        rm -f "$partial"
        warn "Descarga de $name inválida o demasiado chica (${size:-0} bytes)."
        return 1
    fi
    mv "$partial" "$destination"
    ok "Modelo $name: descargado ($size bytes)"
    return 0
}

verify_models() {
    local missing=0
    for model in "${MODEL_NAMES[@]}"; do
        if [ -s "$MODELS_DIR/$model" ]; then
            info "Modelo MediaPipe $model: presente"
        else
            warn "Modelo MediaPipe $model: FALTA en assets/models/"
            missing=1
        fi
    done
    if [ "$missing" -ne 0 ]; then
        warn "La percepción no podrá iniciar sin los modelos .task."
        warn "Ejecutá ./install.sh --fetch-models en una máquina con internet"
        warn "y copiá assets/models/ a este equipo, o ejecutá este instalador online."
    fi
    return 0
}

mkdir -p "$MODELS_DIR" || warn "No se pudo crear assets/models/."

if [ "$FETCH_MODELS" -eq 1 ]; then
    info "Descargando modelos MediaPipe .task a assets/models/ (requiere internet)..."
    fetch_failed=0
    for index in "${!MODEL_NAMES[@]}"; do
        if ! download_model "${MODEL_NAMES[$index]}" "${MODEL_URLS[$index]}"; then
            fetch_failed=1
        fi
    done
    if [ "$fetch_failed" -eq 0 ]; then
        ok "Modelos MediaPipe descargados o ya presentes."
    else
        warn "Algunos modelos no se pudieron descargar; se continúa con lo disponible."
    fi
fi

verify_models

# ---------------------------------------------------------------------------
# 7. Instalación de dependencias
# ---------------------------------------------------------------------------
wheel_count=0
if [ -d "$ROOT/wheels" ]; then
    wheel_count="$(find "$ROOT/wheels" -maxdepth 1 -type f -name '*.whl' 2>/dev/null | wc -l | tr -d ' ')"
fi

if [ "$OFFLINE" -eq 1 ] || [ "$wheel_count" -gt 0 ]; then
    if [ "$OFFLINE" -eq 1 ]; then
        info "Modo offline: se instala solo con wheels/."
    else
        info "Se encontraron $wheel_count wheels locales; se intenta instalar offline."
    fi

    if ! "$PY" -m pip install --no-index --find-links "$ROOT/wheels" -r "$ROOT/requirements.txt"; then
        if [ "$OFFLINE" -eq 1 ]; then
            err "No se pudo instalar en modo offline: las wheels están incompletas."
            err "En una máquina con internet ejecutá ./install.sh --fetch-wheels y"
            err "copiá la carpeta wheels/ a este equipo."
            exit 1
        fi
        warn "Las wheels locales están incompletas o fallaron; se reintenta online."
        if ! "$PY" -m pip install -r "$ROOT/requirements.txt"; then
            err "Falló la instalación de dependencias."
            err "Revisá tu conexión o ejecutá ./install.sh --fetch-wheels en una"
            err "máquina con internet."
            exit 1
        fi
    fi
else
    info "Se descargan dependencias una vez; luego puede funcionar offline."
    if ! "$PY" -m pip install -r "$ROOT/requirements.txt"; then
        err "Falló la instalación de dependencias."
        err "Revisá tu conexión o ejecutá ./install.sh --fetch-wheels en una"
        err "máquina con internet y copiá wheels/ a este equipo."
        exit 1
    fi
fi
ok "Dependencias instaladas o ya presentes."

# ---------------------------------------------------------------------------
# 8. Verificación de imports
# ---------------------------------------------------------------------------
if ! "$PY" -c 'import fastapi, uvicorn, pydantic, yaml, numpy' >/dev/null 2>&1; then
    err "Las dependencias críticas no se pueden importar."
    err "Probá: ./.venv/bin/python -m pip install -r requirements.txt"
    exit 1
fi
ok "Dependencias críticas verificadas (fastapi, uvicorn, pydantic, yaml, numpy)."

if "$PY" -c 'import cv2, mediapipe' >/dev/null 2>&1; then
    ok "Percepción disponible (cv2 y mediapipe)."
    cv_version="$("$PY" -c 'import cv2; print(cv2.__version__)' 2>/dev/null || true)"
    if "$PY" -c 'import cv2, sys; parts = tuple(int(part) for part in cv2.__version__.split(".")[:2]); sys.exit(0 if parts >= (4, 12) else 1)' >/dev/null 2>&1; then
        ok "OpenCV actualizado (cv2 ${cv_version:-desconocida})."
    else
        warn "OpenCV viejo; ejecutá ./install.sh para actualizar"
    fi
else
    warn "No se pudieron importar cv2/mediapipe: percepción deshabilitada."
    warn "El dashboard y el modo mock siguen funcionando."
    warn "Para habilitar la cámara, ejecutá ./install.sh --fetch-wheels (con internet)"
    warn "o ./install.sh en una máquina con internet y copiá wheels/."
fi

# ---------------------------------------------------------------------------
# 9. Versiones y próximo paso
# ---------------------------------------------------------------------------
info "Versiones instaladas en .venv:"
"$PY" - <<'PY'
import importlib.metadata as metadata

for distribution in (
    "fastapi",
    "uvicorn",
    "pydantic",
    "PyYAML",
    "numpy",
    "opencv-contrib-python",
    "mediapipe",
):
    try:
        print(f"  - {distribution}: {metadata.version(distribution)}")
    except metadata.PackageNotFoundError:
        print(f"  - {distribution}: no instalado")
PY

printf '\n'
ok "Instalación completada."
info "Próximo paso: ./start.sh"
info "Para probar sin brazo físico: ./start.sh --mock"
info "Para diagnosticar antes de arrancar: ./start.sh --check"
