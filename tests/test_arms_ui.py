"""Arms tab: the repeat label reads the fields, and the page stays one Run button.

The old repeat button was labelled from a fixed 30 runs x 30 s (5 s apart) even
when the Runs field was 1. Once is a single run. Loop reads Times and Pause.
"""
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _fn(src, name):
    start = src.index(f"function {name}")
    i = src.index("{", start)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[start:j + 1]
    raise AssertionError(name)


def _keys(which):
    text = (ROOT / "web/i18n.js").read_text()
    marker = f"{which}: {{"
    start = text.index(marker) + len(marker) - 1
    depth = 0
    body = ""
    for j in range(start, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                body = text[start:j + 1]
                break
    return set(re.findall(r'"((?:[^"\\]|\\.)*)"\s*:', body))


def test_once_is_one_run_and_loop_reads_the_fields():
    src = (ROOT / "web/app.js").read_text()
    assert "DEMO_RUNS" not in src
    assert "runs: 30" not in src
    assert "skill-demo" not in src
    js = "\n".join(_fn(src, n) for n in ("clampInt", "clampNum", "skillPlan"))
    script = js + """
      const once = skillPlan('once', 30, 5, 30);
      const loop = skillPlan('loop', 1, 5, 30);
      const hi = skillPlan('loop', 99, 500, 1);
      const blank = skillPlan('loop', '', '', '');
      if (once.repeat !== 1 || once.pause !== 0 || once.seconds !== 30) process.exit(1);
      if (loop.repeat !== 1 || loop.pause !== 5 || loop.seconds !== 30) process.exit(2);
      if (hi.repeat !== 50 || hi.pause !== 120 || hi.seconds !== 5) process.exit(3);
      if (blank.repeat !== 1 || blank.pause !== 0 || blank.seconds !== 30) process.exit(4);
    """
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_arms_page_is_one_run_button_and_a_compact_manual_section():
    html = (ROOT / "web/index.html").read_text()
    assert 'id="skill-demo"' not in html
    assert 'id="demo-btn"' not in html
    assert 'id="tele-both"' not in html
    assert 'id="follow-btn"' not in html
    assert 'data-runmode="once"' in html and 'data-runmode="loop"' in html
    assert 'id="skill-loopfields" hidden' in html
    assert 'id="skill-secs"' in html and 'id="skill-adv"' in html
    assert 'id="skill-empty"' in html and 'id="skill-how"' in html
    assert 'id="both-mode"' in html and 'value="each"' in html and 'value="one"' in html
    assert 'id="leader-box"' in html and "<details" in html
    src = (ROOT / "web/app.js").read_text()
    assert 'Kiro.confirmSend(send, "arm:release"' in src
    assert 't: "demo"' in src and 't: "skill"' in src and 't: "follow"' in src
    assert "arm.fall" in src   # torque warning is a tooltip on the hold button, not a banner


def test_header_battery_sits_beside_the_brand_before_the_nav():
    for name in ("index.html", "dashboard.html", "studio.html"):
        text = (ROOT / "web" / name).read_text()
        assert text.index('class="brand"') < text.index('id="battery"') < text.index('class="cells"')
        assert text.index('id="battery"') < text.index('class="toplinks"')
        cells = text.split('<div class="cells">', 1)[1].split('<nav class="toplinks">', 1)[0]
        assert 'id="battery"' not in cells
    css = (ROOT / "web/style.css").read_text()
    assert "grid-area: bat" in css
    assert "grid-area: estop" in css


def test_english_and_korean_have_the_same_strings():
    en, ko = _keys("en"), _keys("ko")
    assert en == ko
    for key in ("skill.once", "skill.loop", "skill.times", "skill.empty", "skill.how",
                "skill.demoopt", "both.each", "both.one", "arm.recal", "leader.sum", "leader.two.tip"):
        assert key in en
    # The old repeat button and the old "Demo pick motion" button are gone, strings included.
    i18n = (ROOT / "web/i18n.js").read_text()
    css = (ROOT / "web/style.css").read_text()
    for key in ("skill.demo", "skill.play", "skill.runs", "skill.pause", "skill.hint",
                "demo.start", "demo.stop", "demo.hint"):
        assert f'"{key}":' not in i18n
    assert ".skill-rep" not in css and "#skill-prog" not in css and ".skill {" not in css
