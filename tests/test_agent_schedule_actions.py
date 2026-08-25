from datetime import UTC, datetime

from app.agent_tools import call_tool
from app.models import SiteSchedule
from app.schemas.site_schedule import SiteScheduleUpdate
from app.services.authorization import Principal
from app.services.agent_action_executor import execute_proposal
from app.services.site_schedule_service import save_schedule


def _admin() -> Principal:
    return Principal(is_admin=True, source="legacy_env")


def test_agent_reads_and_previews_a_managed_site_schedule(client, db, site):
    save_schedule(
        db,
        site,
        SiteScheduleUpdate(
            enabled=True,
            cadence="daily",
            local_time="02:30",
            timezone="Africa/Casablanca",
        ),
        now=datetime(2026, 8, 24, 1, 0, tzinfo=UTC),
        updated_by="operator:old",
    )

    current = call_tool(db, _admin(), "get_site_schedule", {"site_id": site.id})
    assert current["configured"] is True
    assert current["schedule"]["enabled"] is True
    assert current["schedule"]["timezone"] == "Africa/Casablanca"
    assert current["schedule"]["updated_by"] == "operator:old"

    preview = call_tool(
        db,
        _admin(),
        "preview_site_schedule",
        {
            "site_id": site.id,
            "enabled": True,
            "cadence": "weekly",
            "weekday": 2,
            "local_time": "03:15",
            "timezone": "UTC",
        },
    )

    assert preview["ready"] is True
    assert preview["already_current"] is False
    assert preview["current"] == {
        "exists": True,
        "enabled": True,
        "cadence": "daily",
        "weekday": None,
        "local_time": "02:30:00",
        "timezone": "Africa/Casablanca",
    }
    assert preview["desired"] == {
        "exists": True,
        "enabled": True,
        "cadence": "weekly",
        "weekday": 2,
        "local_time": "03:15:00",
        "timezone": "UTC",
    }
    proposal = preview["proposal"]
    assert proposal["kind"] == "site_schedule_update"
    assert proposal["risk"] == "sensitive"
    assert proposal["endpoint"] == f"/api/v1/sites/{site.id}/schedule"
    assert proposal["payload"]["expected"] == preview["current"]
    assert proposal["context"]["site_name"] == site.name
    assert proposal["context"]["next_run_at"] is not None

    response = client.put(f"/api/v1/sites/{site.id}/schedule", json=proposal["payload"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["cadence"] == "weekly"
    assert body["weekday"] == 2
    assert body["local_time"].startswith("03:15")
    assert body["timezone"] == "UTC"
    assert body["updated_by"]

    db.expire_all()
    refreshed = db.query(SiteSchedule).filter_by(site_id=site.id).one()
    assert refreshed.cadence == "weekly"
    assert refreshed.weekday == 2


def test_agent_schedule_preview_can_create_the_first_schedule(client, db, site):
    preview = call_tool(
        db,
        _admin(),
        "preview_site_schedule",
        {
            "site_id": site.id,
            "enabled": True,
            "cadence": "daily",
            "local_time": "04:00",
            "timezone": "UTC",
        },
    )

    assert preview["current"] == {"exists": False}
    assert preview["proposal"]["payload"]["expected"] == {"exists": False}

    response = client.put(
        f"/api/v1/sites/{site.id}/schedule",
        json=preview["proposal"]["payload"],
    )
    assert response.status_code == 200, response.text
    assert response.json()["enabled"] is True
    assert response.json()["next_run_at"] is not None


def test_agent_schedule_confirmation_refuses_a_stale_configuration(client, db, site):
    save_schedule(
        db,
        site,
        SiteScheduleUpdate(
            enabled=True,
            cadence="daily",
            local_time="02:00",
            timezone="UTC",
        ),
    )
    preview = call_tool(
        db,
        _admin(),
        "preview_site_schedule",
        {
            "site_id": site.id,
            "enabled": True,
            "cadence": "weekly",
            "weekday": 0,
            "local_time": "05:00",
            "timezone": "UTC",
        },
    )

    schedule = db.query(SiteSchedule).filter_by(site_id=site.id).one()
    schedule.local_time = datetime.strptime("03:00", "%H:%M").time()
    db.commit()

    response = client.put(
        f"/api/v1/sites/{site.id}/schedule",
        json=preview["proposal"]["payload"],
    )
    assert response.status_code == 409
    assert "changed after this action was previewed" in response.json()["detail"]


def test_mcp_schedule_receipt_dispatch_uses_the_same_guarded_route(db, site):
    preview = call_tool(
        db,
        _admin(),
        "preview_site_schedule",
        {
            "site_id": site.id,
            "enabled": True,
            "cadence": "daily",
            "local_time": "04:00",
            "timezone": "UTC",
        },
    )

    outcome = execute_proposal(
        db,
        _admin(),
        {"tool": "preview_site_schedule", **preview["proposal"]},
        actor="telegram:42",
    )

    assert outcome["message"] == f"Updated refresh schedule for site #{site.id}."
    db.expire_all()
    schedule = db.query(SiteSchedule).filter_by(site_id=site.id).one()
    assert schedule.enabled is True
    assert schedule.updated_by == "telegram:42"
