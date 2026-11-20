# Exp-brazo

Robot de feria que reacciona a gestos de las manos y se opera desde una
página web local. Funciona **sin internet** una vez instalado.

## Clonar en otra PC (rápido)

```bash
git clone --depth 1 https://github.com/CrisAlva1414/exp-brazo.git
cd exp-brazo
./install.sh        # instala dependencias (una vez, con internet)
./start.sh --mock   # prueba sin brazo físico
```

Abrí `http://127.0.0.1:8000`. En **Config → Detectar cámaras** elegí la
cámara USB y guardá. El repo ya incluye los modelos `.task`; no se
descargan en runtime. Si la otra PC no tiene internet, copiá la carpeta
`wheels/` desde esta máquina antes de `./install.sh`.

## ¿Qué hace?

Una cámara mira a las personas y detecta cuatro gestos (HOLA, SI, NO y
SEIS_SIETE). Según el gesto, el brazo ejecuta un movimiento corto
predefinido. Todo se controla desde un dashboard que corre en tu propia
computadora, en `http://127.0.0.1:8000`.

## Requisitos

- Un mini PC con Linux y Python 3.11 o superior.
- Una cámara web (para el modo real; el modo mock no la necesita).
- Conexión de red local al brazo, solo si vas a usar el robot físico.
- Internet **solo la primera vez** para descargar dependencias y
  modelos, o una carpeta `wheels/` preparada en otra máquina para
  instalar offline.

No hace falta conexión a internet para arrancar el sistema: los modelos
de MediaPipe (Tasks API `.task`) están vendorizados en
`assets/models/` y las dependencias se instalan desde `wheels/`.

## Comandos principales

```bash
./install.sh               # crea/actualiza .venv e instala desde wheels/ o PyPI
./install.sh --fetch-models # descarga los modelos .task a assets/models/ (con internet)
./start.sh                 # arranca el sistema
./start.sh --check         # diagnóstico previo, sin arrancar
./start.sh --mock          # arranca con robot simulado (sin brazo físico)
```

## Instalación

Abrí una terminal, entrá a la carpeta del proyecto y ejecutá:

```bash
./install.sh
```

El instalador:

- Verifica que `python3` sea 3.11 o superior.
- Crea las carpetas de trabajo (`logs`, `recordings`, `state`,
  `config_versions`) si faltan.
- Restaura `config/config.yaml` o `config/behaviors.yaml` desde
  `config/defaults/` **solo si faltan**; nunca sobrescribe los existentes.
- Crea un entorno virtual local en `.venv` y instala las dependencias ahí.
- No usa `sudo`, no instala paquetes del sistema y no modifica tu
  configuración.

Podés volver a ejecutarlo las veces que quieras: es idempotente y no
duplica nada.

### Diagnóstico de instalación (sin modificar nada)

```bash
./install.sh --check
```

Muestra Python, `.venv`, dependencias instaladas, wheels disponibles,
cámaras detectadas y archivos de configuración. No crea archivos, no
instala y no usa internet.

### Instalación offline

En una máquina con internet, dentro de la carpeta del proyecto:

```bash
./install.sh --fetch-wheels
```

Eso descarga las dependencias a `wheels/`. Copiá la carpeta `wheels/` al
mini PC y ejecutá allí:

```bash
./install.sh --offline
```

Si las wheels están incompletas y no estás en modo `--offline`, el
instalador avisa y reintenta por internet.

### Modelos vendorizados (offline real)

Los tres modelos `.task` de MediaPipe (Pose Lite, Pose Full y Hand
Landmarker, ~23 MB en total) están en `assets/models/`, con sus URLs y
tamaños documentados en `assets/models/README.md`. En runtime solo se
leen de ahí: **no se descarga nada**. Si faltan, en una máquina con
internet:

```bash
./install.sh --fetch-models
```

El `wheels/` incluido (38 wheels, ~132 MB) es para **linux x86_64 /
Python 3.12**; en otra plataforma usá `./install.sh --fetch-wheels` para
regenerarlo.

## Arranque

```bash
./start.sh
```

Cuando termine de iniciar, abrí en el navegador:

```
http://127.0.0.1:8000
```

Para detenerlo, presioná `Ctrl+C` en la terminal.

Si el puerto ya está ocupado por otra instancia de Exp-brazo, el script lo
detecta y no lanza una segunda: te avisa la URL existente y termina bien.

### Opciones de arranque

```bash
./start.sh --open          # abre el navegador cuando el dashboard responde
./start.sh --mock          # robot simulado, sin brazo físico
./start.sh --no-camera     # arranca sin cámara (modo degradado)
./start.sh --port 8001     # usa otro puerto
./start.sh --host 0.0.0.0  # escucha en otra interfaz de red (requiere --allow-remote)
./start.sh --allow-remote  # expone el dashboard con token obligatorio
./start.sh --help          # muestra todas las opciones
```

### Chequeo previo antes de arrancar

```bash
./start.sh --check
```

Valida Python, dependencias, configuración, comportamientos, cámara y
puerto. No inicia el dashboard. Para ver además el reporte JSON al final:

```bash
./start.sh --check --json
```

Si solo faltan la cámara, OpenCV o MediaPipe, el sistema arranca en **modo
degradado**: el dashboard funciona, pero no hay percepción de gestos. Si
falta una dependencia crítica, la configuración es inválida o el puerto
está ocupado, el script explica qué corregir y no arranca.

### Modo de prueba (sin brazo físico)

```bash
./start.sh --mock
```

El modo mock simula el robot: podés ver la interfaz y el flujo de gestos
sin riesgo. Si tampoco querés usar la cámara:

```bash
./start.sh --mock --no-camera
```

## Seguridad local

El dashboard incluye un **token de acceso** que se genera automáticamente en
cada arranque cuando escucha en loopback. En loopback el token **no se
muestra en consola**: el HTML lo inyecta en un `<meta>` y el frontend lo
usa para todas las llamadas API y para el WebSocket, así que no tenés que
copiarlo a mano. En modo remoto se imprime una única URL de bootstrap en
stdout (nunca se escribe a archivos de log). Las peticiones sin token
reciben `401` y los métodos de escritura con un `Origin` ajeno reciben
`403`.

- Por defecto escucha **solo en `127.0.0.1`**. Si configurás otro host sin
  `--allow-remote`, el arranque se aborta.
- Para exponerlo en la red local usá `--allow-remote` **y** definí
  `EXP_BRAZO_TOKEN` (mínimo 24 caracteres). Generá uno con:

  ```bash
  python3 -c "import secrets;print(secrets.token_urlsafe(32))"
  ```

  ```bash
  EXP_BRAZO_TOKEN="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')" \
    ./start.sh --host 0.0.0.0 --allow-remote
  ```

- En modo remoto, `GET /` exige el token de bootstrap. `start.sh` (y
  `python -m exp_brazo.main --allow-remote`) imprimen una URL como:

  ```text
  http://<host>:<puerto>/?token=<EXP_BRAZO_TOKEN>
  ```

  Abrila en el navegador una vez: el servidor valida el token, sirve el
  dashboard y setea la cookie de sesión `exp_brazo_session`
  (`HttpOnly`, `SameSite=strict`, 12 horas, `Secure` bajo HTTPS). El
  frontend saca el `token` de la barra de direcciones y las siguientes
  peticiones usan la cookie. El token no se escribe a archivos de log.

- **No expongas el dashboard en la Wi-Fi de la feria** si no es
  imprescindible: el token protege la API, pero el paro de emergencia
  físico sigue siendo la única garantía real de seguridad.
- El servidor aplica además `TrustedHost`, cabeceras `nosniff`,
  `X-Frame-Options: DENY`, `Referrer-Policy`, `Permissions-Policy` y una
  CSP local.

## Configuración

- `config/config.yaml`: cámara, percepción, robot, dashboard y supervisor.
- `config/behaviors.yaml`: catálogo de movimientos por gesto.
- `config/defaults/`: copias semilla de ambos archivos.

El instalador restaura desde `config/defaults/` únicamente si falta un
archivo. Tus cambios en `config/config.yaml` y `config/behaviors.yaml` se
conservan.

## Si algo falla

1. Corré el chequeo previo:

   ```bash
   ./start.sh --check
   ```

2. Revisá la sección **Troubleshooting** del dashboard: muestra el estado
   de cámara, configuración, conexión con el robot y tareas de fondo, con
   indicaciones en español.

3. Causas frecuentes:

   - **Python viejo:** se necesita 3.11 o superior. Actualizá Python y
     volvé a ejecutar `./install.sh`.
   - **No se puede crear `.venv`:** en Debian/Ubuntu suele faltar
     `python3-venv` o `python3.12-venv`. Pedí a quien administra el equipo
     que lo instale; los scripts no usan `sudo`.
   - **Faltan dependencias:** ejecutá `./install.sh` (o
     `./install.sh --offline` si tenés `wheels/`).
   - **Cámara no encontrada o índice incorrecto:** revisá `camera.index` en
     `config/config.yaml` y que el cable esté conectado.
   - **Cámara ocupada:** cerrá Zoom, Meet, Cheese u otro programa que la
     esté usando.
   - **Permisos de cámara:** tu usuario debe pertenecer al grupo `video`.
     Pedí a quien administra el equipo que lo agregue y volvé a iniciar
     sesión.
   - **Puerto ocupado:** probá `./start.sh --port 8001` o cerrá el programa
     que usa el puerto.
   - **El Dobot no responde:** el arranque continúa con percepción y
     dashboard. Revisá red, cable e IP en `config/config.yaml`.

## Seguridad

El **paro de emergencia físico** del brazo es la única garantía real de
seguridad. La pausa por software es **complementaria** y puede fallar si se
corta la comunicación. Nunca dejes el robot operando sin supervisión y
mantené siempre el paro de emergencia al alcance.

Los scripts de instalación y arranque no piden `sudo`, no instalan paquetes
del sistema y no abren puertos más allá del dashboard local configurado.

## SIN VALIDAR

Los movimientos, deltas, poses de home, límites y umbrales de gestos de
este repositorio están **SIN VALIDAR** contra el brazo físico. No ejecutes
movimientos reales sin una prueba supervisada y una revisión previa de los
valores.

## Limitaciones conocidas

- **Cámara ~5 FPS**: en el equipo de desarrollo la cámara rinde
  aproximadamente 5 fotogramas por segundo con la Tasks API. Puede ser
  insuficiente para la latencia esperada en feria; verificar en el
  equipo definitivo.
- **Dobot no probado físicamente**: el modo stream y el feedback del
  puerto 30004 son firmware-dependientes (ver `docs/adr/ADR-002`).
- **UI sin validar visualmente**: el dashboard fue verificado por API
  (MJPEG, snapshot, trigger), no en navegador.
- **`wheels/` específicos**: son para **linux x86_64 / Python 3.12**.
- **Los límites mutables solo se achican**: desde la API se pueden
  reducir límites articulares/velocidad/aceleración, nunca ampliarlos
  más allá del envelope físico inmutable.

## Estado de cierre

- 255 tests pasando; ruff limpio; instalación offline con `wheels/` (38 ruedas, 132 MB) y modelos MediaPipe `.task` vendorizados (23 MB).
- Dashboard loopback con token automático. Para LAN: `./start.sh --allow-remote` + `EXP_BRAZO_TOKEN` (mín. 24) y abrir `http://<IP-de-la-notebook>:8000/?token=<token>`.
- Ajuste de host LAN: el servidor incluye el host efectivo de `--host` y las IPs locales detectadas; si usás `0.0.0.0`, averiguá la IP con `hostname -I`.
- Todo movimiento y umbral sigue **SIN VALIDAR** contra el brazo/personas reales. El paro de emergencia físico es la única garantía.
