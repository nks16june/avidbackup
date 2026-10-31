from __future__ import annotations

import pytest

from avidbackup.remote import _verify_sftp_host_key


@pytest.fixture()
def known_hosts(backup_home, monkeypatch):
    # known_hosts_path uses AVIDBACKUP_HOME via paths
    from avidbackup.paths import known_hosts_path

    path = known_hosts_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _rsa_key():
    import paramiko

    return paramiko.RSAKey.generate(2048)


def test_tofu_records_then_ok(known_hosts):
    key = _rsa_key()
    _verify_sftp_host_key("sftp.example.com", 22, key, policy="tofu")
    assert known_hosts.is_file()
    # second connect with same key OK
    _verify_sftp_host_key("sftp.example.com", 22, key, policy="tofu")


def test_tofu_mismatch_aborts(known_hosts):
    key1 = _rsa_key()
    key2 = _rsa_key()
    _verify_sftp_host_key("sftp.example.com", 22, key1, policy="tofu")
    with pytest.raises(RuntimeError, match="HOST KEY MISMATCH"):
        _verify_sftp_host_key("sftp.example.com", 22, key2, policy="tofu")


def test_strict_unknown_aborts(known_hosts):
    key = _rsa_key()
    with pytest.raises(RuntimeError, match="host_key_policy=strict"):
        _verify_sftp_host_key("unknown.example.com", 22, key, policy="strict")


def test_strict_mismatch_aborts(known_hosts):
    key1 = _rsa_key()
    key2 = _rsa_key()
    _verify_sftp_host_key("sftp.example.com", 22, key1, policy="tofu")
    with pytest.raises(RuntimeError, match="HOST KEY MISMATCH"):
        _verify_sftp_host_key("sftp.example.com", 22, key2, policy="strict")
