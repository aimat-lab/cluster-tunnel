"""Python API: what the ``ctun`` CLI does, as functions a program can call.

Every function names its cluster as ``ctun -t <cluster>`` does and reads the
same config: ``config_path`` (default: ``$CTUN_CONFIG``, else the per-user
``config.yaml``), or an already loaded ``config``. In that case
``config_path`` only locates the budget scripts, which sit next to the config
file.

The CLI is built on these functions, so the two behave the same: the same
tunnel check, the same budget guard, the same session and command log.
Failures raise :class:`~cluster_tunnel.errors.ClusterTunnelError` subclasses;
the ones ``ctun`` reports with a stable exit code carry it as ``exit_code``.

This module and :mod:`cluster_tunnel.errors` are the public interface and
follow semantic versioning; the other modules are internal.

Example::

    from cluster_tunnel import api

    if not api.is_live("horeka"):
        ...  # a person runs `ctun -t horeka login`
    result = api.run("horeka", ["squeue", "--me"], capture=True)
    print(result.stdout)
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence, Union

import yaml

from cluster_tunnel import budget as budget_mod
from cluster_tunnel import cmdlog, configedit
from cluster_tunnel import config as config_mod
from cluster_tunnel import facts as facts_mod
from cluster_tunnel import hostkey as hostkey_mod
from cluster_tunnel import jobs as jobs_mod
from cluster_tunnel import notes as notes_mod
from cluster_tunnel import popup, session, ssh, wsl
from cluster_tunnel import records as records_mod
from cluster_tunnel import transfer as transfer_mod
from cluster_tunnel.budget import Decision
from cluster_tunnel.config import Cluster, Config
from cluster_tunnel.errors import (
    BudgetBlocked,
    BudgetExhausted,
    BudgetGuardError,
    ClusterTunnelError,
    ConfigError,
    HostKeyError,
    LoginError,
    LoginRequired,
    NoteReferenceInvalid,
    RecordError,
    TransportError,
    UnknownClusterError,
    budget_blocked,
)
from cluster_tunnel.hostkey import HostKey
from cluster_tunnel.jobs import Job
from cluster_tunnel.popup import Credentials
from cluster_tunnel.records import JobRecord
from cluster_tunnel.ssh import ConnSpec

__all__ = [
    # operations
    "load_config", "clusters", "connection", "is_live", "status", "login", "logout",
    "preflight", "budget", "run", "feed_script", "upload", "download", "jobs",
    "edit_job", "edit_jobs", "projects", "rename_project", "host_key", "pin_host_key",
    # results and inputs
    "Credentials", "Result", "ClusterStatus", "ClusterJobs", "LoginResult", "LogoutResult", "HostKeyStatus",
    "ClusterProjects", "ProjectRename", "RecordEdit",
    "Decision", "Job", "JobRecord", "HostKey", "ConnSpec", "Config", "Cluster",
    # errors
    "ClusterTunnelError", "ConfigError", "UnknownClusterError", "LoginRequired",
    "LoginError", "BudgetBlocked", "BudgetExhausted", "BudgetGuardError", "TransportError",
    "HostKeyError", "NoteReferenceInvalid", "RecordError",
]

PathLike = Union[str, Path]

#: The finished-job window :func:`jobs` uses unless told otherwise (``ctun jobs``: 24h).
DEFAULT_SINCE_MINUTES = 24 * 60

#: Seconds an interactive :func:`login` waits for ssh (``ctun login --timeout``).
DEFAULT_LOGIN_TIMEOUT = 120


@dataclass
class Result:
    """What a command, script or transfer returned.

    ``stdout``/``stderr`` are ``None`` when the output streamed to this
    process's own stdout/stderr instead of being captured.
    """

    exit_code: int
    stdout: Optional[str] = None
    stderr: Optional[str] = None
    #: The IDs of every ``Submitted batch job <N>`` line a :func:`run` printed; empty with ``tty``.
    #: Any such line counts (also one ``cat`` printed); ``sbatch --parsable`` prints none.
    job_ids: list[str] = field(default_factory=list)
    #: The job records :func:`run` stored on the cluster, one per submitted job.
    records: list[JobRecord] = field(default_factory=list)
    #: What went wrong around the command without failing it, e.g. a fact or record that could not be stored.
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


@dataclass
class ClusterStatus:
    """One cluster's tunnel and session: a row of ``ctun status``."""

    cluster: str
    target: str
    live: bool
    session: Optional[dict]
    limit: Optional[float]
    unit: Optional[str]
    commands: dict  # {"count": int, "last_epoch": int | None}, from the command log
    used: Optional[float] = None
    used_error: Optional[str] = None

    def as_dict(self) -> dict:
        """The row as ``ctun status --json`` prints it."""
        return asdict(self)


@dataclass
class ClusterJobs:
    """One cluster's Slurm jobs, as ``ctun jobs`` reports them."""

    cluster: str
    live: bool
    error: Optional[str] = None
    jobs: list[Job] = field(default_factory=list)
    #: Slurm's ``ClusterName``, which jobs see as ``SLURM_CLUSTER_NAME``; ``None`` if unknown.
    slurm_cluster: Optional[str] = None
    #: The job records of the listed jobs and of the window, by job ID; ``None`` when not read.
    records: Optional[dict[str, JobRecord]] = None
    #: Why the records could not be read; the jobs are listed all the same.
    records_error: Optional[str] = None

    def as_dict(self) -> dict:
        """The entry as ``ctun jobs --json`` prints it.

        With records read, each job carries its ``record`` (or ``None``; an
        array's tasks share the array's), and ``records_without_job`` lists the
        records whose job is not listed.
        """
        entry = {
            "cluster": self.cluster,
            "slurm_cluster": self.slurm_cluster,
            "live": self.live,
            "error": self.error,
            "jobs": [job.as_dict() for job in self.jobs],
        }
        if self.records is None and self.records_error is None:
            return entry
        found = self.records or {}
        listed = set()
        for job in entry["jobs"]:
            # The tasks of an array share its record (see records.keys).
            key = next((key for key in records_mod.keys(job["jobid"]) if key in found), None)
            job["record"] = found[key].as_dict() if key else None
            listed.add(key)
        entry["records_without_job"] = [record.as_dict() for key, record in found.items() if key not in listed]
        entry["records_error"] = self.records_error
        return entry


@dataclass
class LoginResult:
    """What :func:`login` did."""

    cluster: str
    target: str
    already_live: bool  # the tunnel was up already, so nothing was opened
    interactive: bool  # opened through the password/OTP dialog
    via: Optional[str]  # Windows: the WSL distro carrying the tunnel
    session: dict
    session_kept: bool  # the running session, and its budget window, was kept


@dataclass
class LogoutResult:
    """One cluster :func:`logout` closed."""

    cluster: str
    was_live: bool


@dataclass
class ClusterProjects:
    """One cluster's projects, as ``ctun projects list`` reports them."""

    cluster: str
    live: Optional[bool]  # None: not checked, as the cluster keeps no records
    job_records: bool  # false: the cluster keeps no records, so it was skipped
    error: Optional[str] = None
    #: ``{"project", "jobs"}`` per project, by name; jobs without a project are not counted.
    projects: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        """The entry as ``ctun projects list --json`` prints it."""
        return asdict(self)


@dataclass
class ProjectRename:
    """What :func:`rename_project` changed on one cluster."""

    cluster: str
    live: Optional[bool]  # None: not checked, as the cluster keeps no records
    job_records: bool  # false: the cluster keeps no records, so it was skipped
    error: Optional[str] = None
    #: The IDs of the jobs whose record was renamed, also those stored before an ``error``.
    renamed: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        """The entry as ``ctun projects rename --json`` prints it."""
        return asdict(self)


@dataclass
class RecordEdit:
    """One job's record after :func:`edit_job`."""

    job_id: str
    #: The record as stored, keys this ctun doesn't know included; ``None`` for a
    #: job without one that the edit left alone.
    record: Optional[dict]
    changed: bool  # false: the record had these values already, nothing was stored


@dataclass
class HostKeyStatus:
    """The host key a cluster's server presents now, and what a login makes of it."""

    cluster: str
    offered: HostKey  # the key the server presents to a login
    trust: str  # what a login would do: "known" accept it, "new" (no key known yet)
    #             accept it on first contact unless strict, "changed" refuse it
    pinned: list[HostKey]  # what known_hosts_file holds for the host; empty: none
    strict: bool  # logins accept only a known key (strict_host_key_checking: yes)
    known_hosts_file: Optional[str]  # set: the cluster is pinned; None: ssh's own files
    conflicts: list[str] = field(default_factory=list)  # "file:line" of a differing known key

    @property
    def matches(self) -> bool:
        """True if the key the server presents is the pinned one."""
        return any(key.fingerprint == self.offered.fingerprint for key in self.pinned)

    def as_dict(self) -> dict:
        """As ``ctun hostkey --json`` prints it."""
        return {
            "cluster": self.cluster,
            "offered": self.offered.as_dict(),
            "trust": self.trust,
            "pinned": [key.as_dict() for key in self.pinned],
            "matches": self.matches,
            "strict": self.strict,
            "known_hosts_file": self.known_hosts_file,
            "conflicts": self.conflicts,
        }


# --- config --------------------------------------------------------------------


def load_config(config_path: Optional[PathLike] = None) -> Config:
    """Load and validate the config; raise :class:`ConfigError` if it is missing or invalid."""
    try:
        return config_mod.load_config(_path_arg(config_path))
    except FileNotFoundError as exc:
        raise ConfigError(str(exc)) from exc
    except (ValueError, TypeError, OSError, yaml.YAMLError) as exc:
        # ValueError covers pydantic's errors and a file that isn't UTF-8;
        # TypeError a YAML document that isn't a mapping.
        path = config_mod.resolve_config_path(_path_arg(config_path))
        raise ConfigError(f"invalid config {path}: {exc}") from exc


def clusters(*, config: Optional[Config] = None, config_path: Optional[PathLike] = None) -> list[str]:
    """The configured clusters' names, sorted."""
    config, _ = _load(config, config_path)
    return sorted(config.clusters)


def connection(
    cluster: str, *, config: Optional[Config] = None, config_path: Optional[PathLike] = None
) -> ConnSpec:
    """How ctun reaches ``cluster``: its ssh target, control socket and keepalives."""
    config, _ = _load(config, config_path)
    return _spec(config, cluster)


# --- tunnel ----------------------------------------------------------------------


def is_live(
    cluster: str, *, config: Optional[Config] = None, config_path: Optional[PathLike] = None
) -> bool:
    """True if the tunnel to ``cluster`` is up (its ssh master answers)."""
    return ssh.is_live(connection(cluster, config=config, config_path=config_path))


def status(
    cluster: Optional[str] = None,
    *,
    usage: bool = False,
    config: Optional[Config] = None,
    config_path: Optional[PathLike] = None,
) -> list[ClusterStatus]:
    """Tunnel and session state of ``cluster``, or of every configured cluster.

    With ``usage``, a live cluster whose session has a limit also gets its
    current budget usage, probed over the tunnel: ``used``, or ``used_error``
    when the probe fails. ``ctun status`` probes for a single ``-t`` cluster
    only, never as a side effect of the overview.
    """
    config, path = _load(config, config_path)
    rows: list[ClusterStatus] = []
    for name in _names(config, cluster):
        entry = config.clusters[name]
        spec = ssh.conn_spec(config, name)
        live = ssh.is_live(spec)
        sess = session.load(name)
        # The unit falls back to the cluster's configured unit; the limit comes
        # from the live session (None when unguarded or not logged in).
        unit = (sess.get("unit") if sess else None) or (entry.budget.unit if entry.budget else None)
        limit = sess.get("limit") if sess else None

        used: Optional[float] = None
        used_error: Optional[str] = None
        if usage and live and sess and limit is not None and entry.budget:
            try:
                script = config_mod.budget_script_path(path, name, entry.budget.script)
                user = budget_mod.remote_user(spec, entry)
                used = budget_mod.used_since(spec, script, int(sess["start_epoch"]), user)
            except Exception as exc:  # noqa: BLE001 — reported in the row, not raised
                used_error = str(exc)

        rows.append(
            ClusterStatus(
                cluster=name,
                target=spec.target,
                live=live,
                session=sess,
                limit=limit,
                unit=unit,
                commands=cmdlog.summary(name),
                used=used,
                used_error=used_error,
            )
        )
    return rows


def login(
    cluster: str,
    *,
    interactive: bool = False,
    credentials: Optional[Credentials] = None,
    limit: Optional[float] = None,
    timeout: int = DEFAULT_LOGIN_TIMEOUT,
    verbose: int = 0,
    config: Optional[Config] = None,
    config_path: Optional[PathLike] = None,
) -> LoginResult:
    """Open the tunnel to ``cluster`` and start a budget session, as ``ctun login``.

    Without ``interactive``, ssh asks for the password and OTP in this process's
    terminal; with it, a dialog lets a present person enter them, and they go
    straight to ssh. ``credentials`` are a password and OTP the calling program
    asked for itself (a window's masked field, say): they go to ssh the same
    way, with no dialog and no terminal, and a cluster that requires a password
    refuses the login without one before ssh starts. A tunnel that is already
    up is kept, and so is its session. A new tunnel starts a new session (and
    an empty command log) whose limit is the dialog's, else ``limit``, else the
    credentials', else the cluster's ``session_limit``; a negative limit means
    unguarded. ``verbose`` (0-3) adds ssh's ``-v`` flags.

    Raises :class:`LoginError` when the tunnel can't be opened or the dialog is
    cancelled or can't be shown; its ``diagnostics`` hold what ssh said about
    why. Raises ``ValueError`` when given both ``credentials`` and
    ``interactive``.
    """
    if interactive and credentials is not None:
        raise ValueError("pass credentials or interactive, not both")
    config, _ = _load(config, config_path)
    entry = _entry(config, cluster)
    spec = ssh.conn_spec(config, cluster)

    via = None  # the WSL distro carrying the tunnel, on Windows
    if wsl.enabled():
        ready = wsl.preflight(spec.wsl_distro)
        if not ready.ok:
            raise LoginError("\n".join(ready.problems), cluster)
        via = ready.distro

    # Default limit: the argument, else the cluster's configured session_limit.
    default_limit = limit
    if default_limit is None and entry.budget is not None:
        default_limit = entry.budget.session_limit
    unit = entry.budget.unit if entry.budget else "units"

    already_live = ssh.is_live(spec)
    chosen_limit = default_limit
    if already_live:
        pass
    elif interactive or credentials is not None:
        if credentials is None:
            if not popup.gui_available():
                raise LoginError(
                    f"No display for the login dialog; run `ctun -t {cluster} login` from a terminal.",
                    cluster,
                )
            credentials = popup.prompt_credentials(
                cluster, spec.target, default_limit, unit,
                entry.requires_otp, entry.requires_password,
            )
            if credentials is None:
                raise LoginError("Login cancelled.", cluster)
            chosen_limit = credentials.limit
            failed = f"Interactive login to '{cluster}' failed or timed out."
        else:
            # As the dialog: the password is required, the OTP advisory (some
            # sites skip it from a trusted network).
            if entry.requires_password and not credentials.password:
                raise LoginError(f"'{cluster}' requires a password.", cluster)
            if limit is None and credentials.limit is not None:
                chosen_limit = credentials.limit
            failed = f"Login to '{cluster}' with the given credentials failed or timed out."
        try:
            live, trouble = popup.open_with_password(
                spec, credentials.password, credentials.otp or None, timeout, verbose)
        except wsl.WslError as exc:
            raise LoginError(str(exc), cluster) from exc
        if not live:
            raise LoginError(failed, cluster, ssh_failed=True, diagnostics=trouble.strip() or None)
    else:
        try:
            rc = ssh.open_master(spec, verbose)
        except wsl.WslError as exc:
            raise LoginError(str(exc), cluster) from exc
        if rc != 0 or not ssh.is_live(spec):
            diagnostics = (ssh.check(spec).stderr or "").strip() if verbose else ""
            raise LoginError(
                f"Failed to open tunnel to '{cluster}' (ssh exit {rc}).",
                cluster,
                ssh_failed=True,
                diagnostics=diagnostics or None,
            )

    # A session limit of -1 (or any negative) means "infinite": no budget guard
    # at all, same as leaving the limit unset.
    if chosen_limit is not None and chosen_limit < 0:
        chosen_limit = None

    # A session begins on a fresh authentication; re-auth resets the window.
    sess = None if not already_live else session.load(cluster)
    if sess is None:
        cmdlog.clear(cluster)  # fresh session: start the command log empty
        sess = session.start(cluster, limit=chosen_limit, unit=unit)
        kept = False
    else:
        kept = True

    return LoginResult(
        cluster=cluster,
        target=spec.target,
        already_live=already_live,
        interactive=interactive and not already_live,
        via=via,
        session=sess,
        session_kept=kept,
    )


def logout(
    cluster: Optional[str] = None,
    *,
    config: Optional[Config] = None,
    config_path: Optional[PathLike] = None,
) -> list[LogoutResult]:
    """Close the tunnel to ``cluster``, or to every configured cluster, as ``ctun logout``.

    Each cluster's session and command log are cleared, live tunnel or not.
    """
    config, _ = _load(config, config_path)
    closed: list[LogoutResult] = []
    for name in _names(config, cluster):
        spec = ssh.conn_spec(config, name)
        was_live = ssh.is_live(spec)
        ssh.close(spec)
        session.clear(name)
        cmdlog.clear(name)
        closed.append(LogoutResult(name, was_live))
    return closed


# --- host keys ---------------------------------------------------------------------


def host_key(
    cluster: str, *, config: Optional[Config] = None, config_path: Optional[PathLike] = None
) -> HostKeyStatus:
    """The host key ``cluster``'s server presents now, and what a login makes of it.

    ssh itself judges the key, against exactly the known_hosts files the
    tunnel's master uses (``trust``); the key is then fetched in known_hosts
    form, as a login negotiates it. Both connections stop before logging in, so
    this needs neither a tunnel nor a password (a ProxyJump host in the ssh
    config logs in as configured). ``pinned`` lists the keys the cluster's
    ``known_hosts_file`` holds for the host. Raises :class:`HostKeyError` when
    the server can't be reached, or presents a different key the second time.
    """
    config, _ = _load(config, config_path)
    entry = _entry(config, cluster)
    spec = ssh.conn_spec(config, cluster)
    verdict = _host_keys(cluster, "check", lambda: hostkey_mod.check(spec))
    offered = _host_keys(cluster, "fetch", lambda: hostkey_mod.scan(spec, verdict.algorithm))[0]
    if offered.fingerprint != verdict.fingerprint:
        raise HostKeyError(
            f"'{cluster}' presented {verdict.fingerprint}, then {offered.fingerprint}: "
            "two different keys for one host. Don't log in until you know why."
        )
    pinned: list[HostKey] = []
    if entry.known_hosts_file:
        file = entry.known_hosts_file
        pinned = _host_keys(cluster, "read", lambda: hostkey_mod.pinned(spec, file, offered.host))
    return HostKeyStatus(
        cluster=cluster,
        offered=offered,
        trust=verdict.trust,
        pinned=pinned,
        strict=entry.strict_host_key_checking == "yes",
        known_hosts_file=entry.known_hosts_file,
        conflicts=verdict.conflicts,
    )


def pin_host_key(
    cluster: str,
    fingerprint: str,
    *,
    replace: bool = False,
    config_path: Optional[PathLike] = None,
) -> HostKeyStatus:
    """Pin ``cluster``'s host key: the one with ``fingerprint``, as a person confirmed it.

    Checks the key again (:func:`host_key`) and refuses (:class:`HostKeyError`)
    unless the server presents exactly that fingerprint, and, without
    ``replace``, when logins trust a different key for the host: replacing a
    trusted key is for a person who knows why it changed. The key then replaces
    any earlier one for the host in the cluster's ``known_hosts_file`` (default:
    ``known_hosts`` next to the config file), and the config gets
    ``strict_host_key_checking: 'yes'`` and that ``known_hosts_file`` (the
    previous config is kept as ``<config>.bak``), so later logins accept no
    other key. Raises :class:`ConfigError` when the config can't be edited
    safely; then nothing is pinned.
    """
    path = config_mod.resolve_config_path(_path_arg(config_path))
    config = load_config(config_path)
    entry = _entry(config, cluster)
    spec = ssh.conn_spec(config, cluster)
    status = host_key(cluster, config=config, config_path=config_path)
    if status.offered.fingerprint != fingerprint:
        raise HostKeyError(
            f"'{cluster}' presents {status.offered.fingerprint}, not {fingerprint}; nothing was pinned."
        )
    if status.trust == "changed" and not replace:
        raise HostKeyError(
            f"logins to '{cluster}' trust a different key than the one it presents; replace it "
            "only once a person knows why it changed (replace=True). Nothing was pinned."
        )
    file = entry.known_hosts_file or hostkey_mod.default_file(path)
    try:
        edit = configedit.plan_host_key_pin(path, cluster, file)
    except configedit.EditError as exc:
        raise ConfigError(f"cannot pin the host key in {path}: {exc}") from exc
    _host_keys(cluster, "pin", lambda: hostkey_mod.pin(spec, file, [status.offered]))
    if edit.changes:
        configedit.apply(edit, backup=True)
    return HostKeyStatus(cluster, status.offered, "known", [status.offered], True, file)


# --- commands ----------------------------------------------------------------------


def preflight(
    cluster: str,
    command: Sequence[str],
    *,
    config: Optional[Config] = None,
    config_path: Optional[PathLike] = None,
) -> Decision:
    """Check that ``command`` may run on ``cluster`` now, without running it (``ctun run -n``).

    Raises :class:`LoginRequired` without a live tunnel, and
    :class:`BudgetExhausted` or :class:`BudgetGuardError` when the budget guard
    blocks the command; otherwise returns the guard's decision.
    """
    tokens = _tokens(command)
    config, path = _load(config, config_path)
    spec = _spec(config, cluster)
    return _preflight(config, path, cluster, spec, tokens)


def budget(
    cluster: str, *, config: Optional[Config] = None, config_path: Optional[PathLike] = None
) -> Decision:
    """The session budget of ``cluster`` as it stands, probed over the tunnel now.

    The verdict the guard gives any job submission at this moment, whatever the
    command: ask it before submitting what the guard can't recognise as a
    submission, such as a script that calls ``sbatch``. A cluster without a
    budget is never blocked. Raises :class:`LoginRequired` without a live
    tunnel; a blocking verdict is returned (``allowed`` false), not raised.
    """
    config, path = _load(config, config_path)
    spec = _spec(config, cluster)
    if not ssh.is_live(spec):
        raise LoginRequired(cluster)
    return budget_mod.check(config, cluster, spec, path)


def run(
    cluster: str,
    command: Sequence[str],
    *,
    capture: bool = False,
    tty: bool = False,
    timeout: Optional[float] = None,
    on_preflight: Optional[Callable[[Decision], None]] = None,
    project: Optional[str] = None,
    note: Optional[str] = None,
    config: Optional[Config] = None,
    config_path: Optional[PathLike] = None,
) -> Result:
    """Run ``command`` (an argv list) on ``cluster``'s login node, as ``ctun run``.

    A live tunnel is required (:class:`LoginRequired`), a job submission must
    pass the budget guard (:class:`BudgetExhausted`, :class:`BudgetGuardError`),
    and the command goes into the session's command log before it runs.
    ``on_preflight`` is called with the guard's decision just before that.

    The output streams to this process's stdout and stderr, or, with
    ``capture``, comes back in the :class:`Result`; ``tty`` needs streaming.
    ``timeout`` (seconds, captured output only) stops waiting with exit code
    124; the remote command may keep running on the cluster. The IDs of the
    jobs the command submitted go into the :class:`Result` and the command log;
    with ``tty`` ctun doesn't see the output, so there are none.

    For each submitted job, unless the cluster has ``job_records: false``, a
    record is stored on the cluster: ``project``, ``note``, and the facts ctun
    collects there itself (job name, working directory, the git state in it),
    except for a job Slurm took before the command started: its ID was only
    printed, e.g. by ``cat`` of a log. A fact or record that can't be had only
    adds to ``Result.warnings``. The
    ``job:<id>`` references in a ``note`` that will be stored are checked on
    the cluster first: one it doesn't know raises :class:`NoteReferenceInvalid`,
    and nothing runs. Its ``commit:<hash>`` references are checked once the jobs
    are submitted, also in their own repos; one the cluster doesn't know is
    stored all the same, with a warning.
    """
    if capture and tty:
        raise ValueError("a TTY needs streamed output; pass capture=False")
    if project:
        project = records_mod.project_name(project) or None
    if timeout is not None and not capture:
        raise ValueError("a timeout needs captured output; pass capture=True")
    tokens = _tokens(command)
    config, path = _load(config, config_path)
    spec = _spec(config, cluster)
    decision = _preflight(config, path, cluster, spec, tokens)
    if note and not tty and config.clusters[cluster].job_records:
        # Commits only after the submission: the first job of a project brings its repo.
        _check_note(spec, notes_mod.references(note, "job"))
    if on_preflight is not None:
        on_preflight(decision)
    # Log the command (not its output) so `status` can show activity.
    entry_id = cmdlog.record(cluster, tokens)
    if tty:
        result = Result(ssh.run(spec, tokens, tty=True))
        if project or note:
            result.warnings.append("no job record written (a TTY hides the output)")
        return result
    job_ids: list[str] = []

    def submitted(output: str) -> None:
        # Log each ID once it is printed: an interrupted run, or one that waits
        # for its job (`sbatch --wait`), still shows what it submitted.
        for job_id in jobs_mod.submitted_job_ids(output):
            job_ids.append(job_id)
            cmdlog.record_jobs(cluster, entry_id, [job_id])

    started = time.monotonic()
    if capture:
        res = ssh.capture(spec, tokens, timeout=timeout)
        result = Result(res.returncode, res.stdout, res.stderr)
        submitted(res.stdout or "")
    else:
        result = Result(ssh.run(spec, tokens, on_line=submitted))
    result.job_ids = job_ids
    if config.clusters[cluster].job_records:
        _write_records(
            spec, result, project=project, note=note, command=tokens, ran_for=time.monotonic() - started
        )
    elif project or note:
        result.warnings.append("no job record written (job_records is off for this cluster)")
    return result


def feed_script(
    cluster: str,
    script: PathLike,
    args: Sequence[object] = (),
    *,
    timeout: Optional[float] = None,
    config: Optional[Config] = None,
    config_path: Optional[PathLike] = None,
) -> Result:
    """Run a local bash ``script`` on ``cluster``'s login node, fed over the tunnel.

    The script travels on stdin to ``bash -s -- <args>`` and is never written on
    the cluster; its output is captured. Like ctun's own probes it is neither
    budget-guarded nor logged, so keep it to read-only checks. ``timeout`` works
    as in :func:`run`. Raises :class:`LoginRequired` without a live tunnel.
    """
    config, _ = _load(config, config_path)
    spec = _spec(config, cluster)
    if not ssh.is_live(spec):
        raise LoginRequired(cluster)
    res = ssh.feed_script(spec, Path(script), list(args), timeout=timeout)
    return Result(res.returncode, res.stdout, res.stderr)


# --- transfers ---------------------------------------------------------------------


def upload(
    cluster: str,
    src: PathLike,
    dest: str,
    *,
    dry_run: bool = False,
    extra: Sequence[str] = (),
    capture: bool = False,
    config: Optional[Config] = None,
    config_path: Optional[PathLike] = None,
) -> Result:
    """Copy local ``src`` to ``dest`` on ``cluster`` (rsync -r over the tunnel), as ``ctun upload``.

    ``dest`` is a remote path, relative to the remote $HOME; trailing slashes
    follow rsync (``dir/`` copies the contents). ``extra`` are further rsync
    flags, ``dry_run`` only reports what would be copied, and ``capture``
    returns rsync's output instead of streaming it. Transfers are not
    budget-guarded: moving data isn't compute. Raises :class:`LoginRequired`
    without a live tunnel, and :class:`TransportError` when, on Windows, the
    local path can't be translated for WSL.
    """
    return _transfer("upload", cluster, src, dest, dry_run, extra, capture, config, config_path)


def download(
    cluster: str,
    src: str,
    dest: PathLike,
    *,
    dry_run: bool = False,
    extra: Sequence[str] = (),
    capture: bool = False,
    config: Optional[Config] = None,
    config_path: Optional[PathLike] = None,
) -> Result:
    """Copy ``src`` on ``cluster`` to local ``dest``, as ``ctun download``; see :func:`upload`."""
    return _transfer("download", cluster, src, dest, dry_run, extra, capture, config, config_path)


# --- jobs --------------------------------------------------------------------------


def jobs(
    cluster: Optional[str] = None,
    *,
    since_minutes: int = DEFAULT_SINCE_MINUTES,
    with_records: bool = False,
    config: Optional[Config] = None,
    config_path: Optional[PathLike] = None,
) -> list[ClusterJobs]:
    """Your Slurm jobs on ``cluster``, or on every configured cluster, as ``ctun jobs``.

    On each cluster with a live tunnel a bundled read-only probe lists the
    running and pending jobs and those that finished within ``since_minutes``
    (``0``: active jobs only). A cluster whose tunnel is down is reported, never
    logged in to; a probe that fails is reported in ``error``.

    ``with_records`` also reads the job records (one more call per cluster, on
    clusters that keep them): those of the listed jobs and those written within
    the window, in ``records``; a failure to read them goes in ``records_error``.
    """
    config, _ = _load(config, config_path)
    results: list[ClusterJobs] = []
    for name in _names(config, cluster):
        entry = config.clusters[name]
        spec = ssh.conn_spec(config, name)
        item = ClusterJobs(name, ssh.is_live(spec))
        if item.live:
            try:
                overview = jobs_mod.query(spec, entry.user, since_minutes)
                item.slurm_cluster = overview.slurm_cluster
                item.jobs = overview.jobs
            except Exception as exc:  # noqa: BLE001 — surface, don't abort the sweep
                item.error = str(exc)
        if with_records and item.live and item.error is None and entry.job_records:
            try:
                keys = dict.fromkeys(key for job in item.jobs for key in records_mod.keys(job.jobid))
                found = records_mod.list_recent(spec, since_minutes, list(keys))
                item.records = {record.job_id: record for record in found}
            except RuntimeError as exc:
                item.records_error = str(exc)
        results.append(item)
    return results


def edit_job(
    cluster: str,
    job_id: str,
    *,
    project: Optional[str] = None,
    note: Optional[str] = None,
    confirm_note: bool = False,
    config: Optional[Config] = None,
    config_path: Optional[PathLike] = None,
) -> RecordEdit:
    """Give job ``job_id``'s record a new ``project`` or ``note``, as ``ctun jobs edit``.

    ``None`` keeps a value, ``""`` clears it. A new note is unconfirmed unless
    ``confirm_note``, which a person's review sets; its references are checked
    as in :func:`run` (:class:`NoteReferenceInvalid`). Each change is logged in
    the record's ``changes``; the facts from submit time never change. A job
    without a record (e.g. one submitted with plain ``sbatch``) gets one without
    facts, if Slurm knows it; a task of an array without one gets a copy of the
    array's record (see :func:`records.keys`). A record another machine changes
    meanwhile gets the edit on top of that change. Raises :class:`LoginRequired`
    without a live tunnel and :class:`RecordError` when ``job_id`` is no job ID
    or the job is unknown, the cluster keeps no records, there is no note to
    confirm, or the record can't be stored.
    """
    [edit] = edit_jobs(
        cluster, [job_id], project=project, note=note, confirm_note=confirm_note,
        config=config, config_path=config_path,
    )
    return edit


def edit_jobs(
    cluster: str,
    job_ids: Sequence[str],
    *,
    project: Optional[str] = None,
    note: Optional[str] = None,
    confirm_note: bool = False,
    config: Optional[Config] = None,
    config_path: Optional[PathLike] = None,
) -> list[RecordEdit]:
    """Give every job in ``job_ids`` the same edit, as :func:`edit_job` does for one.

    One :class:`RecordEdit` per job, in order. Every job is checked before any
    record is stored, so an ID that is no job ID, an unknown job, a job without
    a note to confirm or a reference the cluster doesn't know changes none of
    them. The records are read and stored a few dozen per SSH call. If storing
    fails partway, the :class:`RecordError` names the jobs already stored.
    """
    job_ids = list(dict.fromkeys(job_ids))
    for job_id in job_ids:
        if not records_mod.is_job_id(job_id):
            raise RecordError(f"'{job_id}' is not a Slurm job ID (like 51319, or 51319_4 for an array task).")
    if project is not None:
        project = records_mod.project_name(project)
    config, _ = _load(config, config_path)
    spec = _spec(config, cluster)
    if not ssh.is_live(spec):
        raise LoginRequired(cluster)
    if not config.clusters[cluster].job_records:
        raise RecordError(f"job_records is off for '{cluster}'.")
    edits: dict[str, RecordEdit] = {}
    pending = job_ids
    try:
        for _ in range(_EDIT_ATTEMPTS):
            arrays = [records_mod.keys(job_id)[-1] for job_id in pending]
            found = {
                records_mod.job_id_of(item.record): item
                for item in records_mod.read_many(spec, list(dict.fromkeys(pending + arrays)))
            }
            for job_id, array in zip(pending, arrays):
                if job_id not in found and array in found:
                    # A task's first record of its own starts as its array's, facts and all.
                    record = {**found[array].record, "job_id": job_id, "changes": []}
                    found[job_id] = records_mod.Stored(record, records_mod.ABSENT)
            missing = [job_id for job_id in pending if job_id not in found]
            unknown = notes_mod.unknown_references(spec, [f"job:{job_id}" for job_id in missing])
            if unknown:
                ids = ", ".join(ref[len("job:"):] for ref in unknown)
                which = f"Jobs {ids} are" if len(unknown) > 1 else f"Job {ids} is"
                raise RecordError(f"{which} unknown on '{cluster}' (not in squeue or sacct).")
            changed: list[tuple[dict, str]] = []
            for job_id in pending:
                current = found.get(job_id, records_mod.Stored({}, records_mod.ABSENT))
                updated = records_mod.edited(
                    current.record, job_id, project=project, note=note, confirm_note=confirm_note
                )
                if updated is None:
                    edits[job_id] = RecordEdit(job_id, current.record or None, changed=False)
                else:
                    changed.append((updated, current.version))
            if note and changed:
                workdirs = ((record.get("code") or {}).get("workdir") for record, _ in changed)
                _check_note(spec, notes_mod.references(note), project=project, repos=[w for w in workdirs if w])
            done = records_mod.update_many(spec, changed)
            for record, _ in changed:
                if record["job_id"] in done.stored:
                    edits[record["job_id"]] = RecordEdit(record["job_id"], record, changed=True)
            if not done.changed:
                return [edits[job_id] for job_id in job_ids]
            # Changed on another machine since they were read: apply the edit to that.
            pending = done.changed
    except records_mod.UpdateError as exc:
        stored = [job_id for job_id in job_ids if job_id in exc.stored or job_id in edits and edits[job_id].changed]
        raise RecordError(f"{exc} (already stored: {', '.join(stored) or 'none'})") from exc
    except (RuntimeError, ValueError) as exc:
        raise RecordError(str(exc)) from exc
    raise RecordError(
        f"The records of jobs {', '.join(pending)} kept changing while they were edited; run the command again."
    )


# --- projects ----------------------------------------------------------------------


def projects(
    cluster: Optional[str] = None,
    *,
    config: Optional[Config] = None,
    config_path: Optional[PathLike] = None,
) -> list[ClusterProjects]:
    """Every project in the job records of ``cluster``, or of every configured cluster, as ``ctun projects list``.

    A project exists only through the jobs whose record names it, so this
    reads every record, however old, and counts the jobs per project. A
    cluster whose tunnel is down is reported, never logged in to; one that
    keeps no records is skipped; a failure to read the records goes in ``error``.
    """
    config, _ = _load(config, config_path)
    results: list[ClusterProjects] = []
    for spec, item in _record_clusters(config, cluster, ClusterProjects):
        if spec is not None:
            try:
                item.projects = records_mod.projects(records_mod.list_all(spec))
            except RuntimeError as exc:
                item.error = str(exc)
        results.append(item)
    return results


def rename_project(
    old: str,
    new: str,
    cluster: Optional[str] = None,
    *,
    config: Optional[Config] = None,
    config_path: Optional[PathLike] = None,
) -> list[ProjectRename]:
    """Rename project ``old`` to ``new`` in every job record, as ``ctun projects rename``.

    The projects nested in ``old`` move along: renaming ``htfm`` to
    ``archive/htfm`` makes ``htfm/mh`` ``archive/htfm/mh``. If ``new`` exists
    already, the jobs join it. Each change is logged in the record like
    :func:`edit_job`, and the records go back to the cluster a few dozen per
    SSH call. Works on ``cluster``, or on every configured cluster with a live
    tunnel; a down cluster keeps ``old``, so call it again once it is logged in,
    which is safe. Both names are taken as :func:`records.project_name` stores
    them; ``ValueError`` if either is empty then. A damaged record (no job ID,
    a project that is no string) is left as it is. A record another machine
    changes meanwhile is read and renamed again, never overwritten with what was
    read before. If storing fails, ``renamed`` still lists what was stored.
    """
    old, new = records_mod.project_name(old), records_mod.project_name(new)
    if not old:
        raise ValueError("the project to rename is empty")
    if not new:
        raise ValueError('the new project name is empty; to clear a project, use `jobs edit --project ""`')
    config, _ = _load(config, config_path)
    results: list[ProjectRename] = []
    for spec, item in _record_clusters(config, cluster, ProjectRename):
        if spec is not None:
            try:
                found = records_mod.list_all_stored(spec)
                for _ in range(_EDIT_ATTEMPTS):
                    done = records_mod.update_many(spec, _renamed(found, old, new))
                    item.renamed += done.stored
                    if not done.changed:
                        break
                    # Edited on another machine since they were read: rename what is there now.
                    found = records_mod.read_many(spec, done.changed)
                else:
                    item.error = (
                        "these records kept changing while they were renamed: "
                        + ", ".join(done.changed) + "; run the command again"
                    )
            except records_mod.UpdateError as exc:
                item.renamed += exc.stored
                item.error = str(exc)
            except RuntimeError as exc:
                item.error = str(exc)
        results.append(item)
    return results


# --- helpers -----------------------------------------------------------------------


#: How often an edit is applied again to a record another machine changed meanwhile.
_EDIT_ATTEMPTS = 3


def _renamed(found: list[records_mod.Stored], old: str, new: str) -> list[tuple[dict, str]]:
    """``(record, version)`` for each record in ``found`` that renaming ``old`` to ``new`` changes."""
    changed = []
    for current in found:
        job_id = records_mod.job_id_of(current.record)
        project = records_mod.renamed_project(current.record.get("project"), old, new)
        if job_id is None or project is None:
            continue  # a damaged record must not stop the others
        updated = records_mod.edited(current.record, job_id, project=project, note=None, confirm_note=False)
        if updated is not None:
            changed.append((updated, current.version))
    return changed


def _record_clusters(config: Config, cluster: Optional[str], result_type: type):
    """``(spec, result)`` per cluster asked for; ``spec`` is ``None`` unless the cluster is live and keeps records."""
    for name in _names(config, cluster):
        spec = ssh.conn_spec(config, name)
        keeps_records = config.clusters[name].job_records
        # A cluster without records is skipped, so its tunnel needn't be checked.
        item = result_type(name, ssh.is_live(spec) if keeps_records else None, keeps_records)
        yield (spec if item.live else None), item


def _path_arg(config_path: Optional[PathLike]) -> Optional[str]:
    return None if config_path is None else str(config_path)


def _load(config: Optional[Config], config_path: Optional[PathLike]) -> tuple[Config, Path]:
    """The config to use, and the config file path its budget scripts are relative to."""
    path = config_mod.resolve_config_path(_path_arg(config_path))
    return (config if config is not None else load_config(config_path)), path


def _entry(config: Config, name: str) -> Cluster:
    try:
        return config_mod.get_cluster(config, name)
    except KeyError as exc:
        raise UnknownClusterError(exc.args[0]) from None


def _spec(config: Config, name: str) -> ConnSpec:
    _entry(config, name)
    return ssh.conn_spec(config, name)


def _names(config: Config, cluster: Optional[str]) -> list[str]:
    """The cluster asked for, or every configured cluster (sorted) for ``None``."""
    if cluster is None:
        return sorted(config.clusters)
    _entry(config, cluster)
    return [cluster]


def _tokens(command: Sequence[str]) -> list[str]:
    if isinstance(command, str):
        raise TypeError("command is an argv list, e.g. ['squeue', '--me'], not a string")
    if not command:
        raise ValueError("command is empty (ssh would open a login shell)")
    return list(command)


def _host_keys(cluster: str, action: str, call: Callable):
    """Run a host-key operation, turning its failure into :class:`HostKeyError`."""
    try:
        return call()
    except RuntimeError as exc:
        raise HostKeyError(f"cannot {action} the host key of '{cluster}': {exc}") from exc


def _check_note(
    spec: ConnSpec, refs: list[str], *, project: Optional[str] = None, repos: Sequence[str] = ()
) -> None:
    """Raise :class:`NoteReferenceInvalid` if a note's ``refs`` name a commit or job the cluster doesn't know.

    ``project`` and ``repos`` say where to look for commits, as in
    :func:`notes.unknown_references`.
    """
    try:
        unknown = notes_mod.unknown_references(spec, refs, project=project, repos=repos)
    except RuntimeError as exc:
        raise NoteReferenceInvalid(str(exc)) from exc
    if unknown:
        raise NoteReferenceInvalid(
            "the note names references the cluster does not know: " + ", ".join(unknown), unknown
        )


#: Seconds a job may seem older than the command that submitted it: Slurm's
#: clock and the login node's may differ a little.
_SUBMIT_SLACK = 60


def _write_records(
    spec: ConnSpec,
    result: Result,
    *,
    project: Optional[str],
    note: Optional[str],
    command: list[str],
    ran_for: float,
) -> None:
    """Store a record for each job in ``result.job_ids``; a failure goes in ``result.warnings``, never raises.

    A job Slurm took more than ``ran_for`` seconds ago (the command's run time)
    is an old one the command only printed, and gets none.
    """
    for job_id in result.job_ids:
        facts = facts_mod.collect(spec, job_id)
        if facts.submitted_ago is not None and facts.submitted_ago > ran_for + _SUBMIT_SLACK:
            if project or note:
                result.warnings.append(f"no record for job {job_id}: it was submitted before this command")
            continue
        result.warnings.extend(facts.warnings)
        record = records_mod.build(job_id, facts, project=project, note=note, command=command)
        try:
            records_mod.write(spec, record)
        except RuntimeError as exc:
            result.warnings.append(str(exc))
            continue
        result.records.append(record)
    commits = notes_mod.references(note, "commit") if note else []
    if commits and result.records:
        _check_note_commits(spec, result, commits, project)


def _check_note_commits(spec: ConnSpec, result: Result, commits: list[str], project: Optional[str]) -> None:
    """Warn in ``result.warnings`` about the ``commits`` of a stored note that the cluster doesn't know."""
    repos = [record.code["workdir"] for record in result.records if record.code.get("workdir")]
    try:
        unknown = notes_mod.unknown_references(spec, commits, project=project, repos=repos)
    except RuntimeError as exc:
        result.warnings.append(f"the note's commit references were not checked: {exc}")
        return
    if unknown:
        result.warnings.append(
            "the note is stored, but its commits are in neither the job's repo nor its project's: "
            + ", ".join(unknown)
        )


def _preflight(config: Config, path: Path, name: str, spec: ConnSpec, tokens: list[str]) -> Decision:
    """Require a live tunnel, then the budget guard's consent; return its decision."""
    if not ssh.is_live(spec):
        raise LoginRequired(name)
    decision = budget_mod.decide(config, name, spec, tokens, path)
    if not decision.allowed:
        raise budget_blocked(name, tokens, decision)
    return decision


def _transfer(
    direction: transfer_mod.Direction,
    cluster: str,
    src: PathLike,
    dest: PathLike,
    dry_run: bool,
    extra: Sequence[str],
    capture: bool,
    config: Optional[Config],
    config_path: Optional[PathLike],
) -> Result:
    config, _ = _load(config, config_path)
    spec = _spec(config, cluster)
    if not ssh.is_live(spec):
        raise LoginRequired(cluster)
    try:
        if capture:
            res = transfer_mod.capture_transfer(
                spec, direction, str(src), str(dest), dry_run=dry_run, extra=extra
            )
            return Result(res.returncode, res.stdout, res.stderr)
        return Result(
            transfer_mod.run_transfer(spec, direction, str(src), str(dest), dry_run=dry_run, extra=extra)
        )
    except wsl.WslError as exc:
        raise TransportError(str(exc)) from exc
