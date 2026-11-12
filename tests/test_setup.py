"""Setting clusters up: presets, `add`/`add_cluster`, `remove`/`remove_cluster`, `hostkey --pin --expect`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from cluster_tunnel import api, configedit, hostkey, presets, ssh
from cluster_tunnel.cli import cli, configcmd
from cluster_tunnel.config import Config
from cluster_tunnel.constants import ExitCode
from cluster_tunnel.hostkey import HostKey

from tests.test_cli import _norm

EXISTING = (
    "# my clusters\n"
    "defaults:\n"
    "  terminal: auto\n"
    "\n"
    "clusters:\n"
    "  c:\n"
    "    host: h.example  # the old one\n"
    "    user: u\n"
    "\n"
    "# the end\n"
)

BW = "SHA256:5mZYEpKigwK5ibBMHRrh3WIkOtCqomJW6H7OMbPk3ec"


@pytest.fixture
def config(tmp_path: Path, monkeypatch) -> Path:
    """Where the config goes ($CTUN_CONFIG); the file is not created."""
    p = tmp_path / "cfg" / "config.yaml"
    monkeypatch.setenv("CTUN_CONFIG", str(p))
    return p


def _write(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _cli(*args: str, input: str | None = None):
    return CliRunner().invoke(cli, list(args), input=input)


def _clusters(p: Path) -> dict:
    return (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("clusters") or {}


# --- presets -------------------------------------------------------------------------


def test_presets_name_the_known_clusters() -> None:
    names = [preset.name for preset in api.presets()]
    assert names == ["bwuni3", "horeka", "haicore", "kcist", "intnano"]
    assert presets.get("UC3").name == "bwuni3" and presets.get("int-nano").name == "intnano"
    with pytest.raises(KeyError, match="Unknown preset 'nope'. Known: bwuni3, horeka"):
        presets.get("nope")


def test_only_published_fingerprints_ship_and_each_names_its_source() -> None:
    published = {p.name: p.fingerprint for p in api.presets() if p.fingerprint}
    assert published == {
        "bwuni3": BW,
        "horeka": "SHA256:yEe5nJ5hZZ1YbgieWr+phqRZKYbrV7zRe8OR3X03cn0",
        "haicore": "SHA256:yEe5nJ5hZZ1YbgieWr+phqRZKYbrV7zRe8OR3X03cn0",
    }
    for preset in api.presets():
        assert bool(preset.fingerprint) == bool(preset.fingerprint_source)
        if preset.fingerprint_source:
            assert preset.fingerprint_source.startswith("https://")


def test_every_preset_budget_guards_every_command_with_a_bundled_script() -> None:
    from cluster_tunnel.config import PACKAGE_DIR

    for preset in api.presets():
        assert preset.budget is not None
        entry = preset.budget.entry()
        assert entry["guard_scope"] == "every-command" and entry["fail_mode"] == "closed"
        assert (PACKAGE_DIR / "budget_templates" / preset.budget.script).is_file()


def test_presets_json_is_the_apis() -> None:
    r = _cli("presets", "--json")
    assert r.exit_code == 0, r.output
    assert json.loads(r.output) == {"presets": [p.as_dict() for p in api.presets()]}


def test_presets_says_where_a_key_is_not_published() -> None:
    out = _norm(_cli("presets"))
    assert BW in out and "not published: compare and confirm it" in out and "(give --host)" in out


# --- add_cluster ----------------------------------------------------------------------


def test_add_a_preset_to_an_existing_config(config: Path) -> None:
    _write(config, EXISTING)
    added = api.add_cluster("bwuni3", user="ka_ab1234", preset="bwuni3")

    text = config.read_text(encoding="utf-8")
    assert text.startswith(EXISTING.split("\n# the end")[0])  # every earlier line as it was
    assert "    host: h.example  # the old one\n" in text and text.endswith("# the end\n")
    assert (config.parent / "config.yaml.bak").read_text(encoding="utf-8") == EXISTING
    entry = _clusters(config)["bwuni3"]
    assert entry == added.entry
    assert entry["host"] == "uc3.scc.kit.edu" and entry["user"] == "ka_ab1234" and entry["requires_otp"] is True
    assert entry["budget"]["script"] == "budget/bwuni3.sh" and entry["budget"]["guard_scope"] == "every-command"
    assert "  # added via ctun add (preset bwuni3), " in text
    assert added.fingerprint == BW and added.preset == "bwuni3" and not added.created
    assert added.budget_script == str(config.parent / "budget" / "bwuni3.sh")
    assert (config.parent / "budget" / "bwuni3.sh").is_file()
    assert "bwuni3" in api.load_config().clusters


def test_add_creates_a_missing_config(config: Path) -> None:
    added = api.add_cluster("kc", user="ab1234", preset="kcist")
    assert added.created and added.backup is None and added.fingerprint is None
    config_now = api.load_config()  # a fresh `config --init` file takes the entry and loads
    assert list(config_now.clusters) == ["kc"]
    assert config_now.clusters["kc"].budget.script == "budget/kcist.sh"
    assert not (config.parent / "config.yaml.bak").exists()


def test_add_keeps_a_budget_script_that_is_there(config: Path) -> None:
    _write(config, EXISTING)
    _write(config.parent / "budget" / "bwuni3.sh", "# mine\n")
    added = api.add_cluster("bwuni3", user="ka_x", preset="bwuni3")
    assert added.budget_script is None
    assert (config.parent / "budget" / "bwuni3.sh").read_text(encoding="utf-8") == "# mine\n"


def test_add_without_a_preset_has_no_budget(config: Path) -> None:
    _write(config, EXISTING)
    added = api.add_cluster("mine", user="me", host="login.example.org", requires_otp=True,
                            requires_password=False, description=["My group's cluster."])
    assert added.entry == {"host": "login.example.org", "user": "me", "requires_otp": True,
                           "requires_password": False, "description": ["My group's cluster."]}
    assert _clusters(config)["mine"] == added.entry


def test_add_overrides_what_the_preset_says(config: Path) -> None:
    added = api.add_cluster("bw", user="ka_x", preset="bwuni3", host="uc3-login1.scc.kit.edu",
                            requires_otp=False, session_limit=50)
    assert added.entry["host"] == "uc3-login1.scc.kit.edu" and added.entry["requires_otp"] is False
    assert added.entry["budget"]["session_limit"] == 50


@pytest.mark.parametrize("kwargs, message", [
    ({"name": "1st", "user": "u", "host": "h.example"}, "invalid cluster name"),
    ({"name": "a b", "user": "u", "host": "h.example"}, "invalid cluster name"),
    ({"name": "x", "user": "-oProxyCommand=x", "host": "h.example"}, "invalid user"),
    ({"name": "x", "user": "u@v", "host": "h.example"}, "invalid user"),
    ({"name": "x", "user": "u", "host": "-oProxyCommand=evil"}, "invalid host"),
    ({"name": "x", "user": "u", "host": "h .example"}, "invalid host"),
    ({"name": "x", "user": "u"}, "no host for 'x'"),
    ({"name": "x", "user": "u", "preset": "intnano"}, "preset 'intnano' has none"),
    ({"name": "x", "user": "u", "preset": "nope"}, "Unknown preset 'nope'"),
    ({"name": "x", "user": "u", "host": "h.example", "session_limit": 5}, "only a preset brings one"),
])
def test_add_refuses_bad_input_and_writes_nothing(config: Path, kwargs: dict, message: str) -> None:
    _write(config, EXISTING)
    name = kwargs.pop("name")
    with pytest.raises(api.ConfigError, match=message):
        api.add_cluster(name, **kwargs)
    assert config.read_text(encoding="utf-8") == EXISTING
    assert sorted(p.name for p in config.parent.iterdir()) == ["config.yaml"]


def test_add_refuses_a_name_the_config_has(config: Path) -> None:
    _write(config, EXISTING)
    with pytest.raises(api.ClusterExistsError, match="already has a cluster 'c'") as info:
        api.add_cluster("c", user="u", host="h.example")
    assert info.value.cluster == "c" and isinstance(info.value, api.ConfigError)
    assert config.read_text(encoding="utf-8") == EXISTING


def test_add_quotes_a_name_yaml_would_read_otherwise(config: Path) -> None:
    _write(config, EXISTING)
    api.add_cluster("yes", user="u", host="h.example")
    assert "yes" in api.load_config().clusters


# --- the config edits ----------------------------------------------------------------


@pytest.mark.parametrize("text", [
    EXISTING,
    "clusters: {}  # none yet\n",
    "defaults:\n  terminal: auto",
    "",
    Path(__file__).parents[1].joinpath("cluster_tunnel", "config.example.yaml").read_text(encoding="utf-8"),
])
def test_add_then_remove_gives_back_the_same_data(tmp_path: Path, text: str) -> None:
    p = _write(tmp_path / "config.yaml", text)
    entry = {"host": "h2.example", "user": "u", "requires_otp": True, "description": ["a: b", "two"],
             "budget": {"script": "budget/x.sh", "session_limit": 5, "guard_commands": ["sbatch", "srun"]}}
    configedit.apply(configedit.plan_add_cluster(p, "new", entry, "# added via ctun add, 2026-01-01"))
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert data["clusters"]["new"] == entry
    Config(**data)
    configedit.apply(configedit.plan_remove_cluster(p, "new"))
    before = yaml.safe_load(text) or {}
    before["clusters"] = before.get("clusters") or {}
    assert yaml.safe_load(p.read_text(encoding="utf-8")) == before
    assert "added via ctun add" not in p.read_text(encoding="utf-8")


def test_remove_keeps_the_comments_around_the_entry(tmp_path: Path) -> None:
    p = _write(tmp_path / "config.yaml", EXISTING.replace("\n# the end", "  # second\n  d:\n    host: d.example\n\n# the end"))
    configedit.apply(configedit.plan_remove_cluster(p, "c"))
    assert p.read_text(encoding="utf-8") == (
        "# my clusters\ndefaults:\n  terminal: auto\n\nclusters:\n  # second\n  d:\n    host: d.example\n\n# the end\n"
    )


def test_remove_of_the_last_cluster_leaves_an_empty_mapping(tmp_path: Path) -> None:
    p = _write(tmp_path / "config.yaml", EXISTING)
    configedit.apply(configedit.plan_remove_cluster(p, "c"))
    assert yaml.safe_load(p.read_text(encoding="utf-8"))["clusters"] == {}


def test_edits_refuse_a_flow_mapping_with_entries(tmp_path: Path) -> None:
    p = _write(tmp_path / "config.yaml", "clusters: {c: {host: h}}\n")
    with pytest.raises(configedit.EditError, match="by hand"):
        configedit.plan_add_cluster(p, "d", {"host": "d"})
    with pytest.raises(configedit.EditError, match="by hand"):
        configedit.plan_remove_cluster(p, "c")


# --- remove_cluster ------------------------------------------------------------------

PINNED = (
    "clusters:\n"
    "  bw:\n"
    "    host: uc3.example\n"
    "    strict_host_key_checking: 'yes'\n"
    "    known_hosts_file: /kh\n"
    "  other:\n"
    "    host: o.example\n"
)


@pytest.fixture
def keys(monkeypatch) -> dict:
    """Stubbed key file: ``held`` maps host -> pinned fingerprints; ``unpinned`` records removals."""
    state: dict = {"held": {"uc3.example": ["SHA256:k"]}, "unpinned": []}

    def pinned(spec, file, host):
        return [HostKey(host, "ED25519", 256, fp, f"{host} ssh-ed25519 X") for fp in state["held"].get(host, [])]

    def unpin(spec, file, host):
        state["unpinned"].append((file, host))
        state["held"].pop(host, None)

    monkeypatch.setattr(hostkey, "pinned", pinned)
    monkeypatch.setattr(hostkey, "unpin", unpin)
    return state


def test_remove_takes_the_entry_its_keys_and_its_tunnel(config: Path, keys: dict, monkeypatch) -> None:
    _write(config, PINNED)
    closed = []
    monkeypatch.setattr(ssh, "is_live", lambda spec: spec.name == "bw")
    monkeypatch.setattr(ssh, "close", lambda spec: closed.append(spec.name))
    removed = api.remove_cluster("bw")
    assert removed.logged_out and closed == ["bw"]
    assert removed.keys_removed == 1 and keys["unpinned"] == [("/kh", "uc3.example")]
    assert list(_clusters(config)) == ["other"]
    assert (config.parent / "config.yaml.bak").read_text(encoding="utf-8") == PINNED


def test_remove_keeps_keys_another_cluster_shares(config: Path, keys: dict, monkeypatch) -> None:
    shared = PINNED.replace("o.example", "uc3.example") + "    known_hosts_file: /kh\n"
    _write(config, shared)
    monkeypatch.setattr(ssh, "is_live", lambda spec: False)
    monkeypatch.setattr(ssh, "close", lambda spec: None)
    removed = api.remove_cluster("bw")
    assert removed.keys_removed == 0 and keys["unpinned"] == []
    assert removed.keys_kept == "other uses the same host and key file"


def test_remove_of_an_unknown_cluster_changes_nothing(config: Path, keys: dict) -> None:
    _write(config, PINNED)
    with pytest.raises(api.UnknownClusterError):
        api.remove_cluster("nope")
    assert config.read_text(encoding="utf-8") == PINNED


# --- the commands --------------------------------------------------------------------


def test_add_command_prints_the_next_steps(config: Path) -> None:
    _write(config, EXISTING)
    r = _cli("add", "bwuni3", "--preset", "bwuni3", "--user", "ka_ab1234")
    assert r.exit_code == 0, r.output
    out = _norm(r)
    assert "✓ Added 'bwuni3' from preset 'bwuni3' (ka_ab1234@uc3.scc.kit.edu)." in out
    assert f"ctun -t bwuni3 hostkey --pin --expect {BW}" in out
    assert "ctun -t bwuni3 login" in out


def test_add_command_json_is_the_apis(config: Path, tmp_path: Path, monkeypatch) -> None:
    _write(config, EXISTING)
    r = _cli("add", "kc", "-p", "kcist", "-u", "ab1234", "--json")
    assert r.exit_code == 0, r.output
    via_cli = json.loads(r.output)
    other = _write(tmp_path / "api" / "config.yaml", EXISTING)
    via_api = api.add_cluster("kc", user="ab1234", preset="kcist", config_path=other).as_dict()
    for key in ("config_path", "backup", "budget_script"):
        via_cli.pop(key), via_api.pop(key)
    assert via_cli == via_api
    assert _clusters(config) == _clusters(other)


def test_add_command_reports_an_existing_name(config: Path) -> None:
    _write(config, EXISTING)
    r = _cli("add", "c", "--host", "h.example", "-u", "u")
    assert r.exit_code == 1 and "already has a cluster 'c'" in _norm(r)
    assert config.read_text(encoding="utf-8") == EXISTING


def test_remove_command_asks_first(config: Path, keys: dict, monkeypatch) -> None:
    _write(config, PINNED)
    monkeypatch.setattr(ssh, "is_live", lambda spec: False)
    monkeypatch.setattr(ssh, "close", lambda spec: None)
    monkeypatch.setattr(configcmd, "_stdin_is_tty", lambda: False)
    r = _cli("remove", "bw")
    assert r.exit_code == 0 and "Nothing removed. Run `ctun remove bw --yes`" in _norm(r)
    assert "- bw:" in _norm(r)  # the diff shows what goes
    assert config.read_text(encoding="utf-8") == PINNED

    monkeypatch.setattr(configcmd, "_stdin_is_tty", lambda: True)
    r = _cli("remove", "bw", input="n\n")
    assert "Nothing removed." in _norm(r) and config.read_text(encoding="utf-8") == PINNED
    r = _cli("remove", "bw", input="y\n")
    assert r.exit_code == 0 and "✓ Removed 'bw'" in _norm(r)
    assert "Removed its pinned host key from /kh." in _norm(r)
    assert list(_clusters(config)) == ["other"]


def test_remove_command_yes_json_is_the_apis(config: Path, tmp_path: Path, keys: dict, monkeypatch) -> None:
    _write(config, PINNED)
    monkeypatch.setattr(ssh, "is_live", lambda spec: False)
    monkeypatch.setattr(ssh, "close", lambda spec: None)
    r = _cli("remove", "bw", "--yes", "--json")
    assert r.exit_code == 0, r.output
    via_cli = json.loads(r.output)
    keys["held"]["uc3.example"] = ["SHA256:k"]
    other = _write(tmp_path / "api" / "config.yaml", PINNED)
    via_api = api.remove_cluster("bw", config_path=other).as_dict()
    for key in ("config_path", "backup"):
        via_cli.pop(key), via_api.pop(key)
    assert via_cli == via_api


def test_remove_command_json_needs_yes(config: Path) -> None:
    _write(config, PINNED)
    r = _cli("remove", "bw", "--json")
    assert r.exit_code == 2 and config.read_text(encoding="utf-8") == PINNED


def test_remove_command_names_the_known_clusters(config: Path) -> None:
    _write(config, PINNED)
    r = _cli("remove", "nope", "--yes")
    assert r.exit_code == 1 and "Unknown cluster 'nope'. Configured: bw, other" in _norm(r)


# --- pinning an expected fingerprint ---------------------------------------------------

UNPINNED = "clusters:\n  bw:\n    host: uc3.example\n"


@pytest.fixture
def server(monkeypatch) -> dict:
    """A stubbed server presenting ``state['now']``; ``pins`` records what was pinned."""
    state: dict = {"now": BW, "pins": []}
    monkeypatch.setattr(hostkey, "check", lambda spec: hostkey.Check("new", "ssh-ed25519", state["now"], []))
    monkeypatch.setattr(hostkey, "scan", lambda spec, algorithm=None: [
        HostKey("uc3.example", "ED25519", 256, state["now"], "uc3.example ssh-ed25519 X")])
    monkeypatch.setattr(hostkey, "pinned", lambda spec, file, host: [])
    monkeypatch.setattr(hostkey, "pin", lambda spec, file, keys: state["pins"].append(file))
    return state


def test_pin_expect_pins_the_published_key_without_asking(config: Path, server: dict, monkeypatch) -> None:
    _write(config, UNPINNED)
    monkeypatch.setattr(configcmd, "_stdin_is_tty", lambda: False)  # no one to ask
    r = _cli("-t", "bw", "hostkey", "--pin", "--expect", BW)
    assert r.exit_code == 0, r.output
    assert "It is the fingerprint you expected." in _norm(r) and f"✓ Pinned: logins to 'bw' now accept only {BW}" in _norm(r)
    assert len(server["pins"]) == 1
    assert _clusters(config)["bw"]["strict_host_key_checking"] == "yes"


def test_pin_expect_refuses_another_key_with_its_own_exit_code(config: Path, server: dict) -> None:
    _write(config, UNPINNED)
    server["now"] = "SHA256:someoneelse"
    r = _cli("-t", "bw", "hostkey", "--pin", "--expect", BW)
    assert r.exit_code == ExitCode.HOST_KEY_MISMATCH == 14
    assert "ctun-error: host_key_mismatch" in _norm(r) and "nothing was pinned" in _norm(r)
    assert server["pins"] == [] and config.read_text(encoding="utf-8") == UNPINNED


def test_api_pin_of_another_key_raises_a_mismatch(config: Path, server: dict) -> None:
    _write(config, UNPINNED)
    server["now"] = "SHA256:someoneelse"
    with pytest.raises(api.HostKeyMismatch) as info:
        api.pin_host_key("bw", BW)
    err = info.value
    assert (err.exit_code, err.marker) == (14, "host_key_mismatch")
    assert (err.offered, err.expected, err.cluster) == ("SHA256:someoneelse", BW, "bw")
    assert isinstance(err, api.HostKeyError) and server["pins"] == []


def test_expect_goes_only_with_pin(config: Path, server: dict) -> None:
    _write(config, UNPINNED)
    r = _cli("-t", "bw", "hostkey", "--expect", BW)
    assert r.exit_code == 2
