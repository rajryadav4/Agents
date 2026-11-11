from .base import FetchError, Http, Source
from .bestbuy import BestBuySource
from .keepa import KeepaSource
from .pagewatch import PageWatchSource
from .reddit import RedditSource
from .retail import RETAILERS, RetailSource
from .rss import PRESETS, RssSource
from .shopify import ShopifySource

SOURCE_TYPES: dict[str, type[Source]] = {
    cls.type: cls
    for cls in (RssSource, RedditSource, ShopifySource, BestBuySource, KeepaSource, PageWatchSource,
                RetailSource)
}

__all__ = ["SOURCE_TYPES", "PRESETS", "RETAILERS", "FetchError", "Http", "Source"]
