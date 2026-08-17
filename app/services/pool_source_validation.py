"""Read-only live checks for a proposed content-pool source."""

from typing import Literal

import httpx

from app.connectors.rss_connector import RSSConnector
from app.connectors.wikipedia_connector import WikipediaConnector
from app.models.site import Site
from app.services.pool_source_policy import PoolSourceFetchError

PoolSourceType = Literal["wikipedia", "rss_atom"]


def classify_pool_source(url: str) -> PoolSourceType:
    return "wikipedia" if WikipediaConnector.supports_url(url) else "rss_atom"


def probe_pool_source(site: Site) -> PoolSourceType:
    """Fetch just enough remote data to prove the configured connector can read it."""

    source_type = classify_pool_source(site.base_url)
    connector = WikipediaConnector(site) if source_type == "wikipedia" else RSSConnector(site)
    try:
        if source_type == "wikipedia":
            if connector.fetch_article_by_url(site.base_url) is None:
                raise PoolSourceFetchError(
                    "Wikipedia article was not found or has no readable content"
                )
        else:
            # This performs one bounded GET and verifies both the XML payload and
            # the RSS/Atom version. It does not persist any article.
            connector.get_site_metadata()
    except PoolSourceFetchError:
        raise
    except httpx.TimeoutException as error:
        raise PoolSourceFetchError("source validation timed out") from error
    except httpx.HTTPStatusError as error:
        raise PoolSourceFetchError(f"source returned HTTP {error.response.status_code}") from error
    except httpx.HTTPError as error:
        raise PoolSourceFetchError(f"source request failed: {error}") from error
    except (TypeError, ValueError) as error:
        raise PoolSourceFetchError(str(error) or "source response is invalid") from error
    finally:
        connector.client.close()
    return source_type
