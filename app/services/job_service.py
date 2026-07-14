from rq.exceptions import NoSuchJobError
from rq.job import Job

from app.tasks.queues import redis_conn


def get_job_status(job_id: str) -> dict | None:
    try:
        job = Job.fetch(job_id, connection=redis_conn)
    except NoSuchJobError:
        return None
    status = job.get_status()
    return {
        "job_id": job_id,
        "status": status,
        "result": job.return_value() if status == "finished" else None,
        "error": job.exc_info.strip().splitlines()[-1] if job.exc_info else None,
    }
