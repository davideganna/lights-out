from __future__ import annotations

from datetime import datetime

from lights_out.orchestration.health import Heartbeat


def make():
    now = [1_000_000.0]
    return Heartbeat(clock=lambda: now[0]), now


def test_fresh_is_ok():
    hb, _ = make()
    code, body = hb.snapshot(900)
    assert code == 200 and body["status"] == "ok" and body["age_s"] == 0
    assert body["current_card"] is None and body["stage"] is None


def test_stale_boundary():
    hb, now = make()
    now[0] += 900
    assert hb.snapshot(900)[0] == 200
    now[0] += 1
    code, body = hb.snapshot(900)
    assert code == 503 and body["status"] == "stale" and body["age_s"] == 901


def test_beat_resets_age_and_types():
    hb, now = make()
    now[0] += 500.7
    hb.beat()
    now[0] += 10.5
    _, body = hb.snapshot(900)
    assert isinstance(body["age_s"], int) and body["age_s"] == 10
    assert datetime.fromisoformat(body["last_beat"]).tzinfo is not None


def test_set_and_clear_work():
    hb, now = make()
    now[0] += 100
    hb.set_work("12", "ci")
    _, body = hb.snapshot(900)
    assert (body["current_card"], body["stage"], body["age_s"]) == ("12", "ci", 0)
    now[0] += 5
    hb.clear_work()
    _, body = hb.snapshot(900)
    assert body["current_card"] is None and body["stage"] is None and body["age_s"] == 0
