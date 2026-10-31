from __future__ import annotations

import pytest

from avidbackup.validation import validate_account_id


def test_valid_ids():
    assert validate_account_id("example.com") == "example.com"
    assert validate_account_id("my_site-1") == "my_site-1"


def test_reject_traversal():
    with pytest.raises(ValueError):
        validate_account_id("../etc")
    with pytest.raises(ValueError):
        validate_account_id("a/b")
    with pytest.raises(ValueError):
        validate_account_id("a\\b")
    with pytest.raises(ValueError):
        validate_account_id("")
