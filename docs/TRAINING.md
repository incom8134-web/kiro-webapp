# Training a skill: from the Studio to Google Colab and back

Written 20 Nov 2026. Part 1 is what we found out about the first trained skill. Part 2 is the loop the Studio page now offers. Part 3 is what still needs a real test.

## 1. How the first skill was trained

The first skill is `right_arm_task2_act` in `~/policies`. It was trained on Google Colab with the old notebook from `~/so101/colab`.

### The dataset

| Item | Value |
|---|---|
| Name | `<user>/right_arm_task2` on the Jetson, `local/right_arm_task2` in Colab |
| Recorded | 21 Oct 2026, 23:11 to 23:46, with the old recorder window |
| Size | 43 episodes, 19,283 frames, 15 s per episode, 30 fps, 120 MB |
| Cameras | `top` (Gemini 335) and `right_wrist`, both 640 x 480, H.264 |
| Arm | One arm (right). `robot_type` is `so_follower`. State and action have 6 numbers |
| Task sentences | "pick up the band" (episodes 0-2), "pick up the cube and place it in the box" (episodes 3-42). The real object was a tape roll |
| Package | `right_arm_task2_20261021_2349.tar.gz` (118 MB) + `.sha256`, made by `~/so101/pack_dataset.sh` in `~/Desktop/데이터셋_업로드/` |

### The training (from `train_config.json`)

| Setting | Value |
|---|---|
| Where | Google Colab, free T4 GPU. Paths `/content/so101/...` |
| Policy | ACT (Action Chunking Transformer), ResNet-18 vision with ImageNet weights |
| Steps | 20,000, batch 8 (about 8 passes over the data) |
| Checkpoints | every 2,000 steps, copied to Google Drive |
| Other | learning rate 1e-5, seed 1000, chunk 100 actions (3.3 s), image augmentation off |
| Time | about 3.5 hours of GPU time on 22-23 Oct |
| Result | 51.7 million numbers, `model.safetensors` 207 MB, zip 191 MB |

**Why `train_config.json` says "resume" and "checkpoint 018000".** Free Colab cut the session twice (at about 45 % and 90 %). Each time "Run all" took the newest checkpoint from Drive and went on. The last restart began at step 18,000 and ran to 20,000, so the saved settings remember that start point. The model itself is the final step 20,000.

### From Colab to the robot

1. The notebook's last cell zipped the final checkpoint's `pretrained_model` folder: `right_arm_task2_act_pretrained_model.zip`, with one folder `pretrained_model/` inside.
2. The zip was downloaded from Drive on 23 Oct 11:24 and unpacked by hand:
   `unzip ... -d ~/policies/right_arm_task2_act` gives `~/policies/right_arm_task2_act/pretrained_model/`.
3. `~/so101/run_policy.sh run <folder> 20` plays it on the right (black) arm with LeRobot's `lerobot-rollout`.

### What a model folder must hold

| File | Needed to play? |
|---|---|
| `config.json` | yes (policy type, inputs, outputs) |
| `model.safetensors` | yes (the weights) |
| `policy_preprocessor.json` + the `.safetensors` it names | yes (LeRobot 0.6.1 stops without them) |
| `policy_postprocessor.json` + the `.safetensors` it names | yes |
| `train_config.json` | no (a record, and needed only to continue training) |

### One arm or two arms?

The camera names do not tell. The **size of the action** does:

| Action numbers | Model |
|---|---|
| 6 | one arm. Left arm only if it uses the `left_wrist` camera and not `right_wrist` |
| 12 | both arms (LeRobot's `bi_so_follower`: left 6, then right 6) |
| anything else | fits no arm of this robot |

### What the old notes say about the first skill (`~/so101/CLAUDE.md`)

- On 23 Oct it picked and placed the roll **at night, in the same light as the recording**.
- In daylight it barely moved. It also did nothing when the roll was not put back at the start.
- To work in daylight: record daylight episodes too and train with image augmentation on.

### Problems found

| Problem | Where | State |
|---|---|---|
| Cell 3 of the old notebook is a SyntaxError (a `\n` became a real line break, commit 0da6de3 of 22 Oct). "Run all" stops there | `~/so101/colab/make_notebook.py`, and the copy in the main checkout | Fixed in the new notebook. The old one in `~/so101` is unchanged (it is copied into every package by `pack_dataset.sh`) |
| The old notebook's last cell prints a `lerobot-record --policy.path` command that LeRobot 0.6.1 no longer accepts for playing | same | The new notebook points to the Studio upload |
| A second package of the same dataset was not unpacked again and a finished run was not trained again, so the old model would come back | old notebook | Fixed: one training run per package file |
| The Studio's "add episodes" check expects `so101_follower`, but LeRobot 0.6.1 writes `so_follower` for one arm, so adding to a real one-arm dataset would be refused | `robot_server/recorder.py` `robot_type()` | **Not fixed** (outside this job) |
| The right hand camera now uses MJPG; the first model was trained on YUYV frames | `~/so101/config/cameras.json` (uncommitted) | Just a note |

## 2. The loop in the Studio

Everything is on the Studio page (engineer PIN), section **Train a skill**. There are two ways.

### The automatic way: "Train in Colab" (the robot has internet)

Plug a LAN cable from the router into the Jetson. Its hotspot is set up in "shared" mode, so the robot and every laptop on its Wi-Fi then have internet. Only Colab signs in to Google; the robot signs in to nothing.

| # | You | What happens |
|---|---|---|
| 1 | Studio: pick the dataset, press **Train in Colab** | The robot packages it (or reuses a fresh package); Colab opens in a new tab |
| 2 | Colab: sign in, **Runtime > Run all** (first time: "Run anyway", allow Drive with every box ticked) | Cell 1 opens a temporary address for this Colab session and shows **Connect to the robot** |
| 3 | Click **Connect to the robot**, then **Send** on the robot page that opens | The robot sends the dataset into Colab; training starts by itself |
| 4 | Wait 3-4 hours with the Colab tab open | The Studio shows the step count |
| 5 | Close the Colab tab, log out of Google | The robot fetched the model, checked it and installed it as `<package>_act`: it is in Autonomous skills |

How the two talk (`robot_server/colab.py`, notebook cell 1):

- The notebook runs a small file server on its own computer and opens a **Cloudflare quick tunnel** to it (free, no account). `cloudflared` is a pinned release, checked against its SHA-256 before it runs. The session gets a random one-time key.
- The link carries the address and the key to the Studio page, which asks before sending (the **Send** card). If that tab is not the unlocked Studio, it hands them to the Studio tab that is.
- The robot only makes outgoing HTTPS calls, only to `https://<name>.trycloudflare.com` (in mock mode also `http://127.0.0.1:<port>` for tests), never follows a redirect, and sends the key in a header. The dataset goes in pieces of 32 MB (Cloudflare takes at most 100 MB per request) with its SHA-256 at the end. The model comes back with its size and SHA-256 and goes through every check of a manual upload.
- If Colab disconnects, the Studio shows "No answer". Run all again in Colab, then Connect and Send again: the notebook continues from its checkpoint in Drive.
- The robot keeps the run (including the key) in `~/.cache/kiro-webapp/colab_run.json` (readable only by the user), so a server restart during the hours of training goes on.
- Cloudflare offers quick tunnels without guarantees. If they stop working, the manual way below still works.

### The manual way (the robot has no internet)

Folded under "Manual way" on the page:

```
package -> download -> Google Drive -> Colab (SOURCE = drive, T4, Run all, 3-4 h)
   -> download the model zip -> upload in Studio -> Autonomous skills -> Play
```

| Step | Where | What happens |
|---|---|---|
| 1 Package and download | Studio | The old `pack_dataset.sh` checks the dataset reads back and makes `<name>_<date>.tar.gz` + `.sha256`. Two download buttons |
| 2 Go online, Google Drive | laptop | The robot's Wi-Fi has no internet. Switch Wi-Fi, put both files in My Drive > `SO101` > `데이터셋_업로드` |
| 3 Open Colab | Studio button | Opens `colab/so101_train_colab.ipynb` from the public GitHub repo in Colab. "Notebook file" downloads the same notebook from the robot, for File > Upload notebook |
| 4 Run | Colab | Set `SOURCE` to `drive` in cell 1. Runtime > Change runtime type > T4 GPU, then Runtime > Run all. After a disconnect: Run all again |
| 5 Download the model | Google Drive | My Drive > `SO101` > `outputs` > `<name>_act_pretrained_model.zip` |
| 6 Upload | Studio | Back on the robot's Wi-Fi. The robot checks the zip and installs it as `~/policies/<name>/pretrained_model` |
| 7 Play | Control page, Arms tab | The skill is in Autonomous skills |

**Checkpoints in Drive.** Every 2,000 steps the notebook copies the finished checkpoint to Drive, first as `<step>.part` with a list of file sizes, then renamed. Drive keeps the newest two. After a disconnect, "Run all" continues from the newest copy whose files all match the list, so a copy cut short by the disconnect is skipped. Each package file gets its own training folder (`SO101/outputs/<name>_<date>_act/`), so training a newer package of the same dataset never gives back the old model.

**Why Google Drive and not an upload button in the notebook.** Colab's `files.upload()` keeps the whole file in the browser and sends it in small pieces; uploads of 100 MB and more are known to break. Drive uploads are reliable, survive a disconnect, and the notebook keeps its checkpoints there. Without Drive, the notebook still works with the Files panel upload (`USE_DRIVE` off), but a disconnect then loses everything. The same goes for the model zip: download it from drive.google.com rather than from Colab.

**Why a GitHub link.** `colab.research.google.com/github/<owner>/<repo>/blob/<branch>/<path>` opens a notebook from a public repo with no setup; Colab asks for the Google account itself. The link points at branch `main`, so it works once this branch is merged. If the repo becomes private, use the "Notebook file" button instead.

### What the robot checks in an uploaded zip

| Check | Refused when |
|---|---|
| Name | not letters, numbers, `.`, `_`, `-` (up to 80), or the name is taken (never overwritten) |
| Size | zip over 3 GB, unpacked over 4 GB, a settings file over 10 MB, more than 80 entries (counted before the zip is opened), or less than 2 GB disk left after unpacking |
| Paths | `..`, absolute paths, backslashes, links, more than one model folder, files outside the model folder (for example `training_state/`), folders deeper than 3 |
| File types | anything but `.json` and `.safetensors` (and `README.md`). No `.bin` / `.pt` pickles: loading those can run code |
| Content | broken JSON, `config.json` without type and features, a `.safetensors` file with a broken header, missing processor files or the state files they name |
| Damage | bad or cut-short compressed data, or a packing method the robot cannot read (Windows "Deflate64" for big files): a clear message, never a crash |

The zip is written to `~/policies/.incoming/`, checked and unpacked there, then moved into place in one step, so a half-installed skill never appears. Mac zips are fine (`__MACOSX/` and `.DS_Store` are left out).

An upload that sends nothing for 60 s (the laptop left the Wi-Fi or slept) is given up, so the next upload is not refused as busy. Locking the Studio, leaving or a new operator stops uploads and downloads at once. The upload and unpacking run beside the 20 Hz control loop, never in it (a real 191 MB zip took 3.8 s; the page's ping stayed under 15 ms).

**When the laptop leaves the robot's Wi-Fi** (step 2), the Studio locks itself, but the guide stays on the page: steps 2 to 5 and the Colab link remain readable, and steps 1 and 6 say they need the robot. Back on the robot's Wi-Fi, unlock again with both PINs. The notebook file can be downloaded in step 1, while still on the robot's Wi-Fi.

### Training files

The Studio lists datasets, packages and trained models (size, date, episodes, frames, cameras, steps, arm). Downloads and deletes work only for what is in these lists. A package without its `.sha256` is still being written, or packing failed: it is listed as incomplete and never offered for download. A delete asks first and needs a 2 s hold. Datasets are deleted only if they have `meta/info.json` and never under `calibration/`. A model cannot be deleted while a skill plays; a dataset not while recording or packaging.

### Two-arm models

They are listed, but Play stays off with the message "Trained on both arms: it cannot be played yet". The server refuses them too. `run_policy.sh` drives the right arm only; two-arm playback would need a new runner with `bi_so_follower`. One-arm playback did not change.

### Files

| File | What |
|---|---|
| `robot_server/training.py` | models, packages, upload checks and install, zip download, delete |
| `robot_server/colab.py` | Train in Colab: package, wait for the Colab session, send, follow, fetch, install |
| `robot_server/main.py` | `GET /api/studio/packages/<file>`, `GET /api/studio/models/<name>.zip`, `GET /api/studio/notebook.ipynb`, `POST /api/studio/models`, WebSocket `studio` / `delete` and `colab` (start, connect, stop). All need the engineer token |
| `colab/make_notebook.py` | writes `colab/so101_train_colab.ipynb` (run it after a change; a test checks they match) |
| `tests/test_training.py`, `tests/test_colab.py`, `tests/test_notebook.py` | the checks above; the notebook cells run here without Colab, with the robot end to end (no tunnel, a stand-in for the GPU training) |

Settings: `KIRO_POLICIES_DIR` (mock mode uses `~/.cache/kiro-webapp/mock_policies`, so mock tests never touch real skills), `KIRO_PACKAGES_DIR` (default `~/Desktop/데이터셋_업로드`), `KIRO_COLAB_URL`.

## 3. Still needs a real test

| Test | Why |
|---|---|
| **Train in Colab with the LAN cable in**: Train in Colab, Run all, Connect, Send, 3-4 h, skill installed | Tested on this computer with the real notebook cells, but never through Cloudflare and real Colab |
| Package a real dataset from the Studio and download it to a laptop over the hotspot | Only the pretend recorder was used |
| One full Colab run with the new notebook (3-4 h) | Cells 1, 2, 4 and 5 ran here without a GPU; installing LeRobot and training ran only in the old notebook |
| Upload the real 191 MB zip over the hotspot | Tested with small zips in the browser and in tests |
| Play the uploaded skill on the robot | Never done from an uploaded model. Clear the table, hand near E-STOP |
| The Colab link | Points at `main`: works once this branch is merged and pushed |
