"""Register the daily evaluation snapshot job."""

from app.tasks.evaluation import schedule_evaluation_snapshots

if __name__ == "__main__":
    print(f"Scheduled {schedule_evaluation_snapshots().id}")
