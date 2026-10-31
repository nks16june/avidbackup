from __future__ import annotations

from avidbackup.main import parse_args, run


def test_app_help():
    # argparse help exits via SystemExit
    try:
        parse_args(["app", "--help"])
    except SystemExit as exc:
        assert exc.code == 0


def test_app_in_subcommands():
    ns = parse_args(["app", "--no-tray", "--no-browser", "--port", "0"])
    assert ns.command == "app"
    assert ns.no_tray is True
    assert ns.no_browser is True


def test_run_no_command_returns_0():
    assert run([]) == 0
