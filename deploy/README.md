# Run the command center on the robot

These steps are for the Jetson. **They have not been run on the robot yet.** Do them in order and stop at the first step that fails.

During setup the Jetson needs internet: plug in an Ethernet cable.

## 1. Get the code

```bash
cd ~ && git clone https://github.com/incom8134-web/kiro-webapp.git
```

```bash
cd ~/kiro-webapp && python3 -m venv --system-site-packages .venv && .venv/bin/pip install -r requirements.txt
```

## 2. Create the robot's own Wi-Fi

Check that the Wi-Fi card can be a hotspot. The output must contain `AP`:

```bash
iw list | grep -A 10 "Supported interface modes"
```

Create the hotspot. It asks you to choose a Wi-Fi password:

```bash
bash ~/kiro-webapp/deploy/setup_hotspot.sh
```

If it fails on 5 GHz, use 2.4 GHz:

```bash
BAND=bg CHANNEL=6 bash ~/kiro-webapp/deploy/setup_hotspot.sh
```

On the laptop, join the Wi-Fi **GeminiBOT**.

## 3. Turn on SSH (so the monitor can go)

```bash
sudo systemctl enable --now ssh
```

From the laptop:

```bash
ssh your-jetson-username@10.42.0.1
```

When this works, unplug the monitor, keyboard and mouse and restart the Jetson. The hotspot and SSH should come back by themselves.

## 4. First test with the pretend robot

```bash
cd ~/kiro-webapp && ./run.sh
```

On the laptop open **http://10.42.0.1:8080**, PIN **0000**. If the page works here, the network part is done. Stop the server with Ctrl+C.

## 5. Connect it to the wheels

Install `twist_mux` once:

```bash
sudo apt install ros-jazzy-twist-mux
```

**Terminal 1** starts the wheel node and `twist_mux`:

```bash
source /opt/ros/jazzy/setup.bash && source ~/ros2_ws/install/setup.bash && ros2 launch ~/kiro-webapp/deploy/kiro_base.launch.py
```

**Terminal 2** starts the server in robot mode. Choose your own PIN:

```bash
cd ~/kiro-webapp && KIRO_OPERATOR_PIN=your-pin ./run.sh robot
```

The badge on the page must now say **ROBOT**.

**Put the robot on blocks first**, so the wheels are in the air. Take control and move a stick. The wheels should turn. Then do the floor test from the to-do list.

Useful checks in a third terminal (after `source /opt/ros/jazzy/setup.bash`):

```bash
ros2 topic echo /cmd_vel_web
```

```bash
ros2 topic echo /cmd_vel
```

| Problem | Try |
|---|---|
| `/cmd_vel_web` has values, `/cmd_vel` has none | `twist_mux` is not passing them on. Check `ros2 topic echo /e_stop` is `false` |
| `twist_mux` will not work at all | Skip it: start the server with `KIRO_CMD_TOPIC=cmd_vel` and run `mecanum_base_node` alone |
| Server says "Refusing to drive a real robot with the default PIN" | Set `KIRO_OPERATOR_PIN` |
| Page shows MOCK instead of ROBOT | ROS was not loaded. Use `./run.sh robot` |

## 6. Start everything at power-on

Put the settings (and the PIN) in a file only root can read:

```bash
printf 'KIRO_BACKEND=ros\nKIRO_OPERATOR_PIN=CHANGE-ME\n' | sudo tee /etc/kiro-webapp.env && sudo chmod 600 /etc/kiro-webapp.env
```

```bash
sudo nano /etc/kiro-webapp.env
```

Install the two services:

```bash
cd ~/kiro-webapp && for s in kiro-base kiro-webapp; do sed "s/YOUR_USER/$USER/g" deploy/$s.service | sudo tee /etc/systemd/system/$s.service >/dev/null; done
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now kiro-base kiro-webapp
```

See what they are doing:

```bash
journalctl -u kiro-webapp -u kiro-base -f
```

Restart the Jetson. One minute later the page should be reachable with nobody logged in.

## Files in this folder

| File | What it is |
|---|---|
| `setup_hotspot.sh` | Creates the Wi-Fi hotspot with NetworkManager |
| `twist_mux.yaml` | Priorities: robot gamepad over web, E-STOP over everything |
| `kiro_base.launch.py` | Starts `mecanum_base_node` and `twist_mux` (add `joy:=true` for a gamepad plugged into the robot) |
| `kiro-webapp.service`, `kiro-base.service` | Start at power-on |
