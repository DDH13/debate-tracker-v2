import logging

from app.core.config import settings

_configured = False


def configure_logging() -> None:
    """Idempotent; called from the app entrypoint, never at library import time."""
    global _configured
    if _configured:
        return
    _configured = True

    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("app").setLevel(settings.log_level.upper())
    logging.getLogger("app.services.tabbycat").setLevel(
        "DEBUG" if settings.import_trace else settings.log_level.upper()
    )
