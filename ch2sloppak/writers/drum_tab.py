"""Convert CH drum hits to drum_tab.json for slopsmith-plugin-drum-highway-3d.

Format: {"hits": [{t, p, v?, g?, f?}, ...]} sorted by t.
  t  – time in seconds
  p  – piece ID (kick | snare | hh_closed | tom_hi | tom_mid | tom_floor |
                  ride | crash_l)
  v  – velocity 0-127 (optional; omitted when default 100)
  g  – ghost flag (optional)
  f  – flam flag (optional)

Uses highest available difficulty.  The existing `drums` / `drums_score`
arrangements are kept unchanged; this file is an additional output consumed
only by the 3D highway plugin.
"""

# (ch_note, cymbal_flag) → piece ID
# Derived from arrangement.py DRUM_GM_MAP composed with screen.js MIDI_TO_PIECE.
_CH_TO_PIECE = {
    (0,  False): 'kick',
    (1,  False): 'snare',
    (2,  True):  'hh_closed',
    (2,  False): 'tom_hi',
    (3,  True):  'ride',
    (3,  False): 'tom_mid',
    (4,  True):  'crash_l',
    (4,  False): 'tom_floor',
    (32, False): 'kick',        # 2× kick
}


# Guitar CH lane → piece ID.
# Colors chosen to match Clone Hero's highway lane colors exactly:
#   Green=0 → tom_hi   (palette[4] = green)
#   Red=1   → snare    (palette[0] = red)
#   Yellow=2 → crash_l (palette[1] = yellow)
#   Blue=3  → tom_mid  (palette[2] = blue)
#   Orange=4 → ride    (palette[3] = orange)
#   Open=7  → kick     (full-width amber bar)
_GUITAR_LANE_TO_PIECE = {
    0: "tom_hi",
    1: "snare",
    2: "crash_l",
    3: "tom_mid",
    4: "ride",
    7: "kick",
}


_DIFF_NAMES = {3: "expert", 2: "hard", 1: "medium", 0: "easy"}


def convert_per_diff(difficulties_dict):
    """One drum_tab dict per available difficulty. Returns {arr_id: drum_tab_dict}."""
    result = {}
    for diff, hits in difficulties_dict.items():
        if not hits:
            continue
        tab = convert({diff: hits})
        if tab:
            name = _DIFF_NAMES.get(diff, str(diff))
            result[f"drums-{name}"] = tab
    return result


def convert(difficulties_dict):
    """
    Convert CH drum hit dicts to drum_tab format.

    difficulties_dict: {diff_int: [drum_hit_dicts]}
    Returns {"hits": [...]} or None if no hits.
    """
    if not difficulties_dict:
        return None

    max_diff = max(difficulties_dict)
    raw_hits = difficulties_dict[max_diff]
    if not raw_hits:
        return None

    hits = []
    for h in raw_hits:
        piece = _CH_TO_PIECE.get((h["ch_note"], h["cymbal_flag"]))
        if piece is None:
            piece = _CH_TO_PIECE.get((h["ch_note"], False))
        if piece is None:
            continue
        entry = {"t": round(h["time_ms"] / 1000.0, 3), "p": piece}
        hits.append(entry)

    if not hits:
        return None

    hits.sort(key=lambda x: x["t"])
    return {"hits": hits}


def convert_guitar(difficulties_dict):
    """
    Convert CH guitar note events to drum_tab format using lane-color mapping.

    Guitar lanes map to drum pieces whose colors match Clone Hero's highway:
    green=tom_hi, red=snare, yellow=crash_l, blue=tom_mid, orange=ride, open=kick.

    HOPO / tap notes are flagged as ghost (g: true) so they render as hollow
    rings — visually lighter, matching their "no strum" nature.

    difficulties_dict: {diff_int: [guitar_note_dicts]}
    Returns {"hits": [...]} or None if no hits.
    """
    if not difficulties_dict:
        return None

    max_diff = max(difficulties_dict)
    notes = difficulties_dict[max_diff]
    if not notes:
        return None

    hits = []
    for note in notes:
        is_light = note.get("ho") or note.get("tap")
        for lane in note["frets"]:
            piece = _GUITAR_LANE_TO_PIECE.get(lane)
            if piece is None:
                continue
            entry = {"t": round(note["time_ms"] / 1000.0, 3), "p": piece}
            if is_light:
                entry["g"] = True
            hits.append(entry)

    if not hits:
        return None

    hits.sort(key=lambda x: x["t"])
    return {"hits": hits}
