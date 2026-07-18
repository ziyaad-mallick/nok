"""Session JSONL logger.

Uses stdlib logging with a QueueHandler feeding a QueueListener that drains
to a single file writer, so log_event() is safe to call concurrently from
any thread (audio callback excluded — keep that allocation-free).
"""
import json
import logging
import logging.handlers
import os
import queue
import threading
from datetime import datetime, timezone

_logger = logging.getLogger("miko.session")
_logger.setLevel(logging.INFO)
_logger.propagate = False

_listener = None
_queue = None
_log_path = None
_init_lock = threading.Lock()


class _JsonlFileHandler(logging.Handler):
    """Writes one JSON object per log record to a single open file."""

    def __init__(self, path):
        super().__init__()
        self._fh = open(path, "a", encoding="utf-8")

    def emit(self, record):
        try:
            payload = getattr(record, "payload", None)
            if payload is None:
                payload = {"kind": "log", "message": record.getMessage()}
            self._fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self._fh.flush()
        except Exception:
            self.handleError(record)

    def close(self):
        try:
            self._fh.close()
        finally:
            super().close()


def init_session_logging(log_dir: str = "session-logs") -> str:
    """Idempotent: sets up the queue + listener + file on first call."""
    global _listener, _queue, _log_path
    with _init_lock:
        if _listener is not None:
            return _log_path
        os.makedirs(log_dir, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        _log_path = os.path.join(log_dir, f"session-{ts}.jsonl")

        _queue = queue.Queue(-1)
        queue_handler = logging.handlers.QueueHandler(_queue)
        _logger.addHandler(queue_handler)

        file_handler = _JsonlFileHandler(_log_path)
        _listener = logging.handlers.QueueListener(
            _queue, file_handler, respect_handler_level=False
        )
        _listener.start()
        return _log_path


def log_event(kind: str, **fields) -> None:
    """Log one structured event. Safe to call from any thread."""
    if _listener is None:
        init_session_logging()
    payload = {"ts": datetime.now(timezone.utc).isoformat(), "kind": kind}
    payload.update(fields)
    _logger.info(kind, extra={"payload": payload})


def shutdown() -> None:
    """Flush and stop the background listener thread."""
    global _listener, _queue, _log_path
    with _init_lock:
        if _listener is not None:
            _listener.stop()
            _listener = None
            _queue = None
            _log_path = None
