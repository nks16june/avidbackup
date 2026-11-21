from __future__ import annotations

from avidbackup import agent_service


def test_agent_ui_url_default(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIDBACKUP_HOME", str(tmp_path))
    (tmp_path / "accounts.json").write_text(
        '{"version":3,"data_root":"","ui_host":"127.0.0.1","ui_port":8787,"accounts":[]}',
        encoding="utf-8",
    )
    assert agent_service.agent_ui_url().startswith("http://127.0.0.1:8787")


def test_agent_mode_marker(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIDBACKUP_HOME", str(tmp_path))
    monkeypatch.delenv("AVIDBACKUP_AGENT", raising=False)
    assert agent_service.agent_mode_requested() is False
    marker = tmp_path / "config" / "agent.mode"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("1\n", encoding="utf-8")
    assert agent_service.agent_mode_requested() is True


def test_get_agent_status_shape(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIDBACKUP_HOME", str(tmp_path))
    (tmp_path / "accounts.json").write_text('{"version":3,"accounts":[]}', encoding="utf-8")
    monkeypatch.setattr(agent_service, "agent_task_installed", lambda: False)
    st = agent_service.get_agent_status()
    assert st.task_name == agent_service.AGENT_TASK_NAME
    assert st.installed is False
    assert st.running is False
