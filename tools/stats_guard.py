"""
stats_guard.py — Statistical validity guard for learned patterns.

Prevents APEX from acting on unreliable pattern data by checking:
  1. INSUFFICIENT_DATA: fewer than 20 bets in the relevant category/band
  2. SUSPECT:           win rate > 70% with fewer than 50 bets
                        (likely variance, not genuine edge)
  3. Simplified Bonferroni correction: if ALL categories simultaneously
     show win rate > 65%, flag as potential data-mining artefact.

Returns a validated pattern summary string safe to inject into APEX prompt.
"""

import logging

logger = logging.getLogger(__name__)

# Thresholds
MIN_BETS_FOR_VALID_PATTERN = 20
SUSPECT_WIN_RATE_THRESHOLD = 0.70
SUSPECT_MIN_BETS_THRESHOLD = 50      # below this, >70% WR is flagged SUSPECT
BONFERRONI_CATEGORIES = 3            # number of independent hypotheses tested
BONFERRONI_SUSPICIOUS_WR = 0.65     # if ALL categories beat this, flag it


def validate_patterns(patterns: dict) -> dict:
    """
    Run all validity checks on the patterns dict.

    Returns:
        {
          "valid":    bool,
          "flags":    list[str],
          "warnings": list[str],
        }
    """
    flags: list[str] = []
    warnings: list[str] = []
    total_bets = patterns.get("total_bets", 0)

    # 1. Global insufficient data
    if total_bets < MIN_BETS_FOR_VALID_PATTERN:
        flags.append(f"INSUFFICIENT_DATA ({total_bets}/{MIN_BETS_FOR_VALID_PATTERN} bets)")

    # 2. Per-category SUSPECT and per-band SUSPECT
    by_cat = patterns.get("by_category", {})
    by_band = patterns.get("by_confidence_band", {})

    for name, data in {**by_cat, **by_band}.items():
        w = data.get("wins", 0)
        l = data.get("losses", 0)
        n = w + l
        if n == 0:
            continue
        wr = w / n
        if wr > SUSPECT_WIN_RATE_THRESHOLD and n < SUSPECT_MIN_BETS_THRESHOLD:
            flags.append(
                f"SUSPECT: '{name}' shows {wr*100:.0f}% WR on only {n} bets "
                f"(need {SUSPECT_MIN_BETS_THRESHOLD}+ to trust)"
            )
            warnings.append(f"'{name}' pattern may be variance, not edge.")

    # 3. Simplified Bonferroni: all categories above threshold simultaneously
    cat_win_rates = []
    for name, data in by_cat.items():
        w = data.get("wins", 0)
        l = data.get("losses", 0)
        n = w + l
        if n >= MIN_BETS_FOR_VALID_PATTERN:
            cat_win_rates.append(w / n)

    if (len(cat_win_rates) >= BONFERRONI_CATEGORIES
            and all(wr > BONFERRONI_SUSPICIOUS_WR for wr in cat_win_rates)):
        flags.append(
            f"BONFERRONI_SUSPECT: all {len(cat_win_rates)} categories show "
            f">{BONFERRONI_SUSPICIOUS_WR*100:.0f}% WR simultaneously — may be data artefact"
        )

    valid = not any(f.startswith("INSUFFICIENT_DATA") or f.startswith("SUSPECT") for f in flags)
    return {"valid": valid, "flags": flags, "warnings": warnings}


def get_guarded_summary(patterns: dict, category: str, confidence: int) -> str:
    """
    Return a pattern summary string for injection into APEX prompt.
    Includes validation flags so APEX knows whether to trust the data.

    This replaces get_pattern_summary_for_prompt() from memory.py when
    you want guard-aware output.
    """
    guard = validate_patterns(patterns)
    total = patterns.get("total_bets", 0)

    lines: list[str] = []

    # Prepend any flags/warnings
    if guard["flags"]:
        lines.append("PATTERN VALIDITY WARNINGS:")
        for flag in guard["flags"]:
            lines.append(f"  [{flag}]")
        lines.append("")

    if total < MIN_BETS_FOR_VALID_PATTERN:
        lines.append(f"(Insufficient history: {total}/{MIN_BETS_FOR_VALID_PATTERN} bets needed)")
        return "\n".join(lines)

    # Category stats
    cat_data = patterns.get("by_category", {}).get(category, {})
    if cat_data:
        w, l = cat_data.get("wins", 0), cat_data.get("losses", 0)
        pnl = cat_data.get("total_pnl", 0.0)
        wr = w / (w + l) * 100 if (w + l) > 0 else 0
        trust = "" if guard["valid"] else " [LOW TRUST — see warnings above]"
        lines.append(f"Category '{category}': {wr:.0f}% WR ({w}W/{l}L), P&L ${pnl:.2f}{trust}")

    # Confidence band stats
    if confidence >= 85:
        band = "85+"
    elif confidence >= 75:
        band = "75-84"
    elif confidence >= 65:
        band = "65-74"
    else:
        band = "55-64"

    band_data = patterns.get("by_confidence_band", {}).get(band, {})
    if band_data:
        w, l = band_data.get("wins", 0), band_data.get("losses", 0)
        wr = w / (w + l) * 100 if (w + l) > 0 else 0
        lines.append(f"Confidence band {band}%: {wr:.0f}% WR ({w}W/{l}L)")

    return "\n".join(lines) if lines else "No pattern data available."


def is_pattern_blocked(patterns: dict, category: str) -> bool:
    """
    Returns True if APEX should be blocked from using pattern data for this category.
    Blocking condition: INSUFFICIENT_DATA flag is present.
    """
    guard = validate_patterns(patterns)
    return any("INSUFFICIENT_DATA" in f for f in guard["flags"])
