# KIRO Command Center

One web page that controls the whole KIRO robot (SO-101 dual-arm mobile robot on a mecanum base, Jetson Orin Nano).

The Jetson on the robot runs everything and creates its own Wi-Fi network. One operator opens the page on a laptop or phone and drives. **No internet, no router, nothing to install on the laptop.**

![Command center running in mock mode](docs/img/screenshot.png)

## Status

**Version 0.1.** Driving works in **mock mode** (a pretend robot). It has **not been run on the real robot yet**: the ROS 2 part and the files in `deploy/` are written but untested.

| Part | State |
|---|---|
| Web page: on-screen sticks, gamepad, keyboard | Works (tested in a browser) |
| One operator at a time, with PIN | Works |
| E-STOP and stop-on-lost-connection | Works |
| Mock robot for testing on any computer | Works |
| Real robot (ROS 2, `twist_mux`, hotspot, start at power-on) | Written, **not tested on the robot** |
| Camera, arm buttons, teleop on/off | Next (see the to-do list) |
| Leader arms on the laptop, voice, lidar map | Later |

Plan and checklist: [to-do until 20 Nov](docs/KIRO_Todo_until_20_Nov.docx) · [full project plan](docs/KIRO_Command_Center_Project_Plan.docx)

## Try it on your laptop (2 minutes, no robot)

You need Python 3.10 or newer.

```bash
git clone https://github.com/incom8134-web/kiro-webapp.git
```

```bash
cd kiro-webapp && ./run.sh
```

Open **http://localhost:8080**, type the PIN **0000**, press **Take control**, and drive the pretend robot.

On Windows, use these three commands instead of `./run.sh`:

```bash
python -m venv .venv
```

```bash
.venv\Scripts\pip install -r requirements.txt
```

```bash
.venv\Scripts\python -m robot_server
```

## Controls

| Action | Gamepad | Keyboard | Screen |
|---|---|---|---|
| **Enable** (must be held) | hold LB | hold Space | touch a stick |
| Move | left stick | W A S D | left stick |
| Turn | right stick | Q / E | right stick |
| Turbo | RB | Shift | Turbo switch |
| **E-STOP** | Back / Select | Esc | red button |

If two inputs are used at once, the gamepad wins, then the on-screen sticks, then the keyboard.

## Safety rules built in

| Rule | What happens |
|---|---|
| Dead-man | The robot moves only while LB, a stick or Space is held |
| Lost connection | No message from the operator for 0.5 s: the robot stops |
| E-STOP | Anyone connected can press it. Only the operator can release it |
| After E-STOP or taking control | The controls must be let go once before the robot moves again |
| One operator | Other devices can only watch. Taking over needs the PIN |
| Speed | 0.15 m/s normal, 0.30 m/s turbo. Speed rises smoothly and drops at once |

Software is not enough for public demos. The robot also needs a **physical switch that cuts servo power**.

## How it works

![Architecture](docs/img/architecture.png)

- `robot_server/` is a small Python server (FastAPI). It serves the page, keeps the operator lock and decides 20 times per second which speed is safe to send.
- On the robot it publishes `/cmd_vel_web`. `twist_mux` passes that on to the existing `mecanum_base_node`, which drives the wheel servos.
- The page in `web/` is plain HTML and JavaScript. There is no build step and it loads nothing from the internet.

More detail: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

## Run it on the robot

Step-by-step commands are in **[deploy/README.md](deploy/README.md)**: hotspot, install, `twist_mux`, start at power-on.

## Settings

Set these as environment variables before starting the server.

| Variable | Default | Meaning |
|---|---|---|
| `KIRO_BACKEND` | `auto` | `mock`, `ros`, or `auto` (ROS if it is installed) |
| `KIRO_OPERATOR_PIN` | `0000` in mock mode | **Required** for the real robot. Never put it in git |
| `KIRO_PORT` | `8080` | Port of the web page |
| `KIRO_MAX_VX`, `KIRO_MAX_VY`, `KIRO_MAX_WZ` | `0.15`, `0.15`, `0.8` | Normal speed limits (m/s, m/s, rad/s) |
| `KIRO_TURBO_VX`, `KIRO_TURBO_VY`, `KIRO_TURBO_WZ` | `0.30`, `0.30`, `1.5` | Turbo speed limits |
| `KIRO_HEARTBEAT_TIMEOUT` | `0.5` | Seconds without operator messages before the robot stops |
| `KIRO_CMD_TOPIC` | `cmd_vel_web` | ROS topic for speed commands |

## Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
```

```bash
.venv/bin/python -m pytest
```

## Folders

| Folder | Content |
|---|---|
| `robot_server/` | The server: `control.py` (lock, E-STOP, watchdog), `base.py` (mock and ROS robot), `main.py` (web and WebSocket) |
| `web/` | The page: `index.html`, `style.css`, `app.js` |
| `deploy/` | Files for the Jetson: hotspot script, `twist_mux` config, launch file, start-at-power-on services |
| `docs/` | Plan, to-do list, architecture |
| `tests/` | Automatic tests |

Not in this repo: trained models, datasets, PINs and passwords.
