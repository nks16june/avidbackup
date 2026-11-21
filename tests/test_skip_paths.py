from __future__ import annotations

from avidbackup.sync_files import _should_skip


def test_keeps_account_home_content_dirs():
    skips = ["mail", "etc", "ssl", "cache", "tmp", "temp", "access-logs", "cgi-bin"]
    for path in (
        "/app/index.php",
        "/upload/photo.jpg",
        "/uploads/2026/a.png",
        "/storage/logs/app.log",
        "/public_html/wp-content/uploads/x.jpg",
        "/public_html/templates/page.php",
        "/database/schema.sql",
        "/.env",
        "/.env_local",
        "/.htaccess",
        "/public_html/.htaccess",
        "/public_html/.well-known/acme-challenge/x",
    ):
        assert not _should_skip(path, skips), path


def test_skips_cpanel_and_dot_noise():
    skips = ["mail", "etc", "ssl", "cache", "tmp", "temp", "access-logs", "cgi-bin"]
    assert _should_skip("/mail/cur/1", skips)
    assert _should_skip("/etc/ftpquota", skips)
    assert _should_skip("/ssl/certs/x", skips)
    assert _should_skip("/.cpanel/caches/config", skips)
    assert _should_skip("/.trash/old.txt", skips)
    assert _should_skip("/.caldav/x", skips)
    assert _should_skip("/.spamassassin/user_prefs", skips)
    assert _should_skip("/public_html/wp-content/cache/file", skips)
    # Must not treat "templates" as "temp"
    assert not _should_skip("/public_html/templates/home.php", skips)
