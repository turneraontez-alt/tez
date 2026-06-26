from pathlib import Path


def _template() -> str:
    return Path("templates/index.html").read_text(encoding="utf-8")


def test_live_dashboard_inner_html_escapes_dynamic_strings():
    text = _template()
    assert "${esc(r)}" in text
    assert "${esc(x)}" in text
    assert "${esc(sideText)}" in text
    assert "esc('Was: '+d.ticker)" in text
    assert "esc(d.error||'No market')" in text
    assert "+esc(e.message)+" in text

    assert "${r}</span>" not in text
    assert "${x}</div>" not in text
    assert "+e.message+" not in text
    assert "('Was: '+d.ticker)" not in text.replace("esc('Was: '+d.ticker)", "")
