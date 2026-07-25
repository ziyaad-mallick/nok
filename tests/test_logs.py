import json
import threading

from nok import logs


def test_concurrent_log_events_all_valid_json(tmp_path):
    log_dir = tmp_path / "session-logs"
    path = logs.init_session_logging(log_dir=str(log_dir))

    n_threads = 4
    n_events = 100

    def worker(thread_id):
        for i in range(n_events):
            logs.log_event("test_event", thread_id=thread_id, i=i)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    logs.shutdown()

    with open(path, "r", encoding="utf-8") as f:
        lines = [line for line in f if line.strip()]

    assert len(lines) == n_threads * n_events
    for line in lines:
        parsed = json.loads(line)
        assert parsed["kind"] == "test_event"
        assert "thread_id" in parsed
        assert "i" in parsed
