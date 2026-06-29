"""Rich display classes for the ctun CLI."""

from __future__ import annotations

from pathlib import Path

from rich.padding import Padding
from rich.table import Table
from rich.text import Text

ASSETS_DIR = Path(__file__).parent / "assets"

# Horizontal gap (in spaces) between the logo image and the text beside it.
LOGO_GAP = 2

# Some ANSI-art tools store the escape character as a literal two-character
# sequence (``\e``, ``\033`` …) rather than the real ESC byte. Normalise those
# so ``Text.from_ansi`` can interpret the colour codes.
ESC = "\x1b"
_ESC_LITERALS = ("\\e", "\\033", "\\x1b", "\\u001b")

# The text wordmark (``logo_text.txt``) is figlet "ANSI Shadow" art of "C-tun".
# It is coloured in three regions: "C" before the dash, the "-" glyph itself,
# and "tun" after it. The dash glyph only occupies columns [8, 14) and only on
# the two rows that contain it (the surrounding rows there belong to the
# kerned-in "T" of "tun"), so the dash colour is restricted to exactly those
# cells. If you regenerate the art, re-measure these against the new columns.
TEXT_BEFORE_STYLE = "orange3"  # "C"
TEXT_DASH_STYLE = "orange3"  # "-" (same colour as the "C")
TEXT_AFTER_STYLE = "orange1"  # "tun"
DASH_COLS = (8, 14)
DASH_ROWS = (3, 4)


class RichLogo:
    """Renders the ctun logo: the ANSI-art image on the left and the text
    wordmark on the right, side by side with a small gap between them.

    The image is rendered verbatim (its own ANSI colour codes are honoured); the
    wordmark is colourised into its CLUSTER / "-" / TUNNEL regions.
    """

    def _read(self, name: str) -> str:
        try:
            raw = (ASSETS_DIR / name).read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""
        # Drop trailing newlines so they don't skew vertical centering; keep any
        # intentional internal blank lines and trailing spaces.
        return raw.rstrip("\n")

    def _load_image(self, name: str) -> Text:
        raw = self._read(name)
        for literal in _ESC_LITERALS:
            raw = raw.replace(literal, ESC)
        return Text.from_ansi(raw)

    def _load_text(self, name: str, fallback: str = "") -> Text:
        raw = self._read(name) or fallback
        start, end = DASH_COLS
        text = Text()
        for row, line in enumerate(raw.split("\n")):
            if row:
                text.append("\n")
            text.append(line[:start], style=TEXT_BEFORE_STYLE)
            if row in DASH_ROWS:
                text.append(line[start:end], style=TEXT_DASH_STYLE)
                text.append(line[end:], style=TEXT_AFTER_STYLE)
            else:
                text.append(line[start:], style=TEXT_AFTER_STYLE)
        return text

    def __rich_console__(self, console, options):
        image = self._load_image("logo_image.txt")
        text = self._load_text("logo_text.txt", fallback="ctun")

        lockup = Table.grid()
        lockup.add_column(vertical="middle")  # image
        lockup.add_column(vertical="middle")  # text
        lockup.add_row(image, Padding(text, (0, 0, 0, LOGO_GAP)))

        yield Padding(lockup, (1, 3, 0, 3))


class RichHelp:
    """Renders the project description below the logo."""

    def __rich_console__(self, console, options):
        yield (
            "[bold orange3]cluster-tunnel[/bold orange3] (ctun) — authenticated SSH tunnels "
            "+ budget guard for HPC clusters"
        )
        yield ""
        yield (
            "Log in once (password + OTP) and ctun keeps the SSH connection alive in the "
            "background, so you — and your coding agents — can run commands on the cluster "
            "with no re-authentication. A per-session compute-budget guard checks every job "
            "submission and hard-blocks overspend before it reaches the scheduler."
        )
        yield ""
        yield "[bold]Getting started[/bold]"

        steps = Table.grid(padding=(0, 2))
        steps.add_column(justify="right", style="dim")  # step number
        steps.add_column(no_wrap=True)  # command
        steps.add_column(style="dim")  # description
        steps.add_row("1.", Text("ctun config --init", style="cyan"), "create a starter config")
        steps.add_row(
            "2.", Text("ctun config", style="cyan"), "open it in your editor to add clusters"
        )
        steps.add_row(
            "3.",
            Text("ctun -t <cluster> login", style="cyan"),
            "authenticate once to open the tunnel",
        )
        steps.add_row(
            "4.",
            Text("ctun -t <cluster> run -- squeue", style="cyan"),
            "run commands on the cluster, no re-auth",
        )
        yield Padding(steps, (0, 0, 0, 2))
