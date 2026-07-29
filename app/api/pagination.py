"""Shared bounds for list endpoints.

Every list route took an unbounded ``limit``, so a single request could ask the
database for the whole table. The dashboard pages through at 1000, so that is
the ceiling; anything larger is a client bug rather than a real need.
"""

from app.limits import MAX_ENGINE_PAGE_SIZE

MAX_PAGE_SIZE = MAX_ENGINE_PAGE_SIZE
