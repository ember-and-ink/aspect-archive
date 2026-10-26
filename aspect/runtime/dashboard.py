"""Live terminal dashboard for the running server.

Replaces the plain scrolling log with a three-part frame: a header band naming
the server and its address, a left column carrying the log stream (uvicorn's
included), and a right column listing the characters downloaded this run.

The pieces are deliberately dumb: request handlers only append to two deques
(`LogBuffer`, `DownloadFeed`), and Rich's `Live` re-renders `Dashboard` on its
own refresh tick -- so nothing in the request path ever touches the terminal or
blocks on a redraw.
"""

from __future__ import annotations

import io
import logging
import sys
from collections import deque
from datetime import datetime
from typing import Any, Callable

from rich.align import Align
from rich.console import (
    Console,
    ConsoleOptions,
    Group,
    RenderableType,
    RenderResult,
)
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# Width of the downloads column: enough for "Character / by Creator" on a wide
# terminal, but never more than a third of a narrow one -- at 80 columns a fixed
# 46 would leave the log column too cramped to read a log line.
FEED_WIDTH_MAX = 46
FEED_WIDTH_MIN = 26


def _feed_width(total_width: int) -> int:
    return max(min(total_width // 3, FEED_WIDTH_MAX), FEED_WIDTH_MIN)

# How much scrollback each deque keeps. Only the last screenful is ever drawn;
# the rest is headroom so a resize taller doesn't reveal blank rows.
LOG_SCROLLBACK = 500
FEED_SCROLLBACK = 200

_LEVEL_STYLES = {
    "DEBUG": "dim",
    "INFO": "cyan",
    "WARNING": "yellow",
    "ERROR": "bold red",
    "CRITICAL": "bold white on red",
}

# Short labels for the feed's Source column, keyed by the `source` the build
# endpoints pass in (the same vocabulary as extensions.datacat.sourceKind).
_SOURCE_LABELS = {
    "janitor": ("JAI", "magenta"),
    "saucepan": ("SAUCE", "green"),
}


class LogBuffer(logging.Handler):
    """A logging handler that renders records into an in-memory deque instead of
    writing them anywhere. Doing no I/O is the point: `emit` is called from the
    server's event loop and must never block on the terminal."""

    def __init__(self, maxlen: int = LOG_SCROLLBACK) -> None:
        super().__init__()
        self.lines: deque[Text] = deque(maxlen=maxlen)
        # Warnings and errors, kept separately so they can be replayed to the
        # real terminal once the live frame is torn down -- otherwise a fatal
        # startup error ("address already in use") flashes inside the dashboard
        # for an instant and vanishes with the alt-screen.
        self.problems: deque[Text] = deque(maxlen=20)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            if record.exc_info:
                message = f"{message}\n{self.format_exception(record)}"
        except Exception:  # pragma: no cover -- never let logging kill a request
            message = "<unformattable log record>"
        for line in message.splitlines() or [""]:
            self.append(record.levelname, line, name=record.name)

    def format_exception(self, record: logging.LogRecord) -> str:
        formatter = self.formatter or logging.Formatter()
        return formatter.formatException(record.exc_info)  # type: ignore[arg-type]

    def append(self, level: str, message: str, name: str = "") -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        line = Text(no_wrap=True, overflow="ellipsis")
        line.append(stamp + " ", style="dim")
        line.append(f"{level[:4]:<4} ", style=_LEVEL_STYLES.get(level, ""))
        if name:
            line.append(_short_logger_name(name) + " ", style="dim blue")
        line.append(message)
        self.lines.append(line)
        if level in ("WARNING", "ERROR", "CRITICAL"):
            self.problems.append(line)

    def tail(self, count: int) -> list[Text]:
        if count <= 0:
            return []
        return list(self.lines)[-count:]


def _short_logger_name(name: str) -> str:
    """`aspect.state.captures` -> `state.captures`, `uvicorn.error` -> `uvicorn`."""
    if name.startswith("aspect."):
        return name.split(".", 1)[1]
    return name.split(".", 1)[0]


class StdoutSink(io.TextIOBase):
    """Stand-in for stdout while the dashboard owns the terminal: a stray
    `print()` from anywhere in the process lands in the log column instead of
    scribbling over the frame."""

    def __init__(self, buffer: LogBuffer) -> None:
        self._buffer = buffer
        self._partial = ""

    def write(self, text: str) -> int:
        self._partial += text
        *complete, self._partial = self._partial.split("\n")
        for line in complete:
            if line.strip():
                self._buffer.append("INFO", line, name="stdout")
        return len(text)

    def flush(self) -> None:
        if self._partial.strip():
            self._buffer.append("INFO", self._partial, name="stdout")
        self._partial = ""

    def isatty(self) -> bool:
        return False


class DownloadFeed:
    """The right column's contents: one row per card this run tried to write."""

    def __init__(self, maxlen: int = FEED_SCROLLBACK) -> None:
        self.rows: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self.succeeded = 0
        self.failed = 0
        self.duplicates = 0

    def record(
        self,
        *,
        source: str,
        name: str,
        creator: str,
        ok: bool = True,
        detail: str = "",
        duplicate: bool = False,
        filename: str = "",
    ) -> None:
        self.rows.append(
            {
                "at": datetime.now(),
                "source": source,
                "name": name or "(unnamed)",
                "creator": creator or "",
                "ok": ok,
                "detail": detail,
                "duplicate": duplicate and ok,
                "filename": filename,
            }
        )
        if not ok:
            self.failed += 1
        elif duplicate:
            # Counted apart from `succeeded`: the interesting number mid-run is
            # how many cards the archive actually gained, not how many builds ran.
            self.duplicates += 1
        else:
            self.succeeded += 1

    def tail(self, count: int) -> list[dict[str, Any]]:
        """Newest first, so the most recent download is always at the top of the
        panel and never scrolls out of view mid-run."""
        if count <= 0:
            return []
        return list(self.rows)[-count:][::-1]


class Dashboard:
    """The renderable handed to `Live`. Holds the state, draws a frame on demand.

    `__rich__` is called on every refresh tick, so the panels re-slice their
    deques against the *current* terminal height -- resizing needs no wiring.
    """

    def __init__(
        self,
        *,
        title: str,
        address: str,
        stats: Callable[[], str] | None = None,
    ) -> None:
        self.title = title
        self.address = address
        self.log = LogBuffer()
        self.feed = DownloadFeed()
        self._stats = stats

    # -- header ---------------------------------------------------------

    def _header(self) -> RenderableType:
        line = Text(justify="center")
        line.append(self.title, style="bold white")
        line.append("  running at  ", style="dim")
        line.append(self.address, style="bold cyan")
        if self._stats is not None:
            try:
                extra = self._stats()
            except Exception:
                extra = ""
            if extra:
                line.append("   " + extra, style="dim")
        return Panel(Align.center(line, vertical="middle"), style="blue")

    # -- columns --------------------------------------------------------

    def _logs(self, height: int) -> RenderableType:
        # Two rows of the panel go to its border, one to the padding-free title.
        lines = self.log.tail(height - 2)
        body: RenderableType = (
            Group(*lines) if lines else Text("waiting for requests…", style="dim")
        )
        return Panel(body, title="server log", title_align="left", border_style="dim")

    def _downloads(self, height: int) -> RenderableType:
        table = Table.grid(padding=(0, 1))
        table.add_column(width=5, no_wrap=True)  # source
        table.add_column(ratio=1, overflow="ellipsis")  # name / creator

        # Each row is two printed lines (name, then "by creator"), so the row
        # budget is half the space the panel's interior has.
        rows = self.feed.tail(max((height - 2) // 2, 0))
        for row in rows:
            label, style = _SOURCE_LABELS.get(
                row["source"], (row["source"][:5].upper(), "white")
            )
            if not row["ok"]:
                name_style = "bold red"
            elif row["duplicate"]:
                # Dimmed, not coloured like a failure: the export worked, it just
                # didn't add anything to the archive.
                name_style = "yellow"
            else:
                name_style = "bold"
            headline = Text(row["name"], style=name_style)
            table.add_row(Text(label, style=style), headline)

            # A creator name can be clipped without losing much; a failure's
            # reason is the whole point of the row, so let that one wrap. The
            # extra line it costs just pushes an older row off the bottom.
            sub = Text(no_wrap=row["ok"], overflow="ellipsis" if row["ok"] else "fold")
            sub.append(row["at"].strftime("%H:%M "), style="dim")
            if not row["ok"]:
                sub.append(row["detail"] or "failed", style="red")
            else:
                if row["duplicate"]:
                    sub.append("dup ", style="bold yellow")
                sub.append("by " + (row["creator"] or "unknown"), style="dim")
            table.add_row("", sub)

        body: RenderableType = (
            table if rows else Text("no characters yet", style="dim")
        )
        # Terse on purpose: the title has to survive a 26-column feed panel, and
        # Rich ellipsizes what doesn't fit -- so the rightmost counter is the one
        # that would be lost. Only non-zero counters are shown.
        counts = f"{self.feed.succeeded} new"
        if self.feed.duplicates:
            counts += f" · {self.feed.duplicates} dup"
        if self.feed.failed:
            counts += f" · {self.feed.failed} ✗"
        return Panel(
            body,
            title=f"downloads · {counts}",
            title_align="left",
            border_style="dim",
        )

    # -- frame ----------------------------------------------------------

    def frame(self, height: int, width: int = 120) -> RenderableType:
        body_height = max(height - 3, 3)  # minus the header band

        layout = Layout()
        layout.split_column(
            Layout(self._header(), name="header", size=3),
            Layout(name="body"),
        )
        layout["body"].split_row(
            Layout(self._logs(body_height), name="logs", ratio=1),
            Layout(
                self._downloads(body_height),
                name="downloads",
                size=_feed_width(width),
            ),
        )
        return layout

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        # Take the height Live hands down (the alt-screen's full height) rather
        # than probing the console: stdout is redirected while the dashboard is
        # up, so a freshly built Console would report the 80x25 fallback.
        yield self.frame(
            options.height or console.size.height, options.max_width
        )


def install_logging(dashboard: Dashboard, level: int = logging.INFO) -> None:
    """Point the root logger at the dashboard's buffer and nowhere else.

    Any handler already on root (a stray `basicConfig`, say) is removed -- one
    escaped StreamHandler is enough to scribble over the live frame.
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    dashboard.log.setFormatter(logging.Formatter())
    root.addHandler(dashboard.log)
    root.setLevel(level)


def replay_problems(dashboard: Dashboard, stream: Any) -> None:
    """Reprint whatever warnings and errors the run collected onto the plain
    terminal. Called after the live frame is gone, so that a server which died
    on startup still says why instead of leaving a blank prompt behind."""
    if not dashboard.log.problems:
        return
    console = Console(file=stream, stderr=True)
    console.print("\n[bold]warnings and errors from this run:[/bold]")
    for line in dashboard.log.problems:
        console.print(line)


def live(dashboard: Dashboard, refresh_per_second: float = 4) -> Live:
    """A `Live` pinned to the *real* stdout.

    Rich's default console resolves `sys.stdout` at write time, so once
    server.main installs StdoutSink every frame after the first would be drawn
    into the log buffer instead of the terminal. Binding the console to the
    stream up front keeps the two paths separate.
    """
    console = Console(file=sys.stdout)
    return Live(
        dashboard,
        console=console,
        refresh_per_second=refresh_per_second,
        screen=True,
        redirect_stdout=False,  # we install StdoutSink ourselves, see server.main
        redirect_stderr=False,
    )


# The dashboard currently drawing, or None when the server is running under
# plain logging (no TTY, or ASPECT_DASHBOARD off). Set by `aspect.server.main`
# for the lifetime of the run and cleared on the way out.
#
# It lives here rather than in `aspect.server` because the route modules that
# report into it (`aspect.api.build`) must not import the app module -- the app
# imports them. Read it through the module, never as a from-import: this name is
# rebound at startup, and a frozen binding would leave the feed permanently None.
DASHBOARD: "Dashboard | None" = None
