"""Compatibility entry for the formal MySQL V2 JD scraper.

The old SQLite implementation in this filename has been retired. Keep this
module as a thin import/CLI shim so historical commands and imports resolve to
the maintained V2 pipeline without carrying duplicate extraction or storage
code.
"""


from jd_scraper_v2 import *  # noqa: F401,F403
from jd_scraper_v2 import main as _main


def main() -> None:
    _main()


if __name__ == "__main__":
    main()
