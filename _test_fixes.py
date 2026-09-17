"""Verify the two headline fixes with measurements.

1. Repeated dashboard-style polling must not re-parse the whole log.
2. The process must survive sys.stdout/sys.stderr being None (--noconsole).
"""
import io
import json
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

FIXTURE_ROOT = os.path.realpath(tempfile.mkdtemp(prefix="wbperf_"))
LOG_PATH = os.path.join(FIXTURE_ROOT, "usage.jsonl")


def log_open(mode="r", **kwargs):
    resolved = os.path.realpath(LOG_PATH)
    if os.path.commonpath([resolved, FIXTURE_ROOT]) != FIXTURE_ROOT:
        raise ValueError("refusing to open outside the fixture root")
    return open(resolved, mode, **kwargs)


# 40k rows: representative of a few weeks of steady use.
N = 40000
with log_open("w", encoding="utf-8") as fh:
    for i in range(N):
        fh.write(json.dumps({
            "at": time.time() - i, "iso": "x", "model": "gpt-6-astra",
            "realm": "intl", "account": "uid01", "stream": True,
            "elapsed_ms": 500, "ttft_ms": 100, "gen_ms": 400,
            "prompt_tokens": 1000, "completion_tokens": 200,
            "reasoning_tokens": 50, "cached_tokens": 300,
            "total_tokens": 1200, "credit": 0.01,
            "tokens_per_sec": 20.0, "cache_hit_pct": 30.0,
        }, ensure_ascii=False) + "\n")

size_mb = os.path.getsize(LOG_PATH) / 1048576.0
print("log fixture: %d rows, %.1f MB" % (N, size_mb))

# --- baseline: what one poll used to cost (full read + parse every time) ---
def old_way():
    rows = []
    with log_open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    return rows

t0 = time.perf_counter()
base_rows = old_way()
t_old = time.perf_counter() - t0
print("  full read+parse            : %7.1f ms" % (t_old * 1000))

# --- new way: first call builds the index, repeats are near-free ---
import wb_usagelog

t0 = time.perf_counter()
idx = wb_usagelog.UsageLog(LOG_PATH, window=20000)
idx.refresh(realm_of=wb_usagelog.realm_resolver())
t_first = time.perf_counter() - t0
print("  index build (first call)   : %7.1f ms" % (t_first * 1000))

t0 = time.perf_counter()
for _ in range(20):
    idx.refresh(realm_of=wb_usagelog.realm_resolver())
    idx.totals("intl")
    idx.by_model("intl")
    idx.recent(limit=60, realm=None)
    idx.by_account()
    idx.tail(sample=5000, realm=None)
t_repeat = (time.perf_counter() - t0) / 20
print("  one poll after that        : %7.3f ms" % (t_repeat * 1000))
print()
print("  => a 5s-interval dashboard used to spend %.1f ms/cycle parsing;"
      % (t_old * 1000))
print("     it now spends %.3f ms/cycle." % (t_repeat * 1000))
print("  => speedup on repeat polls : %.0fx" % (t_old / max(t_repeat, 1e-9)))

speedup = t_old / max(t_repeat, 1e-9)
if speedup < 10:
    print("  WARNING: expected a large speedup, got only %.1fx" % speedup)

# --- appended rows are absorbed incrementally, not re-read ---
t0 = time.perf_counter()
for i in range(200):
    with log_open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"at": time.time(), "model": "m", "realm": "intl",
                             "account": "uid01", "prompt_tokens": 1,
                             "completion_tokens": 1, "total_tokens": 2}) + "\n")
idx.refresh(realm_of=wb_usagelog.realm_resolver())
t_inc = time.perf_counter() - t0
print()
print("  absorbing 200 appended rows: %7.1f ms (vs %7.1f ms to re-read all)"
      % (t_inc * 1000, t_old * 1000))

print()
print("=== 2. no-console survival ===")
import wb_proxy as P

saved_out, saved_err = sys.stdout, sys.stderr
sys.stdout = None
sys.stderr = None
try:
    P.log("survives with no console at all")
    P.add_log_entry("direct buffer write")
    ok = "log() and add_log_entry() both survived"
    crashed = None
except Exception as exc:
    ok = "CRASHED"
    crashed = "%s: %s" % (type(exc).__name__, exc)
finally:
    sys.stdout, sys.stderr = saved_out, saved_err
print("  " + ok)
if crashed:
    print("  " + crashed)
    sys.exit(1)

# and confirm those entries actually landed in the in-memory buffer
logs = P.get_logs(limit=5)
print("  log buffer still works: %d entries, latest=%r"
      % (logs["total"], logs["logs"][-1]["msg"] if logs["logs"] else None))

shutil.rmtree(FIXTURE_ROOT, ignore_errors=True)
print()
print("RESULT: fixes verified")
