# Handover (18 November 2026)

> **Update 18 Nov, evening (session on the Jetson).** Version 0.2 now runs on the Jetson from `~/Desktop/kiro-webapp`. New page (Swiss style, English / Korean), live camera, and buttons for base, arms, teleop, skill, voice, health and shutdown, all starting the old scripts in `~/so101`. Web driving now goes to `/cmd_vel_raw` through the old lidar guard, so `twist_mux` was dropped. Version 0.4 adds base quick commands, a guard-distance choice, the engineer tools (servo tester, 13-step check wizard, own PIN), and fixes Ctrl+C so the server always stops the programs it started. Not yet done: data recording (skipped for now on the user's request), moving wheels or arms from the page (power was off), GitHub backup of `~/so101` (needs a private repo and git login on the Jetson), laptop remote arm control, English voice. Sections below are the original cloud handover; section 4 is out of date.

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
| Network | Jetson creates its own hotspot ("GeminiBOT"). Page address `http://10.42.0.1:8080` |
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
