from pydantic import BaseModel


class PendingPublicationSite(BaseModel):
    site_id: int
    awaiting_publication: int
