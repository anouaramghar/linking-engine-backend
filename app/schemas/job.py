from pydantic import BaseModel


class JobAccepted(BaseModel):
    job_id: str


class JobStatus(BaseModel):
    job_id: str
    status: str  # queued | started | finished | failed | ...
    result: dict | None = None
    error: str | None = None
