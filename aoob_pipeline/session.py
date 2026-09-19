"""Per-explore visit cursor over a function sequence."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ExploreCursor:
    sequence: list[str]
    index: int = 0
    opened: set[str] = field(default_factory=set)

    @property
    def n(self) -> int:
        return len(self.sequence)

    def current(self) -> str | None:
        if not self.sequence:
            return None
        return self.sequence[min(max(0, self.index), len(self.sequence) - 1)]

    def remaining(self) -> list[str]:
        return [name for name in self.sequence if name not in self.opened]

    def status(self) -> dict:
        cur = self.current()
        return {
            "index": self.index + 1 if self.sequence else 0,
            "n": self.n,
            "current": cur,
            "opened": sorted(self.opened),
            "remaining": self.remaining(),
            "complete": not self.remaining(),
        }

    def mark_opened(self, name: str | None = None) -> None:
        target = name or self.current()
        if target:
            self.opened.add(target)

    def move(self, *, direction: str = "next", step: int = 0) -> dict:
        if not self.sequence:
            return {"error": "function_sequence is empty", **self.status()}
        if step:
            self.index = min(max(1, int(step)), self.n) - 1
        else:
            d = (direction or "next").strip().lower()
            if d in {"next", "forward"}:
                self.index = min(self.index + 1, self.n - 1)
            elif d in {"prev", "previous", "back"}:
                self.index = max(self.index - 1, 0)
            else:
                return {"error": f"unknown direction {direction!r}; use next/prev or step=N", **self.status()}
        return {"moved": True, **self.status()}
