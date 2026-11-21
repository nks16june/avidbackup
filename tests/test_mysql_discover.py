from __future__ import annotations

from pathlib import Path

from avidbackup.mysql_config import (
    discover_from_local_files,
    expand_mysql_config_candidates,
    parse_mysql_credentials,
)


def test_parse_classic_php_typo_databse_name():
    text = """
    $db_host ="localhost";
    $db_username = "divyanguni_nish3";
    $db_password = "secret";
    $databse_name = "divyanguni_maindb";
    """
    creds = parse_mysql_credentials(text, "include/config.php")
    assert creds is not None
    assert creds.database == "divyanguni_maindb"
    assert creds.user == "divyanguni_nish3"
    assert creds.host == "localhost"
    assert creds.password == "secret"


def test_expand_candidates_includes_public_html_prefixes():
    paths = expand_mysql_config_candidates(["custom/db.php"])
    assert "public_html/include/config.php" in paths
    assert "public_html/wp-config.php" in paths
    assert "custom/db.php" in paths


def test_local_discover_finds_nested_portal_config(tmp_path: Path):
    cfg = (
        tmp_path
        / "public_html"
        / "portal_admin52846"
        / "include"
        / "config.php"
    )
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        """<?php
        $db_host ="localhost";
        $db_username = "u1";
        $db_password = "p1";
        $databse_name = "maindb";
        """,
        encoding="utf-8",
    )
    found = discover_from_local_files(tmp_path)
    assert len(found) == 1
    assert found[0].database == "maindb"
    assert found[0].user == "u1"
