"""
config/settings.py
Tunable parameters (Dials) per Master Spec Section 7 and Feature Toggles.
All dials are version-controlled and single-sourced.
"""

# Feature Flags (Corporate Actions & Calendar Gates)
ENABLE_EARNINGS_FILTER: bool = False         # Master Spec Section 2.1: False for stage 1 testing
EARNINGS_BLACKOUT_DAYS_PRE: int = 2          # Quarantined 2 days before earnings
EARNINGS_BLACKOUT_DAYS_POST: int = 1         # Quarantined 1 day after earnings

ENABLE_DIVIDEND_RISK_GATE: bool = False      # Gating ex-div cash deductions
MAX_EX_DIV_PCT_OF_PRICE: float = 0.005       # 0.5% max dividend-to-price ratio

# Engine A (Mean Reversion) Dials
ENGINE_A_RR_FLOOR: float = 1.20              # Section 1.1: Minimum initial Reward-to-Risk ratio
ENGINE_A_TARGET_CAP_PCT: float = 0.05        # Section 4.1: 5.0% profit cap
ENGINE_A_REGIME_RSQUARED: float = 0.70        # Section 2.1: R^2 of 200 SMA regression
ENGINE_A_MAX_LARGE_GAPS: int = 2             # Section 2.1: Max opening gaps > 2.0x ATR

# Engine B (Breakout) Dials
ENGINE_B_COMPRESSION_THRESHOLD: float = 0.010 # Section 1.2: |EMA32 - SMA20| / Close
ENGINE_B_STOP_ATR_MULT: float = 1.5          # Section 1.2: 1.5 x 14-day ATR stop distance
ENGINE_B_COILING_SPREAD: float = 0.015       # Section 2.1: Median compression over 20 sessions

# Portfolio & Direction Dials
CONCURRENT_DIRECTION_CAP: int = 3            # Section 3.7: Max 3 open longs and 3 open shorts
MAX_SPREAD_STOP_RATIO_STANDARD: float = 0.05 # Section 3.4: 5.0% spread-to-stop gate
MAX_SPREAD_STOP_RATIO_TOP: float = 0.10      # Section 3.4: 10.0% for top priority

# Priority Dial
QUEUE_A_WEIGHT_SUNDAY: float = 0.50          # Section 3.6: 50% Sunday rank
QUEUE_A_WEIGHT_RR: float = 0.50              # Section 3.6: 50% Executable R:R
