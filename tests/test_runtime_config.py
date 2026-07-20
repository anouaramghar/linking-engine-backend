from pathlib import Path


def test_compose_worker_runs_scheduler_for_delayed_retries():
    compose = (Path(__file__).parents[1] / "docker-compose.yml").read_text()
    worker_command = next(
        line for line in compose.splitlines() if line.strip().startswith("command: [\"rq\"")
    )

    assert '"worker", "--with-scheduler"' in worker_command
