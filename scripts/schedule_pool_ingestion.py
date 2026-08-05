"""Register the single daily content-pool coordinator."""

from app.tasks.pool_ingestion import schedule_pool_ingestion


if __name__ == "__main__":
    print(f"Scheduled {schedule_pool_ingestion().id}")
