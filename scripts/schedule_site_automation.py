"""Register the repeating managed-site schedule coordinator."""

from app.tasks.site_scheduler import schedule_site_automation


if __name__ == "__main__":
    print(f"Scheduled {schedule_site_automation().id}")
