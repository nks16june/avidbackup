from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _workflow() -> str:
    return (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")


def test_checksum_file_holds_a_bare_file_name():
    text = _workflow()
    step = text[text.index("- name: SHA256") : text.index("# Optional SignPath")]
    assert "cd dist" in step, "hash from inside dist/ so the .sha256 has no path prefix"
    assert 'SETUP="dist/' not in step


def test_release_candidates_are_published_as_prereleases():
    text = _workflow()
    assert text.count("prerelease: ${{ contains(github.ref_name, '-') }}") == 2
    assert text.count("body_path: .github/release-notes.md") == 2
    assert (ROOT / ".github" / "release-notes.md").is_file()
