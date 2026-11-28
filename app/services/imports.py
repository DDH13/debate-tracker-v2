"""Console imports of Tabbycat tournaments: check first, settle doubtful matches, then import.

A batch holds one `ImportJob` per tournament. Jobs run on one background thread per process
and, across processes, one at a time (a Postgres advisory lock), in batch order, so a school
created by the first tournament is matched by key when the second imports.

- **Check**: a dry run of the importer with the API key, which is held in memory only. Every
  upstream response is staged under `settings.import_staging_dir/<job id>/`. The report's
  issues are stored on the job and each `decision` gets its suggested choice.
- **Import**: the same importer replays the staged responses offline (no key, no second
  download, exactly the data that was reviewed) with the saved decisions as overrides. A
  blocking issue found then (say a chosen school was merged away meanwhile) fails the job.
- **Recovery**: the running process touches its jobs' `updated_at`; an active job whose
  heartbeat is older than `settings.import_stale_seconds` was lost and is marked failed.
"""

import logging
import os
import shutil
import socket
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, select

from app.core.config import settings
from app.models import ImportBatch, ImportJob, Institution, Debater, Judge, Tournament, User
from app.services.stats.profiles import refresh_profiles
from app.services.tabbycat import (
    ImportBlocked,
    ImportIssue,
    ImportOverrides,
    ImportReport,
    TabbycatImportError,
    TournamentAlreadyExists,
    describe_integrity_error,
    import_tournament,
)

logger = logging.getLogger(__name__)

JobStatus = Literal[
    "queued_check", "checking", "ready", "queued_import", "importing", "imported", "failed", "discarded"
]
ACTIVE = ("queued_check", "checking", "queued_import", "importing")
MAX_ROWS = 50
_LOCK_KEY = 0x7AB1CA7  # pg advisory lock id shared by every process running imports
_RESERVED_FIRST_SEGMENTS = {"", "admin", "accounts", "api", "static", "jet", "database", "for", "summernote"}


class ImportsInvalid(Exception):
    """A request the import flow can't accept (422)."""


class ImportsConflict(Exception):
    """A request that clashes with a job's current state (409)."""


# --- URL parsing -----------------------------------------------------------------------------


def parse_tabbycat_url(url: str) -> tuple[str, str]:
    """`https://host/<slug>/...` or `https://host/api/v1/tournaments/<slug>` -> (base URL, slug)."""
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ImportsInvalid(f"{url!r} is not an http(s) URL")
    segments = [s for s in parts.path.split("/") if s]
    if segments[:3] == ["api", "v1", "tournaments"] and len(segments) > 3:
        slug = segments[3]
    elif segments and segments[0].lower() not in _RESERVED_FIRST_SEGMENTS:
        slug = segments[0]
    else:
        raise ImportsInvalid(f"{url!r} doesn't name a tournament (expected https://host/<tournament-slug>/)")
    if not all(c.isalnum() or c in "-_" for c in slug):
        raise ImportsInvalid(f"{slug!r} in {url!r} isn't a valid Tabbycat tournament slug")
    return f"{parts.scheme}://{parts.netloc}", slug


# --- API models -------------------------------------------------------------------------------


class ImportRow(SQLModel):
    url: str
    api_key: str | None = None


class ImportBatchCreate(SQLModel):
    rows: list[ImportRow]
    include_ballots: bool = True
    refresh_stats: bool = True


class RecheckRequest(SQLModel):
    api_key: str | None = None


class RunRequest(SQLModel):
    job_ids: list[int]


class ImportJobPublic(SQLModel):
    id: int
    batch_id: int
    position: int
    base_url: str
    slug: str
    status: JobStatus
    phase: str | None
    progress_done: int
    progress_total: int
    error: str | None
    report: ImportReport | None
    decisions: ImportOverrides
    undecided: int
    tournament_id: int | None
    checked_at: datetime | None
    finished_at: datetime | None
    updated_at: datetime


class ImportBatchPublic(SQLModel):
    id: int
    created_at: datetime
    created_by: str | None
    include_ballots: bool
    refresh_stats: bool
    stats_refreshed_at: datetime | None
    jobs: list[ImportJobPublic]


class ImportBatchSummary(SQLModel):
    id: int
    created_at: datetime
    created_by: str | None
    tournaments: list[str]
    counts: dict[str, int]


# --- Small helpers ----------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _staging(job_id: int) -> Path:
    return Path(settings.import_staging_dir) / str(job_id)


def _clear_staging(job_id: int) -> None:
    shutil.rmtree(_staging(job_id), ignore_errors=True)


def _concurrent_writes_ok(bind: Engine) -> bool:
    """False for the in-memory SQLite engine: it is one shared connection, so a second session
    committing progress would also commit the import's open (dry-run) transaction."""
    return not isinstance(bind.pool, StaticPool)


def _report(job: ImportJob) -> ImportReport | None:
    return ImportReport.model_validate(job.report) if job.report else None


def _decisions(job: ImportJob) -> ImportOverrides:
    return ImportOverrides.model_validate(job.decisions or {})


def _undecided(report: ImportReport | None, decisions: ImportOverrides) -> list[ImportIssue]:
    if report is None:
        return []
    return [
        issue
        for issue in report.issues
        if issue.severity == "decision" and issue.upstream_url not in decisions.for_kind(issue.kind)
    ]


def job_public(job: ImportJob) -> ImportJobPublic:
    report = _report(job)
    decisions = _decisions(job)
    return ImportJobPublic(
        **job.model_dump(exclude={"report", "decisions"}),
        report=report,
        decisions=decisions,
        undecided=len(_undecided(report, decisions)),
    )


# --- Background execution ---------------------------------------------------------------------

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tabbycat-import")
_local_lock = threading.Lock()
_owned: set[int] = set()  # job ids queued or running in this process
_owned_lock = threading.Lock()
_heartbeat_started = False
_RUNNER = f"{socket.gethostname()}:{os.getpid()}"


def _own(job_ids: list[int]) -> None:
    with _owned_lock:
        _owned.update(job_ids)


def _disown(job_ids: list[int]) -> None:
    with _owned_lock:
        _owned.difference_update(job_ids)


def _heartbeat_loop(bind: Engine) -> None:
    while True:
        time.sleep(30)
        with _owned_lock:
            ids = list(_owned)
        if not ids:
            continue
        try:
            with Session(bind) as session:
                for job in session.exec(select(ImportJob).where(ImportJob.id.in_(ids))).all():
                    if job.status in ACTIVE:
                        job.updated_at = _now()
                        session.add(job)
                session.commit()
        except Exception:  # never let the heartbeat thread die
            logger.exception("import heartbeat failed")


def _ensure_heartbeat(bind: Engine) -> None:
    global _heartbeat_started
    if _heartbeat_started or not _concurrent_writes_ok(bind):
        return
    _heartbeat_started = True
    threading.Thread(target=_heartbeat_loop, args=(bind,), daemon=True, name="import-heartbeat").start()


def _submit(bind: Engine, job_ids: list[int], task: Callable[[], None]) -> None:
    _own(job_ids)

    def run() -> None:
        try:
            task()
        except Exception:
            logger.exception("import task for jobs %s crashed", job_ids)
            _fail_unfinished(bind, job_ids, "The import task crashed; see the server log")
        finally:
            _disown(job_ids)

    if settings.import_run_inline:
        run()
    else:
        _ensure_heartbeat(bind)
        _executor.submit(run)


@contextmanager
def _global_lock(bind: Engine) -> Iterator[None]:
    """One check or import at a time across every process. Two concurrent dry runs inserting
    the same new school would otherwise block on each other's uncommitted unique rows."""
    if bind.dialect.name == "postgresql":
        with bind.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(text("SELECT pg_advisory_lock(:key)"), {"key": _LOCK_KEY})
            try:
                yield
            finally:
                conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": _LOCK_KEY})
    else:
        with _local_lock:
            yield


def _update(bind: Engine, job_id: int, **fields: Any) -> ImportJob | None:
    with Session(bind) as session:
        job = session.get(ImportJob, job_id)
        if job is None:
            return None
        for name, value in fields.items():
            setattr(job, name, value)
        job.updated_at = _now()
        session.add(job)
        session.commit()
        session.refresh(job)
        return job


def _transition(bind: Engine, job_id: int, expected: str, **fields: Any) -> ImportJob | None:
    """Move a job on only if it is still in `expected` (it may have been discarded or
    rechecked while it waited in the queue)."""
    with Session(bind) as session:
        job = session.get(ImportJob, job_id)
        if job is None or job.status != expected:
            return None
    return _update(bind, job_id, **fields)


def _fail_unfinished(bind: Engine, job_ids: list[int], message: str) -> None:
    with Session(bind) as session:
        for job in session.exec(select(ImportJob).where(ImportJob.id.in_(job_ids))).all():
            if job.status in ACTIVE:
                job.status = "failed"
                job.error = message
                job.phase = None
                job.updated_at = _now()
                session.add(job)
        session.commit()


# Live progress for this process; the job row is updated too where that's safe (see
# `_concurrent_writes_ok`), so a poll answered by another worker still sees it.
_live_progress: dict[int, tuple[str, int, int]] = {}


def _progress_callback(bind: Engine, job_id: int) -> Callable[[str, int, int], None]:
    last_write = 0.0
    persist = _concurrent_writes_ok(bind)

    def progress(phase: str, done: int, total: int) -> None:
        nonlocal last_write
        _live_progress[job_id] = (phase, done, total)
        now = time.monotonic()
        if persist and now - last_write >= 1.0:
            last_write = now
            try:
                _update(bind, job_id, phase=phase, progress_done=done, progress_total=total)
            except Exception:
                logger.exception("could not record progress for import job %s", job_id)

    return progress


def _describe_failure(exc: Exception) -> str:
    if isinstance(exc, TabbycatImportError):
        if exc.status_code in (401, 403):
            return "Tabbycat rejected the API key (or the tournament needs one). Check again with the right key."
        if exc.status_code == 404:
            return "Tabbycat has no tournament with this slug at this address."
        return str(exc)
    if isinstance(exc, httpx.TransportError):
        return f"Could not reach Tabbycat: {exc}"
    if isinstance(exc, IntegrityError):
        return f"The import conflicts with existing data: {describe_integrity_error(exc)}"
    return f"{type(exc).__name__}: {exc}"


def _already_imported_report(session: Session, slug: str) -> ImportReport:
    existing = session.exec(select(Tournament).where(Tournament.slug == slug)).first()
    report = ImportReport(tournament_id=existing.id if existing else None)
    label = f"{existing.name} (#{existing.id})" if existing else slug
    report.issues.append(
        ImportIssue(
            severity="blocking",
            code="already_imported",
            message=f"This tournament is already in the database as {label}. Delete it first to import it again.",
        )
    )
    return report


def _check_jobs(bind: Engine, jobs: list[tuple[int, str | None]], include_ballots: bool) -> None:
    for job_id, api_key in jobs:
        with _global_lock(bind):
            job = _transition(
                bind, job_id, "queued_check", status="checking", phase="Starting", progress_done=0, progress_total=0
            )
            if job is None:
                continue
            _clear_staging(job_id)
            try:
                with Session(bind) as session:
                    try:
                        report = import_tournament(
                            job.base_url,
                            job.slug,
                            api_key,
                            session=session,
                            include_ballots=include_ballots,
                            dry_run=True,
                            progress=_progress_callback(bind, job_id),
                            cache_dir=str(_staging(job_id)),
                        )
                    except TournamentAlreadyExists:
                        report = _already_imported_report(session, job.slug)
            except Exception as exc:
                logger.warning("import check of %s@%s failed: %s", job.slug, job.base_url, exc)
                _clear_staging(job_id)
                _update(bind, job_id, status="failed", phase=None, error=_describe_failure(exc), checked_at=_now())
                continue
            finally:
                _live_progress.pop(job_id, None)
            # Preselect each suggestion; the reviewer changes what they disagree with.
            decisions = ImportOverrides()
            for issue in report.issues:
                if issue.severity == "decision" and issue.suggested is not None:
                    decisions.for_kind(issue.kind)[issue.upstream_url] = issue.suggested
            if report.blocking:
                _clear_staging(job_id)
            _update(
                bind,
                job_id,
                status="ready",
                phase=None,
                report=report.model_dump(mode="json"),
                decisions=decisions.model_dump(mode="json"),
                checked_at=_now(),
            )


def _import_jobs(bind: Engine, batch_id: int, job_ids: list[int]) -> None:
    imported_any = False
    for job_id in job_ids:
        with _global_lock(bind):
            job = _transition(
                bind, job_id, "queued_import", status="importing", phase="Starting", progress_done=0, progress_total=0
            )
            if job is None:
                continue
            with Session(bind) as session:
                batch = session.get(ImportBatch, batch_id)
                include_ballots = batch.include_ballots if batch else True
            if not _staging(job_id).exists():
                _update(
                    bind,
                    job_id,
                    status="failed",
                    phase=None,
                    error="The checked data is no longer on the server. Check again with the API key.",
                )
                continue
            try:
                with Session(bind) as session:
                    report = import_tournament(
                        job.base_url,
                        job.slug,
                        None,
                        session=session,
                        include_ballots=include_ballots,
                        overrides=_decisions(job),
                        progress=_progress_callback(bind, job_id),
                        cache_dir=str(_staging(job_id)),
                        offline=True,
                    )
            except ImportBlocked as exc:
                _update(
                    bind,
                    job_id,
                    status="failed",
                    phase=None,
                    error=f"Not imported: {exc}",
                    report=exc.report.model_dump(mode="json"),
                )
                continue
            except TournamentAlreadyExists as exc:
                _update(bind, job_id, status="failed", phase=None, error=f"Not imported: {exc}")
                continue
            except Exception as exc:
                logger.warning("import of %s@%s failed: %s", job.slug, job.base_url, exc)
                _update(bind, job_id, status="failed", phase=None, error=_describe_failure(exc))
                continue
            finally:
                _live_progress.pop(job_id, None)
            _clear_staging(job_id)
            _update(
                bind,
                job_id,
                status="imported",
                phase=None,
                report=report.model_dump(mode="json"),
                tournament_id=report.tournament_id,
                finished_at=_now(),
            )
            imported_any = True

    if not imported_any:
        return
    with Session(bind) as session:
        batch = session.get(ImportBatch, batch_id)
        if batch is None or not batch.refresh_stats:
            return
    with _global_lock(bind), Session(bind) as session:
        logger.info("refreshing stats after import batch %s", batch_id)
        refresh_profiles(session)
        batch = session.get(ImportBatch, batch_id)
        batch.stats_refreshed_at = _now()
        session.add(batch)
        session.commit()


# --- Operations behind the endpoints ----------------------------------------------------------


def create_batch(session: Session, user: User, body: ImportBatchCreate) -> ImportBatch:
    if not body.rows:
        raise ImportsInvalid("Add at least one tournament")
    if len(body.rows) > MAX_ROWS:
        raise ImportsInvalid(f"At most {MAX_ROWS} tournaments per batch")
    parsed: list[tuple[str, str, str | None]] = []
    seen: dict[str, int] = {}
    for index, row in enumerate(body.rows, start=1):
        try:
            base_url, slug = parse_tabbycat_url(row.url)
        except ImportsInvalid as exc:
            raise ImportsInvalid(f"Row {index}: {exc}") from exc
        if slug in seen:
            raise ImportsInvalid(f"Row {index}: {slug!r} is already row {seen[slug]}")
        seen[slug] = index
        parsed.append((base_url, slug, (row.api_key or "").strip() or None))

    batch = ImportBatch(
        created_by_id=user.id, include_ballots=body.include_ballots, refresh_stats=body.refresh_stats
    )
    session.add(batch)
    session.flush()
    jobs = []
    for position, (base_url, slug, _key) in enumerate(parsed):
        job = ImportJob(batch_id=batch.id, position=position, base_url=base_url, slug=slug)
        session.add(job)
        jobs.append(job)
    session.commit()
    session.refresh(batch)

    bind = session.get_bind()
    keyed = [(job.id, key) for job, (_b, _s, key) in zip(jobs, parsed)]
    _submit(bind, [job_id for job_id, _ in keyed], lambda: _check_jobs(bind, keyed, body.include_ballots))
    return batch


def recheck(session: Session, job: ImportJob, api_key: str | None) -> None:
    if job.status in ACTIVE:
        raise ImportsConflict("This tournament is still being processed")
    if job.status == "imported":
        raise ImportsConflict("This tournament has already been imported")
    batch = session.get(ImportBatch, job.batch_id)
    job.status = "queued_check"
    job.error = None
    job.report = None
    job.decisions = {}
    job.updated_at = _now()
    session.add(job)
    session.commit()
    bind = session.get_bind()
    key = (api_key or "").strip() or None
    job_id = job.id
    _submit(bind, [job_id], lambda: _check_jobs(bind, [(job_id, key)], batch.include_ballots))


def save_decisions(session: Session, job: ImportJob, decisions: ImportOverrides) -> ImportJob:
    if job.status != "ready":
        raise ImportsConflict("Decisions can only be changed while the tournament is checked and waiting")
    report = _report(job)
    asked = {(i.kind, i.upstream_url) for i in (report.issues if report else []) if i.severity == "decision"}
    models: dict[str, type[SQLModel]] = {"institution": Institution, "debater": Debater, "judge": Judge}
    for kind, model in models.items():
        for upstream_url, choice in decisions.for_kind(kind).items():
            if (kind, upstream_url) not in asked:
                raise ImportsInvalid(f"No {kind} decision is pending for {upstream_url}")
            if isinstance(choice, int) and session.get(model, choice) is None:
                raise ImportsInvalid(f"{kind.capitalize()} #{choice} does not exist")
    merged = _decisions(job)
    for kind in models:
        merged.for_kind(kind).update(decisions.for_kind(kind))
    if "tournament_date" in decisions.model_fields_set:
        merged.tournament_date = decisions.tournament_date
    job.decisions = merged.model_dump(mode="json")
    job.updated_at = _now()
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def run_batch(session: Session, batch: ImportBatch, job_ids: list[int]) -> None:
    if not job_ids:
        raise ImportsInvalid("Choose at least one tournament to import")
    jobs = session.exec(
        select(ImportJob).where(ImportJob.batch_id == batch.id, ImportJob.id.in_(job_ids)).order_by(ImportJob.position)
    ).all()
    if len(jobs) != len(set(job_ids)):
        raise ImportsInvalid("Some of those tournaments aren't in this batch")
    for job in jobs:
        report = _report(job)
        if job.status != "ready" or report is None:
            raise ImportsConflict(f"{job.slug} isn't ready to import")
        if report.blocking:
            raise ImportsConflict(f"{job.slug} has a blocking issue: {report.blocking[0].message}")
        left = _undecided(report, _decisions(job))
        if left:
            raise ImportsConflict(f"{job.slug} has {len(left)} match(es) to decide")
    for job in jobs:
        job.status = "queued_import"
        job.updated_at = _now()
        session.add(job)
    session.commit()
    bind = session.get_bind()
    ids = [job.id for job in jobs]
    batch_id = batch.id
    _submit(bind, ids, lambda: _import_jobs(bind, batch_id, ids))


def discard(session: Session, job: ImportJob) -> None:
    if job.status in ACTIVE:
        raise ImportsConflict("This tournament is still being processed")
    if job.status == "imported":
        raise ImportsConflict("This tournament has already been imported")
    job.status = "discarded"
    job.updated_at = _now()
    session.add(job)
    session.commit()
    _clear_staging(job.id)


def sweep_stale(session: Session) -> None:
    """Fail active jobs nobody is running any more (the process restarted or died)."""
    cutoff = _now() - timedelta(seconds=settings.import_stale_seconds)
    with _owned_lock:
        mine = set(_owned)
    changed = False
    for job in session.exec(select(ImportJob).where(ImportJob.status.in_(ACTIVE))).all():
        if job.id in mine or _as_utc(job.updated_at) > cutoff:
            continue
        stage = "checking" if job.status in ("queued_check", "checking") else "importing"
        job.status = "failed"
        job.error = f"The server restarted while {stage}; nothing was saved. Check again."
        job.phase = None
        session.add(job)
        changed = True
    if changed:
        session.commit()


def sweep_staging() -> None:
    """Remove staged upstream data older than `import_staging_ttl_days` (startup)."""
    root = Path(settings.import_staging_dir)
    if not root.is_dir():
        return
    cutoff = time.time() - settings.import_staging_ttl_days * 86400
    for entry in root.iterdir():
        if entry.is_dir() and entry.stat().st_mtime < cutoff:
            shutil.rmtree(entry, ignore_errors=True)


def batch_public(session: Session, batch: ImportBatch) -> ImportBatchPublic:
    session.expire_all()  # jobs change under other sessions (the runner)
    jobs = session.exec(
        select(ImportJob).where(ImportJob.batch_id == batch.id).order_by(ImportJob.position)
    ).all()
    user = session.get(User, batch.created_by_id) if batch.created_by_id else None
    public_jobs = []
    for job in jobs:
        item = job_public(job)
        live = _live_progress.get(job.id)
        if live is not None and job.status in ACTIVE:
            item.phase, item.progress_done, item.progress_total = live
        public_jobs.append(item)
    return ImportBatchPublic(
        id=batch.id,
        created_at=batch.created_at,
        created_by=user.name if user else None,
        include_ballots=batch.include_ballots,
        refresh_stats=batch.refresh_stats,
        stats_refreshed_at=batch.stats_refreshed_at,
        jobs=public_jobs,
    )


def list_batches(session: Session, limit: int = 25) -> list[ImportBatchSummary]:
    batches = session.exec(select(ImportBatch).order_by(ImportBatch.id.desc()).limit(limit)).all()
    if not batches:
        return []
    ids = [b.id for b in batches]
    jobs = session.exec(select(ImportJob).where(ImportJob.batch_id.in_(ids)).order_by(ImportJob.position)).all()
    user_ids = {b.created_by_id for b in batches if b.created_by_id}
    users = {u.id: u.name for u in session.exec(select(User).where(User.id.in_(user_ids))).all()} if user_ids else {}
    by_batch: dict[int, list[ImportJob]] = {}
    for job in jobs:
        by_batch.setdefault(job.batch_id, []).append(job)
    summaries = []
    for batch in batches:
        counts: dict[str, int] = {}
        names = []
        for job in by_batch.get(batch.id, []):
            counts[job.status] = counts.get(job.status, 0) + 1
            names.append((job.report or {}).get("tournament_name") or job.slug)
        summaries.append(
            ImportBatchSummary(
                id=batch.id,
                created_at=batch.created_at,
                created_by=users.get(batch.created_by_id),
                tournaments=names,
                counts=counts,
            )
        )
    return summaries
