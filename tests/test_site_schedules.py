from datetime import UTC, datetime, time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.models import JobRun, SiteSchedule
from app.schemas.site_schedule import SiteScheduleUpdate
from app.services.site_schedule_service import (
    ScheduledPipelineBusyError,
    next_schedule_run_at,
    save_schedule,
    start_site_pipeline,
)
from app.tasks import site_scheduler
from app.api.routes import site_schedules


def test_next_daily_occurrence_uses_the_site_timezone():
    result = next_schedule_run_at(
        cadence="daily",
        local_time=time(2, 30),
        timezone="Africa/Casablanca",
        weekday=None,
        after=datetime(2026, 8, 24, 1, 0, tzinfo=UTC),
    )

    assert result.tzinfo is not None
    assert result > datetime(2026, 8, 24, 1, 0, tzinfo=UTC)
    local = result.astimezone(ZoneInfo("Africa/Casablanca"))
    assert local.hour == 2
    assert local.minute == 30


def test_weekly_schedule_requires_weekday():
    with pytest.raises(ValueError, match="weekday is required"):
        SiteScheduleUpdate(
            enabled=True,
            cadence="weekly",
            local_time="03:00",
            timezone="UTC",
        )


def test_schedule_round_trip_sets_a_future_cursor(client, db, site):
    response = client.put(
        f"/api/v1/sites/{site.id}/schedule",
        json={
            "enabled": True,
            "cadence": "weekly",
            "weekday": 2,
            "local_time": "03:15",
            "timezone": "UTC",
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["site_id"] == site.id
    assert body["enabled"] is True
    assert body["cadence"] == "weekly"
    assert body["weekday"] == 2
    assert body["local_time"].startswith("03:15")
    assert body["next_run_at"] is not None

    fetched = client.get(f"/api/v1/sites/{site.id}/schedule")
    assert fetched.status_code == 200
    assert fetched.json()["timezone"] == "UTC"


def test_disabling_schedule_removes_the_next_cursor(client, site):
    enabled = client.put(
        f"/api/v1/sites/{site.id}/schedule",
        json={
            "enabled": True,
            "cadence": "daily",
            "local_time": "03:15",
            "timezone": "UTC",
        },
    )
    assert enabled.status_code == 200

    disabled = client.put(
        f"/api/v1/sites/{site.id}/schedule",
        json={
            "enabled": False,
            "cadence": "daily",
            "local_time": "03:15",
            "timezone": "UTC",
        },
    )
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False
    assert disabled.json()["next_run_at"] is None


def test_run_now_queues_the_normal_pipeline(client, site, monkeypatch):
    calls: list[tuple[int, int | None, str | None]] = []

    def fake_start(_db, site_id, *, schedule_id=None, requested_by=None):
        calls.append((site_id, schedule_id, requested_by))
        return SimpleNamespace(id=81), SimpleNamespace(ingestion_job_run_id=82)

    monkeypatch.setattr(site_schedules, "start_site_pipeline", fake_start)

    response = client.post(f"/api/v1/sites/{site.id}/schedule/run-now")

    assert response.status_code == 202, response.text
    assert response.json() == {"batch_id": 81, "ingestion_job_run_id": 82}
    assert calls == [(site.id, None, "local-development")]


def test_due_coordinator_advances_the_cursor_and_queues_once(db, site, monkeypatch):
    schedule = save_schedule(
        db,
        site,
        SiteScheduleUpdate(
            enabled=True,
            cadence="daily",
            local_time="02:00",
            timezone="UTC",
        ),
        now=datetime(2026, 8, 24, 1, 0, tzinfo=UTC),
    )
    schedule.next_run_at = datetime(2026, 8, 24, 0, 0, tzinfo=UTC)
    db.commit()
    calls: list[tuple[int, int | None, str | None]] = []

    def fake_start(_db, site_id, *, schedule_id=None, requested_by=None):
        calls.append((site_id, schedule_id, requested_by))
        return SimpleNamespace(id=71), SimpleNamespace(ingestion_job_run_id=72)

    monkeypatch.setattr(site_scheduler, "start_site_pipeline", fake_start)
    monkeypatch.setattr(
        site_scheduler,
        "_due_schedule_ids",
        lambda: [schedule.id],
    )

    result = site_scheduler.enqueue_due_site_schedules()

    assert result["queued"] == 1
    assert calls == [(site.id, schedule.id, "site-scheduler")]
    db.expire_all()
    refreshed = db.scalar(select(SiteSchedule).where(SiteSchedule.id == schedule.id))
    assert refreshed is not None
    assert refreshed.next_run_at > datetime(2026, 8, 24, 0, 0, tzinfo=UTC)
    assert refreshed.last_attempt_status == "queued"


def test_scheduled_pipeline_refuses_overlap(db, site):
    db.add(JobRun(site_id=site.id, kind="analysis", status="queued"))
    db.commit()

    with pytest.raises(ScheduledPipelineBusyError, match="already has crawl or analysis"):
        start_site_pipeline(db, site.id)
