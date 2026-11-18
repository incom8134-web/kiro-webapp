"use strict";
/* Every string shown on the page, in English and Korean. */
const STRINGS = {
  en: {
    "estop": "E-STOP", "estop.release": "RELEASE", "estop.stopped": "STOPPED",
    "top.robot": "Robot", "top.link": "Link", "top.operator": "Operator",
    "be.ros": "ROBOT", "be.mock": "MOCK",
    "link.offline": "Offline", "link.connecting": "Connecting",
    "op.none": "Nobody", "op.you": "You are driving", "op.other": "{name} is driving",
    "banner.offline": "Not connected to the robot. Trying again...",
    "banner.estop": "E-STOP is on{by}. ", "banner.estop.by": " (pressed on {name})",
    "banner.estop.op": "Press RELEASE when it is safe.", "banner.estop.viewer": "Only the operator can release it.",

    "sec.control": "Control", "sec.base": "Base", "sec.arms": "Arms", "sec.skills": "Skills",
    "sec.voice": "Voice", "sec.health": "Health", "sec.log": "Activity",

    "pin": "Operator PIN", "take": "Take control", "release": "Release control",
    "held": "You are in control.", "takeover": "{name} is in control. Take over?",
    "turbo": "Turbo speed", "mockpin": "Mock mode: the PIN is 0000.",
    "pad.none": "Gamepad: none (press a button on it)", "pad": "Gamepad: {id}",
    "help": "How to drive", "help.enable": "Enable", "help.move": "Move", "help.turn": "Turn",
    "help.turbo": "Turbo", "help.talk": "Talk", "help.gamepad": "Gamepad", "help.keys": "Keyboard", "help.screen": "Screen",
    "help.holdLB": "hold LB", "help.holdSpace": "hold Space", "help.touch": "touch a stick",
    "help.ls": "left stick", "help.rs": "right stick", "help.switch": "switch", "help.red": "red button",
    "help.holdY": "hold Y", "help.holdV": "hold V", "help.button": "hold button",

    "hint.viewer": "Take control to drive", "hint.estop": "E-STOP is on",
    "hint.neutral": "Let go of the controls once to start", "hint.go": "Touch a stick, hold LB, or hold Space",
    "stick.move": "Move", "stick.turn": "Turn",

    "cam.none": "No camera found", "cam.paused": "Camera in use by {job}", "cam.off": "Camera off",
    "cam.reason": "{key}: {reason}",
    "map": "Map", "pose": "x {x} m  y {y} m  {th}°", "pose.none": "No position yet",
    "speed.fwd": "Forward m/s", "speed.side": "Sideways m/s", "speed.turn": "Turn rad/s",

    "base.running": "Wheel node running", "base.stopped": "Wheel node stopped",
    "base.start": "Start base", "base.stop": "Stop base", "base.guard": "Lidar guard",
    "base.lidar.yes": "Lidar connected", "base.lidar.no": "No lidar: guard lets all commands pass",
    "base.exited": "Wheel node ended (code {code}). Is the 12 V wheel power on?",

    "arm.right": "Right", "arm.left": "Left", "arm.both": "Both",
    "arm.fold": "Fold", "arm.hold": "Hold", "arm.release": "Release",
    "arm.teleop": "Teleop", "arm.on": "On", "arm.off": "Off",
    "arm.missing": "not connected", "arm.leader": "leader", "arm.follower": "follower",
    "arm.leftblocked": "Left arm calibration is broken. Motion is blocked until it is recalibrated.",
    "arm.confirm.release": "Release torque? The arm will fall. Hold it first.",

    "skill.none": "No trained models found in {dir}", "skill.run": "Run", "skill.stop": "Stop",
    "skill.running": "Running", "skill.needs": "Needs cameras: {cams}",

    "voice.start": "Start voice", "voice.stop": "Stop voice", "voice.talk": "Hold to talk",
    "voice.off": "Voice is off", "voice.ready": "Listening when the button is held",
    "voice.heard": "Heard", "voice.lang": "Speech language", "voice.koonly": "Only a Korean speech model is installed. English needs an English Vosk model.",

    "h.jetson": "Jetson", "h.cpu": "CPU", "h.temp": "Temp", "h.mem": "Memory", "h.disk": "Disk",
    "h.servo": "Servos", "h.volt": "V", "h.inuse": "in use by {job}", "h.noread": "no answer",
    "h.shutdown": "Shut down", "h.reboot": "Restart", "h.holdconfirm": "Hold 2 s",
    "h.confirm.shutdown": "Shut down the Jetson? The page goes offline.",
    "h.confirm.reboot": "Restart the Jetson? The page comes back in about one minute.",

    "log.empty": "Nothing yet.",
    "busy": "Working...",
  },
  ko: {
    "estop": "비상정지", "estop.release": "해제", "estop.stopped": "정지됨",
    "top.robot": "로봇", "top.link": "연결", "top.operator": "조종자",
    "be.ros": "실제 로봇", "be.mock": "모의",
    "link.offline": "끊김", "link.connecting": "연결 중",
    "op.none": "없음", "op.you": "내가 조종 중", "op.other": "{name} 조종 중",
    "banner.offline": "로봇과 연결되지 않았습니다. 다시 연결하는 중...",
    "banner.estop": "비상정지 상태입니다{by}. ", "banner.estop.by": " ({name}에서 누름)",
    "banner.estop.op": "안전하면 해제를 누르세요.", "banner.estop.viewer": "조종자만 해제할 수 있습니다.",

    "sec.control": "조종", "sec.base": "베이스", "sec.arms": "팔", "sec.skills": "스킬",
    "sec.voice": "음성", "sec.health": "상태", "sec.log": "기록",

    "pin": "조종자 PIN", "take": "조종 시작", "release": "조종 넘기기",
    "held": "지금 조종하고 있습니다.", "takeover": "{name}이(가) 조종 중입니다. 가져올까요?",
    "turbo": "터보 속도", "mockpin": "모의 모드: PIN은 0000입니다.",
    "pad.none": "게임패드: 없음 (버튼을 한 번 누르세요)", "pad": "게임패드: {id}",
    "help": "조종 방법", "help.enable": "활성", "help.move": "이동", "help.turn": "회전",
    "help.turbo": "터보", "help.talk": "말하기", "help.gamepad": "게임패드", "help.keys": "키보드", "help.screen": "화면",
    "help.holdLB": "LB 누름", "help.holdSpace": "Space 누름", "help.touch": "스틱 터치",
    "help.ls": "왼쪽 스틱", "help.rs": "오른쪽 스틱", "help.switch": "스위치", "help.red": "빨간 버튼",
    "help.holdY": "Y 누름", "help.holdV": "V 누름", "help.button": "버튼 누름",

    "hint.viewer": "조종을 시작해야 움직일 수 있습니다", "hint.estop": "비상정지 상태",
    "hint.neutral": "조종 입력을 한 번 놓아야 시작합니다", "hint.go": "스틱 터치, LB 또는 Space를 누른 채로",
    "stick.move": "이동", "stick.turn": "회전",

    "cam.none": "카메라 없음", "cam.paused": "{job}이(가) 카메라 사용 중", "cam.off": "카메라 꺼짐",
    "cam.reason": "{key}: {reason}",
    "map": "지도", "pose": "x {x} m  y {y} m  {th}°", "pose.none": "위치 정보 없음",
    "speed.fwd": "전진 m/s", "speed.side": "횡이동 m/s", "speed.turn": "회전 rad/s",

    "base.running": "바퀴 노드 실행 중", "base.stopped": "바퀴 노드 꺼짐",
    "base.start": "베이스 시작", "base.stop": "베이스 끄기", "base.guard": "라이다 보호",
    "base.lidar.yes": "라이다 연결됨", "base.lidar.no": "라이다 없음: 보호 기능이 명령을 그대로 통과",
    "base.exited": "바퀴 노드 종료 (코드 {code}). 12 V 바퀴 전원이 켜져 있나요?",

    "arm.right": "오른팔", "arm.left": "왼팔", "arm.both": "양팔",
    "arm.fold": "접기", "arm.hold": "힘 주기", "arm.release": "힘 빼기",
    "arm.teleop": "원격조종", "arm.on": "켜기", "arm.off": "끄기",
    "arm.missing": "연결 안 됨", "arm.leader": "리더", "arm.follower": "팔로워",
    "arm.leftblocked": "왼팔 캘리브레이션이 망가져 있습니다. 다시 캘리브레이션하기 전까지 움직임을 막습니다.",
    "arm.confirm.release": "토크를 끌까요? 팔이 떨어집니다. 먼저 팔을 잡으세요.",

    "skill.none": "{dir}에 학습된 모델이 없습니다", "skill.run": "실행", "skill.stop": "중지",
    "skill.running": "실행 중", "skill.needs": "필요한 카메라: {cams}",

    "voice.start": "음성 켜기", "voice.stop": "음성 끄기", "voice.talk": "누르고 말하기",
    "voice.off": "음성 꺼짐", "voice.ready": "버튼을 누르는 동안 듣습니다",
    "voice.heard": "인식", "voice.lang": "음성 언어", "voice.koonly": "한국어 음성 모델만 설치되어 있습니다. 영어는 영어 Vosk 모델이 필요합니다.",

    "h.jetson": "젯슨", "h.cpu": "CPU", "h.temp": "온도", "h.mem": "메모리", "h.disk": "디스크",
    "h.servo": "서보", "h.volt": "V", "h.inuse": "{job} 사용 중", "h.noread": "응답 없음",
    "h.shutdown": "끄기", "h.reboot": "재시작", "h.holdconfirm": "2초 누르기",
    "h.confirm.shutdown": "젯슨을 끌까요? 페이지가 끊깁니다.",
    "h.confirm.reboot": "젯슨을 재시작할까요? 약 1분 뒤 페이지가 돌아옵니다.",

    "log.empty": "아직 없음.",
    "busy": "처리 중...",
  },
};

let LANG = (navigator.language || "").startsWith("ko") ? "ko" : "en";
try { LANG = localStorage.getItem("kiro-lang") || LANG; } catch (e) { /* private window */ }
if (!STRINGS[LANG]) LANG = "en";

function t(key, vars) {
  let s = (STRINGS[LANG] && STRINGS[LANG][key]) ?? STRINGS.en[key] ?? key;
  if (vars) for (const [k, v] of Object.entries(vars)) s = s.split("{" + k + "}").join(String(v));
  return s;
}

function applyStrings(root = document) {
  document.documentElement.lang = LANG;
  for (const el of root.querySelectorAll("[data-i18n]")) el.textContent = t(el.dataset.i18n);
  for (const el of root.querySelectorAll("[data-i18n-ph]")) el.placeholder = t(el.dataset.i18nPh);
  for (const el of root.querySelectorAll("[data-i18n-title]")) el.title = t(el.dataset.i18nTitle);
}

function setLang(l) {
  LANG = STRINGS[l] ? l : "en";
  try { localStorage.setItem("kiro-lang", LANG); } catch (e) { /* ignore */ }
  applyStrings();
}
