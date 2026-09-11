"""One logging setup for all five services.

Handlers go to stdout only. The previous per-service setups wrote to a file
inside the source tree, which in a container means the logs die with it and
never reach `docker logs`.
"""

import logging

from rich.logging import RichHandler


def setup_logging(service: str, level: int = logging.INFO) -> logging.Logger:
    """Attach a stdout handler to the root logger and return `service`'s logger.

    Idempotent, so importing it from several modules in one process is safe.
    """
    root = logging.getLogger()
    if not any(isinstance(handler, RichHandler) for handler in root.handlers):
        handler = RichHandler(rich_tracebacks=True, show_path=False)
        handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
        root.addHandler(handler)
    root.setLevel(level)
    return logging.getLogger(service)
