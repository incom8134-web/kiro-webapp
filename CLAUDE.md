# KIRO Command Center

Web command center for the KIRO robot: an SO-101 dual-arm mobile robot on a mecanum base with a Jetson Orin Nano. One operator controls the whole robot from a browser over the Jetson's own Wi-Fi hotspot. Offline, no router, nothing installed on the laptop, zero cost.

**Read `docs/HANDOVER.md` before doing anything.** It holds the decisions, the current state and the next jobs.

## Working with the user

- The user took this project over from another developer and is a beginner in networking and Linux. Use plain words. Give one step at a time. Put commands in their own code blocks. Use tables.
- The user wants to **talk and decide before anything is built**. Propose, wait for a yes, then build.
- Deadline: a working demo on **20 November 2026**.
- Steps that need `sudo` (apt, systemd, NetworkManager, udev) are run by the user. Give the command and explain it.

## Safety

- This is a real robot. Never start anything that moves wheels or arms without telling the user first and getting a yes.
- First wheel tests are done with the robot lifted on blocks. Arms can fall when torque is switched off.
- Never weaken the safety rules in `robot_server/control.py` (dead-man, heartbeat stop, E-STOP latch, one operator) or the obstacle stop in `robot_server/guard.py` without the user agreeing.

## Secrets and big files

- The GitHub repo was **public** on 18 Nov 2026. Never commit PINs, passwords, API keys, Wi-Fi passwords, usernames or device serial numbers.
- Before pushing any old code, search it for keys (`AIza`, `API_KEY`, `password`).
- Models, datasets, videos and virtual environments never go into git (GitHub rejects files over 100 MB).

## Run and test

```bash
./run.sh            # mock robot, http://localhost:8080, PIN 0000
./run.sh robot      # real robot, needs ROS 2; asks for the PINs
.venv/bin/pip install -r requirements-dev.txt && .venv/bin/python -m pytest
```

## Layout

| Path | Content |
|---|---|
| `robot_server/control.py` | Operator lock, PIN, E-STOP, watchdog, speed limits. Pure logic with tests |
| `robot_server/stopmeter.py` | Stop meter for the test forms: each stop's time to command zero, time to still, distance and creep (System tab, `/api/stops.csv`). Pure logic with tests. Procedure: `docs/TESTING.md` |
| `robot_server/guard.py` | Obstacle stop of the web server: lidar points limit the speed in each direction (stops 20 cm before an obstacle; turning and backing away stay free). Pure logic with tests. On by default, the operator can switch it off on the Drive tab, `KIRO_GUARD=0` starts with it off |
| `robot_server/lidar_setup.py` | Studio lidar setup: mounting angle and the robot's own parts the lidar sees (self mask), saved to `~/so101/config/lidar.env` (`LASER_YAW`, `LIDAR_MASK`, `OBSTACLE_STOP=1`). Pure logic with tests. Nothing moves |
| `robot_server/base.py` | `MockBase` and `RosBase` (publishes `/cmd_vel_raw` into the old lidar guard, `/e_stop`, voice topics; reads `/odom`) |
| `robot_server/robot.py` | Arms, teleop, skills, voice, health, shutdown. Starts the OLD programs in `~/so101` |
| `robot_server/jobs.py` | Child processes with device locks (one program per arm port or camera) |
| `robot_server/camera.py` | MJPEG preview, device lookup through the old `cams_config.py` |
| `robot_server/leadermath.py` | Leader arm on the laptop: leader servo positions -> follower targets (the old `twin_bridge.py` moves the arm with its limits). Each leader moves the arm of its own side. The one-leader-for-both-arms copy was removed on 23 Nov 2026 (user's choice): the right leader drives the right arm, the left leader the left arm (teleop and Studio recording) |
| `robot_server/servo_sync.py` | Before the twin bridge switches arm motors on: every target = the motor's current position (after power-up the targets are 0, and a recalibration makes old targets wrong: the arm would flip). Refuses to start if a motor does not answer |
| `robot_server/wheel_stop.py` | E-STOP wheel check after the wheel program stopped: a wheel still turning gets speed 0 and torque off. Never starts a wheel |
| `robot_server/servo_probe.py` | Read-only servo voltage / temperature / current, run as a short subprocess |
| `robot_server/servo_tool.py` | Servo tester commands, reusing the old `so101_servo_tester.Bus` |
| `robot_server/wizard_runner.py` | Runs one step of the old `hw_test_wizard.py` without a screen |
| `robot_server/recorder.py`, `record_runner.py` | Studio page: records demonstration datasets with the old recorder (LeRobot's `lerobot-record`, set up like `~/so101/record_gui.py`), checks before recording, next / again / stop / force stop, a copy before adding episodes, `pack_dataset.sh` for upload |
| `robot_server/training.py` | Studio training files: dataset packages and trained models (arm, steps), checks and installs a model zip uploaded from the browser, model zip download, delete only inside its own folder. Two-arm / left-arm models cannot be played. See `docs/TRAINING.md` |
| `robot_server/colab.py` | Studio "Train in Colab": the robot (internet through its LAN cable) sends the dataset to the Colab notebook's temporary Cloudflare address and fetches the trained model back, then installs it through `training.py`. Outgoing calls only, to `*.trycloudflare.com` |
| `robot_server/main.py` | FastAPI app, WebSocket `/ws`, `/camera/<key>.mjpg`, serves `web/`, Studio file downloads and the model upload (engineer token) |
| `web/studio.html`, `studio.js` | Studio page (lock icon in the top bars): recording training data, training a skill in Google Colab (package and download, model upload, training files), the engineer tools (guard distance, servo tester, check wizard) and the read-only engineer information. Locked until the operator unlocks it with the engineer PIN |
| `colab/make_notebook.py` | Writes `colab/so101_train_colab.ipynb`, the Google Colab training notebook the Studio opens (run it after a change; a test checks they match) |
| `tests/fake_so101/` | Pretend camera list for mock mode: `KIRO_SO101_DIR=tests/fake_so101 ./run.sh` shows test pictures and never opens a real camera |
| `web/` | Plain HTML, CSS, JavaScript. `style.css` holds the look of both pages (colour tokens at the top). `i18n.js` holds every string in English and Korean, `feetech.js` reads the leader arm over Web Serial (read only). No build step, no CDN |
| `deploy/` | Hotspot script, systemd unit, shutdown sudoers rule, robot setup steps |
| `docs/` | Handover, architecture, plan, to-do list |

## Conventions

- Keep the page free of build tools. Libraries come from cdnjs but are **stored in `web/vendor/`** (the hotspot has no internet); see `web/vendor/README.md` for versions and checksums.
- Every string shown in the page must exist in English and Korean (`web/i18n.js`).
- Look (chosen by the user 20 Nov 2026, after trying a lime/lilac look): quiet and neutral. Near-black or white surfaces, **one blue accent**, Inter for all text, flat controls, no gradients or glows, a **dark and a light theme** (tokens at the top of `web/style.css`, `web/theme.js`). Blue = interactive, green = live, amber = needs attention, red only for E-STOP. Icons: Remix Icon. Charts: Chart.js, colours from the `--series` palette. Dashboard: 2 x 2 live boxes (main camera, lidar, both hands) with the live server log in a terminal window on the right, then stats and charts. Tools on the control page live in the dock's four tabs.
- Add a test for every change to the control logic.
- The repo on the Jetson is `~/Desktop/kiro-webapp`. The old code is `~/so101` (its own git repo, branch `master`), backed up to the **private** GitHub repo `incom8134-web/so101-jetson` (all branches, 19 Nov 2026). The Jetson is logged in to GitHub with `gh` (device login).
- Claude Code skills live in `.claude/skills/` (`impeccable` for design work, `skill-test-skill`).
- Never re-implement what the old code does. Start its scripts as jobs (`robot_server/robot.py`).
- The old robot notes on the Jetson (`CLAUDE.md` in the old code folder, probably `~/so101`) are the source of truth for hardware facts: device names, servo IDs, voltages, known problems.
