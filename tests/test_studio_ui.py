"""Studio page: record, train, files and lidar stay; the engineer panels do not."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_studio_has_no_engineer_panels():
    html = (ROOT / "web/studio.html").read_text()
    js = (ROOT / "web/studio.js").read_text()
    i18n = (ROOT / "web/i18n.js").read_text()
    for gone in ("sec-tools", "sec-info", "guard-profile", "sv-scan", "wz-steps", "id=\"info\""):
        assert gone not in html
    assert "renderTools" not in js and "renderInfo" not in js and "guard_profile" not in js
    # Record, train, download and lidar are still on the page.
    for kept in ('id="stepper"', 'id="tsect"', 'id="files"', 'id="fm-models"', 'id="lidar"',
                 'id="c-go"', 'id="t-manual"', 'id="offer"'):
        assert kept in html
    assert 't("fm.dlmodel")' in js
    for gone in ("studio.tools", "studio.info", "eng.servo", "inf.server", "base.profile", "sv.vel"):
        assert f'"{gone}":' not in i18n
    assert "Engineer tools and technical values" not in i18n
    assert "The same PIN opens the engineer tools." not in i18n
    assert "both PINs" not in html
    # The server still says this; the page only translates it.
    assert "Unlock the engineer tools first." in i18n
