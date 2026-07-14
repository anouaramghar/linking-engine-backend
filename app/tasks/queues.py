from redis import Redis
from rq import Queue

from app.config import settings

redis_conn = Redis.from_url(settings.redis_url)
default_queue = Queue("default", connection=redis_conn)
