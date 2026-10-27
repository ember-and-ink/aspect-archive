"""3C-2 -- the job runner (docs/PHASE_3C_PLAN.md §7).

`media_writer.download_item` and the manifest it writes are already correct
(3C-1); the thing that was missing was a way to run a batch of them that
doesn't die when the browser tab that started it closes. `POST
/characters/{id}/media` still exists and is still correct -- it streams
NDJSON over the request's own connection, so the run is only as long-lived as
that connection. `JobStore` runs the identical per-item loop as a detached
asyncio task instead: the request that submits a job returns immediately with
a job id, and the download keeps going on the server regardless of what the
browser does next.

One job at a time, not a pool of jobs. `GalleryIndex` and the manifest dict
are built once per run and mutated in place across every item in it
(docs/PHASE_3C_PLAN.md §3's "Dedup is a scandir") -- exactly the reason
`web/modules/media-download-queue.js` already runs its own jobs strictly
serially. A single background coroutine draining one queue preserves that
invariant without a lock: two jobs against the same gallery folder can never
overlap. Within one job the items themselves do run concurrently
(`media_writer.download_batch`), which is safe for the same reason a lock
would be: only the fetch awaits, so no two items can be mid-`finish_item` at
once on this loop.

Job state (this module) is a live-progress cache, not a record of what
actually happened -- that's the manifest and dead ledger, written by `_run`
exactly like the synchronous route writes them. A job lost to a server
restart mid-run leaves the manifest showing whatever finished; nothing here
needs to survive a restart for the archive to stay correct.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal


from aspect.archive import thumbs
from aspect.media import manifest as media_manifest, writer as media_writer
from aspect.config import settings
from aspect.runtime import net

logger = logging.getLogger("aspect.media.jobs")

JobState = Literal["queued", "running", "done", "error", "cancelled"]


JobScope = Literal["card", "all"]

# Resolve one card id to the work for it, or None to skip it. Returns
# `(folder_name, gallery_dir, items, sources)` -- `sources` being the source
# ledger records `_run` writes alongside the run (`manifest.record_source`).
# Called on a worker thread, one card at a time, at the moment that card comes
# up -- never up front for the whole batch.
BatchPlanner = Callable[[str], "tuple[str, Path, list[dict[str, Any]], list[dict[str, Any]]] | None"]

# A batch over 3,868 cards can emit far more events than any poller reads, and
# `_job_status_out(after=0)` materializes every one of them into a Pydantic
# model. Keep a bounded tail and count what fell off -- the manifest is the
# actual record of what happened (see the module docstring).
MAX_EVENTS = 2000


@dataclass
class MediaJob:
    id: str
    card_id: str | None
    prefix: str
    phase: str
    total: int
    created_at: str
    scope: JobScope = "card"
    state: JobState = "queued"
    done: int = 0
    saved: int = 0
    skipped: int = 0
    errors: int = 0
    events: list[dict[str, Any]] = field(default_factory=list)
    events_dropped: int = 0
    error_message: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    cancel_requested: bool = False
    # Batch-only. `cards_total` counts cards *considered*, so
    # cards_done + cards_skipped converges on it.
    cards_total: int = 0
    cards_done: int = 0
    cards_skipped: int = 0
    current_card_id: str | None = None

    # Total events ever appended. The poll cursor counts *these*, not indices
    # into `events`, so trimming the front cannot make an old cursor point at
    # the wrong event -- it can only make it point at one already dropped.
    events_seen: int = 0

    def record_event(self, event: dict[str, Any]) -> None:
        self.events.append(event)
        self.events_seen += 1
        if len(self.events) > MAX_EVENTS:
            # Drop from the front: a poller this far behind has already lost the
            # head, and the tail is what it wants next.
            overflow = len(self.events) - MAX_EVENTS
            del self.events[:overflow]
            self.events_dropped += overflow

    def events_after(self, cursor: int) -> list[dict[str, Any]]:
        """Events with sequence number >= `cursor`, as far back as the buffer
        still holds. A cursor pointing into the dropped region yields the whole
        buffer rather than an error -- `events_dropped` is how a caller learns
        it missed some."""
        first_held = self.events_seen - len(self.events)
        return self.events[max(0, cursor - first_held) :]


class JobStore:
    """Bound to the running event loop at app startup (`bind`). `submit` is
    called from a FastAPI threadpool thread (the sync-`def` house style --
    see the module docstring in `aspect/api/v1/`) and hands the item list
    to the worker via `run_coroutine_threadsafe`; it must never be called
    from the loop's own thread, or the `.result()` wait deadlocks."""

    def __init__(self, thumbnail_store: thumbs.ThumbnailStore, max_history: int = 200) -> None:
        self._thumbnail_store = thumbnail_store
        self.max_history = max_history
        self._jobs: dict[str, MediaJob] = {}
        self._order: deque[str] = deque()
        self._queue: asyncio.Queue | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._worker_task: asyncio.Task | None = None

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        # A fresh loop means a fresh process (or, in tests, a fresh
        # TestClient lifespan) -- any prior job history belongs to a loop
        # that no longer exists, so start clean rather than leak across runs.
        self._jobs.clear()
        self._order.clear()
        self._loop = loop
        self._queue = asyncio.Queue()
        self._worker_task = loop.create_task(self._worker())

    def _enqueue(self, job: MediaJob, payload: tuple) -> MediaJob:
        if self._loop is None or self._queue is None:
            raise RuntimeError("JobStore.bind() must run before the first submission")
        self._jobs[job.id] = job
        self._order.append(job.id)
        self._evict_finished()
        asyncio.run_coroutine_threadsafe(self._queue.put((job.id, payload)), self._loop).result()
        return job

    def submit(
        self,
        gallery_dir: Path,
        folder_name: str,
        items: list[dict[str, Any]],
        prefix: str,
        phase: str,
        card_id: str,
        sources: list[dict[str, Any]] | None = None,
    ) -> MediaJob:
        job = MediaJob(
            id=uuid.uuid4().hex,
            card_id=card_id,
            prefix=prefix,
            phase=phase,
            total=len(items),
            created_at=media_manifest.now_iso(),
        )
        return self._enqueue(job, ("card", gallery_dir, folder_name, items, sources or []))

    def submit_batch(
        self,
        card_ids: list[str],
        prefix: str,
        phase: str,
        *,
        plan: "BatchPlanner",
    ) -> MediaJob:
        """Stage 6B -- bulk localize, as one job that walks `card_ids` in order.

        Not a pool of child jobs: the single serial worker below *is* the lock
        that keeps two runs off the same gallery's manifest (see the module
        docstring), and spawning per-card jobs would give that up for no gain.
        Concurrency already lives one level down, inside `download_batch`.

        `plan` resolves a card id to `(folder_name, gallery_dir, items, sources)`
        at the moment that card comes up, or None to skip the card. It is a
        callback rather than a precomputed list for two reasons: resolving a
        gallery directory *creates* it -- doing that for 3,868 cards up front
        would litter the archive with empty folders and rewrite every card that
        has no `gallery_id` yet -- and the skip decision itself needs the card's
        own text (`manifest.sources_satisfied`), which is the planner's to read.

        Every card is therefore in `card_ids`, and `cards_skipped` fills in as
        the run walks them. The totals still describe the archive rather than a
        worklist ("3,868 cards, 3,144 skipped"); they just aren't known in
        advance any more.
        """
        total = len(card_ids)
        job = MediaJob(
            id=uuid.uuid4().hex,
            card_id=None,
            scope="all",
            prefix=prefix,
            phase=phase,
            total=total,
            cards_total=total,
            created_at=media_manifest.now_iso(),
        )
        return self._enqueue(job, ("all", card_ids, plan))

    def get(self, job_id: str) -> MediaJob | None:
        return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job is None or job.state not in ("queued", "running"):
            return False
        job.cancel_requested = True
        return True

    def list_jobs(self, *, card_id: str | None = None, active_only: bool = False) -> list[MediaJob]:
        out = [self._jobs[jid] for jid in self._order if jid in self._jobs]
        if card_id is not None:
            out = [j for j in out if j.card_id == card_id]
        if active_only:
            out = [j for j in out if j.state in ("queued", "running")]
        return out

    def _evict_finished(self) -> None:
        while len(self._order) > self.max_history:
            oldest = self._order[0]
            job = self._jobs.get(oldest)
            if job is None or job.state in ("done", "error", "cancelled"):
                self._order.popleft()
                self._jobs.pop(oldest, None)
            else:
                # Oldest entry is still active -- history grows past
                # max_history rather than evicting live progress.
                break

    async def _worker(self) -> None:
        assert self._queue is not None
        while True:
            job_id, payload = await self._queue.get()
            job = self._jobs.get(job_id)
            if job is None:
                self._queue.task_done()
                continue
            job.state = "running"
            job.started_at = media_manifest.now_iso()
            try:
                if payload[0] == "all":
                    _kind, card_ids, plan = payload
                    await self._run_batch(job, card_ids, plan)
                else:
                    _kind, gallery_dir, folder_name, items, sources = payload
                    await self._run(job, gallery_dir, folder_name, items, sources=sources)
                if job.state == "running":
                    job.state = "done"
            except Exception as exc:  # one bad job must not kill the worker
                job.state = "error"
                job.error_message = str(exc)
                logger.exception("media job %s failed", job.id)
            finally:
                job.finished_at = media_manifest.now_iso()
                self._queue.task_done()

    async def _run(
        self,
        job: MediaJob,
        gallery_dir: Path,
        folder_name: str,
        items: list[dict[str, Any]],
        *,
        sources: list[dict[str, Any]] | None = None,
        ledger: dict | None = None,
    ) -> None:
        """One card's worth of downloading.

        `ledger` lets a batch own the dead ledger across many cards: it is a
        single global file (`manifest.py`), so loading and atomically rewriting
        it per card would mean thousands of rewrites of the same few thousand
        entries. Passed in, this method neither loads nor saves it.

        `sources` are the source-ledger records the planner produced for this
        card. They are written here, with the run, rather than at discovery
        time: a run that is abandoned or crashes must not leave behind a
        manifest claiming its galleries were dealt with.
        """
        owns_ledger = ledger is None
        # The job's counters are cumulative across a batch, but the manifest run
        # this appends belongs to *this card* -- so record the deltas, not the
        # running totals.
        saved_before, skipped_before, errors_before = job.saved, job.skipped, job.errors
        manifest = media_manifest.load_manifest(gallery_dir)
        if ledger is None:
            ledger = media_manifest.load_dead_ledger()
        index_state = media_writer.GalleryIndex.build(gallery_dir)
        # Same scheme the synchronous route uses: a millisecond timestamp as
        # the starting file index, unique across runs with no persistent counter.
        start_index = int(time.time() * 1000)
        async with net.async_client(
            timeout=30.0, headers={"User-Agent": "Mozilla/5.0"}
        ) as client:
            batch = media_writer.download_batch(
                client,
                gallery_dir,
                folder_name,
                items=items,
                prefix=job.prefix,
                start_index=start_index,
                index_state=index_state,
                manifest=manifest,
                ledger=ledger,
                thumbnail_store=self._thumbnail_store,
                should_cancel=lambda: job.cancel_requested,
            )
            async for outcome in batch:
                if outcome.status == "saved":
                    job.saved += 1
                elif outcome.status == "skipped":
                    job.skipped += 1
                else:
                    job.errors += 1
                # A batch counts cards, not URLs, so its `done` is advanced by
                # `_run_batch` instead.
                if job.scope == "card":
                    job.done += 1
                job.record_event(
                    {
                        "url": outcome.url,
                        "status": outcome.status,
                        "file": outcome.file,
                        "reason": outcome.reason,
                        "bytes": outcome.bytes,
                    }
                )

        # `download_batch` stops handing out work as soon as `should_cancel`
        # fires; the state flip is this loop's to make, after it drains.
        if job.cancel_requested and job.scope == "card":
            job.state = "cancelled"

        for record in sources or []:
            media_manifest.record_source(
                manifest,
                record["key"],
                record["handler"],
                record["status"],
                count=record.get("count"),
                reason=record.get("reason"),
            )
        media_manifest.append_run(
            manifest,
            {
                "at": media_manifest.now_iso(),
                "phase": job.phase,
                "saved": job.saved - saved_before,
                "skipped": job.skipped - skipped_before,
                "errors": job.errors - errors_before,
            },
        )
        media_manifest.save_manifest(gallery_dir, manifest)
        if owns_ledger:
            media_manifest.save_dead_ledger(ledger)

    # Save the shared dead ledger every N cards rather than every card: it is
    # one global file, and a batch would otherwise rewrite it thousands of times.
    LEDGER_SAVE_EVERY = 25

    async def _run_batch(
        self,
        job: MediaJob,
        card_ids: list[str],
        plan: "BatchPlanner",
    ) -> None:
        """Stage 6B bulk localize: every card, in order, inside one job.

        Sequential on purpose -- see `submit_batch`. Each card gets its own
        manifest run, so a batch interrupted at any point leaves every finished
        card correct on disk; the job record itself is a live-progress cache and
        is not expected to survive a restart.
        """
        ledger = media_manifest.load_dead_ledger()
        # Cards ruled out before the job started are already counted.
        base_done = job.done
        try:
            for position, card_id in enumerate(card_ids, start=1):
                if job.cancel_requested:
                    job.state = "cancelled"
                    break
                job.current_card_id = card_id
                try:
                    # `plan` reads and parses a PNG and may mint a gallery id --
                    # synchronous work that would otherwise stall the single
                    # event loop (and with it browse, thumbnails and every other
                    # request) for the length of the run.
                    planned = await asyncio.to_thread(plan, card_id)
                except Exception as exc:
                    job.errors += 1
                    job.record_event(
                        {"url": card_id, "status": "error", "reason": f"could not plan: {exc}"}
                    )
                    planned = None

                if planned is None:
                    # Nothing to fetch (already complete, unreadable, or no
                    # media found). Deliberately *not* an error.
                    job.cards_skipped += 1
                else:
                    folder_name, gallery_dir, items, sources = planned
                    await self._run(
                        job, gallery_dir, folder_name, items, sources=sources, ledger=ledger
                    )
                    job.cards_done += 1

                job.done = base_done + position
                if position % self.LEDGER_SAVE_EVERY == 0:
                    media_manifest.save_dead_ledger(ledger)
        finally:
            job.current_card_id = None
            media_manifest.save_dead_ledger(ledger)
