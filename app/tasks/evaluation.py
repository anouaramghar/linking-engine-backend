from rq import Repeat, Retry
from rq.exceptions import DuplicateJobError as RQDuplicateJobError
from rq.job import Job

from app.config import settings
from app.db import SessionLocal
from app.services.evaluation_service import capture_daily_evaluation_snapshots
from app.tasks.queues import default_queue, redis_conn

_SCHEDULE_JOB_ID = "linkmesh-evaluation-daily-snapshot"


def capture_evaluation_snapshot() -> dict[str, int]:
    with SessionLocal() as db:
        captured = capture_daily_evaluation_snapshots(db)
        db.commit()
    return {"captured": captured}


def schedule_evaluation_snapshots() -> Job:
    try:
        return default_queue.enqueue(
            capture_evaluation_snapshot,
            job_id=_SCHEDULE_JOB_ID,
            unique=True,
            repeat=Repeat(
                times=settings.evaluation_snapshot_repeat_count,
                interval=settings.evaluation_snapshot_interval_seconds,
            ),
            retry=Retry(max=2, interval=[60, 300]),
            job_timeout=300,
            result_ttl=settings.evaluation_snapshot_interval_seconds,
            description="Daily evaluation snapshot",
        )
    except RQDuplicateJobError:
        return Job.fetch(_SCHEDULE_JOB_ID, connection=redis_conn)
