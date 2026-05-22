"""Convert raw lyric event lists into Slopsmith lyrics.json format.

Slopsmith lyric entry: {"t": float, "d": float, "w": str}
  t – start time in SECONDS
  d – duration in SECONDS
  w – syllable text

Slopsmith line-break convention (from highway.js):
  w ending with "+"  → end of an authored line (line break after this syllable)
  w ending with "-"  → syllable continues into next with no space (same word)
  fallback: gap > 4s forces a line break when no "+" markers are present

CH lyric special characters and their translation:
  #  – standalone phrase separator → marks the PREVIOUS syllable as line-end (+)
  +  – leading "connect to previous" marker → stripped
  ^  – gentle/connected marker → stripped
  %  – phrase reset → stripped
  $  – gender marker (some charts) → stripped
  -  – trailing word-continues marker → preserved (Slopsmith respects it)
"""

# Maximum duration for a single syllable (catches very long rests at phrase end)
_MAX_DURATION_S = 5.0
_DEFAULT_LAST_S = 0.5
_MIN_DURATION_S = 0.05

_STRIP_CHARS = "^%$"


def _clean(text):
    """Strip CH control characters. Returns empty string for pure control events."""
    text = text.strip().lstrip("+").strip(_STRIP_CHARS).strip()
    # Strip standalone # (phrase marker); embedded # (rare) also removed
    text = text.replace("#", "").strip()
    return text


def _is_phrase_marker(text):
    """True if this event is a CH phrase separator with no display text."""
    return not _clean(text)


def convert(raw_lyrics):
    """
    raw_lyrics: list of {time_ms: float, text: str}  (sorted by time_ms)

    Returns list of {"t": float, "d": float, "w": str} ready for lyrics.json.
    Returns [] if raw_lyrics is empty.
    """
    if not raw_lyrics:
        return []

    # First pass: build cleaned syllable list and flag line-ends.
    # A syllable gets a line-end flag when the next event is a phrase marker (#)
    # or when it's the last syllable in the song.
    syllables = []  # [{time_ms, text, line_end}]

    for lyric in raw_lyrics:
        text = _clean(lyric["text"])
        if _is_phrase_marker(lyric["text"]):
            # Mark the previous syllable as a line-end
            if syllables:
                syllables[-1]["line_end"] = True
        else:
            if text:
                syllables.append({"time_ms": lyric["time_ms"],
                                   "text": text, "line_end": False})

    if not syllables:
        return []

    # Last syllable is always a line-end
    syllables[-1]["line_end"] = True

    # Second pass: emit wire format
    result = []
    for i, syl in enumerate(syllables):
        t_s = round(syl["time_ms"] / 1000.0, 3)

        gap = ((syllables[i + 1]["time_ms"] - syl["time_ms"]) / 1000.0
               if i + 1 < len(syllables) else None)
        d_s = min(gap, _MAX_DURATION_S) if gap is not None else _DEFAULT_LAST_S
        d_s = max(d_s, _MIN_DURATION_S)

        text = syl["text"]
        # Apply Slopsmith line-end marker — don't overwrite an existing "-"
        # (word-continues) since that takes visual priority.
        if syl["line_end"] and not text.endswith("-"):
            text = text.rstrip("+") + "+"

        result.append({"t": t_s, "d": round(d_s, 3), "w": text})

    return result
