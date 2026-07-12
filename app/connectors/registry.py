from app.connectors.base import ContentConnector
from app.connectors.html_crawler import HTMLConnector
from app.connectors.wordpress import WordPressConnector
from app.models.site import Site

CONNECTORS: dict[str, type[ContentConnector]] = {
    "wordpress": WordPressConnector,
    "html": HTMLConnector,
}


def get_connector(site: Site) -> ContentConnector:
    return CONNECTORS[site.platform](site)
