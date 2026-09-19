"""wb_usagelog.py —— 用量日志的增量索引。

背景：看板每 5 秒轮询 /usage、/usage/recent、/usage/perf 等接口，而这些接口
原先各自把整个 usage.jsonl 从头读到尾再解析 JSON。同一批数据在一次刷新里被
重复解析多遍，且开销随日志体积线性增长——日志攒到几十万行后，光是一个空转的
看板就能把 CPU 吃满。

本模块把「读文件 + 解析 JSON」收敛成一份按需增量维护的索引：

* 文件没变 → 直接命中缓存，零 I/O；
* 文件追加了 → 只读新增的那段，逐行解析后并入索引；
* 文件被截断/轮转 → 检测到体积变小，整体重建。

对外仍按原有语义分别提供「全量累计」「按账号聚合」「尾部若干行」等视图，
调用方拿到的数据结构与改造前一致，因此上层无需改动逻辑。

索引只保存每条记录的紧凑字段集，并为全量统计维护累加器，避免把整个日志的
原始 JSON 常驻内存。
"""

import json
import os
import threading
import time

import wb_pricing


def wb_pricing_cost(model, prompt_tokens, completion_tokens, cached_tokens):
    """Thin indirection so tests can patch wb_pricing.cost_of in one place."""
    return wb_pricing.cost_of(model, prompt_tokens, completion_tokens,
                              cached_tokens)

#: Fields carried through from each JSONL row for aggregation.
_TOKEN_FIELDS = ("prompt_tokens", "completion_tokens", "reasoning_tokens",
                 "cached_tokens", "total_tokens", "credit")

#: How many parsed rows to retain for the "recent" and percentile views.
#: Bounded so a multi-gigabyte log cannot exhaust memory.
DEFAULT_WINDOW = 20000

#: Per-day aggregate bucket. Kept tiny (one per calendar day, not per row)
#: so years of history stay cheap; cost is folded in at absorb time using
#: whatever price table was current, and can be recomputed from tokens on
#: demand when prices change.
def _new_day_bucket():
    return {
        "date": "", "requests": 0, "errors": 0,
        "prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0,
        "cached_tokens": 0, "total_tokens": 0, "credit": 0.0,
        "cost": 0.0, "cost_estimated": 0,
    }


def day_of_row(row):
    """Local calendar date (YYYY-MM-DD) for a log row.

    Prefers the ``iso`` stamp the proxy writes; falls back to the epoch
    ``at`` field for rows from other writers.
    """
    iso = row.get("iso")
    if isinstance(iso, str) and len(iso) >= 10:
        return iso[:10]
    at = row.get("at")
    try:
        return time.strftime("%Y-%m-%d", time.localtime(float(at)))
    except (TypeError, ValueError):
        return ""


def _copy_model_bucket(bucket):
    """Deep-enough copy so callers cannot mutate the index's own state."""
    out = dict(bucket)
    out["accounts"] = dict(bucket.get("accounts") or {})
    return out


def _new_totals():
    base = {"requests": 0, "errors": 0}
    for field in _TOKEN_FIELDS:
        base[field] = 0
    return base


def _new_model_bucket():
    """Per-model aggregate shaped like the original usage_snapshot output.

    Carries a per-account request count (the panel shows it) and no `errors`
    key, matching what consumers of /usage already expect.
    """
    base = {"requests": 0, "accounts": {}}
    for field in _TOKEN_FIELDS:
        base[field] = 0
    return base


class UsageLog(object):
    """Incrementally indexed view over a JSONL usage log."""

    def __init__(self, path, window=DEFAULT_WINDOW):
        self.path = path
        self.window = window
        self._lock = threading.Lock()
        self._offset = 0          # bytes already consumed
        self._rows = []           # bounded tail window, oldest first
        self._row_realms = []     # parallel list: realm of each row in _rows
        self._totals = {}         # realm -> totals
        self._by_model = {}       # realm -> {model: {requests, accounts, tokens}}
        self._by_account = {}     # account -> aggregate bucket
        self._by_day = {}         # YYYY-MM-DD -> day bucket (all realms)
        self._parsed = 0

    # ---------------------------------------------------------------- reading
    def _parse_line(self, line):
        line = line.strip()
        if not line:
            return None
        try:
            row = json.loads(line)
        except Exception:
            return None
        return row if isinstance(row, dict) else None

    def _absorb(self, row, realm):
        """Fold one parsed row into every aggregate.

        The row itself is stored exactly as it was read: callers hand rows
        straight back to clients, so attaching bookkeeping fields here would
        change what /usage/recent returns.
        """
        is_error = bool(row.get("error"))
        totals = self._totals.setdefault(realm, _new_totals())
        if is_error:
            totals["errors"] += 1
        else:
            totals["requests"] += 1
            for field in _TOKEN_FIELDS:
                if field in row:
                    totals[field] += (row[field] or 0)
            model = row.get("model") or "unknown"
            bucket = self._by_model.setdefault(realm, {}).setdefault(
                model, _new_model_bucket())
            bucket["requests"] += 1
            for field in _TOKEN_FIELDS:
                if field in row:
                    bucket[field] += (row[field] or 0)
            acct_id = row.get("account")
            if acct_id:
                bucket["accounts"][acct_id] = bucket["accounts"].get(acct_id, 0) + 1

            key = row.get("account") or "(unattributed)"
            acct = self._by_account.setdefault(key, {
                "account": key, "requests": 0, "prompt_tokens": 0,
                "completion_tokens": 0, "reasoning_tokens": 0,
                "cached_tokens": 0, "total_tokens": 0, "models": {},
            })
            acct["requests"] += 1
            for field in ("prompt_tokens", "completion_tokens",
                          "reasoning_tokens", "cached_tokens", "total_tokens"):
                acct[field] += row.get(field) or 0
            model_key = row.get("model") or "?"
            acct["models"][model_key] = acct["models"].get(model_key, 0) + 1

        # Daily bucket (all realms; the per-realm cost view filters rows via
        # _row_realms at read time instead of duplicating buckets per realm).
        day = day_of_row(row)
        if day:
            self._fold_day(self._by_day, day, row, is_error)

        self._rows.append(row)
        self._row_realms.append(realm)
        if len(self._rows) > self.window:
            del self._rows[:len(self._rows) - self.window]
            del self._row_realms[:len(self._row_realms) - self.window]

    #: Day-bucket fold used by both the index and the cost view's per-realm
    #: recomputation. Kept as a module function so the cost endpoint can
    #: rebuild buckets for arbitrary row selections with identical semantics.
    @staticmethod
    def _fold_day(bucket_map, day, row, is_error):
        bucket = bucket_map.setdefault(day, _new_day_bucket())
        bucket["date"] = day
        if is_error:
            bucket["errors"] += 1
            return
        bucket["requests"] += 1
        for field in ("prompt_tokens", "completion_tokens", "reasoning_tokens",
                      "cached_tokens", "total_tokens", "credit"):
            if field in row:
                bucket[field] += (row[field] or 0)
        cost, entry = wb_pricing_cost(
            row.get("model"), row.get("prompt_tokens") or 0,
            row.get("completion_tokens") or 0, row.get("cached_tokens") or 0)
        bucket["cost"] += cost
        if entry is None:
            bucket["cost_estimated"] += 1

    def _reset(self):
        self._offset = 0
        self._rows = []
        self._row_realms = []
        self._totals = {}
        self._by_model = {}
        self._by_account = {}
        self._by_day = {}
        self._parsed = 0

    def refresh(self, realm_of=None):
        """Bring the index up to date, reading only newly appended bytes.

        ``realm_of`` maps a parsed row to a realm; it is resolved at absorb
        time because the original code attached realm via the account pool.
        """
        with self._lock:
            try:
                size = os.path.getsize(self.path)
            except OSError:
                # Missing or unreadable: treat as empty, but keep any window
                # we already have so a transient error does not blank the UI.
                return
            if size < self._offset:
                self._reset()          # truncated or rotated
            if size == self._offset:
                return
            try:
                with open(self.path, "rb") as fh:
                    fh.seek(self._offset)
                    chunk = fh.read()
            except OSError:
                return
            # A partially written trailing line must not be parsed yet: keep
            # the remainder for the next refresh so no row is ever half-read.
            if chunk and not chunk.endswith(b"\n"):
                cut = chunk.rfind(b"\n")
                if cut == -1:
                    return             # nothing complete yet
                chunk, remainder = chunk[:cut + 1], len(chunk) - (cut + 1)
            else:
                remainder = 0
            self._offset = size - remainder
            text = chunk.decode("utf-8", "replace")
            for line in text.splitlines():
                row = self._parse_line(line)
                if row is None:
                    continue
                self._parsed += 1
                realm = row.get("realm")
                if not realm:
                    realm = realm_of(row) if realm_of else "intl"
                self._absorb(row, realm)

    # ---------------------------------------------------------------- views
    def invalidate(self):
        """Force a full re-read on the next refresh (used by tests/settings)."""
        with self._lock:
            self._reset()

    def totals(self, realm=None):
        with self._lock:
            if not self._is_all(realm):
                return dict(self._totals.get(realm) or _new_totals())
            out = _new_totals()
            for totals in self._totals.values():
                out["requests"] += totals["requests"]
                out["errors"] += totals["errors"]
                for field in _TOKEN_FIELDS:
                    out[field] += totals[field]
            return out

    def by_model(self, realm=None):
        with self._lock:
            if not self._is_all(realm):
                src = self._by_model.get(realm) or {}
                return {k: _copy_model_bucket(v) for k, v in src.items()}
            out = {}
            for models in self._by_model.values():
                for model, bucket in models.items():
                    agg = out.setdefault(model, _new_model_bucket())
                    agg["requests"] += bucket["requests"]
                    for field in _TOKEN_FIELDS:
                        agg[field] += bucket[field]
                    for uid, count in bucket["accounts"].items():
                        agg["accounts"][uid] = agg["accounts"].get(uid, 0) + count
            return out

    def by_account(self):
        with self._lock:
            out = []
            for bucket in self._by_account.values():
                item = dict(bucket)
                item["models"] = dict(bucket["models"])
                out.append(item)
        out.sort(key=lambda b: -b["total_tokens"])
        for item in out:
            item["models"] = sorted(item["models"].items(), key=lambda kv: -kv[1])[:5]
        return out

    def recent(self, limit=100, realm=None, keep=0):
        """Return (total_rows_matching, last `limit` rows).

        Rows come back exactly as they were read from the log — the realm is
        tracked alongside them, not injected into them.
        """
        want = max(limit, keep)
        with self._lock:
            rows = self._select(realm)
        total = len(rows)
        if limit and limit > 0:
            rows = rows[-limit:]
        return total, rows

    def tail(self, sample=5000, realm=None):
        with self._lock:
            rows = self._select(realm)
        if sample and sample > 0:
            rows = rows[-sample:]
        return rows

    @staticmethod
    def _is_all(realm):
        """True when the filter means "every realm".

        ``auto`` is a routing mode, not a realm, so it selects everything.
        Without this a viewer that passes CURRENT_REALM ("auto") matched no row
        at all and every figure read zero.
        """
        return not realm or realm == "auto"

    def _select(self, realm):
        """Rows for a realm, or all rows when no realm filter is given."""
        if self._is_all(realm):
            return list(self._rows)
        return [row for row, row_realm in zip(self._rows, self._row_realms)
                if row_realm == realm]

    def stats(self):
        with self._lock:
            return {"parsed": self._parsed, "window": len(self._rows),
                    "offset": self._offset}

    def by_day(self, realm=None):
        """Per-calendar-day aggregates, oldest first.

        The incremental ``_by_day`` buckets are all-realm; a realm filter
        recomputes from the bounded tail window (same semantics as the
        ``recent`` views — full history across realms remains the common
        case, since the tail window holds the last DEFAULT_WINDOW rows).

        When prices change, call :meth:`invalidate` and refresh(): the
        buckets are rebuilt from the log at the new prices.
        """
        with self._lock:
            if self._is_all(realm):
                buckets = [dict(v) for v in self._by_day.values()]
            else:
                map_by_day = {}
                for row, row_realm in zip(self._rows, self._row_realms):
                    if row_realm != realm:
                        continue
                    day = day_of_row(row)
                    if day:
                        self._fold_day(map_by_day, day, row, bool(row.get("error")))
                buckets = list(map_by_day.values())
        buckets.sort(key=lambda b: b["date"])
        return buckets


_default = None
_default_lock = threading.Lock()


def default_log():
    """Process-wide index over the active usage log.

    Creation is deferred because wb_proxy computes the log path at import
    time and may reassign it from --usage-dir before serving traffic.
    """
    global _default
    with _default_lock:
        if _default is None:
            _default = UsageLog(_current_path())
        return _default


def _current_path():
    try:
        import wb_proxy
        return wb_proxy.USAGE_LOG
    except Exception:
        return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "usage", "usage.jsonl")


def ensure_path(path):
    """Rebind the shared index when the configured log path changes."""
    global _default
    with _default_lock:
        if _default is None or os.path.abspath(_default.path) != os.path.abspath(path):
            _default = UsageLog(path)
        return _default


def realm_resolver():
    """Build a resolver replicating wb_proxy.row_matches_realm's fallback.

    Only rows written before this build (which always stamps ``realm``) lack
    the field, so the fallback order mirrors the original: the owning
    account's realm first, then model-based detection.
    """
    def resolve(row):
        try:
            import wb_proxy
        except Exception:
            return "intl"
        uid = row.get("account")
        pool = getattr(wb_proxy, "POOL", None)
        if uid and pool:
            try:
                account = pool.get(uid)
            except Exception:
                account = None
            if account is not None and getattr(account, "realm", None):
                return account.realm
        model = row.get("model")
        if model:
            return wb_proxy.detect_model_realm(model) or "intl"
        return "intl"
    return resolve
