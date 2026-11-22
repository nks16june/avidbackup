"""The real PHP helper script, run under PHP's built-in web server (skipped without php)."""

from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.parse
import urllib.request

import pytest
from helper_env import php_exe, php_has_mysqli, php_site  # noqa: F401

from avidbackup.php_helper import HelperClient, HelperError, generate_helper, token_sha256

TOKEN = "S3cret-" + "z" * 40
NAME = "avidbackup-sec.php"


def _source(**over) -> str:  # noqa: ANN003
    args = {"token": TOKEN, "db_host": "127.0.0.1", "db_port": 1, "db_user": "nobody",
            "db_password": "nope", "db_name": "nodb", "allowed_ips": ""}
    args.update(over)
    return generate_helper(**args)


def _request(url: str, data: dict | None = None, method: str | None = None):  # noqa: ANN202
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method or ("POST" if body is not None else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read(), resp.headers
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), exc.headers


def _json(raw: bytes) -> dict:
    return json.loads(raw.decode())


def test_generated_php_is_valid_even_with_nasty_credentials(php_exe, tmp_path):  # noqa: F811
    src = _source(db_user="us'er", db_password="pa'ss\\w\"$rd{$x}`;", db_name="d'b")
    f = tmp_path / "h.php"
    f.write_text(src, encoding="utf-8")
    out = subprocess.run([php_exe, "-l", str(f)], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stdout + out.stderr


def test_get_requests_are_refused(php_site):  # noqa: F811
    site = php_site({NAME: _source()})
    status, body, _ = _request(site.url(NAME))
    assert status == 405 and _json(body)["ok"] is False
    assert _request(site.url(NAME), method="PUT")[0] == 405


def test_token_in_the_url_is_ignored(php_site):  # noqa: F811
    site = php_site({NAME: _source()})
    assert _request(site.url(NAME) + "?token=" + TOKEN + "&action=ping")[0] == 405


def test_missing_and_wrong_tokens_are_rejected(php_site):  # noqa: F811
    site = php_site({NAME: _source()})
    assert _request(site.url(NAME), {"action": "ping"})[0] == 403
    assert _request(site.url(NAME), {"action": "ping", "token": ""})[0] == 403
    assert _request(site.url(NAME), {"action": "ping", "token": "wrong"})[0] == 403
    # the hash is not a valid token
    assert _request(site.url(NAME), {"action": "ping", "token": token_sha256(TOKEN)})[0] == 403


def test_correct_token_answers_without_touching_the_database(php_site):  # noqa: F811
    site = php_site({NAME: _source()})  # the database login is invalid on purpose
    status, body, headers = _request(site.url(NAME), {"action": "ping", "token": TOKEN})
    data = _json(body)
    assert status == 200 and data["ok"] and data["helper"] == "avidbackup"
    assert "no-store" in headers["Cache-Control"]
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert "noindex" in headers["X-Robots-Tag"]


def test_nothing_secret_is_revealed_in_any_reply(php_site):  # noqa: F811
    site = php_site({NAME: _source(db_password="hunter2-very-secret")})
    for data in ({"action": "ping", "token": TOKEN}, {"action": "ping", "token": "x"}, {"action": "nope", "token": TOKEN}):
        _status, body, _ = _request(site.url(NAME), data)
        assert b"hunter2-very-secret" not in body and TOKEN.encode() not in body


def test_unknown_actions_are_refused(php_site):  # noqa: F811
    site = php_site({NAME: _source()})
    status, body, _ = _request(site.url(NAME), {"action": "shell", "token": TOKEN})
    assert status == 400 and "Unknown action" in _json(body)["error"]


def test_repeated_wrong_tokens_lock_the_helper_even_for_the_right_token(php_site):  # noqa: F811
    site = php_site({NAME: _source()})
    for _ in range(5):
        assert _request(site.url(NAME), {"action": "ping", "token": "bad"})[0] == 403
    status, body, _ = _request(site.url(NAME), {"action": "ping", "token": TOKEN})
    assert status == 429 and "Too many" in _json(body)["error"]
    with pytest.raises(HelperError, match="429"):
        HelperClient(site.url(NAME), TOKEN).ping()


def test_a_correct_token_before_the_limit_resets_the_counter(php_site):  # noqa: F811
    site = php_site({NAME: _source()})
    for _ in range(4):
        _request(site.url(NAME), {"action": "ping", "token": "bad"})
    assert _request(site.url(NAME), {"action": "ping", "token": TOKEN})[0] == 200
    for _ in range(4):
        _request(site.url(NAME), {"action": "ping", "token": "bad"})
    assert _request(site.url(NAME), {"action": "ping", "token": TOKEN})[0] == 200


def test_ip_allow_list_blocks_other_addresses(php_site):  # noqa: F811
    site = php_site({NAME: _source(allowed_ips="10.9.9.9")})
    status, body, _ = _request(site.url(NAME), {"action": "ping", "token": TOKEN})
    assert status == 403 and "address" in _json(body)["error"]


def test_ip_allow_list_lets_the_listed_address_in(php_site):  # noqa: F811
    site = php_site({NAME: _source(allowed_ips="10.9.9.9, 127.0.0.1")})
    assert _request(site.url(NAME), {"action": "ping", "token": TOKEN})[0] == 200


def test_each_helper_keeps_its_own_lockout_state(php_site):  # noqa: F811
    site = php_site({"avidbackup-a.php": _source(), "avidbackup-b.php": _source()})
    for _ in range(5):
        _request(site.url("avidbackup-a.php"), {"action": "ping", "token": "bad"})
    assert _request(site.url("avidbackup-a.php"), {"action": "ping", "token": TOKEN})[0] == 429
    assert _request(site.url("avidbackup-b.php"), {"action": "ping", "token": TOKEN})[0] == 200


def test_database_failures_are_reported_not_leaked(php_site, php_has_mysqli):  # noqa: F811
    if not php_has_mysqli:
        pytest.skip("php has no mysqli")
    site = php_site({NAME: _source(db_password="hunter2-very-secret")})
    status, body, _ = _request(site.url(NAME), {"action": "check", "token": TOKEN})
    assert status == 502 and b"hunter2-very-secret" not in body
    with pytest.raises(HelperError, match="connect to the database"):
        HelperClient(site.url(NAME), TOKEN).check()
