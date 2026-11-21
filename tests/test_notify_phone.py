from __future__ import annotations

import pytest

from avidbackup.notify import _ntfy, ensure_phone_config, uses_public_ntfy
from avidbackup.store import AppConfig, NotifyConfig


class _Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


@pytest.fixture
def captured(monkeypatch):
    seen: dict = {}

    def fake_open(req, timeout=30):
        seen["url"] = req.full_url
        seen["body"] = req.data.decode("utf-8")
        seen["title"] = req.get_header("Title")
        seen["priority"] = req.get_header("Priority")
        return _Response()

    monkeypatch.setattr("avidbackup.notify.urllib.request.urlopen", fake_open)
    return seen


def test_self_hosted_ntfy_gets_full_message(captured):
    n = NotifyConfig(ntfy_server="https://ntfy.example.net", ntfy_topic="topic-test")
    assert _ntfy(n, "AvidBackup problem", "Example Site: FAILED", urgent=True) is True
    assert captured["url"] == "https://ntfy.example.net/topic-test"
    assert captured["body"] == "Example Site: FAILED"
    assert captured["title"] == "AvidBackup problem"
    assert captured["priority"] == "high"


def test_public_ntfy_gets_generic_message_by_default(captured):
    n = NotifyConfig(ntfy_server="https://ntfy.sh", ntfy_topic="topic-test")
    assert uses_public_ntfy(n)
    _ntfy(n, "AvidBackup problem", "Example Site: 3 files failed at /public_html", urgent=True)
    assert "Example Site" not in captured["body"]
    assert "/public_html" not in captured["body"]
    assert "Example Site" not in captured["title"]
    assert captured["priority"] == "high"


def test_public_ntfy_details_only_when_opted_in(captured):
    n = NotifyConfig(ntfy_server="https://ntfy.sh", ntfy_topic="t", ntfy_details=True)
    _ntfy(n, "AvidBackup", "Example Site: OK", urgent=False)
    assert captured["body"] == "Example Site: OK"


def test_phone_alerts_are_off_by_default_and_need_opt_in(backup_home):
    cfg = AppConfig()
    assert cfg.notify.phone_enabled is False
    ensure_phone_config(cfg)
    assert cfg.notify.ntfy_topic == ""  # no topic is created until the user opts in
    cfg.notify.phone_enabled = True
    ensure_phone_config(cfg)
    assert len(cfg.notify.ntfy_topic) >= 20
