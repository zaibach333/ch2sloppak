"""Convert raw lyric event lists into Slopsmith lyrics.json format.

Slopsmith lyric entry: {"t": float, "d": float, "w": str}
  t – start time in SECONDS
  d – duration in SECONDS
  w – syllable text

Duration is estimated as the gap to the next syllable, capped at 5 s and
floored at 0.05 s.  The last syllable defaults to 0.5 s.
"""

# Maximum duration for a single syllable (catches very long rests at phrase end)
_MAX_DURATION_S = 5.0
_DEFAULT_LAST_S = 0.5
_MIN_DURATION_S = 0.05


def convert(raw_lyrics):
    """
    raw_lyrics: list of {time_ms: float, text: str}  (sorted by time_ms)

    Returns list of {"t": float, "d": float, "w": str} ready for lyrics.json.
    Returns [] if raw_lyrics is empty.
    """
    if not raw_lyrics:
        return []

    result = []
    for i, lyric in enumerate(raw_lyrics):
        t_s = round(lyric["time_ms"] / 1000.0, 3)

        if i + 1 < len(raw_lyrics):
            gap = (raw_lyrics[i + 1]["time_ms"] - lyric["time_ms"]) / 1000.0
            d_s = min(gap, _MAX_DURATION_S)
        else:
            d_s = _DEFAULT_LAST_S

        d_s = max(d_s, _MIN_DURATION_S)

        result.append({
            "t": t_s,
            "d": round(d_s, 3),
            "w": lyric["text"],
        })

    return result
