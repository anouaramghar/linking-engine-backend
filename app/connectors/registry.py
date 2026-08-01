from app.connectors.base import ContentConnector
from app.connectors.html_crawler import HTMLConnector
from app.connectors.rss_connector import RSSConnector
from app.connectors.wikipedia_connector import WikipediaConnector
from app.connectors.wordpress import WordPressConnector
from app.models.site import Site
from app.services.pool_source_policy import require_approved_pool_source

CONNECTORS: dict[str, type[ContentConnector]] = {
    "wordpress": WordPressConnector,
    "html": HTMLConnector,
}


def get_connector(site: Site) -> ContentConnector:
    if site.platform == "pool":
        require_approved_pool_source(site)
        if WikipediaConnector.supports_url(site.base_url):
            return WikipediaConnector(site)
        return RSSConnector(site)
    return CONNECTORS[site.platform](site)
