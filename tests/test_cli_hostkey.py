"""`ctun hostkey`: show the key a cluster presents, and pin it (ssh's verdict stubbed)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from cluster_tunnel import hostkey
from cluster_tunnel.cli import cli, configcmd
from cluster_tunnel.hostkey import HostKey

from tests.test_cli import _norm

UNPINNED = "clusters:\n  bw:\n    host: uc3.example  # login node\n"
PINNED = (
    "clusters:\n  bw:\n    host: uc3.example\n"
    "    strict_host_key_checking: 'yes'\n    known_hosts_file: /kh\n"
)


def _key(fingerprint: str) -> HostKey:
    return HostKey("uc3.example", "ED25519", 256, fingerprint, f"uc3.example ssh-ed25519 {fingerprint[-3:]}")


@pytest.fixture
def server(monkeypatch) -> dict:
    """A stubbed server presenting SHA256:new. Set ``trust`` for ssh's verdict,
    ``pinned`` for the key file's keys, ``conflicts`` for where a known key sits."""
    state: dict = {"fingerprints": ["SHA256:new"], "trust": "new", "pinned": [], "conflicts": [], "pins": []}

    def fake_check(spec):
        # Each check (and the fetch after it) sees the next fingerprint, if there is one.
        state["now"] = state["fingerprints"].pop(0) if len(state["fingerprints"]) > 1 else state["fingerprints"][0]
        return hostkey.Check(state["trust"], "ssh-ed25519", state["now"], list(state["conflicts"]))

    monkeypatch.setattr("cluster_tunnel.hostkey.check", fake_check)
    monkeypatch.setattr("cluster_tunnel.hostkey.scan", lambda spec, algorithm=None: [_key(state["now"])])
    monkeypatch.setattr("cluster_tunnel.hostkey.pinned", lambda spec, file, host: state["pinned"])
    monkeypatch.setattr(
        "cluster_tunnel.hostkey.pin", lambda spec, file, keys: state["pins"].append((file, keys))
    )
    return state


def _config(tmp_path: Path, monkeypatch, text: str) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(text, encoding="utf-8")
    monkeypatch.setenv("CTUN_CONFIG", str(p))
    return p


def _tty(monkeypatch, tty: bool) -> None:
    monkeypatch.setattr(configcmd, "_stdin_is_tty", lambda: tty)


def _hostkey(*args: str, input: str | None = None):
    return CliRunner().invoke(cli, ["-t", "bw", "hostkey", *args], input=input)


# --- showing --------------------------------------------------------------------


def test_a_new_key(tmp_path: Path, monkeypatch, server) -> None:
    _config(tmp_path, monkeypatch, UNPINNED)
    r = _hostkey()
    assert r.exit_code == 0, r.output
    out = _norm(r)
    assert "Host key of 'bw' (uc3.example): ED25519 SHA256:new" in out
    assert "not pinned: the next login accepts it on first contact" in out
    assert server["pins"] == []


def test_a_key_ssh_knows_from_an_earlier_login(tmp_path: Path, monkeypatch, server) -> None:
    _config(tmp_path, monkeypatch, UNPINNED)
    server["trust"] = "known"
    r = _hostkey()
    assert r.exit_code == 0
    assert "known to ssh from an earlier login, not pinned by ctun; pin it with" in _norm(r)


def test_a_pinned_key(tmp_path: Path, monkeypatch, server) -> None:
    _config(tmp_path, monkeypatch, PINNED)
    server.update(trust="known", pinned=[_key("SHA256:new")])
    r = _hostkey()
    assert r.exit_code == 0, r.output
    assert "pinned: logins accept only this key." in _norm(r)


def test_a_matching_key_that_is_not_yet_checked_strictly(tmp_path: Path, monkeypatch, server) -> None:
    _config(tmp_path, monkeypatch, PINNED.replace("'yes'", "accept-new"))
    server.update(trust="known", pinned=[_key("SHA256:new")])
    r = _hostkey()
    assert r.exit_code == 0
    assert "matches the key in /kh, but logins still take a new key on first contact" in _norm(r)


def test_strict_checking_without_a_pinned_key(tmp_path: Path, monkeypatch, server) -> None:
    _config(tmp_path, monkeypatch, PINNED)
    r = _hostkey()
    assert r.exit_code == 0
    assert "not in /kh: logins are refused until the key is pinned" in _norm(r)


def test_a_key_other_than_the_pinned_one_fails(tmp_path: Path, monkeypatch, server) -> None:
    _config(tmp_path, monkeypatch, PINNED)
    server.update(trust="changed", pinned=[_key("SHA256:old")], conflicts=["/kh:1"])
    r = _hostkey()
    assert r.exit_code == 1
    out = _norm(r)
    assert "DIFFERENT from the key logins trust: ED25519 SHA256:old (the trusted key is in /kh:1)" in out
    assert "Don't log in until you know why" in out


def test_a_key_other_than_the_one_ssh_trusts_fails_too(tmp_path: Path, monkeypatch, server) -> None:
    # Not pinned by ctun, but ssh's own known_hosts holds another key for the host.
    _config(tmp_path, monkeypatch, UNPINNED)
    server.update(trust="changed", conflicts=["/home/u/.ssh/known_hosts:3"])
    r = _hostkey()
    assert r.exit_code == 1
    assert "(the trusted key is in /home/u/.ssh/known_hosts:3)" in _norm(r)


def test_json(tmp_path: Path, monkeypatch, server) -> None:
    _config(tmp_path, monkeypatch, PINNED)
    server.update(trust="known", pinned=[_key("SHA256:new")])
    r = _hostkey("--json")
    assert r.exit_code == 0
    payload = json.loads(r.output)
    assert payload["trust"] == "known" and payload["matches"] is True and payload["strict"] is True
    assert payload["offered"]["fingerprint"] == "SHA256:new"

    server["trust"] = "changed"
    assert _hostkey("--json").exit_code == 1


def test_an_unreachable_server(tmp_path: Path, monkeypatch, server) -> None:
    _config(tmp_path, monkeypatch, UNPINNED)

    def refused(spec):
        raise RuntimeError("ssh: connect to host uc3.example port 22: Connection refused")

    monkeypatch.setattr("cluster_tunnel.hostkey.check", refused)
    r = _hostkey()
    assert r.exit_code == 1
    assert "cannot check the host key of 'bw': ssh: connect to host" in _norm(r)


@pytest.mark.parametrize("args", [["--yes"], ["--json", "--pin"]])
def test_option_combinations_that_make_no_sense(tmp_path: Path, monkeypatch, server, args) -> None:
    _config(tmp_path, monkeypatch, UNPINNED)
    assert _hostkey(*args).exit_code == 2


# --- pinning --------------------------------------------------------------------


def test_pin_without_a_terminal_only_shows_the_change(tmp_path: Path, monkeypatch, server) -> None:
    p = _config(tmp_path, monkeypatch, UNPINNED)
    _tty(monkeypatch, False)
    r = _hostkey("--pin")
    assert r.exit_code == 0, r.output
    out = _norm(r)
    assert f"Pinning SHA256:new for 'bw' in {tmp_path / 'known_hosts'}." in out
    assert "+ strict_host_key_checking: 'yes'" in out
    assert "ctun 0.6.0 and older can't read a config with these keys" in out
    assert "Compare the fingerprint with the one your cluster's operators publish" in out
    assert "Nothing pinned. Run `ctun -t bw hostkey --pin --yes` to pin it." in out
    assert p.read_text(encoding="utf-8") == UNPINNED and server["pins"] == []


def test_pin_with_yes(tmp_path: Path, monkeypatch, server) -> None:
    p = _config(tmp_path, monkeypatch, UNPINNED)
    _tty(monkeypatch, False)
    r = _hostkey("--pin", "--yes")
    assert r.exit_code == 0, r.output
    file = str(tmp_path / "known_hosts")
    assert server["pins"] == [(file, [_key("SHA256:new")])]
    text = p.read_text(encoding="utf-8")
    assert "uc3.example  # login node\n    strict_host_key_checking: 'yes'\n" in text
    assert f"known_hosts_file: {file}" in text
    assert "✓ Pinned: logins to 'bw' now accept only SHA256:new." in _norm(r)
    assert (tmp_path / "config.yaml.bak").read_text(encoding="utf-8") == UNPINNED


@pytest.mark.parametrize("answer, pinned", [("y\n", True), ("n\n", False)])
def test_pin_asks_on_a_terminal(tmp_path: Path, monkeypatch, server, answer, pinned) -> None:
    _config(tmp_path, monkeypatch, UNPINNED)
    _tty(monkeypatch, True)
    r = _hostkey("--pin", input=answer)
    assert r.exit_code == 0, r.output
    assert "Pin this key?" in r.output
    assert bool(server["pins"]) is pinned


@pytest.mark.parametrize(
    "text, conflicts",
    [(PINNED, ["/kh:1"]), (UNPINNED, ["/home/u/.ssh/known_hosts:3"])],
    ids=["pinned key", "key ssh knows"],
)
def test_a_changed_key_needs_a_terminal_even_with_yes(tmp_path: Path, monkeypatch, server, text, conflicts) -> None:
    p = _config(tmp_path, monkeypatch, text)
    server.update(trust="changed", conflicts=conflicts)
    _tty(monkeypatch, False)
    r = _hostkey("--pin", "--yes")
    assert r.exit_code == 1
    assert "re-pin it from a terminal (--yes does not apply to a changed key)" in _norm(r)
    assert server["pins"] == [] and p.read_text(encoding="utf-8") == text


def test_a_changed_key_is_re_pinned_after_asking_on_a_terminal(tmp_path: Path, monkeypatch, server) -> None:
    p = _config(tmp_path, monkeypatch, PINNED)
    server.update(trust="changed", pinned=[_key("SHA256:old")], conflicts=["/kh:1"])
    _tty(monkeypatch, True)
    r = _hostkey("--pin", "--yes", input="y\n")
    assert r.exit_code == 0, r.output
    assert "Replace the trusted key with this one?" in r.output
    assert server["pins"] == [("/kh", [_key("SHA256:new")])]
    assert p.read_text(encoding="utf-8") == PINNED  # already strict with this file
    assert "previous config" not in _norm(r)


def test_pin_when_already_pinned(tmp_path: Path, monkeypatch, server) -> None:
    _config(tmp_path, monkeypatch, PINNED)
    server.update(trust="known", pinned=[_key("SHA256:new")])
    r = _hostkey("--pin")
    assert r.exit_code == 0
    assert "Already pinned; nothing to do." in _norm(r)
    assert server["pins"] == []


def test_pin_refuses_when_the_key_changes_while_asking(tmp_path: Path, monkeypatch, server) -> None:
    # The key is checked again right before pinning; a different one pins nothing.
    p = _config(tmp_path, monkeypatch, UNPINNED)
    server["fingerprints"] = ["SHA256:new", "SHA256:swapped"]
    _tty(monkeypatch, False)
    r = _hostkey("--pin", "--yes")
    assert r.exit_code == 14
    assert "presents SHA256:swapped, not SHA256:new; nothing was pinned." in _norm(r)
    assert "ctun-error: host_key_mismatch" in _norm(r)
    assert server["pins"] == [] and p.read_text(encoding="utf-8") == UNPINNED
