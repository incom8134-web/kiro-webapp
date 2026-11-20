# Handover (18 November 2026)

> **Update 18 Nov, evening (session on the Jetson).** Version 0.2 now runs on the Jetson from `~/Desktop/kiro-webapp`. New page (Swiss style, English / Korean), live camera, and buttons for base, arms, teleop, skill, voice, health and shutdown, all starting the old scripts in `~/so101`. Web driving now goes to `/cmd_vel_raw` through the old lidar guard, so `twist_mux` was dropped. Version 0.5 (19 Nov) simplifies the page, shows the two hand cameras under the main camera, fixes keyboard driving (movement keys alone, arrows too), adds the leader arm on the laptop (Web Serial over https 8443, through twin_bridge) and a Wave button. Version 0.4 adds base quick commands, a guard-distance choice, the engineer tools (servo tester, 13-step check wizard, own PIN), and fixes Ctrl+C so the server always stops the programs it started. Not yet done: data recording (skipped for now on the user's request), moving wheels or arms from the page (power was off), GitHub backup of `~/so101` (needs a private repo and git login on the Jetson), laptop remote arm control, English voice. Sections below are the original cloud handover; section 4 is out of date.

> **Update 20 Nov, training loop in the Studio (branch `claude/colab-training-a49c01`).** The Studio page has a new section "Train a skill": package a dataset and download it (.tar.gz + .sha256), put it in Google Drive, open the training notebook (`colab/so101_train_colab.ipynb`, LeRobot 0.6.1 ACT, 20,000 steps) in Google Colab, download the model zip, upload it in the Studio (checked, then installed as `~/policies/<name>/pretrained_model`), play it from Autonomous skills. "Training files" lists datasets, packages and models with download and delete (hold to confirm). Models trained on both arms or the left arm are listed but cannot be played. The old notebook's cell 3 was a SyntaxError; the new one fixes it. Mock mode now keeps its models in `~/.cache/kiro-webapp/mock_policies`. Details and what still needs a real test: `docs/TRAINING.md`.
>
> **Update 20 Nov, one leader records both arms.** There is no left leader arm. Studio's recorder now lets the silver arm (the right leader) drive the green arm alone or both arms: `record_runner.py` wraps LeRobot's leader (`leadermath.one_leader_action`, LeRobot 0.6 values are degrees) and applies `follow_offsets.json` (green wrist +123 deg). For both arms it first copies `left_follower.json` / `right_follower.json` to LeRobot's two-arm files `so101_left.json` / `so101_right.json` (old ones kept as `so101_*_before_*.json`). Before every recording the motor targets are synced (`servo_sync.py`). Playing a two-arm skill is not built yet (the skill runner drives the right arm only).
>
> **Update 20 Nov, E-STOP (user's decision).** E-STOP now also kills teleop (before, teleop stayed), freezes the arms (arm_tuck --hold), stops the wheel program and then checks every wheel's speed (wheel_stop.py; a wheel still turning is switched off). About 2 s later both arms fold home by themselves, slowly (arm_tuck --tuck; the left arm only when allowed). Pressing E-STOP again while they wait or fold cancels it and they stay frozen. `KIRO_ESTOP_FOLD_S=0` turns the auto-fold off. A physical 12 V stop switch is still recommended.
>
> **Update 20 Nov, Studio page (merged into main on 20 Nov at the user's request).** New page `studio.html` ("Studio" / "스튜디오", lock icon in the top bar of both pages) for recording demonstration datasets, protected by the engineer PIN. The engineer tools (guard distance, servo tester, check wizard) moved there from the control page; the main pages no longer ask for the engineer PIN. Recording uses the old recorder: LeRobot's `lerobot-record` set up exactly like `~/so101/record_gui.py`, started as the job `record` through `robot_server/record_runner.py`, which hands LeRobot the page's next / again / stop instead of desktop key presses. Four steps: dataset (new or add to existing, never overwrites; a copy is made before adding), cameras and checks, record, review (counts read back from the dataset) with the old `pack_dataset.sh`. Tested only with the pretend robot (`KIRO_SO101_DIR=tests/fake_so101`); **never recorded on the real robot yet**. E-STOP kills a running recording like teleop (the arms are held, then fold home); the recorder cannot finish the dataset then, so that recording's episodes may be missing. A dataset that was added to keeps its copy from before in `~/.cache/kiro-webapp/dataset_backups`.
>
> **Update 20 Nov, wrist offset.** The silver leader and the black arm have full-circle wrist calibrations (old style); the green arm's is its real range, so the same folded wrist reads -30 deg on the leader and +93 deg on the green arm. The both-arm copy adds the per-arm offsets in `~/.config/kiro-webapp/follow_offsets.json` (now `{"left": {"wrist_flex": 123}}`). Do not recalibrate the black arm or the leader before the demo: the AI skills were trained with their current calibration.
>
> **Update 20 Nov, afternoon.** Left (green) arm calibrated again with every joint moved fully (the 5 Nov file had the elbow range 6 steps wide; the 12 Oct file no longer fitted the elbow, so the motor was probably re-mounted). New ranges match the right arm (shoulder 2380, elbow 2239, gripper 1510); at rest it reads shoulder -103 deg, elbow +97 deg. Old files are kept next to it (`left_follower_before_recalib_*.json`, `backup_2026-11-05/`). The fold pose was taught again (`arm_tuck.py --teach --arm left`). Start the server with `KIRO_LEFT_ARM_OK=1` to use the left arm. Note: `lerobot-calibrate` switches torque on before calibrating; with stale goal registers the arm can move, so the calibration was done with a script that keeps torque off.
>
> **Update 20 Nov.** The user did not like the lime/lilac look from the 19 Nov evening session, so both pages are back to the neutral black-and-white look (`web/style.css` only; `console.css`, `dashboard.css` and the Geist Mono font were removed). Kept from that session: the server's obstacle stop (`robot_server/guard.py` and its switch on the Drive tab) and the phone top-bar fixes. The dashboard shows 2 x 2 live boxes plus the live server log; a button pauses the camera streams.
>
> **Update 19 Nov, evening (cloud session, could not reach the robot).** New look for both pages (rounded dark tiles, lime and lilac, Geist Mono numbers, all stored offline): `web/dashboard.css`, `web/console.css`. The dashboard was rebuilt (camera block on top, then tiles). The web server got its own obstacle stop (`robot_server/guard.py`, 71 tests pass, tried only with the pretend robot). The phone page now has a visible Dashboard button. Two Claude Code skills were added in `.claude/skills/`. **Not done:** nothing here was run on the real robot; voice still uses the Jetson's microphone, not the phone's; the leader arm still needs a laptop with Chrome or Edge. Sections 4 and 7 below are out of date.

Written by a Claude session that ran in the cloud and could not reach the robot. It is for the next session, which should run **on the Jetson**. Everything here was agreed with the user unless marked "proposed" or "open".

## 1. The goal

| | |
|---|---|
| What | A standalone, wireless robot controlled from one web page |
| Deadline | Demo on **20 Nov 2026** |
| Rules | Works offline. No router. Laptop or phone needs only a browser. One operator at a time. Zero cost |
| User | New owner of the project, beginner in networking. Wants to discuss and decide before building |

## 2. What the cloud session never saw

The newest robot code exists **only on the Jetson**. The cloud session read an 11 October copy from Google Drive and five screenshots taken on 18 November. Treat every statement below about the local app as "from screenshots" until you have read the code.

## 3. Decisions made

| Topic | Decision |
|---|---|
| Network | Jetson creates its own hotspot ("haebotwifi", renamed from GeminiBOT on 20 Nov 2026). Page address `http://10.42.0.1:8080` |
| Tailscale | Not used. Optional later for remote work from home |
| SSH | Only a tool for setup and fixing. Already enabled on the Jetson on 18 Nov |
| Operator device | Browser only. No ROS or Python on the laptop |
| Driving inputs | On-screen sticks, laptop gamepad (hold LB), keyboard (hold Space, W A S D, Q E). All three stay |
| Voice | On-screen hold-to-talk button. English and Korean. Runs on the Jetson, offline |
| Arms for now | Leader arms plugged into the robot by USB. Wireless leader arms later |
| Skills | Trained ACT models run on the Jetson |
| Web page tech | Plain HTML and JavaScript, no build step, no CDN |
| Scope of the web app | **All** functions of the old local app move to the web app, including servo tester and calibration (a developer sits next to the robot) |
| Old code | Reuse its logic. Do not rewrite what already works on the robot |
| Language | The page gets an English / Korean switch |

## 4. State of this repo (version 0.1)

| Part | State |
|---|---|
| Driving page, operator lock with PIN, E-STOP, heartbeat stop | Works in mock mode. 18 automatic tests pass. Checked in a real browser |
| `RosBase`, `deploy/twist_mux.yaml`, `deploy/kiro_base.launch.py`, hotspot script, systemd units | **Written, never run on the robot.** Expect fixes |
| Camera, arm buttons, teleop, skills, voice, health, shutdown | Not built |

The user has seen the mock version and liked the idea.

## 5. The old local app on the Jetson

Window title "SO-101 테스트 통합 UI", a Tkinter program with eight tabs. From screenshots:

| Tab | What it does |
|---|---|
| 팔 제어판 | Teleop (left, right, both, bimanual), calibration, fold / hold / release / save pose |
| 서보 테스터 | Scan servos, change IDs, move one servo |
| 카메라 테스트 | View cameras |
| 데이터 기록 | Record LeRobot datasets. Arm choice (right, left, bimanual). Cameras from `config/cameras.json`: `top` (Gemini 335 colour), `right_wrist`, `left_wrist`. H.264 or AV1 |
| 베이스 조종 | Start the base stack. Gamepad. **Voice driving in Korean** (push-to-talk, fixed steps of 0.2 m and 45°, confidence limit, wake word). **Lidar obstacle guard** (front, left, right, back). Quick-command buttons and typed commands |
| 모드 테스터 | Switch between record mode and drive mode with pre-checks |
| 검증 마법사 | 13-step hardware check with report |
| 노트북 원격 제어 | A laptop controls a follower arm over the network (sends on 5005, receives on 5558, "gemini format"), with ramp, speed and dead-man settings |

Seen in the screenshots:

- Mode tester log shows a crash: `min() iterable argument is empty` (likely when no follower voltage can be read).
- Only 2 of 4 arm boards were connected.
- `right_wrist` and `left_wrist` cameras were not plugged in. `top` was found.

Was it "vibe coded"? It was built with Claude Code and tested on the real robot, with dated measurements in its notes. The logic is worth keeping. It is a developer test tool, not a product screen.

## 6. First jobs for the session on the Jetson

Do these in order. Ask the user before each step that changes something.

| # | Job | Notes |
|---|---|---|
| 1 | Find the old code | Probably `~/so101`. Read its `CLAUDE.md` first |
| 2 | Check it | `git status`, `git remote -v`, size, and search for keys and passwords |
| 3 | Ask the user to make the GitHub repo private | Repo Settings, General, Danger Zone, Change visibility |
| 4 | Back the old code up to GitHub | For example as branch `jetson-legacy` of `kiro-webapp`, or a separate private repo. Set up git login on the Jetson together with the user. No models, datasets, videos or venvs |
| 5 | Bring up version 0.1 on the robot | Follow `deploy/README.md`: mock test over the hotspot, then `twist_mux`, then robot mode with wheels on blocks, then the floor test. Fix what breaks and push the fixes |
| 6 | Map old functions to web modules | For each tab in section 5, find the function or script to reuse |

Creating the hotspot takes over the Wi-Fi card, so the Jetson loses internet. Do the GitHub work first, or use an Ethernet cable.

## 7. UI redesign (agreed direction)

The user finds the current page generic. The agreed direction follows the "Swiss" style from the frontend-design skill at `github.com/Ilm-Alan/frontend-design` (a style guide, read it before designing).

| Aspect | Rule |
|---|---|
| Surface | White or `#F7F7F8`. Thin 1 px lines or a visible grid. No shadows, no rounded card stack |
| Type | One sans-serif family for everything. Left-aligned. Numbers as layout elements. Fonts must be system fonts or bundled in the repo (offline) |
| Colour | One accent (proposed: blue `#002FA7`). **Red is used only for E-STOP** |
| Content | Only real information. No fake numbers, no filler labels, no "coming soon" tiles. An empty panel stays empty |
| Language | English / Korean switch in the top bar. All strings in one dictionary. Choice is remembered |
| Voice | On-screen hold-to-talk button, also Y on the gamepad |
| Safety | E-STOP always visible and large. Big touch targets on phones |

Layout: the camera is the main view. The map is a small inset. The sticks sit over the lower corners of the camera view. A right-hand rail holds Mode, Arms, Skills, Voice and Health.

```
+-----------------------------------------------+------------------+
| KIRO   ROBOT   Link 12 ms   You are driving   |     [ E-STOP ]   |
+-----------------------------------------------+------------------+
|                                               | Mode             |
|              CAMERA (live)                    | Arms             |
|   [map]                          speed        | Skills           |
|  (move stick)                  (turn stick)   | Voice [hold]     |
|                                               | Health           |
+-----------------------------------------------+------------------+
```

## 8. Features to add, in the agreed order

| # | Feature | How |
|---|---|---|
| 1 | New UI and camera view | Small preview stream from the Jetson. Only one program can open a camera at a time |
| 2 | Arm buttons: fold, hold, release | Reuse the old fold script |
| 3 | Teleop on / off | Start and stop the existing LeRobot teleop command |
| 4 | Health and shutdown | Servo voltage, temperature, current. Shut down and restart buttons |
| 5 | Run a skill | Model `right_arm_task2_act`: ACT, right arm, cameras `top` and `right_wrist` at 640x480, trained 20,000 steps. The 206 MB file is in the project Google Drive, not in git |
| 6 | Voice | Reuse the old voice code and obstacle guard. Add English |
| 7 | Laptop remote arm control | Reuse the old network receiver. A browser version (Web Serial) can come later |
| 8 | Recording, check wizard, servo tester, calibration | After the demo unless the user says otherwise |

Camera conflict to remember: the Orbbec ROS driver and LeRobot cannot open the Gemini 335 at the same time.

## 9. Security (proposed, waiting for the user's answers)

| Layer | Proposal |
|---|---|
| Wi-Fi | Password on the hotspot |
| Roles | Viewer (watch, press E-STOP), Operator (PIN: drive, arms, skills, voice), Engineer (longer password: servo tester, calibration, settings, shutdown) |
| PIN storage | Hashed, in a root-only file on the Jetson. Never in git |
| Sessions | Auto-logout after a few idle minutes |
| Dangerous actions | Hold-to-confirm for release torque, change servo ID, shut down |
| Log | Who took control, every E-STOP, every dangerous action |
| HTTPS | Self-signed certificate. Also lets the microphone work without a browser flag |
| Hardware | A physical switch that cuts servo power |

Open questions: three roles or two? May a viewer watch without logging in? May a viewer press E-STOP (recommended: yes)? HTTPS now or after the demo?

## 10. Other open questions

| Question | Why it matters |
|---|---|
| What exactly must be shown on 20 Nov, and to whom? | Sets the order of work |
| Is voice a must for 20 Nov? | It needs the most testing |
| Is there a physical servo-power switch on the robot? | Needed before public demos |

## 11. Documents

| File | Content |
|---|---|
| `docs/KIRO_Todo_until_20_Nov.docx` | Two-page to-do list for the deadline |
| `docs/KIRO_Command_Center_Project_Plan.docx` | Full plan with milestones M0 to M5 |
| `docs/ARCHITECTURE.md` | Message formats and ROS topics |
| `deploy/README.md` | Step-by-step robot setup |
