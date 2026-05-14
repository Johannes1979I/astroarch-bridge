"""Entry point: `python -m astroarch_bridge` or `astroarch-bridge` script."""
from __future__ import annotations

import logging
import sys

import uvicorn

from astroarch_bridge.config import get_settings


def main() -> int:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("astroarch_bridge")
    log.info("starting astroarch-bridge on %s:%d", settings.host, settings.port)
    log.info("INDI=%s:%d  PHD2=%s:%d  watch=%s",
             settings.indi_host, settings.indi_port,
             settings.phd2_host, settings.phd2_port,
             settings.images_dir)
    uvicorn.run(
        "astroarch_bridge.app:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        access_log=True,  # log ogni HTTP request, utile per debug
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
