"""Convert parsed CH note data into Slopsmith arrangement wire-format dicts.

Accepts multi-difficulty input: {diff_int: [event_dicts]}
  diff 3 = Expert, 2 = Hard, 1 = Medium, 0 = Easy

Top-level `notes` always contains the highest available difficulty.
`phrases` is populated (one whole-song phrase) only when 2+ difficulties exist,
which enables Slopsmith's difficulty slider.

Drum GM MIDI encoding:
  string = midi // 24,  fret = midi % 24   (slopsmith-plugin-drums formula)

Guitar lane mapping:
  CH lane 0-4 → Slopsmith string 0-4, fret 0  (5 vertical highway lanes)
  CH open (7) → string 5, fret 0
"""

# ---------------------------------------------------------------------------
# Drum GM MIDI mapping
# ---------------------------------------------------------------------------

# (ch_note, cymbal_flag) → GM MIDI note
DRUM_GM_MAP = {
    (0,  False): 36,   # Kick
    (1,  False): 38,   # Red – Snare
    (2,  True):  42,   # Yellow Cymbal – Hi-Hat (closed)
    (2,  False): 48,   # Yellow Tom  – Tom 1 (high)
    (3,  True):  51,   # Blue Cymbal – Ride
    (3,  False): 45,   # Blue Tom    – Tom 2 (mid)
    (4,  True):  49,   # Green Cymbal – Crash
    (4,  False): 41,   # Green Tom   – Tom 3 (low)
    (32, False): 36,   # 2× Kick → same Kick lane
}

# GM MIDI → (string, fret, is_cymbal) for treble-clef staff display in tab view.
# rs2gp.py (unmodified) treats this arrangement as a 6-string guitar, so each
# string/fret pair is chosen so that:
#   pitch = GUITAR_STANDARD[5 - string] + fret
# lands on the correct percussion staff position.
# GUITAR_STANDARD = [64, 59, 55, 50, 45, 40]
#
# Same-lane pairs (green/blue/yellow tom vs cymbal) share a string because
# CH never places a tom and cymbal on the same lane at the same tick.
DRUM_SCORE_MAP = {
    36: (0, 13, False),  # Kick        → F4  (MIDI 65, space 1, between lines 1–2)
    38: (1, 15, False),  # Snare       → C5  (MIDI 72, space 3, between lines 3–4)
    41: (2,  7, False),  # Floor tom   → A4  (MIDI 69, space 2, between lines 2–3)
    45: (3,  7, False),  # Blue tom    → D5  (MIDI 74, line 4)
    48: (4,  5, False),  # Yellow tom  → E5  (MIDI 76, space 4, between lines 4–5)
    42: (4,  8, True),   # Hi-hat      → G5  (MIDI 79, space above staff, ×)
    51: (3, 10, True),   # Ride        → F5  (MIDI 77, line 5 top, ×)
    49: (5,  5, True),   # Crash       → A5  (MIDI 81, ledger line above staff, ×)
}


def _midi_to_sf(midi):
    return midi // 24, midi % 24


# ---------------------------------------------------------------------------
# Wire-format helpers
# ---------------------------------------------------------------------------

def _base_note(time_ms, string, fret, sustain_ms=0.0):
    return {
        "t":   round(time_ms / 1000.0, 3),
        "s":   string,
        "f":   fret,
        "sus": round(sustain_ms / 1000.0, 3),
        "sl":  -1,
        "slu": -1,
        "bn":  0.0,
        "ho":  False,
        "po":  False,
        "hm":  False,
        "hp":  False,
        "pm":  False,
        "mt":  False,
        "vb":  False,
        "tr":  False,
        "ac":  False,
        "tp":  False,
    }


def _arrangement_shell(name, tuning):
    return {
        "name":       name,
        "tuning":     tuning,
        "capo":       0,
        "notes":      [],
        "chords":     [],
        "anchors":    [],
        "handshapes": [],
        "templates":  [],
        "phrases":    None,
    }


# ---------------------------------------------------------------------------
# Phrases / difficulty builder
# ---------------------------------------------------------------------------

def _build_phrases(wire_by_diff):
    """
    Wrap all difficulty levels into a single phrase covering the whole song.
    Only called when 2+ difficulties are present.

    Difficulties are normalised to 0-100 so the slider spans the full range.
    RS songs have max_difficulty in the hundreds; using raw CH values (0-3)
    means the slider hits Expert after only ~1% of travel.
    """
    all_times = [n["t"] for notes in wire_by_diff.values() for n in notes]
    end_time = (max(all_times) + 0.5) if all_times else 0.5

    diffs  = sorted(wire_by_diff.keys())
    n_diff = len(diffs)
    # Map e.g. [0,1,2,3] → [0, 33, 67, 100]
    remap  = {d: round(i * 100 / (n_diff - 1)) for i, d in enumerate(diffs)}

    levels = [
        {
            "difficulty": remap[diff],
            "notes":      [n.copy() for n in wire_by_diff[diff]],
            "chords":     [],
            "anchors":    [],
            "handshapes": [],
        }
        for diff in diffs
    ]

    return [{
        "start_time":      0.0,
        "end_time":        round(end_time, 3),
        "max_difficulty":  100,
        "levels":          levels,
    }]


# ---------------------------------------------------------------------------
# Drums
# ---------------------------------------------------------------------------

def _drum_hits_to_wire(hits):
    notes = []
    for hit in hits:
        midi = DRUM_GM_MAP.get((hit["ch_note"], hit["cymbal_flag"]))
        if midi is None:
            midi = DRUM_GM_MAP.get((hit["ch_note"], False))
        if midi is None:
            continue
        s, f = _midi_to_sf(midi)
        notes.append(_base_note(hit["time_ms"], s, f))
    return notes


def _drum_hits_to_wire_score(hits):
    notes = []
    for hit in hits:
        midi = DRUM_GM_MAP.get((hit["ch_note"], hit["cymbal_flag"]))
        if midi is None:
            midi = DRUM_GM_MAP.get((hit["ch_note"], False))
        if midi is None:
            continue
        mapping = DRUM_SCORE_MAP.get(midi)
        if mapping is None:
            continue
        s, f, cymbal = mapping
        note = _base_note(hit["time_ms"], s, f)
        if cymbal:
            note["mt"] = True
        notes.append(note)
    return notes


def convert_drums_score(difficulties_dict):
    """
    Second drums arrangement encoded for correct treble-clef staff positions
    in the tab view.  Not compatible with the drum highway (different encoding).
    """
    arr = _arrangement_shell("drums_score", [0, 0, 0, 0, 0, 0])

    wire_by_diff = {diff: _drum_hits_to_wire_score(hits)
                    for diff, hits in difficulties_dict.items()
                    if hits}

    if not wire_by_diff:
        return arr

    max_diff = max(wire_by_diff)
    arr["notes"] = wire_by_diff[max_diff]

    if len(wire_by_diff) > 1:
        arr["phrases"] = _build_phrases(wire_by_diff)

    return arr


def convert_drums(difficulties_dict):
    """
    difficulties_dict: {diff_int: [drum_hit_dicts]}
    Returns a complete arrangement dict.
    """
    arr = _arrangement_shell("drums", [0, 0, 0, 0, 0, 0])

    wire_by_diff = {diff: _drum_hits_to_wire(hits)
                    for diff, hits in difficulties_dict.items()
                    if hits}

    if not wire_by_diff:
        return arr

    max_diff = max(wire_by_diff)
    arr["notes"] = wire_by_diff[max_diff]

    # Wide anchor so the default highway fret range covers all GM drum fret
    # values (kick f=12, snare f=14, hi-hat f=18, toms f=0/17/21, etc.).
    # The slopsmith-plugin-drums lane view ignores anchors entirely; this only
    # affects the fallback guitar-style highway if that plugin is absent.
    # Anchors must also appear in every phrase level because when phrases are
    # active Slopsmith uses _filteredAnchors (built from phrase-level anchors)
    # and ignores the top-level anchors array.
    all_notes_flat = [n for notes in wire_by_diff.values() for n in notes]
    anchor = None
    if all_notes_flat:
        max_fret = max(n["f"] for n in all_notes_flat)
        anchor = {"time": 0.0, "fret": 0, "width": max_fret + 4}
        arr["anchors"] = [anchor]

    if len(wire_by_diff) > 1:
        phrases = _build_phrases(wire_by_diff)
        if anchor:
            for phrase in phrases:
                for level in phrase["levels"]:
                    level["anchors"] = [anchor]
        arr["phrases"] = phrases

    return arr


# ---------------------------------------------------------------------------
# Piano / Pro Keys
# ---------------------------------------------------------------------------

# Tuning offsets so each Slopsmith string covers one octave (C-B) of piano range.
# Formula: stored_pitch = GUITAR_STANDARD[5-s] + tuning[s] + fret
# alphaTab +12 display transposition: display = stored + 12
# Stored bases (C1-C6 = MIDI 24-84) → display bases (C2-C7 = MIDI 36-96)
PIANO_TUNING = [-16, -9, -2, 5, 13, 20]
_PIANO_STORED_BASES = [24, 36, 48, 60, 72, 84]  # C1–C6


def _piano_pitch_to_sf(midi_pitch):
    stored = midi_pitch - 12  # compensate for alphaTab +12 display shift
    for s in range(5, -1, -1):
        if stored >= _PIANO_STORED_BASES[s]:
            return s, stored - _PIANO_STORED_BASES[s]
    return 0, max(0, stored - _PIANO_STORED_BASES[0])


def _piano_notes_to_wire(piano_notes):
    wire = []
    for note in piano_notes:
        s, f = _piano_pitch_to_sf(note["pitch"])
        wire.append(_base_note(note["time_ms"], s, f, note["sustain_ms"]))
    return wire


def convert_keys(difficulties_dict):
    """
    difficulties_dict: {diff_int: [{time_ms, pitch, sustain_ms}, ...]}
    Returns a complete arrangement dict with piano pitch encoding.
    """
    arr = _arrangement_shell("keys", PIANO_TUNING)

    wire_by_diff = {diff: _piano_notes_to_wire(notes)
                    for diff, notes in difficulties_dict.items()
                    if notes}

    if not wire_by_diff:
        return arr

    max_diff = max(wire_by_diff)
    arr["notes"] = wire_by_diff[max_diff]

    if len(wire_by_diff) > 1:
        arr["phrases"] = _build_phrases(wire_by_diff)

    return arr


# ---------------------------------------------------------------------------
# Guitar / Bass
# ---------------------------------------------------------------------------

# CH lane → (string, fret)
# String follows lane order (green=0 … orange=4) to match highway lane colours.
# Fret follows the colour order red=1, yellow=2, blue=3, orange=4, green=5.
GUITAR_LANE_MAP = {0: (0, 5), 1: (1, 1), 2: (2, 2), 3: (3, 3), 4: (4, 4), 7: (5, 1)}


def _guitar_notes_to_wire(guitar_notes):
    wire = []
    for event in guitar_notes:
        for lane in event["frets"]:
            string, fret = GUITAR_LANE_MAP.get(lane, (lane, 0))
            note = _base_note(event["time_ms"], string, fret, event["sustain_ms"])
            note["ho"] = event["ho"]
            note["tp"] = event["tap"]
            wire.append(note)
    return wire


def convert_guitar(difficulties_dict, arrangement_name="lead"):
    """
    difficulties_dict: {diff_int: [guitar_note_dicts]}
    Returns a complete arrangement dict.
    """
    arr = _arrangement_shell(arrangement_name, [0, 0, 0, 0, 0, 0])

    wire_by_diff = {diff: _guitar_notes_to_wire(notes)
                    for diff, notes in difficulties_dict.items()
                    if notes}

    if not wire_by_diff:
        return arr

    max_diff = max(wire_by_diff)
    arr["notes"] = wire_by_diff[max_diff]

    if len(wire_by_diff) > 1:
        arr["phrases"] = _build_phrases(wire_by_diff)

    return arr
