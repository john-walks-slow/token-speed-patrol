import asyncio
import time


class TokenBucket:
    """令牌桶：容量 = 每分钟速率（充许一次突发用光整分钟配额），按分钟速率匀速补充。"""

    def __init__(self, rate_per_min: int):
        self.rate = rate_per_min
        self.capacity = rate_per_min
        self.tokens = float(rate_per_min)
        self.updated = time.monotonic()


class RateLimiter:
    """按 key 分桶的分钟级限流器。rate<=0 表示不限速（no-op）。

    每个 provider(base_url+api_key) 独立一个令牌桶，跨并发请求共享。
    """

    def __init__(self):
        self._buckets: dict[tuple[str, str], TokenBucket] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, key: tuple[str, str], rate_per_min: int) -> None:
        """获取一个令牌；rate<=0 直接放行。必要时等待令牌补充。"""
        if rate_per_min <= 0:
            return
        while True:
            wait = None
            async with self._lock:
                b = self._buckets.get(key)
                if b is None or b.rate != rate_per_min:
                    b = TokenBucket(rate_per_min)
                    self._buckets[key] = b
                now = time.monotonic()
                b.tokens = min(b.capacity, b.tokens + (now - b.updated) * b.rate / 60.0)
                b.updated = now
                if b.tokens >= 1.0:
                    b.tokens -= 1.0
                    return
                wait = (1.0 - b.tokens) * 60.0 / b.rate
            await asyncio.sleep(wait)


# 全局单例：batch 端点与定时调度器共享同一限流器
limiter = RateLimiter()