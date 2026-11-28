import re
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
WF = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
ON = WF.get("on", WF.get(True))
STEPS = WF["jobs"]["release"]["steps"]


def _step(pred):
    return next(s for s in STEPS if pred(s))


def test_trigger_is_version_tags_only():
    assert set(ON) == {"push"}
    assert set(ON["push"]) == {"tags"}
    assert ON["push"]["tags"] == ["v[0-9]+.[0-9]+.[0-9]+"]


def test_permissions():
    assert WF["permissions"] == {"contents": "write", "packages": "write"}
    assert all("permissions" not in job for job in WF["jobs"].values())


def test_image_build():
    step = _step(lambda s: s.get("uses", "").startswith("docker/build-push-action"))
    w = step["with"]
    assert "linux/amd64" in w["platforms"] and "linux/arm64" in w["platforms"]
    assert w["push"] is True
    for part in ("ghcr.io/", "lights-out:", ":latest"):
        assert part in w["tags"]
    for label in ("source", "version", "licenses"):
        assert f"org.opencontainers.image.{label}" in w["labels"]


def test_tag_check_runs_before_make_check():
    check = next(i for i, s in enumerate(STEPS) if "tag" in s.get("name", "").lower())
    make = next(i for i, s in enumerate(STEPS) if s.get("run", "").strip() == "make check")
    assert check < make
    run = STEPS[check]["run"]
    assert "GITHUB_REF_NAME" in run and "pyproject.toml" in run and "exit 1" in run


def test_release_step():
    run = _step(lambda s: "gh release create" in s.get("run", ""))["run"]
    assert "scripts/changelog_section.py" in run


def test_actions_pinned_to_major():
    for s in STEPS:
        if "uses" in s:
            assert re.search(r"@v\d+$", s["uses"]), s["uses"]


def test_compose_has_image_and_build():
    svc = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]["lights-out"]
    assert svc["build"] == "."
    assert svc["image"] == "ghcr.io/davideganna/lights-out:latest"


def test_releasing_doc_exists():
    assert (ROOT / "RELEASING.md").exists()
