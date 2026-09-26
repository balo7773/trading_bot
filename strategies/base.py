"""
strategies/base.py
Data contracts and shared structures for post-close signal generation.
"""
from dataclasses import dataclass, field
from typing import Optional
from config.enums import AbortReason, EngineType, SignalStatus, TradeDirection


@dataclass(frozen=True)
class DailySignalCandidate:
    watchlist_id: str
    epic: str
    engine: EngineType
    direction: TradeDirection
    signal_date: str
    signal_close_price: float
    structural_stop_price: float
    stop_distance_pct: float
    provisional_priority: float
    reclaim_deadline_at: int
    created_at: int
    signal_status: SignalStatus = SignalStatus.PENDING
    abort_reason: Optional[AbortReason] = None

    # Engine A (MR) specific fields (MUST be None for Engine B)
    target_price: Optional[float] = None
    initial_rr_ratio: Optional[float] = None

    # Engine B (BO) specific fields (MUST be None for Engine A)
    breakout_thrust_atr: Optional[float] = None
    compression_spread_pct: Optional[float] = None

    executable_rr_ratio: Optional[float] = None
    final_priority: Optional[float] = None
    next_ex_div_pct_of_price: Optional[float] = None

    # Deterministic ID for idempotency: SIG_{YYYYMMDD}_{EPIC}_{ENGINE}
    signal_id: str = field(init=False)

    def __post_init__(self):
        clean_date = self.signal_date.replace("-", "")
        deterministic_id = f"SIG_{clean_date}_{self.epic}_{self.engine.value}"
        object.__setattr__(self, "signal_id", deterministic_id)
