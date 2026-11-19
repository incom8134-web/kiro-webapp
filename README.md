# KIRO Command Center

One web page that controls the whole KIRO robot (SO-101 dual-arm mobile robot on a mecanum base, Jetson Orin Nano).

The Jetson on the robot runs everything and creates its own Wi-Fi network. One operator opens the page on a laptop or phone and drives. **No internet, no router, nothing to install on the laptop.**

![Command center running in mock mode](docs/img/screenshot.png)

## Status

**Version 0.5 (19 Nov 2026).** Runs on the Jetson. Checked on the robot: page over the network, live camera, robot mode connected to ROS 2, read-only servo check. **Wheels and arms have not been moved from the page yet** (12 V and 5 V power were off).

| Part | State |
|---|---|
| Driving: on-screen sticks, gamepad, keyboard (W A S D or arrows, no extra key), one operator with PIN, E-STOP, heartbeat stop | Works. 31 automatic tests |
| Camera-first console (dark): video fills the screen with map, speeds and sticks on it, hand cameras below, tools in four tabs (Drive, Arms, Voice, System). Works on phones | Works |
| Base shows when the wheels do not answer (12 V off) | Works |
| Leader arm on the laptop (USB, Chrome or Edge, https page): plug in, press one button, the right arm follows | Built on the old twin_bridge. Protocol code checked; not yet tried with a real leader arm |
| Wave button: the right arm waves its wrist and gripper from where it is, then holds | Built on the old twin_bridge. Not yet tried on the arm |
| Swiss-style page, English / Korean switch, phone layout | Works |
| Live camera (Gemini 335 `top`, wrist webcams when plugged in, MJPG 320x240) | Top works on the robot. Hand cameras were not plugged in on 19 Nov |
| Base start / stop (old launcher with lidar guard) | Wired, needs 12 V wheel power to test |
| Arms: fold, hold, release, teleop right / left / both | Wired to the old scripts. Left arm blocked until recalibrated |
| Run a skill (`right_arm_task2_act`) | Wired to `run_policy.sh`. Needs the `right_wrist` webcam |
| Voice: hold-to-talk (button, V key, gamepad Y), Korean and English (follows the page language) | Wired to the old `voice_cmd`. Korean and English Vosk models installed in `~/models/vosk`; English checked on the robot 19 Nov |
| Lidar points on the map (`/scan`, same mounting angle as the obstacle guard) | Works with the simulated room in mock mode. Lidar not plugged in yet |
| Dashboard page `/dashboard.html`: camera, lidar, speed, distance, nearest object, Jetson, servos, programs, activity | Works. Read-only, for a big screen |
| Health: Jetson, servo voltage / temperature / current, cameras. Shutdown / restart | Works (shutdown needs `deploy/kiro-shutdown.sudoers`) |
| Base extras: quick-command pad, typed commands, guard distance (close / safe) | Works (commands go through the old voice node) |
| Engineer tools (own PIN): servo tester (scan, torque, move, change ID) and the 13-step check wizard | Works. Scan and wizard step 1 checked on the robot. Arm ports read-only unless ticked |
| Data recording, calibration, laptop remote arm control, camera tuning, save fold pose | Not in the page yet. Use the old app |

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
| **Enable** (must be held) | hold LB | hold a movement key | touch a stick |
| Move | left stick | W A S D or arrow keys (holding the key is the safety hold) | left stick |
| Turn | right stick | Q / E or left / right arrows | right stick |
| Turbo | RB | Shift | Turbo switch |
| Talk (voice on) | hold Y | hold V | Hold to talk button |
| **E-STOP** | Back / Select | Esc | red button |

If two inputs are used at once, the gamepad wins, then the on-screen sticks, then the keyboard.

## Safety rules built in

| Rule | What happens |
|---|---|
| Dead-man | The robot moves only while LB, a stick or Space is held |
| Lost connection | No message from the operator for 0.5 s: the robot stops |
| E-STOP | Anyone connected can press it. Only the operator can release it |
| After E-STOP or taking control | The controls must be let go once before the robot moves again |
| One operator | Other devices can only watch. Taking over needs the PIN. 5 wrong PINs lock that device address for 30 s, also across reconnects |
| Other websites | The robot connection refuses pages from any other site (Origin check) |
| Slow devices | A page that stops reading is dropped after 0.3 s, so it can never stall the 20 Hz control loop |
| Speed | 0.15 m/s normal, 0.30 m/s turbo. Speed rises smoothly and drops at once |
| Lidar guard | Web commands go to `/cmd_vel_raw`, through the old obstacle guard, like the robot gamepad and voice |
| E-STOP also stops | Every press: arm programs (skill, leader arm, wave, fold) are killed at once and the arm is held where it is (their own clean-up would move it); voice, check wizard and base stop together (wheels: speed 0, torque off; the robot's gamepad cannot keep driving). Also when E-STOP comes while a program is still starting. Teleop keeps running: a person holds the leader arm |
| Devices | Two programs never open the same arm port or camera. The servo check skips busy ports |
| Dangerous buttons | Release torque, shut down and restart must be held for 2 s |
| Left arm | Blocked until recalibrated (`KIRO_LEFT_ARM_OK=1`) |
| Engineer tools | Need operator control **and** a second PIN (`KIRO_ENGINEER_PIN`). Off on the robot until that is set |
| Server stop | Ctrl+C, service stop or closing its terminal first stops every program the server started |

Software is not enough for public demos. The robot also needs a **physical switch that cuts servo power**.

## How it works

![Architecture](docs/img/architecture.png)

- `robot_server/` is a small Python server (FastAPI). It serves the page, keeps the operator lock and decides 20 times per second which speed is safe to send.
- On the robot it publishes `/cmd_vel_raw`. The old lidar `obstacle_guard` passes that on to `/cmd_vel` and the existing `mecanum_base_node`, which drives the wheel servos (same path as the robot gamepad and voice).
- The page in `web/` is plain HTML and JavaScript. There is no build step and it loads nothing from the internet.

More detail: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

## Run it on the robot

Step-by-step commands are in **[deploy/README.md](deploy/README.md)**: hotspot, install, robot test, leader arm over https, start at power-on.

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
| `KIRO_CMD_TOPIC` | `cmd_vel_raw` | ROS topic for speed commands (into the lidar guard) |

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
| `deploy/` | Files for the Jetson: hotspot script, start-at-power-on service, shutdown permission |
| `docs/` | Plan, to-do list, architecture |
| `tests/` | Automatic tests |

Not in this repo: trained models, datasets, PINs and passwords.
