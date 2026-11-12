"""Host-key pinning: the sh helper (run for real) and how ssh's words are read.

The helper tests run ``sh`` and ``ssh-keygen`` for real, with a fake ``ssh``
that records a generated key the way a server's key exchange would.
``CTUN_TEST_SSHD=<host>`` adds checks against a real SSH server (no login needed).
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path, PurePosixPath

import pytest

from cluster_tunnel import hostkey, wsl
from cluster_tunnel.ssh import ConnSpec

needs_tools = pytest.mark.skipif(
    sys.platform == "win32" or not all(shutil.which(t) for t in ("sh", "ssh-keygen")),
    reason="runs the POSIX sh helper with ssh-keygen",
)
needs_sshd = pytest.mark.skipif(
    not os.environ.get("CTUN_TEST_SSHD") or sys.platform == "win32",
    reason="set CTUN_TEST_SSHD=<host> to check a real server",
)


def _spec(target: str = "fakehost") -> ConnSpec:
    return ConnSpec("k", target, Path("sock"), "12h", 60, 3, None)


def _key(tmp_path: Path, host: str, name: str) -> tuple[str, str]:
    """A fresh ed25519 key: its known_hosts line for `host`, and its fingerprint."""
    key = tmp_path / name
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    kind, data = Path(f"{key}.pub").read_text(encoding="utf-8").split()[:2]
    printed = subprocess.run(
        ["ssh-keygen", "-l", "-f", f"{key}.pub"],
        capture_output=True, text=True, encoding="utf-8", check=True,
    ).stdout
    return f"{host} {kind} {data}", printed.split()[1]


def _fake_ssh(tmp_path: Path, monkeypatch, *, line: str | None, stderr: str) -> None:
    """Put an ``ssh`` on PATH that writes `line` to its UserKnownHostsFile, then fails."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "ssh"
    write = f"printf '%s\\n' '{line}' > \"$f\"\n" if line else ""
    script.write_text(
        "#!/bin/sh\n"
        "for arg in \"$@\"; do\n"
        "  case $arg in UserKnownHostsFile=*) f=${arg#UserKnownHostsFile=}; f=${f#\\\"}; f=${f%\\\"} ;; esac\n"
        "done\n"
        f"{write}"
        f"echo '{stderr}' >&2\n"
        "exit 255\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")


# --- the helper, for real ---------------------------------------------------------


@needs_tools
def test_scan_returns_the_key_the_server_offered(tmp_path: Path, monkeypatch) -> None:
    line, fingerprint = _key(tmp_path, "fakehost", "server")
    _fake_ssh(tmp_path, monkeypatch, line=line, stderr="Permission denied (publickey).")
    [key] = hostkey.scan(_spec())
    assert (key.host, key.kind, key.bits, key.fingerprint, key.line) == (
        "fakehost", "ED25519", 256, fingerprint, line,
    )


@needs_tools
def test_scan_reports_what_ssh_said_when_no_key_came(tmp_path: Path, monkeypatch) -> None:
    _fake_ssh(tmp_path, monkeypatch, line=None, stderr="ssh: connect to host fakehost port 22: Connection refused")
    with pytest.raises(RuntimeError, match="Connection refused"):
        hostkey.scan(_spec())


@needs_tools
def test_pin_replaces_only_the_hosts_own_lines(tmp_path: Path) -> None:
    file = str(tmp_path / "keys" / "known_hosts")  # its directory is created
    old, _ = _key(tmp_path, "bw", "old")
    other, other_fp = _key(tmp_path, "horeka", "other")
    new, new_fp = _key(tmp_path, "bw", "new")
    spec = _spec()

    hostkey.pin(spec, file, hostkey.parse(f"{old}\n--\n256 SHA256:x bw (ED25519)\n"))
    hostkey.pin(spec, file, hostkey.parse(f"{other}\n--\n256 {other_fp} horeka (ED25519)\n"))
    hostkey.pin(spec, file, hostkey.parse(f"{new}\n--\n256 {new_fp} bw (ED25519)\n"))

    assert Path(file).read_text(encoding="utf-8") == f"{other}\n{new}\n"
    assert [k.fingerprint for k in hostkey.pinned(spec, file, "bw")] == [new_fp]
    assert [k.fingerprint for k in hostkey.pinned(spec, file, "horeka")] == [other_fp]
    assert hostkey.pinned(spec, file, "nobody") == []
    assert hostkey.pinned(spec, str(tmp_path / "missing"), "bw") == []
    assert sorted(p.name for p in Path(file).parent.iterdir()) == ["known_hosts"]  # no temp, no .old


@needs_tools
def test_unpin_removes_only_the_hosts_own_lines(tmp_path: Path) -> None:
    file = str(tmp_path / "known_hosts")
    bw, bw_fp = _key(tmp_path, "bw", "bw")
    other, other_fp = _key(tmp_path, "horeka", "other")
    spec = _spec()
    hostkey.pin(spec, file, hostkey.parse(f"{bw}\n--\n256 {bw_fp} bw (ED25519)\n"))
    hostkey.pin(spec, file, hostkey.parse(f"{other}\n--\n256 {other_fp} horeka (ED25519)\n"))

    hostkey.unpin(spec, file, "bw")
    assert Path(file).read_text(encoding="utf-8") == f"{other}\n"
    assert hostkey.pinned(spec, file, "bw") == []
    hostkey.unpin(spec, file, "nobody")  # nothing for it: nothing changes
    assert Path(file).read_text(encoding="utf-8") == f"{other}\n"
    hostkey.unpin(spec, str(tmp_path / "missing" / "known_hosts"), "bw")  # no file: nothing to do
    assert not (tmp_path / "missing").exists()
    assert sorted(p.name for p in tmp_path.iterdir() if p.name.startswith("known_hosts")) == ["known_hosts"]


@needs_tools
def test_a_leading_tilde_means_home(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    line, fingerprint = _key(tmp_path, "bw", "server")
    hostkey.pin(_spec(), "~/.config/ctun/known_hosts", hostkey.parse(f"{line}\n--\n256 {fingerprint} bw (ED25519)\n"))
    assert (tmp_path / ".config" / "ctun" / "known_hosts").read_text(encoding="utf-8") == f"{line}\n"
    assert hostkey.pinned(_spec(), "~/.config/ctun/known_hosts", "bw")[0].fingerprint == fingerprint


def test_pin_takes_one_host_at_a_time() -> None:
    keys = hostkey.parse("a ssh-ed25519 AAA\nb ssh-ed25519 BBB\n--\n256 SHA256:a a (ED25519)\n256 SHA256:b b (ED25519)\n")
    with pytest.raises(ValueError, match="one host"):
        hostkey.pin(_spec(), "kh", keys)


@needs_tools
def test_hashed_entries_count_and_are_replaced(tmp_path: Path) -> None:
    # A known_hosts written with HashKnownHosts (Ubuntu's default) hides the host names.
    old, old_fp = _key(tmp_path, "bw", "old")
    other, _ = _key(tmp_path, "horeka", "other")
    file = tmp_path / "known_hosts"
    file.write_text(f"{old}\n{other}", encoding="utf-8")  # no newline at the end
    subprocess.run(["ssh-keygen", "-H", "-f", str(file)], check=True, capture_output=True)
    (tmp_path / "known_hosts.old").unlink()  # ssh-keygen's backup of the plain file
    hosts = [line.split()[0] for line in file.read_text(encoding="utf-8").splitlines()]
    assert all(host.startswith("|1|") for host in hosts)  # no host name in sight

    assert [k.fingerprint for k in hostkey.pinned(_spec(), str(file), "bw")] == [old_fp]
    new, new_fp = _key(tmp_path, "bw", "new")
    hostkey.pin(_spec(), str(file), hostkey.parse(f"{new}\n--\n256 {new_fp} bw (ED25519)\n"))
    assert [k.fingerprint for k in hostkey.pinned(_spec(), str(file), "bw")] == [new_fp]
    assert len(hostkey.pinned(_spec(), str(file), "horeka")) == 1
    assert not (tmp_path / "known_hosts.old").exists()


@needs_tools
def test_pin_keeps_a_backup_that_was_already_there(tmp_path: Path) -> None:
    line, fingerprint = _key(tmp_path, "bw", "server")
    file = tmp_path / "known_hosts"
    file.write_text(f"{line}\n", encoding="utf-8")
    (tmp_path / "known_hosts.old").write_text("theirs\n", encoding="utf-8")
    hostkey.pin(_spec(), str(file), hostkey.parse(f"{line}\n--\n256 {fingerprint} bw (ED25519)\n"))
    assert (tmp_path / "known_hosts.old").read_text(encoding="utf-8") == "theirs\n"


# --- against a real server (opt-in) ------------------------------------------------


def _real(tmp_path: Path, known_hosts: str | None) -> ConnSpec:
    spec = _spec(os.environ.get("CTUN_TEST_SSHD", "localhost"))
    spec.known_hosts_file = known_hosts
    return spec


@needs_sshd
def test_a_real_server_new_known_and_changed(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.write_text("", encoding="utf-8")
    new = hostkey.check(_real(tmp_path, str(empty)))
    assert new.trust == "new" and new.fingerprint.startswith("SHA256:")

    [key] = hostkey.scan(_real(tmp_path, None), new.algorithm)
    assert key.fingerprint == new.fingerprint
    good = tmp_path / "good"
    hostkey.pin(_real(tmp_path, None), str(good), [key])
    known = hostkey.check(_real(tmp_path, str(good)))
    assert known.trust == "known" and known.fingerprint == key.fingerprint

    other, _ = _key(tmp_path, key.host, "other")
    bad = tmp_path / "bad"
    bad.write_text(f"{other}\n", encoding="utf-8")
    changed = hostkey.check(_real(tmp_path, str(bad)))
    assert changed.trust == "changed" and changed.conflicts == [f"{bad}:1"]


# --- reading the helper's output ---------------------------------------------------


def test_parse_pairs_lines_with_fingerprints() -> None:
    out = (
        "[uc3.example]:2222 ssh-rsa AAAAB3\n"
        "--\n"
        "3072 SHA256:8szdMQTAZ [uc3.example]:2222 (RSA)\n"
    )
    [key] = hostkey.parse(out)
    assert (key.host, key.kind, key.bits, key.fingerprint) == ("[uc3.example]:2222", "RSA", 3072, "SHA256:8szdMQTAZ")
    assert key.line == "[uc3.example]:2222 ssh-rsa AAAAB3"
    assert hostkey.parse("") == []


@pytest.mark.parametrize("out", ["h ssh-ed25519 AAA\n", "h ssh-ed25519 AAA\n--\n"])
def test_parse_refuses_output_it_cannot_pair(out: str) -> None:
    with pytest.raises(RuntimeError):
        hostkey.parse(out)


# --- where it runs -------------------------------------------------------------------


def test_default_file_sits_next_to_the_config(tmp_path: Path, monkeypatch) -> None:
    assert hostkey.default_file(tmp_path / "config.yaml") == str(tmp_path / "known_hosts")
    monkeypatch.chdir(tmp_path)  # a relative --config still pins to an absolute file
    assert hostkey.default_file(Path("config.yaml")) == str(Path.cwd() / "known_hosts")
    monkeypatch.setattr(wsl, "enabled", lambda: True)  # ssh runs inside WSL: a path there
    assert hostkey.default_file(tmp_path / "config.yaml") == wsl.DEFAULT_KNOWN_HOSTS


# --- reading ssh's verdict ------------------------------------------------------------

_SERVER = (
    "debug1: kex: host key algorithm: ssh-ed25519\n"
    "debug1: Server host key: ssh-ed25519 SHA256:1usCqqf113Q0zG/Llupry8XHwMCA4h8VIPwCzr+7nFE\n"
)


@pytest.mark.parametrize(
    "said, trust, conflicts",
    [
        ("debug1: Host 'localhost' is known and matches the ED25519 host key.\n"
         "jonas@localhost: Permission denied (publickey,password).\n", "known", []),
        ("No ED25519 host key is known for localhost and you have requested strict checking.\n"
         "Host key verification failed.\n", "new", []),
        ("@    WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!     @\n"
         "Offending ED25519 key in /home/u/my keys/known_hosts:3\n"
         "Host key verification failed.\n", "changed", ["/home/u/my keys/known_hosts:3"]),
    ],
)
def test_parse_check(said: str, trust: str, conflicts: list) -> None:
    verdict = hostkey.parse_check(_SERVER + said)
    assert (verdict.trust, verdict.algorithm, verdict.conflicts) == (trust, "ssh-ed25519", conflicts)
    assert verdict.fingerprint == "SHA256:1usCqqf113Q0zG/Llupry8XHwMCA4h8VIPwCzr+7nFE"


def test_parse_check_of_an_unreachable_server() -> None:
    said = "debug1: Connecting to h [10.0.0.1] port 22.\nssh: connect to host h port 22: Connection refused\n"
    with pytest.raises(RuntimeError, match="Connection refused") as info:
        hostkey.parse_check(said)
    assert "debug1" not in str(info.value)


def test_parse_check_refuses_to_guess() -> None:
    with pytest.raises(RuntimeError, match="could not tell"):
        hostkey.parse_check(_SERVER + "debug1: something else entirely\n")


# ssh -v through a ProxyJump (OpenSSH 9.6): the jump connection's lines come first.
_JUMP = (
    "debug1: Executing proxy command: exec ssh -v -W '[login2]:22' gate\n"
    "debug1: Authenticating to gate:22 as 'u'\n"
    "debug1: kex: host key algorithm: rsa-sha2-512\n"
    "debug1: Server host key: ssh-rsa SHA256:O2DTboTllKGqhQoPIVnj/lvHs034dr7DeIYH+TV6GrU\n"
    "debug1: Host 'gate' is known and matches the RSA host key.\n"
)
_JUMPED = 'Authenticated to gate ([10.0.0.1]:22) using "publickey".\n'


def test_parse_check_through_a_jump_host_reads_the_clusters_lines() -> None:
    said = "No ED25519 host key is known for login2 and you have requested strict checking.\n"
    verdict = hostkey.parse_check(_JUMP + _JUMPED + "debug1: Authenticating to login2:22 as 'u'\n" + _SERVER + said)
    assert (verdict.trust, verdict.algorithm) == ("new", "ssh-ed25519")
    assert verdict.fingerprint == "SHA256:1usCqqf113Q0zG/Llupry8XHwMCA4h8VIPwCzr+7nFE"


@pytest.mark.parametrize(
    "said",
    [
        _JUMP + _JUMPED + "channel 0: open failed: connect failed: Connection refused\n"
        "kex_exchange_identification: Connection closed by remote host\n",  # the cluster is down
        _JUMP + "Host key verification failed.\n",  # the jump host itself is refused
    ],
)
def test_parse_check_never_takes_the_jump_hosts_key(said: str) -> None:
    with pytest.raises(RuntimeError):
        hostkey.parse_check(said)


def test_check_runs_ssh_verbosely_with_the_masters_known_hosts(monkeypatch) -> None:
    seen: dict = {}

    def fake_captured(spec, argv, stdin=None, timeout=None):
        seen.update(argv=argv, timeout=timeout)
        return subprocess.CompletedProcess(argv, 255, "", _SERVER + "Host key verification failed.\n")

    monkeypatch.setattr("cluster_tunnel.ssh.captured", fake_captured)
    spec = _spec("bw.example")
    spec.known_hosts_file = "/kh"
    assert hostkey.check(spec).trust == "new"
    argv = seen["argv"]
    assert argv[:2] == ["ssh", "-v"] and argv[-2:] == ["bw.example", "true"]
    assert "StrictHostKeyChecking=yes" in argv and f'UserKnownHostsFile="{Path("/kh")}"' in argv
    assert "GlobalKnownHostsFile=/dev/null" in argv and seen["timeout"] == hostkey.TIMEOUT


def test_windows_runs_the_helper_inside_wsl_with_unix_line_ends(monkeypatch) -> None:
    seen: dict = {}

    def fake_run(argv, **kwargs):
        seen.update(argv=argv, **kwargs)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(wsl, "enabled", lambda: True)
    monkeypatch.setattr(subprocess, "run", fake_run)
    spec = ConnSpec("k", "h", PurePosixPath("~/s/k"), "12h", 60, 3, None, wsl_distro="Ubuntu")
    assert hostkey.pinned(spec, "~/.config/cluster-tunnel/known_hosts", "h") == []
    assert seen["argv"][:5] == ["wsl.exe", "-d", "Ubuntu", "-e", "sh"]
    assert seen["argv"][-3:] == ["pinned", "~/.config/cluster-tunnel/known_hosts", "h"]
    assert isinstance(seen["input"], bytes) and b"\r\n" not in seen["input"]


def test_a_missing_sh_is_reported(monkeypatch) -> None:
    def missing(argv, **kwargs):
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(subprocess, "run", missing)
    with pytest.raises(RuntimeError, match="sh"):
        hostkey.scan(_spec())
