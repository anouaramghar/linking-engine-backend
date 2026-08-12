import logging
from datetime import UTC, datetime
from time import monotonic, sleep

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from rq.command import send_stop_job_command
from rq.exceptions import NoSuchJobError
from rq.job import Job
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_audit_actor, get_db, require_api_key
from app.db import SessionLocal
from app.models import JobRun, PipelineBatch, PipelineSiteRun, Site
from app.schemas.pipeline import PipelineBatchCreate, PipelineBatchOut, PipelineSiteRunOut
from app.services.authorization import Principal, authorize_site
from app.services.job_service import DuplicateJobError, enqueue_job
from app.services.pipeline_service import (
    pipeline_batch_counts,
    refresh_pipeline_batch_status,
    update_pipeline_site,
)
from app.tasks.pipeline import analyze_pipeline_site, ingest_pipeline_site
from app.tasks.queues import redis_conn


router = APIRouter(prefix="/pipelines", tags=["pipelines"])
logger = logging.getLogger(__name__)
TERMINAL_BATCH_STATUSES = {"succeeded", "failed", "partial_failed", "cancelled"}


def _batch_out(db: Session, batch: PipelineBatch) -> PipelineBatchOut:
    items = list(
        db.scalars(
            select(PipelineSiteRun)
            .where(PipelineSiteRun.batch_id == batch.id)
            .order_by(PipelineSiteRun.id)
        )
    )
    return PipelineBatchOut(
        id=batch.id,
        status=batch.status,
        **pipeline_batch_counts(db, batch.id),
        created_at=batch.created_at,
        started_at=batch.started_at,
        finished_at=batch.finished_at,
        sites=[PipelineSiteRunOut.model_validate(item) for item in items],
    )


def _authorized_batch(db: Session, principal: Principal, batch_id: int) -> PipelineBatch:
    """Load a batch the caller is allowed to act on, or raise 404/403.

    A batch carries no tenant of its own: the sites it runs are its only
    ownership evidence, so every one of them must be in scope. Reading, streaming
    and cancelling all go through here, because a caller who may not see a batch
    must not be able to stop it either.
    """
    batch = db.get(PipelineBatch, batch_id)
    if batch is None:
        raise HTTPException(404, f"pipeline batch {batch_id} not found")
    site_ids = list(
        db.scalars(select(PipelineSiteRun.site_id).where(PipelineSiteRun.batch_id == batch.id))
    )
    if not site_ids and not principal.is_admin:
        # Deleting a site cascades its runs away, which can empty a batch that
        # still exists. Hiding it from tenants is right — there is nothing left
        # to prove ownership with — but an admin should still see the record.
        raise HTTPException(404, f"pipeline batch {batch_id} not found")
    for site_id in site_ids:
        authorize_site(db, principal, site_id)
    return batch


def _enqueue_pipeline_stage(
    db: Session,
    item: PipelineSiteRun,
    stage: str,
) -> None:
    if stage == "ingestion":
        kind, task, timeout = "ingestion", ingest_pipeline_site, 3600
    else:
        kind, task, timeout = "analysis", analyze_pipeline_site, 7200
    run = enqueue_job(
        db,
        item.site_id,
        kind,
        task,
        job_timeout=timeout,
        task_kwargs={"batch_site_run_id": item.id},
    )
    db.refresh(item)
    if stage == "ingestion":
        item.ingestion_job_run_id = run.id
    else:
        item.analysis_job_run_id = run.id
        if item.status == "queued":
            item.status = "analysis_queued"
    db.commit()


@router.post("/batches", status_code=202, response_model=PipelineBatchOut)
def create_pipeline_batch(
    payload: PipelineBatchCreate,
    principal: Principal = Depends(require_api_key),
    db: Session = Depends(get_db),
) -> PipelineBatchOut:
    sites = list(db.scalars(select(Site).where(Site.id.in_(payload.site_ids))))
    found = {site.id for site in sites}
    missing = sorted(set(payload.site_ids) - found)
    if missing:
        raise HTTPException(404, f"site(s) not found: {', '.join(map(str, missing))}")
    for site in sites:
        authorize_site(db, principal, site.id)
    pool_sites = sorted(site.id for site in sites if site.platform == "pool")
    if pool_sites:
        raise HTTPException(
            409,
            "content-pool sources cannot generate suggestions: " + ", ".join(map(str, pool_sites)),
        )

    batch = PipelineBatch()
    db.add(batch)
    db.flush()
    items = [PipelineSiteRun(batch_id=batch.id, site_id=site_id) for site_id in payload.site_ids]
    db.add_all(items)
    db.commit()
    for item in items:
        try:
            _enqueue_pipeline_stage(db, item, "ingestion")
        except DuplicateJobError as error:
            update_pipeline_site(
                db,
                item.id,
                status="failed",
                stage="ingestion",
                error=str(error),
            )
            db.commit()
        except Exception as error:
            update_pipeline_site(
                db,
                item.id,
                status="failed",
                stage="ingestion",
                error=f"could not enqueue ingestion: {error}",
            )
            db.commit()
    refresh_pipeline_batch_status(db, batch.id)
    db.commit()
    db.refresh(batch)
    return _batch_out(db, batch)


@router.get("/batches/{batch_id}", response_model=PipelineBatchOut)
def get_pipeline_batch(
    batch_id: int,
    principal: Principal = Depends(require_api_key),
    db: Session = Depends(get_db),
) -> PipelineBatchOut:
    return _batch_out(db, _authorized_batch(db, principal, batch_id))


@router.get("/batches/{batch_id}/events")
def stream_pipeline_batch(
    batch_id: int,
    principal: Principal = Depends(require_api_key),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    # Authorized once, at connect time, through the same dependency as every
    # other route here. The snapshots below carry site ids, job state and error
    # text, so a caller who may not read this batch must not reach them.
    _authorized_batch(db, principal, batch_id)
    # Then hand the connection back before the stream starts. A dependency's
    # session is only released once the response completes, which for a stream
    # is however long the batch runs; each snapshot opens its own short
    # read-only session instead.
    db.close()

    def events():
        previous = None
        last_sent = monotonic()
        while True:
            with SessionLocal() as stream_db:
                batch = stream_db.get(PipelineBatch, batch_id)
                if batch is None:
                    yield 'event: error\ndata: {"detail":"batch deleted"}\n\n'
                    return
                payload = _batch_out(stream_db, batch).model_dump_json()
                terminal = batch.status in TERMINAL_BATCH_STATUSES
            now = monotonic()
            if payload != previous:
                yield f"event: batch\ndata: {payload}\n\n"
                previous = payload
                last_sent = now
            elif now - last_sent >= 15:
                yield ": keep-alive\n\n"
                last_sent = now
            if terminal:
                yield "event: done\ndata: {}\n\n"
                return
            sleep(1)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _stop_queue_job(db: Session, job_run_id: int | None, reason: str) -> None:
    if job_run_id is None:
        return
    run = db.get(JobRun, job_run_id)
    if run is None or not run.queue_job_id:
        return
    try:
        job = Job.fetch(run.queue_job_id, connection=redis_conn)
        status = job.get_status(refresh=True)
        if status in {"queued", "deferred", "scheduled"}:
            job.cancel()
        elif status == "started":
            send_stop_job_command(redis_conn, job.id)
    except NoSuchJobError:
        pass
    except Exception:
        logger.exception("could not stop RQ job %s during pipeline cancellation", run.queue_job_id)
    if run.status in {"queued", "running"}:
        run.status = "failed"
        run.error = reason
        run.finished_at = datetime.now(UTC)


@router.post("/batches/{batch_id}/cancel", response_model=PipelineBatchOut)
def cancel_pipeline_batch(
    batch_id: int,
    principal: Principal = Depends(require_api_key),
    db: Session = Depends(get_db),
    actor: str = Depends(get_audit_actor),
) -> PipelineBatchOut:
    _authorized_batch(db, principal, batch_id)
    batch = db.scalar(
        select(PipelineBatch).where(PipelineBatch.id == batch_id).with_for_update()
    )
    if batch is None:
        raise HTTPException(404, f"pipeline batch {batch_id} not found")
    if batch.status in {"succeeded", "failed", "partial_failed"}:
        raise HTTPException(409, f"pipeline batch {batch_id} is already {batch.status}")
    if batch.status == "cancelled":
        return _batch_out(db, batch)

    reason = f"Cancelled by {actor}"
    items = list(
        db.scalars(
            select(PipelineSiteRun)
            .where(PipelineSiteRun.batch_id == batch_id)
            .with_for_update()
        )
    )
    now = datetime.now(UTC)
    for item in items:
        if item.status in {"succeeded", "failed"}:
            continue
        _stop_queue_job(db, item.ingestion_job_run_id, reason)
        _stop_queue_job(db, item.analysis_job_run_id, reason)
        item.status = "cancelled"
        item.error = reason
        item.finished_at = now
    batch.status = "cancelled"
    batch.started_at = batch.started_at or now
    batch.finished_at = now
    db.commit()
    db.refresh(batch)
    return _batch_out(db, batch)


@router.post(
    "/batches/{batch_id}/sites/{site_id}/retry",
    status_code=202,
    response_model=PipelineBatchOut,
)
def retry_pipeline_site(
    batch_id: int,
    site_id: int,
    principal: Principal = Depends(require_api_key),
    db: Session = Depends(get_db),
) -> PipelineBatchOut:
    authorize_site(db, principal, site_id)
    batch = db.get(PipelineBatch, batch_id)
    if batch is None:
        raise HTTPException(404, f"pipeline batch {batch_id} not found")
    item = db.scalar(
        select(PipelineSiteRun).where(
            PipelineSiteRun.batch_id == batch_id,
            PipelineSiteRun.site_id == site_id,
        )
    )
    if item is None:
        raise HTTPException(404, f"site {site_id} is not in pipeline batch {batch_id}")
    if item.status != "failed":
        raise HTTPException(409, f"site {site_id} pipeline is {item.status}, not failed")
    retry_stage = item.stage
    item.status = "queued"
    item.error = None
    item.finished_at = None
    item.retry_count += 1
    refresh_pipeline_batch_status(db, batch_id)
    db.commit()
    try:
        _enqueue_pipeline_stage(db, item, retry_stage)
    except DuplicateJobError as error:
        update_pipeline_site(
            db,
            item.id,
            status="failed",
            stage=retry_stage,
            error=str(error),
        )
        db.commit()
        raise HTTPException(409, str(error)) from error
    except Exception as error:
        update_pipeline_site(
            db,
            item.id,
            status="failed",
            stage=retry_stage,
            error=f"could not enqueue retry: {error}",
        )
        db.commit()
        raise HTTPException(503, "could not enqueue pipeline retry") from error
    db.refresh(batch)
    return _batch_out(db, batch)
