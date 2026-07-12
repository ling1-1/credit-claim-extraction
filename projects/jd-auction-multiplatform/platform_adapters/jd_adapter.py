
from pathlib import Path
from typing import Any, Callable, Mapping

from jd_scraper_v2 import JDCategory, JDClient, JDAuctionScraper


JD_SOURCE_PLATFORM = "jd"
JD_DATA_SOURCE = "京东拍卖"


class JDPlatformAdapter:
    """Facade that places the existing JD scraper under platform_adapters.

    The JD pipeline still uses its mature API-based scraper internally. This
    adapter keeps the platform layout consistent without rewriting JD crawling
    into the multi-platform runner in one risky step.
    """

    source_platform = JD_SOURCE_PLATFORM
    source_site_name = JD_DATA_SOURCE

    def __init__(
        self,
        *,
        throttle_seconds: float | None = None,
        timeout: int | None = None,
        client: JDClient | None = None,
    ) -> None:
        if client is not None:
            self.client = client
        elif throttle_seconds is None and timeout is None:
            self.client = JDClient()
        elif timeout is None:
            self.client = JDClient(throttle_seconds=throttle_seconds or 0)
        elif throttle_seconds is None:
            self.client = JDClient(timeout=timeout)
        else:
            self.client = JDClient(throttle_seconds=throttle_seconds, timeout=timeout)

    def get_categories(self) -> list[JDCategory]:
        return self.client.get_categories()

    def create_scraper(self, db: Any) -> JDAuctionScraper:
        return JDAuctionScraper(db=db, client=self.client)

    def crawl_sample(
        self,
        *,
        db: Any,
        per_category_limit: int,
        output_dir: str | Path,
        categories: set[str] | None = None,
        total_limit: int | None = None,
        mode: str = "sample",
        ai_mode: str = "async",
        checkpoint_callback: Callable[..., None] | None = None,
        resume_checkpoint: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        scraper = self.create_scraper(db)
        return scraper.crawl_sample(
            per_category_limit=per_category_limit,
            output_dir=Path(output_dir),
            categories=categories,
            total_limit=total_limit,
            mode=mode,
            ai_mode=ai_mode,
            checkpoint_callback=checkpoint_callback,
            resume_checkpoint=resume_checkpoint,
        )


__all__ = [
    "JD_DATA_SOURCE",
    "JD_SOURCE_PLATFORM",
    "JDPlatformAdapter",
    "JDCategory",
    "JDClient",
    "JDAuctionScraper",
]
