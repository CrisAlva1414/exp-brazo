#!/usr/bin/env bash
# Arranque idempotente del dashboard local de Exp-brazo.
#
# No usa sudo ni instala paquetes del sistema. Si falta el entorno o las
# dependencias, llama a ./install.sh (también idempotente).
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
Arranque de Exp-brazo

Uso:
  ./start.sh                 Arranca el dashboard local.
  ./start.sh --check         Solo chequeos previos; no arranca el servidor.
  ./start.sh --check --json  Chequeo previo con reporte JSON al final.
  ./start.sh --mock          Usa el robot simulado (sin brazo físico).
  ./start.sh --no-camera     Arranca sin cámara (modo degradado).
  ./start.sh --port 8001     Usa otro puerto.
  ./start.sh --host 0.0.0.0  Escucha en otra interfaz de red.
  ./start.sh --allow-remote  Permite escuchar fuera de loopback (exige
                             EXP_BRAZO_TOKEN de 24+ caracteres).
  ./start.sh --open          Abre el navegador cuando el dashboard esté listo.
  ./start.sh --help          Muestra esta ayuda.

Seguridad:
  SIN VALIDAR: el paro de emergencia físico es la única garantía real;
  la pausa de software es complementaria.
EOF
}

CHECK_ONLY=0
CHECK_JSON=0
MOCK=0
NO_CAMERA=0
OPEN_BROWSER=0
ALLOW_REMOTE=0
PORT_ARG=""
HOST_ARG=""

while [ "$#" -gt 0 ]; do
    case "$1" in
        --check)
            CHECK_ONLY=1
            ;;
        --json)
            CHECK_JSON=1
            ;;
        --mock)
            MOCK=1
            ;;
        --no-camera)
            NO_CAMERA=1
            ;;
        --open)
            OPEN_BROWSER=1
            ;;
        --allow-remote)
            ALLOW_REMOTE=1
            ;;
        --port)
            if [ "$#" -lt 2 ]; then
                err "Falta el valor de --port (ejemplo: ./start.sh --port 8001)."
                exit 2
            fi
            PORT_ARG="$2"
            shift
            ;;
        --host)
            if [ "$#" -lt 2 ]; then
                err "Falta el valor de --host (ejemplo: ./start.sh --host 127.0.0.1)."
                exit 2
            fi
            HOST_ARG="$2"
            shift
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

if [ -n "$PORT_ARG" ]; then
    if ! [[ "$PORT_ARG" =~ ^[0-9]{1,5}$ ]]; then
        err "--port debe ser un número entre 1 y 65535."
        exit 2
    fi
    PORT_ARG=$((10#$PORT_ARG))
    if [ "$PORT_ARG" -lt 1 ] || [ "$PORT_ARG" -gt 65535 ]; then
        err "--port debe estar entre 1 y 65535."
        exit 2
    fi
fi

if [ -n "$HOST_ARG" ]; then
    if ! [[ "$HOST_ARG" =~ ^[A-Za-z0-9._:-]+$ ]]; then
        err "--host contiene caracteres no válidos."
        exit 2
    fi
fi

if [ "$CHECK_JSON" -eq 1 ] && [ "$CHECK_ONLY" -eq 0 ]; then
    warn "--json solo tiene efecto junto con --check; se ignora."
fi

printf '\n%s============================================================%s\n' "$BOLD" "$RESET"
printf '%s  Exp-brazo · Robot de feria%s\n' "$BOLD" "$RESET"
printf '%s============================================================%s\n' "$BOLD" "$RESET"
printf '%s  SEGURIDAD: SIN VALIDAR.%s\n' "$YELLOW" "$RESET"
printf '  El paro de emergencia físico es la única garantía real;\n'
printf '  la pausa de software es complementaria.\n'
printf '  No dejes el brazo operando sin supervisión.\n'
printf '%s============================================================%s\n\n' "$BOLD" "$RESET"

# ---------------------------------------------------------------------------
# Funciones de apoyo
# ---------------------------------------------------------------------------
require_core_deps() {
    "$1" -c 'import fastapi, uvicorn, pydantic, yaml, numpy' >/dev/null 2>&1
}

port_is_busy() {
    "$1" - "$2" "$3" <<'PY'
import socket
import sys

host = sys.argv[1]
port = int(sys.argv[2])
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.settimeout(0.7)
try:
    result = sock.connect_ex((host, port))
except OSError as exc:
    print(f"No se pudo comprobar {host}:{port}: {exc}", file=sys.stderr)
    raise SystemExit(2)
finally:
    sock.close()
raise SystemExit(0 if result == 0 else 1)
PY
}

health_status() {
    "$1" - "$2" "$3" <<'PY'
import json
import sys
import urllib.request

host = sys.argv[1]
port = sys.argv[2]
url = f"http://{host}:{port}/api/health"
try:
    with urllib.request.urlopen(url, timeout=2) as response:
        payload = json.loads(response.read(4096) or b"{}")
    if response.status == 200 and payload.get("ok") is True:
        print("ok")
    else:
        print("other")
except Exception:
    print("other")
PY
}

health_is_ok() {
    "$1" - "$2" "$3" <<'PY'
import json
import sys
import urllib.request

host = sys.argv[1]
port = sys.argv[2]
url = f"http://{host}:{port}/api/health"
try:
    with urllib.request.urlopen(url, timeout=2) as response:
        payload = json.loads(response.read(4096) or b"{}")
    raise SystemExit(0 if response.status == 200 and payload.get("ok") is True else 1)
except Exception:
    raise SystemExit(1)
PY
}

read_config_network() {
    "$1" - "$ROOT/config/config.yaml" <<'PY'
import sys

import yaml

host = "127.0.0.1"
port = 8000
try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    dashboard = data.get("dashboard") or {}
    host = str(dashboard.get("host") or host)
    port = int(dashboard.get("port") or port)
except Exception:
    host = "127.0.0.1"
    port = 8000
print(f"{host} {port}")
PY
}

read_robot_target() {
    "$1" - "$ROOT/config/config.yaml" <<'PY'
import sys

import yaml

adapter = "mock"
ip = ""
try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    robot = data.get("robot") or {}
    adapter = str(robot.get("adapter") or adapter)
    ip = str(robot.get("ip") or "")
except Exception:
    pass
print(f"{adapter} {ip}")
PY
}

print_troubleshooting() {
    printf '\n%sNo se puede arrancar todavía. Revisá esto:%s\n' "$YELLOW" "$RESET"
    printf '  - Cámara desconectada o índice incorrecto: revisá camera.index en\n'
    printf '    config/config.yaml y que el cable esté conectado.\n'
    printf '  - Cámara ocupada: cerrá Zoom, Meet, Cheese u otro programa que la use.\n'
    printf '  - Permisos de cámara: tu usuario debe pertenecer al grupo video.\n'
    printf '    Pedí a quien administra el equipo que lo agregue y volvé a iniciar sesión.\n'
    printf '  - Dependencias: ejecutá ./install.sh (o ./install.sh --offline).\n'
    printf '  - Configuración: revisá config/config.yaml; si falta, ./install.sh la\n'
    printf '    restaura desde config/defaults/ sin sobrescribir la existente.\n'
    printf '  - Puerto ocupado: probá ./start.sh --port 8001.\n'
    printf '  - Diagnóstico completo: ./start.sh --check --json\n'
    printf '  - Sin cámara, para probar la interfaz: ./start.sh --mock --no-camera\n'
}

open_browser_when_ready() {
    local python_bin="$1"
    local probe_host="$2"
    local port="$3"
    local browser_host="$4"
    local browser_token="${5:-}"
    local browser_url="http://${browser_host}:${port}"
    local attempt=0

    if [ -n "$browser_token" ]; then
        browser_url="${browser_url}/?token=${browser_token}"
    fi

    while [ "$attempt" -lt 120 ]; do
        if health_is_ok "$python_bin" "$probe_host" "$port"; then
            if command -v xdg-open >/dev/null 2>&1; then
                xdg-open "$browser_url" >/dev/null 2>&1 &
            fi
            return 0
        fi
        attempt=$((attempt + 1))
        sleep 0.5
    done
    return 1
}

# ---------------------------------------------------------------------------
# 1. Elegir intérprete (y autoinstalar si hace falta)
# ---------------------------------------------------------------------------
if [ -x "$ROOT/.venv/bin/python" ]; then
    PY="$ROOT/.venv/bin/python"
    ok "Entorno virtual: .venv"
else
    PY="$(command -v python3 2>/dev/null || true)"
    if [ -z "$PY" ]; then
        err "No se encontró python3. Instalá Python 3.11 o superior y reintentá."
        exit 1
    fi

    if [ "$CHECK_ONLY" -eq 1 ]; then
        warn "No existe .venv; el chequeo usa python3 del sistema."
    else
        info "No existe .venv; se ejecuta ./install.sh automáticamente..."
        if ! "$ROOT/install.sh"; then
            err "La instalación automática falló. Revisá el mensaje anterior y"
            err "volvé a ejecutar ./start.sh."
            exit 1
        fi
        if [ -x "$ROOT/.venv/bin/python" ]; then
            PY="$ROOT/.venv/bin/python"
        fi
    fi
fi

# ---------------------------------------------------------------------------
# 2. Dependencias críticas
# ---------------------------------------------------------------------------
if ! require_core_deps "$PY"; then
    if [ "$CHECK_ONLY" -eq 1 ]; then
        err "Faltan dependencias críticas para el chequeo."
        err "Ejecutá ./install.sh y volvé a probar con ./start.sh --check."
        exit 1
    fi

    warn "Faltan dependencias críticas; se ejecuta ./install.sh una vez..."
    if ! "$ROOT/install.sh"; then
        err "La instalación falló. No se puede arrancar."
        exit 1
    fi
    if [ -x "$ROOT/.venv/bin/python" ]; then
        PY="$ROOT/.venv/bin/python"
    fi

    if ! require_core_deps "$PY"; then
        err "Siguen faltando dependencias críticas después de instalar."
        print_troubleshooting
        exit 1
    fi
fi
ok "Dependencias críticas presentes."

# ---------------------------------------------------------------------------
# 3. Modo --check (no arranca el servidor)
# ---------------------------------------------------------------------------
if [ "$CHECK_ONLY" -eq 1 ]; then
    info "Ejecutando chequeo previo (no se inicia el dashboard)..."
    if [ "$CHECK_JSON" -eq 1 ]; then
        "$PY" -m exp_brazo.preflight --json
    else
        "$PY" -m exp_brazo.preflight
    fi
    exit $?
fi

# ---------------------------------------------------------------------------
# 4. Puerto y posible instancia ya corriendo
# ---------------------------------------------------------------------------
read -r CFG_HOST CFG_PORT < <(read_config_network "$PY")
if [ -z "${CFG_HOST:-}" ]; then
    CFG_HOST="127.0.0.1"
fi
RUN_HOST="${HOST_ARG:-$CFG_HOST}"
if [ -z "$RUN_HOST" ]; then
    RUN_HOST="127.0.0.1"
fi
RUN_PORT="${PORT_ARG:-$CFG_PORT}"

if [[ "$RUN_PORT" =~ ^[0-9]{1,5}$ ]]; then
    RUN_PORT=$((10#$RUN_PORT))
    if [ "$RUN_PORT" -lt 1 ] || [ "$RUN_PORT" -gt 65535 ]; then
        RUN_PORT=8000
    fi
else
    RUN_PORT=8000
fi

PROBE_HOST="$RUN_HOST"
BROWSER_HOST="$RUN_HOST"
if [ "$PROBE_HOST" = "0.0.0.0" ] || [ "$PROBE_HOST" = "::" ]; then
    PROBE_HOST="127.0.0.1"
fi
if [ "$BROWSER_HOST" = "0.0.0.0" ] || [ "$BROWSER_HOST" = "::" ]; then
    BROWSER_HOST="127.0.0.1"
fi

is_loopback_host() {
    case "$1" in
        127.0.0.1|localhost|::1) return 0 ;;
        *) return 1 ;;
    esac
}

if ! is_loopback_host "$RUN_HOST" && [ "$ALLOW_REMOTE" -ne 1 ]; then
    err "Por seguridad el dashboard solo escucha en 127.0.0.1."
    err "Para exponerlo usá --allow-remote y EXP_BRAZO_TOKEN."
    exit 1
fi

if [ "$ALLOW_REMOTE" -eq 1 ]; then
    if [ -z "${EXP_BRAZO_TOKEN:-}" ] || [ "${#EXP_BRAZO_TOKEN}" -lt 24 ]; then
        err "Con --allow-remote es obligatorio EXP_BRAZO_TOKEN (mínimo 24 caracteres)."
        err "Generá uno con:"
        printf '  python3 -c "import secrets;print(secrets.token_urlsafe(32))"\n'
        exit 1
    fi
fi

port_is_busy "$PY" "$PROBE_HOST" "$RUN_PORT"
PORT_STATE=$?

if [ "$PORT_STATE" -eq 2 ]; then
    err "No se pudo comprobar el puerto ${RUN_PORT} en ${PROBE_HOST}."
    err "Revisá el valor de --host o la configuración de red."
    exit 1
fi

if [ "$PORT_STATE" -eq 0 ]; then
    if [ "$(health_status "$PY" "$PROBE_HOST" "$RUN_PORT")" = "ok" ]; then
        ok "Ya hay una instancia de Exp-brazo corriendo en http://${BROWSER_HOST}:${RUN_PORT}"
        info "No se lanza una segunda instancia."
        exit 0
    fi

    NEXT_PORT=$((RUN_PORT + 1))
    if [ "$NEXT_PORT" -gt 65535 ]; then
        NEXT_PORT=8001
    fi
    err "El puerto ${RUN_PORT} en ${PROBE_HOST} está ocupado por otro programa."
    err "Probá con otro puerto: ./start.sh --port ${NEXT_PORT}"
    err "O cerrá el programa que está usando ese puerto."
    exit 1
fi
ok "Puerto ${RUN_PORT} libre."

# ---------------------------------------------------------------------------
# 5. Preflight y modo degradado
# ---------------------------------------------------------------------------
info "Ejecutando chequeo previo..."
PREFLIGHT_OUTPUT="$("$PY" -m exp_brazo.preflight --json)"
PREFLIGHT_RC=$?
printf '%s\n' "$PREFLIGHT_OUTPUT"

PREFLIGHT_JSON="$(printf '%s\n' "$PREFLIGHT_OUTPUT" | "$PY" -c '
import json
import sys

lines = [line for line in sys.stdin.read().splitlines() if line.strip()]
try:
    print(json.dumps(json.loads(lines[-1])))
except Exception:
    print("{}")
')"

if [ "$PREFLIGHT_RC" -ne 0 ]; then
    if [ -n "$HOST_ARG" ] || [ -n "$PORT_ARG" ]; then
        PORT_ONLY_BLOCK="$(printf '%s' "$PREFLIGHT_JSON" | "$PY" -c '
import json
import sys

try:
    report = json.load(sys.stdin)
except Exception:
    report = {}
deps = report.get("deps") or {}
critical = ("fastapi", "uvicorn", "pydantic", "yaml", "numpy")
critical_ok = all((deps.get(name) or {}).get("ok") for name in critical)
errors = [str(item) for item in (report.get("errors") or [])]
config_ok = bool((report.get("config") or {}).get("ok"))
behaviors_ok = bool((report.get("behaviors") or {}).get("ok"))
port_only = bool(errors) and all(item.startswith("puerto ocupado") for item in errors)
print("1" if critical_ok and config_ok and behaviors_ok and port_only else "0")
')"
        if [ "$PORT_ONLY_BLOCK" = "1" ]; then
            warn "El puerto de config/config.yaml está ocupado, pero se usará"
            warn "el puerto alternativo ${RUN_PORT} indicado por --port/--host."
        else
            print_troubleshooting
            exit 1
        fi
    else
        print_troubleshooting
        exit 1
    fi
fi

WARNINGS="$(printf '%s' "$PREFLIGHT_JSON" | "$PY" -c '
import json
import sys

try:
    report = json.load(sys.stdin)
except Exception:
    report = {}
print("\n".join(report.get("warnings", [])))
')"

DEGRADED="$(printf '%s' "$PREFLIGHT_JSON" | "$PY" -c '
import json
import sys

try:
    report = json.load(sys.stdin)
except Exception:
    report = {}
keywords = ("cámara", "camara", "cv2", "mediapipe")
print("1" if any(k in w.lower() for w in report.get("warnings", []) for k in keywords) else "0")
')"

if [ -n "$WARNINGS" ]; then
    warn "El chequeo previo reportó advertencias:"
    printf '%s\n' "$WARNINGS" | while IFS= read -r line; do
        [ -n "$line" ] && printf '  - %s\n' "$line"
    done
    if [ "$DEGRADED" = "1" ]; then
        warn "Se continúa en modo degradado (sin cámara o sin percepción)."
        warn "El dashboard y el modo mock siguen disponibles."
    fi
else
    ok "Chequeo previo sin advertencias."
fi

# ---------------------------------------------------------------------------
# 6. Aviso opcional sobre el controlador Dobot (no bloquea)
# ---------------------------------------------------------------------------
read -r ROBOT_ADAPTER ROBOT_IP < <(read_robot_target "$PY")
if [ "$ROBOT_ADAPTER" = "dobot" ] && [ -n "$ROBOT_IP" ]; then
    if [[ "$ROBOT_IP" =~ ^[0-9A-Fa-f:.]+$ ]]; then
        if command -v ping >/dev/null 2>&1; then
            if ping -c 1 -W 1 "$ROBOT_IP" >/dev/null 2>&1; then
                ok "Controlador Dobot responde en ${ROBOT_IP}."
            else
                warn "El controlador Dobot no responde en ${ROBOT_IP}."
                warn "Se continúa con percepción/dashboard; revisá red y cable."
            fi
        else
            info "No hay 'ping' disponible; se omite el chequeo del Dobot."
        fi
    else
        warn "La IP del Dobot en config/config.yaml no parece válida."
    fi
fi

# ---------------------------------------------------------------------------
# 7. Lanzamiento
# ---------------------------------------------------------------------------
ARGS=(--config config/config.yaml --behaviors config/behaviors.yaml)
if [ "$MOCK" -eq 1 ]; then
    ARGS+=(--mock)
fi
if [ "$NO_CAMERA" -eq 1 ]; then
    ARGS+=(--no-camera)
fi
if [ -n "$HOST_ARG" ]; then
    ARGS+=(--host "$HOST_ARG")
fi
if [ -n "$PORT_ARG" ]; then
    ARGS+=(--port "$PORT_ARG")
fi
if [ "$ALLOW_REMOTE" -eq 1 ]; then
    ARGS+=(--allow-remote)
fi

DASHBOARD_URL="http://${BROWSER_HOST}:${RUN_PORT}"
if [ "$ALLOW_REMOTE" -eq 1 ]; then
    REMOTE_DISPLAY_HOST="$BROWSER_HOST"
    if [ "$REMOTE_DISPLAY_HOST" = "0.0.0.0" ] || [ "$REMOTE_DISPLAY_HOST" = "::" ]; then
        REMOTE_DISPLAY_HOST="<IP-de-la-notebook>"
        info "Averiguá la IP con: hostname -I"
    fi
    info "Bootstrap remoto: http://${REMOTE_DISPLAY_HOST}:${RUN_PORT}/?token=${EXP_BRAZO_TOKEN}"
else
    info "Dashboard local: ${DASHBOARD_URL}"
fi
info "Ctrl+C para detener."
if [ "$MOCK" -eq 1 ]; then
    info "Modo mock activo: el robot es simulado."
fi
if [ "$NO_CAMERA" -eq 1 ]; then
    info "Cámara deshabilitada: modo degradado."
fi

if [ "$OPEN_BROWSER" -eq 1 ]; then
    if command -v xdg-open >/dev/null 2>&1; then
        if [ "$ALLOW_REMOTE" -eq 1 ]; then
            open_browser_when_ready "$PY" "$PROBE_HOST" "$RUN_PORT" "$BROWSER_HOST" "$EXP_BRAZO_TOKEN" &
        else
            open_browser_when_ready "$PY" "$PROBE_HOST" "$RUN_PORT" "$BROWSER_HOST" &
        fi
        info "El navegador se abrirá cuando el dashboard responda."
    else
        warn "No se encontró 'xdg-open'; abrí manualmente ${DASHBOARD_URL}"
    fi
fi

exec "$PY" -m exp_brazo.main "${ARGS[@]}"
