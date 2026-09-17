"""Regression test: the usage log must not lose rows under concurrency.

Background: the original code appended with ``open(path, "a")`` + ``write()``.
Measured with 16 concurrent writers, 640 rows in produced 541 on disk, plus
223 ``WinError 5`` failures from the summary's ``os.replace()`` racing other
threads. Both are fixed by a single locked ``os.write`` on an ``O_APPEND``
descriptor; this test keeps them fixed.

Run:  python _test_concurrency.py
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

N_THREADS = 16
ROWS_EACH = 40
EXPECTED = N_THREADS * ROWS_EACH


def main():
    import wb_proxy

    root = os.path.realpath(tempfile.mkdtemp(prefix="wbconc_"))
    scratch = os.path.join(root, "scratch")
    wb_proxy.USAGE_DIR = scratch
    wb_proxy.USAGE_LOG = os.path.join(scratch, "usage.jsonl")
    wb_proxy.USAGE_SUMMARY = os.path.join(scratch, "usage-summary.json")

    errors = []

    def worker(tid):
        try:
            for _ in range(ROWS_EACH):
                wb_proxy.record_usage(
                    "gpt-6-astra",
                    {"prompt_tokens": 10, "completion_tokens": 5,
                     "total_tokens": 15},
                    stream=True, elapsed_ms=100, ttft_ms=20, gen_ms=80,
                    account="u%02d" % tid,
                )
        except Exception as exc:
            errors.append("%s: %s" % (type(exc).__name__, exc))

    started = time.time()
    threads = [threading.Thread(target=worker, args=(t,))
               for t in range(N_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.time() - started

    with open(wb_proxy.USAGE_LOG, encoding="utf-8") as fh:
        lines = [line for line in fh if line.strip()]
    parsed = 0
    for line in lines:
        try:
            json.loads(line)
            parsed += 1
        except Exception:
            pass

    summary = json.load(open(wb_proxy.USAGE_SUMMARY, encoding="utf-8"))
    stray = [f for f in os.listdir(scratch) if f.endswith(".tmp")]

    print("threads x rows        : %d x %d = %d" % (N_THREADS, ROWS_EACH, EXPECTED))
    print("lines on disk         : %d" % len(lines))
    print("lines parsing as JSON : %d" % parsed)
    print("summary.requests      : %s" % summary.get("requests"))
    print("worker exceptions     : %s" % (errors or "none"))
    print("stray .tmp files      : %s" % (stray or "none"))
    print("elapsed               : %.0f ms" % (elapsed * 1000))
    print()

    checks = [
        ("no row lost", len(lines) == EXPECTED),
        ("every line is valid JSON", parsed == EXPECTED),
        ("no worker raised", not errors),
        ("no temp file leaked", not stray),
    ]
    failed = [name for name, ok in checks if not ok]
    for name, ok in checks:
        print("  %-26s %s" % (name, "PASS" if ok else "FAIL"))

    shutil.rmtree(root, ignore_errors=True)
    print()
    if failed:
        print("RESULT: %d FAILURE(S): %s" % (len(failed), ", ".join(failed)))
        return 1
    print("RESULT: PASS - concurrency-safe, zero rows lost")
    return 0


if __name__ == "__main__":
    sys.exit(main())
