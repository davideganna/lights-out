"""Label → repository routing and its configuration."""

from __future__ import annotations

import pytest

from lights_out.core.config import Config, ConfigError
from lights_out.core.models import Ticket
from lights_out.core.routing import Route, RoutingError, parse_repo_labels, resolve

MAPPING = {"lights-out": "davideganna/lights-out", "web": "acme/website"}


def card(*labels: str) -> Ticket:
    return Ticket(id="c", short_id="1", title="t", description="", url="u", labels=list(labels))


def test_parse_repo_labels():
    assert parse_repo_labels(" Lights-Out=davideganna/lights-out , web=acme/website,") == MAPPING
    assert parse_repo_labels("") == {}


@pytest.mark.parametrize("spec", ["lights-out", "x=notarepo", "=a/b", "x=a/b/c", "x=a/b,X=c/d"])
def test_parse_repo_labels_rejects_bad_entries(spec):
    with pytest.raises(ValueError):
        parse_repo_labels(spec)


def test_resolve_picks_the_one_repository_label_and_ignores_others():
    route = resolve(card("bug", "LIGHTS-OUT"), MAPPING)
    assert route == Route("lights-out", "davideganna/lights-out")
    assert route.url == "https://github.com/davideganna/lights-out.git"


def test_resolve_errors_are_actionable():
    with pytest.raises(RoutingError, match="no repository label.*\\*\\*lights-out\\*\\*"):
        resolve(card("bug"), MAPPING)
    with pytest.raises(RoutingError, match="more than one"):
        resolve(card("web", "lights-out"), MAPPING)


def test_resolve_falls_back_to_default_repo():
    assert resolve(card(), MAPPING, default_repo="acme/main") == Route("", "acme/main")
    # an explicit label still wins over the default
    assert resolve(card("web"), MAPPING, default_repo="acme/main").repo == "acme/website"


@pytest.fixture
def env(monkeypatch, tmp_path):
    for key, value in {
        "TRELLO_API_KEY": "k",
        "TRELLO_TOKEN": "t",
        "TRELLO_BOARD_ID": "b",
        "GITHUB_TOKEN": "g",
        "DATA_DIR": str(tmp_path),
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("GITHUB_REPO", raising=False)
    return monkeypatch


def test_config_reads_repo_labels(env):
    env.setenv("REPO_LABELS", "web=acme/website")
    cfg = Config.from_env(scrub_secrets=False)
    assert cfg.repo_labels == {"web": "acme/website"} and cfg.default_repo == ""


def test_config_requires_some_route(env):
    env.delenv("REPO_LABELS", raising=False)
    with pytest.raises(ConfigError, match="REPO_LABELS"):
        Config.from_env(scrub_secrets=False)


def test_config_explains_the_github_repo_migration(env):
    env.setenv("REPO_LABELS", "web=acme/website")
    env.setenv("GITHUB_REPO", "acme/website")
    with pytest.raises(ConfigError, match="replaced by REPO_LABELS"):
        Config.from_env(scrub_secrets=False)


def test_parse_members():
    from lights_out.core.config import parse_members

    assert parse_members(" Alice, @bob ,,CAROL ") == ["alice", "bob", "carol"]
    assert parse_members("") == [] and parse_members("  ,  ") == []
    assert parse_members("a,A,@a") == ["a"]


def test_config_reads_allowed_members(env):
    env.setenv("REPO_LABELS", "web=acme/website")
    assert Config.from_env(scrub_secrets=False).allowed_members == []
    env.setenv("ALLOWED_MEMBERS", " Alice, @bob")
    assert Config.from_env(scrub_secrets=False).allowed_members == ["alice", "bob"]
