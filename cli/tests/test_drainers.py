import sys
import time

import pytest

from llmd.errors import UsageError
from llmd.rollouts.drainers import Drainer, Drainers, alive, command, tail


def test_the_command_is_split_and_filled_in():
    values = {
        "role": "decode",
        "source": "/src",
        "namespace": "ns",
        "set": "s",
        "old_revision": "abc",
        "max_unavailable": "2",
    }
    template = (
        "python3 {source}/guards/drain_group.py roll {role} --batch {max_unavailable} "
        "--old-revision {old_revision}"
    )
    assert command(template, values) == [
        "python3",
        "/src/guards/drain_group.py",
        "roll",
        "decode",
        "--batch",
        "2",
        "--old-revision",
        "abc",
    ]
    with pytest.raises(UsageError, match="unknown field"):
        command("drain {group}", values)
    with pytest.raises(UsageError, match="not a valid command"):
        command("drain 'unclosed", values)


def test_drainers_start_log_and_stop(tmp_path):
    drainers = Drainers(tmp_path / "rollout")
    script = "import sys, time; print('draining', sys.argv[1], flush=True); time.sleep(60)"
    planned = [
        Drainer(
            role,
            [sys.executable, "-c", script, role],
            tmp_path / "rollout" / f"drain-{role}.log",
            str(tmp_path),
        )
        for role in ("prefill", "decode")
    ]
    started = drainers.start(planned)
    assert all(item.pid for item in started) and len(drainers.running()) == 2
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not tail(started[0].log):
        time.sleep(0.05)
    assert tail(started[0].log) == ["draining prefill"]
    assert {item.role for item in drainers.stop()} == {"prefill", "decode"}
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and any(alive(item.pid) for item in started):
        time.sleep(0.05)
    assert not any(alive(item.pid) for item in started)
    assert drainers.running() == [] and all(item.pid == 0 for item in drainers.load())


def test_a_drainer_that_exits_is_reported(tmp_path):
    drainers = Drainers(tmp_path)
    [started] = drainers.start([Drainer("decode", [sys.executable, "-c", "pass"], tmp_path / "d.log")])
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and alive(started.pid):
        time.sleep(0.05)
    assert [item.role for item in drainers.exited()] == ["decode"]
