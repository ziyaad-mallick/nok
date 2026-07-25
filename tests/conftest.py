"""Shared test fixtures.

The session logger (nok.logs) is a process-global singleton: the first
log_event() wires up a QueueListener + file writer and later init calls are
idempotent no-ops. Because several modules log during tests, reset that
singleton around every test so state can't leak between them (notably so
test_logs starts from a clean slate).
"""
import pytest

from nok import logs


@pytest.fixture(autouse=True)
def _reset_session_logging(tmp_path, monkeypatch):
    logs.shutdown()
    # Redirect the DEFAULT log directory into this test's tmp dir. Any test
    # touching a module that logs triggers a lazy init on the first
    # log_event(), which otherwise wrote a real session file -- full of fixture
    # data -- into the repo's session-logs/ on every single run.
    monkeypatch.setattr(logs, "_REPO_ROOT", tmp_path)
    yield
    logs.shutdown()
