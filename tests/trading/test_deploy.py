"""Deployment config and the engine entry point (plan T11)."""
import asyncio
from pathlib import Path

import yaml

from src.trading import run
from src.trading.broker.fake import FakeAdapter
from src.trading.journal import Journal

ROOT = Path(__file__).resolve().parents[2]


def compose():
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text())


def test_services_and_restart_policy():
    services = compose()["services"]
    assert set(services) == {"ib-gateway", "trading-engine", "web-ui"}
    for svc in services.values():
        assert svc["restart"] == "unless-stopped"


def test_published_ports_bind_to_localhost_only():
    for name, svc in compose()["services"].items():
        for port in svc.get("ports", []):
            assert str(port).startswith("127.0.0.1:"), f"{name} exposes {port}"


def test_gateway_image_pinned_and_read_only():
    gw = compose()["services"]["ib-gateway"]
    image = gw["image"]
    assert ":" in image and not image.endswith((":latest", ":stable"))
    assert gw["environment"]["READ_ONLY_API"] == "yes"


def test_engine_and_web_use_distinct_client_ids():
    services = compose()["services"]
    ids = {
        services[s]["environment"]["IB_CLIENT_ID"] for s in ("trading-engine", "web-ui")
    }
    assert ids == {"1", "2"}


def test_secrets_are_gitignored():
    import subprocess

    for path in (".env", "deploy/gateway.env"):
        assert (
            subprocess.run(["git", "check-ignore", "-q", path], cwd=ROOT).returncode
            == 0
        )


def test_monitor_once_journals_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "HEARTBEAT_FILE", str(tmp_path / "hb" / "heartbeat.json"))
    broker = FakeAdapter(account_id="DU123", read_only=True)
    journal = Journal(tmp_path / "journal.db")
    code = asyncio.run(run.monitor(0, once=True, adapter=broker, journal=journal))
    assert code == 0
    assert (tmp_path / "hb" / "heartbeat.json").exists()
    assert check_positions_journaled(tmp_path)
    check = Journal(tmp_path / "journal.db")
    [rec] = check.read("account_snapshot")
    assert rec.key == "DU123" and rec.data.net_liquidation == 1_000_000
    assert not broker.is_connected  # disconnected on exit


def test_monitor_requires_account_id(monkeypatch, tmp_path):
    monkeypatch.delenv("TRADING_ACCOUNT_ID", raising=False)
    monkeypatch.chdir(tmp_path)  # no trading.json -> defaults, no account
    journal = Journal(tmp_path / "j.db")
    assert asyncio.run(run.monitor(0, once=True, journal=journal)) == 2


def check_positions_journaled(tmp_path):
    return Journal(tmp_path / "journal.db").latest("positions_snapshot") is not None
