# Run the command center on the robot

These steps are for the Jetson. Do them in order and stop at the first step that fails. Steps marked **sudo** ask for your password.

State on 18 Nov 2026: steps 1 and 4 were checked on the robot (mock page with live camera, robot mode connected to ROS). The hotspot profile `kiro-ap` already exists. Steps 5 to 7 are not done yet.

## 1. Get the code and the Python environment

The repo lives in `~/Desktop/kiro-webapp`. Everything it starts comes from the old code in `~/so101`.

```bash
cd ~/Desktop/kiro-webapp && python3 -m venv --system-site-packages .venv && .venv/bin/pip install -r requirements.txt
```

## 2. The robot's own Wi-Fi

The Wi-Fi card supports hotspot mode (`AP`), and the profile `kiro-ap` (network **GeminiBOT**) already exists. **Turning it on ends the internet connection**, so push to GitHub first.

Turn the hotspot on:

```bash
nmcli con up kiro-ap
```

Back to your normal Wi-Fi (internet). `nmcli con show` lists the names:

```bash
nmcli con up "YOUR-WIFI-NAME"
```

To create the hotspot again with a new password: `bash deploy/setup_hotspot.sh` (**sudo**). If 5 GHz fails: `BAND=bg CHANNEL=6 bash deploy/setup_hotspot.sh`.

`kiro-ap` has autoconnect priority 100, so after a restart the Jetson may come up as the hotspot instead of on your normal Wi-Fi.

## 3. SSH

Already on. From a laptop on GeminiBOT: `ssh YOUR-USER@10.42.0.1`.

## 4. Try it: pretend robot

```bash
cd ~/Desktop/kiro-webapp && ./run.sh
```

Open **http://localhost:8080** on the Jetson, or **http://10.42.0.1:8080** from a laptop or phone on GeminiBOT. PIN **0000**. Buttons start harmless stand-in programs. The camera is real. Stop with Ctrl+C.

## 4b. Leader arm on a laptop (optional)

The leader arm can be plugged into the **laptop** instead of the robot. The browser reads it over USB (Web Serial), which only works in **Chrome or Edge** and only on an **https** page:

1. On the laptop open **https://10.42.0.1:8443** (on the Jetson itself: https://localhost:8443).
2. The browser warns that the certificate is not trusted (the robot made it itself). Choose *Advanced*, then *Proceed*. Once per laptop.
3. Take control, plug the leader arm (with its power on) into the laptop, press **Use leader arm**. The first time, pick the USB device in the list. After that it is found automatically.

The right follower arm then follows. Pressing the button again, E-STOP, closing the tab or unplugging the leader stops it, and the follower holds where it is.

The certificate lives in `~/.config/kiro-webapp/tls/` (never in git). Set `KIRO_HTTPS_PORT=0` to turn https off.

## 5. Real robot

Choose your own PINs (the engineer PIN opens the servo tester and check wizard; leave it out to keep them off):

```bash
~/Desktop/kiro-webapp/run.sh robot     # asks for the operator and engineer PINs (hidden, never saved)
```

The page must say **ROBOT** at the top. Then, in this order:

| Step | What | Check |
|---|---|---|
| 1 | Turn on the 12 V wheel power. **Robot on blocks**, wheels in the air | |
| 2 | Take control, press **Start base** | The log under Base says `휠 서보 응답 4/4`. If `0/4`, the 12 V power is off |
| 3 | Touch the move stick gently | Wheels turn. Let go: they stop within half a second |
| 4 | Press E-STOP | Wheels stop. Only the operator can release it |
| 5 | Close the browser tab while driving | Wheels stop (heartbeat) |
| 6 | Floor test at low speed, turbo off | |

Arms (5 V arm power on, leader arms plugged in):

| Button | Runs (old code) |
|---|---|
| Fold / Hold / Release | `arm_tuck.py --tuck / --hold / --release --arm <side> --yes` |
| Teleop On | `lerobot-teleoperate` with the same ports and ids as the old panel |
| Skill Run | `run_policy.sh run <model> 20` (right arm, cameras `top` + `right_wrist`) |
| Voice start | `ros2 run so101_base voice_cmd` push-to-talk, Korean phrases, 0.2 m / 45° steps |

Release torque makes the arm fall. Hold the arm first. Teleop off also turns torque off (LeRobot does this).

The **left arm** stays blocked: its calibration was broken on 16 Nov. After a new calibration, start the server with `KIRO_LEFT_ARM_OK=1`.

Useful checks in another terminal (`source /opt/ros/jazzy/setup.bash` first):

```bash
ros2 topic echo /cmd_vel_raw
```

```bash
ros2 topic echo /cmd_vel
```

| Problem | Try |
|---|---|
| Base log says `응답 없는 서보 ID [8, 7, 10, 9]` | 12 V wheel power is off. Turn it on, press Start base again |
| `/cmd_vel_raw` has values, `/cmd_vel` has none | The base (with its guard) is not running. Press Start base |
| Page shows MOCK instead of ROBOT | Use `./run.sh robot` |
| Camera says "in use" | A skill or recording holds it. It comes back when that ends |
| "Refusing to drive a real robot with the default PIN" | Set `KIRO_OPERATOR_PIN` |

## 6. Shut down and restart buttons (optional, sudo)

```bash
sed "s/YOUR_USER/$USER/" ~/Desktop/kiro-webapp/deploy/kiro-shutdown.sudoers | sudo tee /etc/sudoers.d/kiro-shutdown >/dev/null && sudo chmod 440 /etc/sudoers.d/kiro-shutdown && sudo visudo -c
```

This allows exactly two commands, `systemctl poweroff` and `systemctl reboot`, without a password.

## 7. Start at power-on (sudo)

Put the settings (and the PIN) in a file only root can read:

```bash
printf 'KIRO_BACKEND=ros\nKIRO_OPERATOR_PIN=CHANGE-ME\nKIRO_ENGINEER_PIN=CHANGE-ME-TOO\n' | sudo tee /etc/kiro-webapp.env >/dev/null && sudo chmod 600 /etc/kiro-webapp.env
```

```bash
sudo nano /etc/kiro-webapp.env
```

Install the service:

```bash
cd ~/Desktop/kiro-webapp && sed "s#REPO_DIR#$PWD#g; s/YOUR_USER/$USER/g" deploy/kiro-webapp.service | sudo tee /etc/systemd/system/kiro-webapp.service >/dev/null
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now kiro-webapp
```

See what it is doing:

```bash
journalctl -u kiro-webapp -f
```

The base is not started at power-on. The operator presses **Start base** on the page after the 12 V power is on.

## Files in this folder

| File | What it is |
|---|---|
| `setup_hotspot.sh` | Creates the GeminiBOT hotspot with NetworkManager |
| `kiro-webapp.service` | Starts the server at power-on |
| `kiro-shutdown.sudoers` | Lets the page shut down or restart the Jetson |
