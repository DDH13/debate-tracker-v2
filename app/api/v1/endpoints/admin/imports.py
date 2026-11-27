"""Tabbycat imports from the console: check a batch of tournaments, settle doubtful matches,
import. The work runs in the background (app/services/imports.py); the UI polls the batch."""

from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import APIRouter, HTTPException, Query, Response, status

from app.api.deps import BoardUserDep, SessionDep, get_or_404
from app.models import ImportBatch, ImportJob
from app.services import imports as service
from app.services.tabbycat import ImportOverrides

router = APIRouter(prefix="/imports", tags=["admin: imports"])


@contextmanager
def _errors() -> Iterator[None]:
    try:
        yield
    except service.ImportsInvalid as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    except service.ImportsConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("", response_model=service.ImportBatchPublic, status_code=status.HTTP_201_CREATED)
def create_batch(
    body: service.ImportBatchCreate, session: SessionDep, user: BoardUserDep
) -> service.ImportBatchPublic:
    """Queue a check of each tournament (a dry run that stages the upstream data). API keys
    are used for the check only and never stored."""
    with _errors():
        batch = service.create_batch(session, user, body)
    return service.batch_public(session, batch)


@router.get("", response_model=list[service.ImportBatchSummary])
def list_batches(session: SessionDep, limit: int = Query(25, ge=1, le=100)) -> list[service.ImportBatchSummary]:
    service.sweep_stale(session)
    return service.list_batches(session, limit)


# Static /jobs paths are declared before /{batch_id}.
@router.put("/jobs/{job_id}/decisions", response_model=service.ImportJobPublic)
def save_decisions(job_id: int, body: ImportOverrides, session: SessionDep) -> service.ImportJobPublic:
    """Pick an existing record (id) or "new" for each school or person the check flagged."""
    with _errors():
        job = service.save_decisions(session, get_or_404(session, ImportJob, job_id), body)
    return service.job_public(job)


@router.post("/jobs/{job_id}/check", response_model=service.ImportJobPublic)
def recheck(job_id: int, body: service.RecheckRequest, session: SessionDep) -> service.ImportJobPublic:
    """Check the tournament again (fresh upstream data; earlier decisions are cleared)."""
    job = get_or_404(session, ImportJob, job_id)
    with _errors():
        service.recheck(session, job, body.api_key)
    session.refresh(job)
    return service.job_public(job)


@router.delete("/jobs/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
def discard_job(job_id: int, session: SessionDep) -> Response:
    """Leave this tournament out and delete its staged data."""
    with _errors():
        service.discard(session, get_or_404(session, ImportJob, job_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{batch_id}", response_model=service.ImportBatchPublic)
def get_batch(batch_id: int, session: SessionDep) -> service.ImportBatchPublic:
    service.sweep_stale(session)
    return service.batch_public(session, get_or_404(session, ImportBatch, batch_id))


@router.post("/{batch_id}/run", response_model=service.ImportBatchPublic)
def run_batch(batch_id: int, body: service.RunRequest, session: SessionDep) -> service.ImportBatchPublic:
    """Import the chosen checked tournaments, in batch order. Refused while any of them has a
    blocking issue or an undecided match."""
    batch = get_or_404(session, ImportBatch, batch_id)
    with _errors():
        service.run_batch(session, batch, body.job_ids)
    return service.batch_public(session, batch)
