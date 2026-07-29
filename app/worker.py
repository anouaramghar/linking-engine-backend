"""Application RQ worker configuration."""

from rq import Worker

from app.db import engine
from app.services.job_service import handle_abandoned_job, handle_work_horse_killed


class LinkMeshWorker(Worker):
    """RQ worker that reconciles durable rows after task or worker termination."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("work_horse_killed_handler", handle_work_horse_killed)
        existing_handlers = kwargs.get("exception_handlers")
        if isinstance(existing_handlers, (list, tuple)):
            exception_handlers = list(existing_handlers)
        elif existing_handlers is None:
            exception_handlers = []
        else:
            exception_handlers = [existing_handlers]
        if handle_abandoned_job not in exception_handlers:
            exception_handlers.insert(0, handle_abandoned_job)
        kwargs["exception_handlers"] = exception_handlers
        super().__init__(*args, **kwargs)

    def fork_work_horse(self, job, queue):
        # The parent kill callback uses SQLAlchemy. Close every pooled parent
        # connection immediately before os.fork() so the child cannot inherit a
        # live PostgreSQL socket or pool bookkeeping from an earlier callback.
        engine.dispose()
        return super().fork_work_horse(job, queue)
