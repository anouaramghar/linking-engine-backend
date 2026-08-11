from redis import Redis
from rq import Queue

from app.config import settings

redis_conn = Redis.from_url(settings.redis_url)

# Separate queues per pipeline stage (Phase 0, finding 6) — lets a heavy analysis
# never starve ingestion or publication once dedicated workers exist.
ingestion_queue = Queue("ingestion", connection=redis_conn)
analysis_queue = Queue("analysis", connection=redis_conn)
publication_queue = Queue("publication", connection=redis_conn)
publication_preparation_queue = Queue("publication-preparation", connection=redis_conn)
