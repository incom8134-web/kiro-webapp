# Architecture

![Architecture](img/architecture.png)

## Parts

| Part | File | Job |
|---|---|---|
| Page | `web/app.js` | Reads gamepad, on-screen sticks and keyboard. Picks one. Sends it 20 times per second |
| Server | `robot_server/main.py` | Serves the page and one WebSocket at `/ws` |
| Controller | `robot_server/control.py` | Operator lock, PIN, E-STOP, watchdog, speed limits. No network or ROS inside, so it is easy to test |
| Base | `robot_server/base.py` | `MockBase` (pretend robot) or `RosBase` (real robot through ROS 2) |

## From stick to wheel

```
stick / gamepad / keys
   -> browser, 20 messages per second
   -> WebSocket /ws
   -> Controller: is this the operator? is E-STOP off? is the message fresh?
   -> /cmd_vel_raw                      (ROS 2 topic, same as the robot gamepad and voice)
   -> obstacle_guard                    (old code: lidar stop; pass-through when there is no lidar)
   -> /cmd_vel
   -> mecanum_base_node                 (old code: mecanum math, writes servo speeds, 0.5 s timeout)
   -> 4 wheel servos
```

The server sends speeds only while the operator is active. When driving ends it sends a few zeros and then goes silent, so the gamepad on the robot can drive.

## Robot programs

`robot_server/robot.py` starts the old programs from `~/so101` as child processes (`robot_server/jobs.py`). Each job lists the devices it opens. A second job that wants the same arm port or camera is refused. The camera preview lets go of a camera while a skill uses it and takes it back afterwards.

| Page button | Job | Program | Devices |
|---|---|---|---|
| Start base | `base` | `run_base_teleop.sh --inner` (wheel node, lidar, guard, robot gamepad) | `/dev/so101_base`, `/dev/rplidar` |
| Teleop on | `teleop_<side>` | `lerobot-teleoperate` | follower and leader ports |
| Fold / Hold / Release | `arm_<side>` | `arm_tuck.py --tuck / --hold / --release --yes` | follower ports |
| Skill Run | `skill` | `run_policy.sh run <model> 20` | right follower, `cam:top`, `cam:right_wrist` |
| Voice start | `voice` | `ros2 run so101_base voice_cmd` (push-to-talk, `lang:=ko` or `en`) | microphone |
| Leader arm on the laptop, Wave | `bridge` | `twin_bridge.py --arms right --listen 127.0.0.1:5558 --allow-from 127.0.0.1 --ramp 90 --deadman 0.5`. The server converts leader readings to angles (LeRobot's maths, leader calibration file) and sends them by UDP | follower port |
| Check wizard step | `wizard` | `wizard_runner.py <step>`: runs `hw_test_wizard.Wizard.t_<step>` from the old code without a screen | per step (see `WIZARD_USES`) |
| Studio: start recording | `record` | `record_runner.py record <settings>`: the old recorder's set-up (`record_gui.ARMS`, `cams_config`, `cam_tune`), then LeRobot's own `lerobot-record`. Next / again / stop arrive on stdin (the old window pressed → ← Esc on the desktop); force stop = Ctrl+C | follower and leader ports, `cam:<key>` of the chosen cameras |
| Studio: package for upload | `pack` | `pack_dataset.sh <user/name>` (tar.gz, checksum, training notes in `~/Desktop/데이터셋_업로드`) | none |

Not jobs (short, one command each, under the servo-probe lock): servo tester commands (`servo_tool.py`, reusing `so101_servo_tester.Bus`) and quick / typed commands (published on `/voice/say` for the old voice node).

Health (`servo_probe.py`) runs every 6 s as a short separate process with a time limit, because the servo SDK can hang on a stuck USB board.

## Messages on the WebSocket

All messages are JSON with a field `t`.

**Page to server**

| `t` | Fields | Meaning |
|---|---|---|
| `drive` | `x`, `y`, `z` (-1 to 1), `en`, `turbo`, `src` | Speed wish. `en` = dead-man held. Also the heartbeat |
| `take` | `pin`, `force` | Become the operator |
| `release` | | Give up control |
| `estop` | `on` | Anyone may send `true`. Only the operator may send `false` |
| `ping` | `ts` | Measures the link delay |
| `base` | `on` | Start or stop the wheel stack (operator) |
| `teleop` | `side` (`right`, `left`, `both`), `on` | Leader to follower (operator) |
| `arm` | `side`, `action` (`fold`, `hold`, `release`) | Arm pose (operator) |
| `skill` | `name`, `on` | Run or stop a trained model (operator) |
| `voice` | `on` | Start or stop the voice node (operator) |
| `talk` | `on` | Hold-to-talk. Publishes `/voice/listen` (operator) |
| `system` | `action` (`shutdown`, `reboot`) | Jetson power (operator) |
| `leader` | `on`, `side` | Start or stop the arm bridge for the leader arm on the laptop (operator) |
| `leader_pos` | `side`, `raw` (6 servo positions 0-4095) | Leader arm readings, about 30 per second (operator) |
| `wave` | `side` | Small wave from the current pose (operator) |
| `say` | `text` | Quick or typed command for the voice node (operator) |
| `engineer` | `pin` or `lock` | Unlock / lock the engineer tools, the Studio page (operator) |
| `studio` | `cmd`: `hello`, `check` + `settings`, `start` + `settings`, `next` / `redo` (+ `ep`, `phase` the page saw), `stop`, `kill`, `pack` + `repo` | Recording datasets (engineer). A command for an episode or phase that is already over is ignored |
| `guard_profile` | `profile` (`close`, `safe`) | Lidar guard distance used the next time the base starts (engineer) |
| `servo` | `cmd` (`scan`, `torque`, `move`, `setid`), `port`, `id`, `value`, `lo`, `hi`, `arm_ok` | Servo tester (engineer) |
| `wizard` | `step`, `on`, `seconds` | Run or stop one check step (engineer) |
| `wizard_decide` | `step`, `status` (`pass`, `fail`, `skip`) | Person's verdict (engineer) |

**Server to page**

| `t` | Fields | Meaning |
|---|---|---|
| `state` | `you`, `operator`, `estop`, `link_ok`, `need_neutral`, `src`, `cmd`, `odom`, `backend`, `clients` | Sent 10 times per second |
| `robot` | `jobs`, `health`, `cams`, `models`, `voice`, `events`, `left_arm_ok`, `lidar` | Once per second. Health, voice, events and stops are empty until the connection is signed in |
| `wizard_steps` | `steps` | The 13 steps of the old wizard, sent after unlocking |
| `studio` | `session`, `datasets`, `check`, `pack`, `cams`, `ports`, `servo_scan`, `wizard`, `guard_profile`, `info`, `token`, `now` | Once per second and after each Studio command, **only to the unlocked engineer**. The page uses the session cookie for downloads and camera previews. `token` is accepted only as the `X-Studio-Token` header, never in the URL |
| `confirm` | `action`, `ok`, `token` | One-use token for shutdown, reboot, torque release and a servo ID change |
| `scan` | `xy` (flat list of x, y in cm, robot frame), `src` (`lidar` or `mock`) | 5 times per second, only while a scan is fresh |
| `note` | `ok`, `msg`, `about` | Answer to a request |
| `pong` | `ts` | Answer to `ping` |

Axes: `x` forward, `y` left, `z` turning left. The same directions ROS uses.

## ROS 2 topics

| Topic | Type | Who publishes |
|---|---|---|
| `/cmd_vel_raw` | `geometry_msgs/Twist` | Robot server, robot gamepad (`teleop_twist_joy`), voice node |
| `/cmd_vel` | `geometry_msgs/Twist` | `obstacle_guard` |
| `/odom` | `nav_msgs/Odometry` | `mecanum_base_node`, read by the server for the map |
| `/e_stop` | `std_msgs/Bool` | Robot server, 5 times per second (nobody reads it yet) |
| `/voice/listen`, `/voice/say` | `std_msgs/String` | Robot server (hold-to-talk, stop word on E-STOP) |
| `/voice/text`, `/voice/status`, `/obstacle_guard/status` | `std_msgs/String` | Old nodes, shown on the page |

## Pages

| Page | Job |
|---|---|
| `/` | Control: drive, arms, skills, voice, health |
| `/dashboard.html` | Read-only stats for a big screen. Charts keep the last 2 minutes in the browser |

The server answers on HTTPS only (port 8080, self-made certificate unless `KIRO_TLS_CERT` and `KIRO_TLS_KEY` are set). The browser warns once. Web Serial (leader arm) and the microphone use that same address.

## Camera

`GET /camera/<key>.mjpg` streams 480x360 JPEG at 10 fps (about 2 Mbit/s) to a signed-in browser. Devices come from the old `cams_config.load()`. A camera is open only while someone watches.
