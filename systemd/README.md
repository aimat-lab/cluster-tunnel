# Logout on shutdown (systemd)

This directory installs a hook that runs **`ctun logout`** automatically whenever
you log out, reboot, or power off — cleanly tearing down every live tunnel and
clearing its session record instead of leaving stale state behind.

It uses a **systemd *user* service** (not a system-wide one), because ctun's
state lives under your own `~/.config` and `~/.cache`. Tearing a tunnel down only
talks to the local SSH control socket, so it still succeeds after the network has
already gone away during shutdown.

## Install

```console
$ ./install.sh
```

That copies `cluster-tunnel-logout.service` into `~/.config/systemd/user/`,
points its `ExecStop` at your actual `ctun` binary, and enables it.

Manual equivalent:

```console
$ mkdir -p ~/.config/systemd/user
$ cp cluster-tunnel-logout.service ~/.config/systemd/user/
$ systemctl --user daemon-reload
$ systemctl --user enable --now cluster-tunnel-logout.service
```

## Test it without rebooting

Stopping the unit fires the same `ExecStop` that shutdown does:

```console
$ ctun -t horeka login          # open a tunnel
$ systemctl --user stop cluster-tunnel-logout.service
$ ctun status                   # the tunnel should now be down
```

## Remove

```console
$ ./install.sh --remove
```

## How it works / caveats

- The unit is `Type=oneshot` with `RemainAfterExit=yes`: `ExecStart` does nothing
  (`/bin/true`), and the real work is in `ExecStop=ctun logout`, which systemd
  runs when it stops the unit at session/manager teardown.
- `ExecStop` is prefixed with `-` so a non-zero exit (e.g. no config yet) never
  blocks shutdown.
- **Hard power loss** (holding the button, pulling the plug) cannot run any hook —
  but the tunnels die with the machine anyway, and the next `ctun login` starts a
  fresh session, so nothing is left in a broken state.
- If you enabled **lingering** (`loginctl enable-linger`), your user manager keeps
  running after you log out and only stops at real shutdown — the hook then fires
  on poweroff/reboot rather than on logout. Either way the tunnels get closed.
