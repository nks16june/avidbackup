from __future__ import annotations

import argparse
import logging

import pytest

from avidbackup import notify
from avidbackup.main import cmd_digest
from avidbackup.store import (
    AccountConfig,
    AppConfig,
    FtpConfig,
    load_config,
    save_config,
    upsert_account,
)


def test_no_channels_means_no_warning_and_nothing_sent(caplog, monkeypatch):
    called: list[str] = []
    monkeypatch.setattr(notify, "_ntfy", lambda *a, **k: called.append("ntfy") or True)
    monkeypatch.setattr(notify, "_webhook", lambda *a, **k: called.append("webhook") or True)
    cfg = AppConfig()
    assert notify.channels_configured(cfg) == []
    with caplog.at_level(logging.DEBUG, logger="avidbackup.notify"):
        assert notify.send_status(cfg, "t", "m", urgent=False) is False
    assert called == []
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_failure_of_a_configured_channel_is_still_warned(caplog, monkeypatch):
    monkeypatch.setattr(notify, "_webhook", lambda *a, **k: False)
    cfg = AppConfig()
    cfg.notify.webhook_url = "https://hooks.example.test/x"
    assert notify.channels_configured(cfg) == ["webhook"]
    with caplog.at_level(logging.WARNING, logger="avidbackup.notify"):
        assert notify.send_status(cfg, "t", "m", urgent=False) is False
    assert any("webhook" in r.getMessage() for r in caplog.records)


def test_only_configured_channels_are_used(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(notify, "_ntfy", lambda *a, **k: calls.append("ntfy") or True)
    monkeypatch.setattr(notify, "_webhook", lambda *a, **k: calls.append("webhook") or True)
    cfg = AppConfig()
    cfg.notify.phone_enabled = True
    cfg.notify.ntfy_topic = "topic"
    assert notify.channels_configured(cfg) == ["phone"]
    assert notify.send_status(cfg, "t", "m", urgent=False) is True
    assert calls == ["ntfy"]


@pytest.fixture
def one_site(backup_home):
    cfg = load_config()
    upsert_account(
        cfg, AccountConfig(id="q.test", label="Quiet", ftp=FtpConfig(host="h", user="u", password="p"))
    )
    cfg.notify.phone_enabled = False
    save_config(cfg)


def test_digest_with_no_channels_is_not_reported_as_a_failed_delivery(one_site, capsys):
    code = cmd_digest(argparse.Namespace(no_notify=False))
    out = capsys.readouterr().out
    assert "not delivered" not in out.lower()
    assert "No notification channel is set up" in out
    assert code in (0, 1)  # 1 only if the digest itself reports a problem site, never from delivery
