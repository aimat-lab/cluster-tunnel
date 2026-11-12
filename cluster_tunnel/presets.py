"""Ready-made cluster entries for the clusters ctun knows: `ctun presets`, `ctun add --preset`.

A preset is what the commented examples in ``config.example.yaml`` describe,
in a form :func:`cluster_tunnel.api.add_cluster` writes into the config: the
login host, what a login asks for, the bundled budget script with its default
limit, and a short description.

``fingerprint`` is the ED25519 host-key fingerprint the cluster's operators
publish, with the page it is published on in ``fingerprint_source``. It is
``None`` where the operators publish none: ctun never ships a key it only saw
itself, since a key seen on first contact could be anyone's. A front end pins
a preset's key with ``api.pin_host_key(name, preset.fingerprint)``, which
refuses (:class:`~cluster_tunnel.errors.HostKeyMismatch`) unless the server
presents exactly that key; without a published fingerprint a person has to
compare the key the server presents and confirm it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional

#: The commands every bundled preset's budget guard checks, wherever they start.
_GUARD_COMMANDS = ("sbatch", "srun", "salloc", "aslurm", "aslurmx")


@dataclass(frozen=True)
class PresetBudget:
    """The budget guard a preset brings: a bundled script and its default limit."""

    script: str  # bundled in budget_templates/; written as budget/<script> next to the config
    session_limit: float
    unit: str = "jobh"
    guard_commands: tuple[str, ...] = _GUARD_COMMANDS
    guard_scope: str = "every-command"
    fail_mode: str = "closed"

    def entry(self) -> dict:
        """The ``budget:`` mapping as the config holds it."""
        return {
            "script": f"budget/{self.script}",
            "session_limit": self.session_limit,
            "unit": self.unit,
            "guard_commands": list(self.guard_commands),
            "guard_scope": self.guard_scope,
            "fail_mode": self.fail_mode,
        }


@dataclass(frozen=True)
class Preset:
    """One known cluster, ready to add."""

    name: str  # the cluster name `ctun add` suggests (and the default for `-t`)
    title: str
    host: Optional[str]  # None: the login host is not known; `ctun add` needs --host
    requires_otp: bool
    requires_password: bool = True
    user_hint: str = ""  # how the username looks there
    description: tuple[str, ...] = ()
    budget: Optional[PresetBudget] = None
    fingerprint: Optional[str] = None  # the operators' published ED25519 fingerprint
    fingerprint_source: Optional[str] = None  # where they publish it
    aliases: tuple[str, ...] = field(default=(), compare=False)

    def as_dict(self) -> dict:
        """As ``ctun presets --json`` prints it."""
        data = asdict(self)
        data["description"] = list(self.description)
        data["aliases"] = list(self.aliases)
        if self.budget is not None:
            data["budget"] = self.budget.entry()
        return data


PRESETS: tuple[Preset, ...] = (
    Preset(
        name="bwuni3",
        title="bwUniCluster 3.0 (KIT / bwHPC)",
        host="uc3.scc.kit.edu",
        requires_otp=True,
        user_hint="KIT users: 'ka_' + your KIT username, e.g. ka_ab1234",
        description=("bwUniCluster 3.0 (uc3) @ KIT/bwHPC: general-purpose state cluster.",),
        budget=PresetBudget("bwuni3.sh", 200),
        # Published on https://wiki.bwhpc.de/e/BwUniCluster3.0/Login ("Host Keys");
        # checked against the server on 2026-10-22 and 2026-11-12.
        fingerprint="SHA256:5mZYEpKigwK5ibBMHRrh3WIkOtCqomJW6H7OMbPk3ec",
        fingerprint_source="https://wiki.bwhpc.de/e/BwUniCluster3.0/Login",
        aliases=("uc3", "bwunicluster"),
    ),
    Preset(
        name="horeka",
        title="HoreKa (KIT, NHR)",
        host="horeka.scc.kit.edu",
        requires_otp=True,
        user_hint="your KIT username, e.g. ab1234",
        description=("HoreKa @ KIT (NHR). GPU jobs -> partition 'accelerated', CPU-only -> 'cpuonly'.",),
        budget=PresetBudget("horeka.sh", 500),
        # Published on https://www.nhr.kit.edu/userdocs/horeka/login ("Host authenticity");
        # checked against the server on 2026-11-12.
        fingerprint="SHA256:yEe5nJ5hZZ1YbgieWr+phqRZKYbrV7zRe8OR3X03cn0",
        fingerprint_source="https://www.nhr.kit.edu/userdocs/horeka/login",
    ),
    Preset(
        name="haicore",
        title="HAICORE (KIT, Helmholtz AI)",
        host="haicore.scc.kit.edu",
        requires_otp=True,
        user_hint="your KIT username, e.g. ab1234",
        description=("HAICORE @ KIT: Helmholtz AI GPU cluster for machine-learning workloads.",),
        budget=PresetBudget("haicore.sh", 200),
        # Published on https://www.nhr.kit.edu/userdocs/haicore/login (the same key as
        # HoreKa's); checked against the server on 2026-11-12.
        fingerprint="SHA256:yEe5nJ5hZZ1YbgieWr+phqRZKYbrV7zRe8OR3X03cn0",
        fingerprint_source="https://www.nhr.kit.edu/userdocs/haicore/login",
    ),
    Preset(
        name="kcist",
        title="KCIST (KIT)",
        host="login2.kcist.kit.edu",
        requires_otp=False,
        user_hint="your KCIST username",
        description=("KCIST @ KIT: NVIDIA DeepOps Slurm GPU cluster, no enforced usage quotas.",),
        budget=PresetBudget("kcist.sh", 100),
        # No published fingerprint: a person compares and confirms the key.
    ),
    Preset(
        name="intnano",
        title="INT-NANO (KIT Institute of Nanotechnology)",
        host=None,  # not public: `ctun add intnano --preset intnano --host <login host>`
        requires_otp=False,
        user_hint="your INT username",
        description=("int-nano @ KIT INT: Slurm cluster. Verify `sacct` reports Start/End for the budget probe.",),
        budget=PresetBudget("intnano.sh", 200),
        aliases=("int-nano",),
    ),
)


def get(name: str) -> Preset:
    """The preset called ``name`` (or one of its aliases), case-insensitively.

    Raises :class:`KeyError` naming the known presets otherwise.
    """
    wanted = name.strip().lower()
    for preset in PRESETS:
        if wanted == preset.name or wanted in preset.aliases:
            return preset
    raise KeyError(f"Unknown preset '{name}'. Known: {', '.join(p.name for p in PRESETS)}")
