"""Pretend camera list with the interface of ~/so101/cams_config.py, for the mock server and tests.

Devices named "mock:<key>" are drawn by robot_server/camera.py, so nothing opens a real camera:

  KIRO_SO101_DIR=tests/fake_so101 KIRO_PORT=8090 KIRO_HTTPS_PORT=8453 ./run.sh
"""
CAMS = [
    {"key": "top", "label": "Top view (pretend)", "source": "gemini", "device": "mock:top", "present": True,
     "reason": "정상", "width": 640, "height": 480, "fps": 30, "fourcc": None},
    {"key": "right_wrist", "label": "Right wrist (pretend)", "source": "/dev/v4l/by-path/pretend-2.2", "device": "mock:right_wrist",
     "present": True, "reason": "정상", "width": 640, "height": 480, "fps": 30, "fourcc": None},
    {"key": "left_wrist", "label": "Left wrist (pretend, unplugged)", "source": "/dev/v4l/by-path/pretend-2.3.2", "device": None,
     "present": False, "reason": "경로 없음(USB 미연결 또는 포트 바뀜)", "width": 640, "height": 480, "fps": 30, "fourcc": "MJPG"},
]


def load(enabled_only=True, exclude=()):
    return [dict(c) for c in CAMS if c["key"] not in exclude]


def _webcam_bypaths():
    return []


def lerobot_arg(cams):
    return "{ " + ", ".join(f"{c['key']}: {{type: opencv, index_or_path: {c['device']}}}" for c in cams) + " }"
