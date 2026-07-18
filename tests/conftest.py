"""Shared test fixtures.

The session logger (miko.logs) is a process-global singleton: the first
log_event() wires up a QueueListener + file writer and later init calls are
idempotent no-ops. Because several modules log during tests, reset that
singleton around every test so state can't leak between them (notably so
test_logs starts from a clean slate).
"""
import pytest

from miko import logs


@pytest.fixture(autouse=True)
def _reset_session_logging():
    logs.shutdown()
    yield
    logs.shutdown()
