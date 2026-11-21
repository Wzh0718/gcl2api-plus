"""Small worker-local preference on top of the shared account rotation."""

from collections import Counter, deque
from time import monotonic


class AccountPriority:
    def __init__(self):
        self.buckets = deque()
        self.totals = Counter()
        self.credits = {}  # One opportunity per account, earned by regular rotation.
        self.turn = 0

    def _expire(self, now):
        while self.buckets and self.buckets[0][0] <= now - 3600:
            _, expired = self.buckets.popleft()
            for name, tokens in expired.items():
                self.totals[name] -= tokens
                if not self.totals[name]:
                    del self.totals[name]

    def record_tokens(self, name, tokens):
        if not name or tokens <= 0:
            return
        now = int(monotonic())
        self._expire(now)
        if not self.buckets or self.buckets[-1][0] != now:
            self.buckets.append((now, Counter()))
        self.buckets[-1][1][name] += tokens
        self.totals[name] += tokens

    def regular_selection(self, name):
        self.credits.pop(name, None)
        self.credits[name] = int(monotonic())

    def take_preference(self, excluded):
        self.turn = (self.turn + 1) % 4
        if self.turn:
            return None
        now = int(monotonic())
        self._expire(now)
        while self.credits and next(iter(self.credits.values())) <= now - 3600:
            del self.credits[next(iter(self.credits))]
        peak = max((tokens for name, tokens in self.totals.items() if name not in excluded), default=0)
        candidates = (name for name in self.credits
                      if name not in excluded and peak - self.totals[name] >= 8192)
        preferred = min(candidates, key=self.totals.__getitem__, default=None)
        if preferred:
            del self.credits[preferred]  # Consume before yielding to another request.
        return preferred


account_priority = AccountPriority()
