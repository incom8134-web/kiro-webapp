#!/usr/bin/env python3
"""Runs ONE step of the old hardware check wizard (hw_test_wizard.py) without a screen.

The step code is the old code, unchanged: this file only supplies the few
helpers the steps use (log, child process, stop flag, teleop time). The two
"are you sure?" dialogs are answered by the web page before this starts
(checklist and hold-to-confirm).

  wizard_runner.py --list           all steps as JSON
  wizard_runner.py STEP [SECONDS]   run one step, last line: @RESULT {json}
"""
import json
import os
import queue
import signal
import sys
import threading
import time
from pathlib import Path

SO101 = Path(os.environ.get("KIRO_SO101_DIR", "~/so101")).expanduser()
sys.path.insert(0, str(SO101))
os.environ.setdefault("HW_TEST_NO_RECORD", "1")

import hw_test_wizard as hw  # noqa: E402  (old code)

hw.messagebox.askokcancel = lambda *a, **k: True  # confirmed on the web page
hw.messagebox.showinfo = hw.messagebox.showwarning = lambda *a, **k: None


class Var:
    def __init__(self, v):
        self.v = v

    def get(self):
        return self.v


class Headless:
    """Stands in for the Tk window. Steps call self.put / self.proc / self.ros ..."""

    def __init__(self, seconds: int):
        self.log_q: queue.Queue = queue.Queue()
        self.proc = hw.Proc("검사", self.log_q)
        self.stop_flag = False
        self.dur = Var(seconds)
        self.record = Var(False)
        self.thumb = None
        self.step = None
        threading.Thread(target=self._drain, daemon=True).start()

    def put(self, text):
        self.log_q.put(text)

    def ui(self, fn):
        try:  # screen-only callbacks (thumbnail picture) fail without Tk; status changes still work
            fn()
        except Exception:  # noqa: BLE001
            pass

    def set_status(self, s, status):
        s.status = status

    def _drain(self):
        while True:
            line = str(self.log_q.get())
            print(line, flush=True)
            if self.step is not None:
                self.step.lines.append(line)


for _name, _fn in vars(hw.Wizard).items():
    if _name.startswith("t_") or _name in ("wait_proc", "ros", "ros_hz", "_teleop"):
        setattr(Headless, _name, _fn)


def main(argv):
    if argv and argv[0] == "--list":
        print(json.dumps([{"key": s.key, "title": s.title, "desc": s.desc, "prep": s.prep, "do": s.do, "expect": s.expect,
                           "fail": s.fail, "optional": s.optional, "confirm": s.needs_confirm, "minutes": s.minutes}
                          for s in hw.STEPS], ensure_ascii=False))
        return 0
    key = argv[0]
    seconds = int(argv[1]) if len(argv) > 1 else 20
    s = hw.STEPS[hw.STEP_INDEX[key]]
    h = Headless(seconds)
    h.step = s
    s.status, s.lines, s.data = "running", [], {}

    def stop(*_):
        h.stop_flag = True
        h.proc.stop()
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    print(f"===== {s.title} =====", flush=True)
    try:
        result = getattr(h, "t_" + key)(s)
    except Exception as e:  # noqa: BLE001 - same rule as the old wizard: an error is a failed step
        h.put(f"오류: {type(e).__name__}: {e}")
        result = False
    for _ in range(30):  # let the log drain
        if h.log_q.empty():
            break
        time.sleep(0.05)
    if s.status == "skip":
        status = "skip"
    elif h.stop_flag:
        status = "stopped"
    elif result is None or s.needs_confirm:
        status = "confirm"  # a person decides pass / fail on the page
    else:
        status = "pass" if result else "fail"
    print("@RESULT " + json.dumps({"key": key, "status": status, "auto": result, "data": s.data},
                                  ensure_ascii=False, default=str), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
