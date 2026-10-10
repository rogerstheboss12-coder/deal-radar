"""Circuit breaker shared by the backtest and live trading.

If the account falls `pause_drawdown` below its floor, the bot sells everything and sits in cash for
`pause_days`. Then it trades again with a new floor set from the equity it resumes with. It caps the
damage of a bad stretch without sitting out the recovery for good.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import pandas as pd

from .config import Settings


@dataclass
class Guard:
    floor: float | None = None        # pause when equity drops below this
    paused_until: str | None = None   # ISO timestamp; in cash until then
    pauses: int = 0

    def allows_trading(self, equity: float, now: pd.Timestamp, s: Settings) -> bool:
        """Call once per pass, before placing orders. False means: hold only cash."""
        if self.floor is None:
            self.floor = equity * (1 - s.pause_drawdown)
        if self.paused_until is not None:
            if now < pd.Timestamp(self.paused_until):
                return False
            self.paused_until = None
            self.floor = equity * (1 - s.pause_drawdown)
        if equity < self.floor:
            self.paused_until = (now + pd.Timedelta(days=s.pause_days)).isoformat()
            self.pauses += 1
            return False
        return True

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict | None) -> "Guard":
        return cls(**d) if d else cls()
