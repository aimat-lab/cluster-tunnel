"""Unit tests for the job-overview parsing/ordering (no ssh, no cluster)."""

from __future__ import annotations

from cluster_tunnel.jobs import Job, _order, parse_line


def test_parse_line_basic() -> None:
    job = parse_line("active|966737|RUNNING|14:44|7:00:00|booster|1|train.sh")
    assert job == Job("active", "966737", "RUNNING", "14:44", "7:00:00", "booster", "1", "train.sh")
    assert job.active is True


def test_parse_line_preserves_spaced_state() -> None:
    # sacct reports cancellations as "CANCELLED by <uid>" — a space, never a '|'.
    job = parse_line("done|960652|CANCELLED by 30927|01:56:26|07:00:00|booster|1|run.sh")
    assert job is not None
    assert job.state == "CANCELLED by 30927"
    assert job.active is False


def test_parse_line_name_may_contain_pipe() -> None:
    # The name is the last field and split-limited, so a '|' in it stays intact
    # and never shifts the earlier columns.
    job = parse_line("active|1|RUNNING|1:00|2:00|p|1|weird|name")
    assert job is not None
    assert job.nodes == "1"
    assert job.name == "weird|name"


def test_parse_line_rejects_malformed() -> None:
    assert parse_line("") is None
    assert parse_line("   ") is None
    assert parse_line("too|few|fields") is None
    assert parse_line("bogus|1|R|0|0|p|1|n") is None  # bad source marker
    assert parse_line("active||R|0|0|p|1|n") is None  # empty jobid


def test_order_active_first_then_finished_newest_first() -> None:
    jobs = [
        parse_line("active|10|RUNNING|1:00|2:00|p|1|a"),
        parse_line("done|1|COMPLETED|0:10|1:00|p|1|old"),
        parse_line("done|2|COMPLETED|0:10|1:00|p|1|new"),
    ]
    ordered = _order([j for j in jobs if j])
    assert [j.jobid for j in ordered] == ["10", "2", "1"]  # active, then reversed finished


def test_order_dedupes_active_over_finished() -> None:
    # A job seen both live and finished (it ended between the two probe calls) is
    # kept only in its active form.
    jobs = [
        parse_line("active|5|RUNNING|1:00|2:00|p|1|x"),
        parse_line("done|5|COMPLETED|1:00|2:00|p|1|x"),
    ]
    ordered = _order([j for j in jobs if j])
    assert len(ordered) == 1
    assert ordered[0].active is True
