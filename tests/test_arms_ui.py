"""The repeat button used to ignore the Runs, pause and seconds fields.

It was labelled, and it ran, from a fixed 30 runs x 30 s (5 s apart) even when
Runs was 1. The numbers now come from those three inputs.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_repeat_button_reads_the_fields_not_a_fixed_thirty():
    src = (ROOT / "web/app.js").read_text()
    assert "DEMO_RUNS" not in src
    assert "runs: 30" not in src
    click = src.split('$("skill-demo").addEventListener', 1)[1].split("});", 1)[0]
    assert "skillNums()" in click
    assert 't("skill.demo", { n: nums.repeat, s: nums.pause, d: nums.seconds })' in src
    # skillNums is the three fields, clamped, not a constant
    nums = src.split("function skillNums()", 1)[1].split("}", 1)[0]
    assert '$("skill-repeat")' in nums
    assert '$("skill-pause")' in nums
    assert '$("skill-secs")' in nums
