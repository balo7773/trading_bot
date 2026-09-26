def is_earnings_quarantined(epic: str, target_date: str) -> bool:
    """
    Checks if an epic falls within an earnings blackout window.
    If the feature flag is disabled, it acts as a transparent pass-through.
    """
    if not ENABLE_EARNINGS_FILTER:
        return False  # Pass through: filter disabled for testing/staging

    # Real calendar lookup logic runs only when flag is True:
    days_to_earnings = get_days_to_next_earnings(epic, target_date)
    if days_to_earnings is not None and -EARNINGS_BLACKOUT_DAYS_POST <= days_to_earnings <= EARNINGS_BLACKOUT_DAYS_PRE:
        return True

    return False
