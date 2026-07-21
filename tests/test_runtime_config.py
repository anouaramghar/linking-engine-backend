from pathlib import Path

from rq import Worker

import app.worker as worker_module
from app.services.job_service import handle_abandoned_job, handle_work_horse_killed
from app.worker import LinkMeshWorker


def test_compose_worker_runs_scheduler_for_delayed_retries():
    compose = (Path(__file__).parents[1] / "docker-compose.yml").read_text()
    worker_command = next(
        line for line in compose.splitlines() if line.strip().startswith("command: [\"rq\"")
    )

    assert '"worker", "--worker-class", "app.worker.LinkMeshWorker"' in worker_command
    assert '"--with-scheduler"' in worker_command


def test_linkmesh_worker_wires_durable_handlers_and_preserves_existing(monkeypatch):
    captured = {}

    def existing_handler(*_args):
        return False

    def capture_init(_self, *args, **kwargs):
        captured.update(args=args, kwargs=kwargs)

    monkeypatch.setattr(Worker, "__init__", capture_init)

    LinkMeshWorker(
        ["ingestion", "analysis"],
        exception_handlers=[existing_handler],
    )

    assert captured["kwargs"]["work_horse_killed_handler"] is handle_work_horse_killed
    assert captured["kwargs"]["exception_handlers"] == [
        handle_abandoned_job,
        existing_handler,
    ]


def test_linkmesh_worker_disposes_database_pool_immediately_before_fork(monkeypatch):
    events = []
    worker = object.__new__(LinkMeshWorker)
    job = object()
    queue = object()

    monkeypatch.setattr(
        worker_module.engine,
        "dispose",
        lambda: events.append(("dispose", None, None)),
    )

    def capture_fork(_self, received_job, received_queue):
        events.append(("fork", received_job, received_queue))
        return "fork-result"

    monkeypatch.setattr(Worker, "fork_work_horse", capture_fork)

    result = worker.fork_work_horse(job, queue)

    assert result == "fork-result"
    assert events == [
        ("dispose", None, None),
        ("fork", job, queue),
    ]
