"""Dev-mode trace log: a file of everything that crosses the HomeKit boundary.

Enabled for the whole integration with ``kitbash: dev_mode: true`` in
``configuration.yaml`` (default off). One file per Core start, named by the
start time, under ``<config>/kitbash/logs/``, with no size cap. Lines are
prefixed with the accessory name. Three
kinds of line, plus the wire lines below:

* ``HK write``   -- what a controller sent, raw and as HAP-python stored it,
                   followed by the Home Assistant service calls it produced.
* ``push``       -- each characteristic value Home Assistant pushed to HomeKit,
                   with the previous value and the source entity.
* ``entity``     -- each source entity state change, or ``unchanged`` when the
                   event carried no difference, so a part that never moves is
                   visible as such.
* ``HK fetched`` / ``HK read`` / ``HK put`` -- the wire itself, from the driver:
                   the controller fetching the accessory database, reading
                   characteristics, and each PUT as sent (writes and event
                   subscriptions), by characteristic name.

The file handler sits behind a ``QueueHandler``/``QueueListener`` pair, the way
Home Assistant's own log does, so a trace call from the event loop only
enqueues; the disk write happens on the listener's thread. The trace logger does
not propagate into Home Assistant's log, so ``logger:`` levels do not apply.
"""

from __future__ import annotations

import logging
from logging.handlers import QueueHandler, QueueListener
import os
import queue
import threading
import time

from homeassistant.core import HomeAssistant

from .const import DOMAIN

TRACE_LOGGER_NAME = f"custom_components.{DOMAIN}.trace"
LOG_DIR_PARTS = ("kitbash", "logs")
LOG_FILENAME_FORMAT = "kitbash-%Y%m%d-%H%M%S.log"

_LOGGER = logging.getLogger(__name__)

# One file per process: chosen the first time dev mode asks for it. The lock is
# load-bearing -- every config entry publishes in parallel and each one asks for
# the handler from its own executor thread, so without it several threads all
# find _session empty and attach a handler each. The symptom is every trace line
# repeated once per racing entry (four times over, observed 2026-09-14).
_session: dict[str, object] = {}
_session_lock = threading.Lock()


def log_dir(hass: HomeAssistant) -> str:
    """Where trace files go: /config/kitbash/logs."""
    return hass.config.path(*LOG_DIR_PARTS)


def install_trace_handler(directory: str) -> logging.Logger:
    """Attach the queued file handler once per process and return the trace logger.

    Runs in the executor (creates the directory and opens the file). Later calls
    return the same logger; the file name carries the time Core first asked.
    """
    logger = logging.getLogger(TRACE_LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    with _session_lock:
        if "listener" in _session:
            return logger
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(
            directory, time.strftime(LOG_FILENAME_FORMAT, time.localtime())
        )
        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        log_queue: queue.SimpleQueue = queue.SimpleQueue()
        listener = QueueListener(log_queue, file_handler, respect_handler_level=False)
        listener.start()
        logger.addHandler(QueueHandler(log_queue))
        _session["listener"] = listener
        _session["path"] = path
    _LOGGER.info("Kitbash dev-mode trace log at %s", path)
    return logger


class Tracer:
    """Per-accessory writer onto the shared trace logger; a no-op when disabled."""

    def __init__(self, name: str, logger: logging.Logger | None) -> None:
        self._prefix = f"[{name}]"
        self._logger = logger

    @property
    def enabled(self) -> bool:
        return self._logger is not None

    def __call__(self, message: str, *args: object) -> None:
        if self._logger is not None:
            self._logger.debug("%s " + message, self._prefix, *args)
