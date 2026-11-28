import importlib.util
from pathlib import Path

ROOT = Path(__file__).parents[1]
_spec = importlib.util.spec_from_file_location("changelog_section", ROOT / "scripts/changelog_section.py")
cs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cs)

FIXTURE = """# Changelog

## [Unreleased]

## [1.2.0] - 2026-03-01

### Added
- new thing

## [1.1.0] - 2026-02-01

### Fixed
- a bug
- another bug

## [1.0.0] - 2026-01-01

- first release
"""


def test_middle_section():
    out = cs.section(FIXTURE, "1.1.0")
    assert out.startswith("### Fixed")
    assert "- a bug" in out and "- another bug" in out
    assert "new thing" not in out and "first release" not in out
    assert "## [" not in out


def test_last_section():
    assert cs.section(FIXTURE, "1.0.0") == "- first release"


def test_missing_and_prefix_do_not_match():
    assert cs.section(FIXTURE, "9.9.9") is None
    assert cs.section(FIXTURE, "1.1") is None
    assert cs.section(FIXTURE, "1.1.0-rc1") is None


def test_main_missing_exits_1(tmp_path, capsys):
    f = tmp_path / "CHANGELOG.md"
    f.write_text(FIXTURE)
    assert cs.main(["9.9.9", str(f)]) == 1
    assert "9.9.9" in capsys.readouterr().err


def test_main_prints_section(tmp_path, capsys):
    f = tmp_path / "CHANGELOG.md"
    f.write_text(FIXTURE)
    assert cs.main(["1.1.0", str(f)]) == 0
    assert "- a bug" in capsys.readouterr().out


def test_real_changelog_unreleased_section_is_found():
    assert cs.section((ROOT / "CHANGELOG.md").read_text(), "Unreleased") is not None
