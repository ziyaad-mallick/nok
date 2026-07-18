"""Capped Codex CLI quota probe.

Run manually AFTER `codex login` (never invoked automatically — zero spend
policy). Makes at most 6 `codex exec` calls total, each capped at a 120s
timeout, and records every artifact needed to debug a bad response later:
full stdout, full stderr, the -o output file content, and elapsed seconds,
all under probe-results/call-N/.

Usage (manual, after codex login):
    python scripts/verify_codex.py            # calls 1-4 only
    python scripts/verify_codex.py --all       # calls 1-6

DO NOT invoke `codex exec` or `codex login` from any automated script or
CI path. This module is written but intentionally not executed as part of
tonight's build.
"""
import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE_RESULTS_DIR = REPO_ROOT / "probe-results"
CALL_TIMEOUT_SECONDS = 120
MAX_CALLS = 6

PROMPT_1 = (
    'Reply with exactly the JSON '
    '{"action":"open_app","args":{"name":"spotify"},"say":"opening spotify"} '
    'and nothing else'
)
PROMPT_2 = (
    "I want to open Spotify — can you go ahead and do that for me and let "
    "me know what you're doing? Reply however feels natural."
)
PROMPT_3 = "What model are you? State your own model id/name."
PROMPT_4 = "Describe what you see in the attached image in one sentence."

# Each entry: (call_num, prompt, extra_cli_args, needs_image)
CALLS = [
    (1, PROMPT_1, [], False),
    (2, PROMPT_2, [], False),
    (3, PROMPT_3, [], False),
    (4, PROMPT_4, [], True),
    (5, PROMPT_1, [], False),  # reserved — only runs with --all
    (6, PROMPT_2, [], False),  # reserved — only runs with --all
]


@dataclass
class CallResult:
    call_num: int
    prompt: str
    command: list
    elapsed_seconds: float
    returncode: int
    stdout: str
    stderr: str
    output_file_content: str
    parsed_json: Optional[dict] = field(default=None)
    passed: bool = False
    fail_reason: str = ""


def extract_last_json_block(text: str):
    """Return the last valid top-level JSON object/array found in `text`,
    scanning left to right past leading prose and duplicate blocks.

    Returns None if no valid JSON object/array is found (including the
    case of truncated/incomplete JSON).
    """
    if not text:
        return None
    decoder = json.JSONDecoder()
    last = None
    i = 0
    n = len(text)
    while i < n:
        if text[i] in "{[":
            try:
                obj, end = decoder.raw_decode(text, i)
                last = obj
                i = end
                continue
            except json.JSONDecodeError:
                pass
        i += 1
    return last


def make_test_screenshot(dest: Path) -> Path:
    """Capture a screenshot with mss and save it as a PNG at `dest`."""
    import mss
    import mss.tools

    with mss.mss() as sct:
        monitor = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
        shot = sct.grab(monitor)
        mss.tools.to_png(shot.rgb, shot.size, output=str(dest))
    return dest


def build_command(call_num: int, prompt: str, extra_args: list, output_file: Path, image_file: Optional[Path]) -> list:
    cmd = ["codex", "exec", prompt, "-o", str(output_file)]
    if image_file is not None:
        cmd += ["--image", str(image_file)]
    cmd += extra_args
    return cmd


def run_call(call_num: int, prompt: str, extra_args: list, needs_image: bool, out_root: Path) -> CallResult:
    call_dir = out_root / f"call-{call_num}"
    call_dir.mkdir(parents=True, exist_ok=True)

    image_file = None
    if needs_image:
        image_file = call_dir / "screenshot.png"
        make_test_screenshot(image_file)

    output_file = call_dir / "output.txt"
    command = build_command(call_num, prompt, extra_args, output_file, image_file)

    t0 = time.time()
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=CALL_TIMEOUT_SECONDS,
            cwd=str(REPO_ROOT),
        )
        elapsed = time.time() - t0
        stdout, stderr, returncode = proc.stdout, proc.stderr, proc.returncode
    except subprocess.TimeoutExpired as exc:
        elapsed = time.time() - t0
        stdout = exc.stdout or ""
        stderr = (exc.stderr or "") + f"\n[TIMEOUT after {CALL_TIMEOUT_SECONDS}s]"
        returncode = -1

    (call_dir / "stdout.txt").write_text(stdout, encoding="utf-8")
    (call_dir / "stderr.txt").write_text(stderr, encoding="utf-8")
    (call_dir / "elapsed.txt").write_text(f"{elapsed:.3f}\n", encoding="utf-8")

    output_file_content = ""
    if output_file.exists():
        output_file_content = output_file.read_text(encoding="utf-8", errors="replace")

    parsed = extract_last_json_block(output_file_content) or extract_last_json_block(stdout)
    passed = parsed is not None
    fail_reason = "" if passed else "no valid JSON block found in -o file or stdout"

    return CallResult(
        call_num=call_num,
        prompt=prompt,
        command=command,
        elapsed_seconds=elapsed,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        output_file_content=output_file_content,
        parsed_json=parsed,
        passed=passed,
        fail_reason=fail_reason,
    )


def print_codex_version() -> None:
    try:
        proc = subprocess.run(["codex", "--version"], capture_output=True, text=True, timeout=30)
        print(f"codex CLI version: {proc.stdout.strip() or proc.stderr.strip()}")
    except Exception as exc:
        print(f"codex CLI version: FAILED ({exc})")


def print_summary(results: list) -> None:
    print()
    print(f"{'call':>4}  {'result':6}  {'elapsed':>8}  reason")
    for r in results:
        status = "PASS" if r.passed else "FAIL"
        print(f"{r.call_num:>4}  {status:6}  {r.elapsed_seconds:>7.2f}s  {r.fail_reason}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true", help="also run reserved calls 5-6")
    args = parser.parse_args()

    print_codex_version()

    calls_to_run = CALLS if args.all else CALLS[:4]
    if len(calls_to_run) > MAX_CALLS:
        raise RuntimeError(f"refusing to exceed MAX_CALLS={MAX_CALLS}")

    PROBE_RESULTS_DIR.mkdir(exist_ok=True)
    results = []
    for call_num, prompt, extra_args, needs_image in calls_to_run:
        print(f"Running call {call_num}...")
        result = run_call(call_num, prompt, extra_args, needs_image, PROBE_RESULTS_DIR)
        results.append(result)

    print_summary(results)


if __name__ == "__main__":
    main()
