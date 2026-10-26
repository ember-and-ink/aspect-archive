import io
import logging

from rich.console import Console

from aspect.runtime.dashboard import (
    Dashboard,
    DownloadFeed,
    LogBuffer,
    StdoutSink,
    replay_problems,
)


def _render(dashboard: Dashboard, *, width: int = 120, height: int = 30) -> str:
    console = Console(width=width, height=height, record=True, file=io.StringIO())
    console.print(dashboard.frame(height, width))
    return console.export_text()


def test_log_buffer_captures_records():
    buffer = LogBuffer()
    logger = logging.getLogger("aspect.test_dashboard")
    logger.addHandler(buffer)
    logger.setLevel(logging.INFO)
    try:
        logger.info("captured system prompt (#1, 42 chars)")
    finally:
        logger.removeHandler(buffer)

    assert len(buffer.lines) == 1
    text = buffer.lines[0].plain
    assert "INFO" in text
    assert "test_dashboard" in text  # logger name shortened
    assert "captured system prompt" in text


def test_log_buffer_splits_multiline_and_evicts():
    buffer = LogBuffer(maxlen=3)
    buffer.append("INFO", "one")
    buffer.emit(
        logging.LogRecord("x", logging.WARNING, __file__, 1, "a\nb\nc", None, None)
    )
    assert len(buffer.lines) == 3
    assert [line.plain.split()[-1] for line in buffer.lines] == ["a", "b", "c"]


def test_log_buffer_tail():
    buffer = LogBuffer()
    for i in range(10):
        buffer.append("INFO", str(i))
    assert [line.plain[-1] for line in buffer.tail(3)] == ["7", "8", "9"]
    assert buffer.tail(0) == []


def test_stdout_sink_only_emits_complete_lines():
    buffer = LogBuffer()
    sink = StdoutSink(buffer)
    sink.write("[datacat] fetched ")
    assert not buffer.lines  # nothing yet -- no newline
    sink.write("a token\n")
    assert len(buffer.lines) == 1
    assert "fetched a token" in buffer.lines[0].plain


def test_download_feed_counts_and_orders_newest_first():
    feed = DownloadFeed()
    feed.record(source="janitor", name="Aurora", creator="Ravenborn")
    feed.record(source="saucepan", name="Akane Kujo", creator="someone")
    feed.record(
        source="janitor", name="Ghost", creator="", ok=False, detail="no capture"
    )

    assert (feed.succeeded, feed.failed) == (2, 1)
    assert [row["name"] for row in feed.tail(2)] == ["Ghost", "Akane Kujo"]


def test_download_feed_counts_duplicates_apart_from_saves():
    feed = DownloadFeed()
    feed.record(source="janitor", name="Aurora", creator="Ravenborn")
    feed.record(source="janitor", name="Aurora", creator="Ravenborn", duplicate=True)

    assert (feed.succeeded, feed.duplicates, feed.failed) == (1, 1, 0)
    # A failure is never also a duplicate, whatever the caller passes.
    feed.record(source="janitor", name="Ghost", creator="", ok=False, duplicate=True)
    assert (feed.duplicates, feed.failed) == (1, 1)
    assert feed.tail(1)[0]["duplicate"] is False


def test_duplicate_download_is_marked_in_the_panel():
    dashboard = Dashboard(title="aspect-archive", address="http://x")
    dashboard.feed.record(
        source="saucepan",
        name="Emma",
        creator="Theodrax",
        duplicate=True,
        filename="Emma_123456.png",
    )
    out = _render(dashboard)
    assert "Emma" in out
    assert "dup" in out  # both the row and the panel title
    assert "by Theodrax" in out
    assert "0 new · 1 dup" in out


def test_download_feed_names_unnamed_cards():
    feed = DownloadFeed()
    feed.record(source="janitor", name="", creator="")
    assert feed.tail(1)[0]["name"] == "(unnamed)"


def test_frame_renders_header_and_both_columns():
    dashboard = Dashboard(
        title="aspect-archive",
        address="http://127.0.0.1:8000",
        stats=lambda: "3 captures",
    )
    dashboard.log.append("INFO", "listening for requests")
    dashboard.feed.record(source="janitor", name="Aurora", creator="Ravenborn")

    out = _render(dashboard)
    assert "aspect-archive" in out
    assert "http://127.0.0.1:8000" in out
    assert "3 captures" in out
    assert "server log" in out and "listening for requests" in out
    assert "downloads" in out and "1 new" in out
    assert "Aurora" in out and "by Ravenborn" in out
    assert "JAI" in out


def test_frame_renders_empty_and_survives_a_short_terminal():
    dashboard = Dashboard(title="aspect-archive", address="http://127.0.0.1:8000")
    out = _render(dashboard)
    assert "no characters yet" in out
    assert "waiting for requests" in out

    # A terminal barely taller than the header band must still render.
    assert "aspect-archive" in _render(dashboard, height=5)


def test_frame_survives_a_failing_stats_callable():
    def boom() -> str:
        raise RuntimeError("stats unavailable")

    dashboard = Dashboard(title="aspect-archive", address="http://x", stats=boom)
    assert "aspect-archive" in _render(dashboard)


def test_failed_download_shows_its_reason():
    dashboard = Dashboard(title="aspect-archive", address="http://x")
    dashboard.feed.record(
        source="janitor",
        name="Ghost",
        creator="",
        ok=False,
        detail="hidden — no chat capture yet",
    )
    out = _render(dashboard)
    assert "Ghost" in out
    assert "no chat capture" in out
    assert "1 ✗" in out


def test_problems_are_kept_for_replay():
    dashboard = Dashboard(title="aspect-archive", address="http://x")
    dashboard.log.append("INFO", "listening")
    dashboard.log.append("ERROR", "[Errno 48] Address already in use")

    assert len(dashboard.log.problems) == 1

    stream = io.StringIO()
    replay_problems(dashboard, stream)
    out = stream.getvalue()
    assert "Address already in use" in out
    assert "listening" not in out


def test_replay_is_silent_with_nothing_to_report():
    dashboard = Dashboard(title="aspect-archive", address="http://x")
    dashboard.log.append("INFO", "listening")
    stream = io.StringIO()
    replay_problems(dashboard, stream)
    assert stream.getvalue() == ""
