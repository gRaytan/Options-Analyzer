"""Opportunities page (plan T32)."""
from src.trading.journal import Journal


def client_with(tmp_path, monkeypatch, scan=None):
    from src.web.app import create_app

    monkeypatch.setenv("TRADING_JOURNAL", str(tmp_path / "j.db"))
    if scan is not None:
        Journal(tmp_path / "j.db").append("scan_results", scan)
    return create_app().test_client()


CANDIDATE = {
    "strategy": "index_put",
    "key": "XSP 2026-12-31 P 600",
    "contracts": 1,
    "bid": 3.0,
    "ask": 3.1,
    "delta": -0.13,
    "iv": 0.22,
    "credit": 305.0,
    "net_credit": 302.7,
    "ev": 120.0,
    "stress_loss": 7100.0,
    "annual_yield": 0.21,
    "score": 0.0019,
    "raw_score": 0.0016,
    "timing": 1.2,
    "fire": True,
    "timing_reasons": ["VIX spike fading"],
    "dte": 38,
}


def test_page_without_scans(tmp_path, monkeypatch):
    html = (
        client_with(tmp_path, monkeypatch)
        .get("/bot/opportunities")
        .get_data(as_text=True)
    )
    assert "No scan yet" in html and not (tmp_path / "j.db").exists()


def test_page_lists_candidates_and_rejections(tmp_path, monkeypatch):
    scan = {
        "date": "2026-11-23",
        "best": [CANDIDATE],
        "rejected": {"delta outside the band": 9, "spread too wide": 2},
    }
    html = (
        client_with(tmp_path, monkeypatch, scan)
        .get("/bot/opportunities")
        .get_data(as_text=True)
    )
    assert (
        "XSP 2026-12-31 P 600" in html
        and "Ready" in html
        and "VIX spike fading" in html
    )
    assert "$303" in html and "21%" in html and "1.20×" in html
    assert html.index("delta outside the band") < html.index("spread too wide")


def test_page_with_empty_scan(tmp_path, monkeypatch):
    scan = {"date": "2026-11-23", "best": [], "rejected": {"IV rank too low": 4}}
    html = (
        client_with(tmp_path, monkeypatch, scan)
        .get("/bot/opportunities")
        .get_data(as_text=True)
    )
    assert "Nothing passed" in html and "IV rank too low" in html


def test_nav_link(tmp_path, monkeypatch):
    assert 'href="/bot/opportunities"' in client_with(tmp_path, monkeypatch).get(
        "/bot"
    ).get_data(as_text=True)
