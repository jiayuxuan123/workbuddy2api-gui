"""wb_health.py —— 熔断与故障转移（对齐 CC Switch 的熔断器参数与判定）。

账号池原本只有"失败就冷却 60 秒"这一条规则，粒度太粗：一个账号偶发
一次 429 也会被整体摘掉。这里补上一层真正的熔断器，参数直接取自
CC Switch 的 `proxy/circuit_breaker.rs`，不是自己拍的：

| 参数 | 默认 | 含义 |
|---|---|---|
| failure_threshold | 4 | 连续失败多少次跳闸 |
| success_threshold | 2 | 半开状态下连续成功多少次恢复 |
| timeout_seconds | 60 | 跳闸后冷却多久（**固定值，没有指数退避**） |
| error_rate_threshold | 0.6 | 错误率超过它且样本够多也跳闸 |
| min_requests | 10 | 算错误率前至少要有的请求数 |

状态机（三种状态，与 CC Switch 同名）：

* **closed** —— 正常放行；
* **open** —— 全部拒绝，直到 `timeout_seconds` 到期；
* **half_open** —— 只放**一个**探测请求；成功够 `success_threshold` 次
  就恢复（并清零累计计数），**失败一次立刻重新跳闸**。

两个关键判定：

1. **哪些算失败。** 不是所有 4xx 都算。CC Switch 的
   `categorize_proxy_error` 把 400/405/406/413/414/415/422/501 视为
   "请求本身有问题"，不该算在出口头上；而所有 5xx 连同
   401/403/404/408/409/429/451 都算出口故障。照搬这个划分，
   才不会因为客户端发错参数就把好账号熔断掉。
2. **半开的探测名额只有一个。** 并发请求同时涌入时，只能有一个去试探，
   其余直接拒绝，避免一堆请求同时打向一个还没恢复的出口。

只用标准库。
"""

import threading
import time

#: 状态名。与 CC Switch 的 CircuitState 一致。
CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half_open"

#: 熔断参数，取自 CircuitBreakerConfig::default()。
DEFAULTS = {
    "failure_threshold": 4,
    "success_threshold": 2,
    "timeout_seconds": 60,
    "error_rate_threshold": 0.6,
    "min_requests": 10,
}

#: 半开状态下同时允许的探测请求数。固定为 1。
MAX_HALF_OPEN_PROBES = 1

#: 这些状态码表示"请求本身有问题"，不计入出口故障。
#: 名单来自 CC Switch 的 categorize_proxy_error。
NON_RETRYABLE_STATUS = frozenset((400, 405, 406, 413, 414, 415, 422, 501))


def is_retryable_status(status):
    """True when an HTTP status should count against the endpoint.

    A 5xx is always the endpoint's fault. Among 4xx, only the ones that mean
    "this request is malformed" are excluded - everything else (401, 403, 404,
    408, 409, 429, 451) says something about the credential or the quota, which
    a different endpoint may well serve fine.
    """
    try:
        code = int(status)
    except (TypeError, ValueError):
        return True
    if code in NON_RETRYABLE_STATUS:
        return False
    return code >= 400


def classify(error=None, status=None):
    """Decide whether one attempt counts as a failure.

    Returns True (failed), False (the caller's fault, not the endpoint's), or
    None (neutral: a client disconnect says nothing about the endpoint).
    """
    if error == "client_abort":
        return None
    if status is not None:
        return is_retryable_status(status)
    return True


class Breaker(object):
    """One endpoint's circuit breaker.

    Thread-safe: the gateway serves requests from many threads at once, and a
    breaker that is not atomic can let a whole burst through just after the
    cooldown expires - exactly when the endpoint is least able to take it.
    """

    def __init__(self, key, config=None):
        self.key = key
        self.config = dict(DEFAULTS)
        if config:
            self.config.update(config)
        self._lock = threading.RLock()
        self.state = CLOSED
        self.consecutive_failures = 0
        self.consecutive_successes = 0
        self.total_requests = 0
        self.failed_requests = 0
        self.opened_at = 0.0
        self.half_open_inflight = 0
        self.last_error = ""
        self.last_status = None
        self.last_failure_at = 0.0
        self.last_success_at = 0.0
        self.opened_count = 0

    # -- introspection ------------------------------------------------------
    def snapshot(self, now=None):
        now = now if now is not None else time.time()
        with self._lock:
            self._refresh(now)
            return {
                "key": self.key,
                "state": self.state,
                "consecutive_failures": self.consecutive_failures,
                "consecutive_successes": self.consecutive_successes,
                "total_requests": self.total_requests,
                "failed_requests": self.failed_requests,
                "error_rate": (round(self.failed_requests / self.total_requests, 4)
                               if self.total_requests else 0.0),
                "cooldown_remaining": (round(max(0.0, self.opened_at
                                                 + self.config["timeout_seconds"] - now), 1)
                                       if self.state == OPEN else 0.0),
                "opened_count": self.opened_count,
                "last_error": self.last_error,
                "last_status": self.last_status,
                "last_failure_at": self.last_failure_at or None,
                "last_success_at": self.last_success_at or None,
                "config": dict(self.config),
            }

    # -- state machine ------------------------------------------------------
    def _refresh(self, now):
        """Move Open -> HalfOpen once the cooldown has elapsed.

        Done lazily on access rather than on a timer: there is no background
        thread to keep alive, and the transition only matters when someone is
        about to send a request anyway.
        """
        if self.state == OPEN and now - self.opened_at >= self.config["timeout_seconds"]:
            self.state = HALF_OPEN
            self.consecutive_successes = 0
            self.half_open_inflight = 0

    def _to_open(self, now):
        self.state = OPEN
        self.opened_at = now
        self.consecutive_failures = 0
        self.consecutive_successes = 0
        self.half_open_inflight = 0
        self.opened_count += 1

    def _to_closed(self):
        self.state = CLOSED
        self.consecutive_failures = 0
        self.consecutive_successes = 0
        self.half_open_inflight = 0
        # The error-rate test looks at lifetime totals, so recovering has to
        # clear them - otherwise one bad spell keeps tripping the breaker
        # forever, no matter how healthy the endpoint is now.
        self.total_requests = 0
        self.failed_requests = 0

    def is_available(self, now=None):
        """Whether a request may be sent. Does not consume a probe slot."""
        now = now if now is not None else time.time()
        with self._lock:
            self._refresh(now)
            return self.state in (CLOSED, HALF_OPEN)

    def allow_request(self, now=None):
        """Claim the right to send one request.

        Returns ``(allowed, used_probe)``. ``used_probe`` must be handed back
        to ``record_success`` / ``record_failure`` or released with
        ``release_probe``, otherwise the single probe slot stays taken and the
        endpoint can never recover.
        """
        now = now if now is not None else time.time()
        with self._lock:
            self._refresh(now)
            if self.state == CLOSED:
                return True, False
            if self.state == OPEN:
                return False, False
            if self.half_open_inflight >= MAX_HALF_OPEN_PROBES:
                return False, False
            self.half_open_inflight += 1
            return True, True

    def release_probe(self):
        """Give back a probe slot without recording an outcome."""
        with self._lock:
            if self.half_open_inflight > 0:
                self.half_open_inflight -= 1

    def record_success(self, used_probe=False, now=None):
        now = now if now is not None else time.time()
        with self._lock:
            if used_probe and self.half_open_inflight > 0:
                self.half_open_inflight -= 1
            self.last_success_at = now
            self.total_requests += 1
            if self.state == HALF_OPEN:
                self.consecutive_successes += 1
                if self.consecutive_successes >= self.config["success_threshold"]:
                    self._to_closed()
            elif self.state == CLOSED:
                self.consecutive_failures = 0
                self.consecutive_successes += 1

    def record_failure(self, used_probe=False, error="", status=None, now=None):
        now = now if now is not None else time.time()
        with self._lock:
            if used_probe and self.half_open_inflight > 0:
                self.half_open_inflight -= 1
            self.last_failure_at = now
            self.last_error = str(error or "")[:200]
            self.last_status = status
            self.total_requests += 1
            self.failed_requests += 1

            if self.state == HALF_OPEN:
                # One failure is enough: the endpoint just proved it is still
                # broken, so stop spending requests on it.
                self._to_open(now)
                return

            if self.state == OPEN:
                return

            self.consecutive_failures += 1
            if self.consecutive_failures >= self.config["failure_threshold"]:
                self._to_open(now)
                return
            if self.total_requests >= self.config["min_requests"]:
                rate = self.failed_requests / float(self.total_requests)
                if rate >= self.config["error_rate_threshold"]:
                    self._to_open(now)

    def record(self, ok, used_probe=False, error="", status=None, neutral=False):
        """Record one attempt's outcome, or skip it entirely when neutral."""
        if neutral:
            if used_probe:
                self.release_probe()
            return
        if ok:
            self.record_success(used_probe=used_probe)
        else:
            self.record_failure(used_probe=used_probe, error=error, status=status)

    def reset(self):
        """Force the breaker closed - the operator says it is fine now."""
        with self._lock:
            self._to_closed()
            self.last_error = ""
            self.last_status = None


class Registry(object):
    """Every breaker, keyed by whatever identifies an endpoint."""

    def __init__(self, config=None):
        self._lock = threading.RLock()
        self._config = dict(config) if config else None
        self._breakers = {}

    def get(self, key, config=None):
        key = str(key)
        with self._lock:
            breaker = self._breakers.get(key)
            if breaker is None:
                breaker = Breaker(key, config or self._config)
                self._breakers[key] = breaker
            return breaker

    def drop(self, key):
        with self._lock:
            self._breakers.pop(str(key), None)

    def reset(self, key=None):
        if key is None:
            with self._lock:
                for breaker in self._breakers.values():
                    breaker.reset()
            return
        self.get(key).reset()

    def set_config(self, config):
        """Apply new thresholds everywhere without losing breaker state."""
        with self._lock:
            self._config = dict(config)
            for breaker in self._breakers.values():
                breaker.config.update(config)

    def snapshot(self):
        with self._lock:
            items = list(self._breakers.values())
        return [b.snapshot() for b in items]

    def healthy_keys(self):
        """Keys that would accept a request right now."""
        return [b.key for b in self.snapshot_breakers() if b.is_available()]

    def snapshot_breakers(self):
        with self._lock:
            return list(self._breakers.values())


#: Process-wide registry for upstream accounts.
ACCOUNTS = Registry()


def available_account_uids(uids, registry=None, now=None):
    """Filter a candidate list down to the accounts whose breaker is closed.

    Order is preserved: the pool's round-robin cursor decides fairness, and
    this only removes what is currently unusable.

    ``now`` is passed through to the breakers so the whole check runs against a
    single instant. Letting each breaker read the clock separately would mean a
    request could be admitted by a breaker whose cooldown expired mid-loop.
    """
    reg = registry or ACCOUNTS
    stamp = now if now is not None else time.time()
    out = []
    for uid in uids:
        if reg.get(uid).is_available(stamp):
            out.append(uid)
    return out
