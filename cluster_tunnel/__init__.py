"""cluster-tunnel (ctun) — authenticated SSH tunnels + budget guard for HPC clusters."""

from cluster_tunnel.constants import get_version

#: Read from the bundled VERSION file so it can never drift from the packaged
#: version. Bump both at once with `bump-my-version bump <part>`.
__version__ = get_version()
