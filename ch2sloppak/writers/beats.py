"""Generate beat-grid data for the Slopsmith beats.json sloppak entry.

Beat wire format: [{"t": seconds, "m": measure_or_minus1}, ...]
  t  – beat time in seconds
  m  – measure number (0-indexed) when this beat is a downbeat; -1 otherwise

rs2gp.py (slopsmith-plugin-tabview) reads song.beats to place notes in the
correct measures of the GP5 file.  Without beats it falls back to a single
measure spanning the entire song.

Drum encoding note
──────────────────
Drum notes use  midi = string * 24 + fret  (GM percussion map).
This is the standard pitch convention expected by both slopsmith-plugin-drums
(highway lane view) and slopsmith-plugin-tabview (GP5 staff notation).
Standard GM drum MIDI values:  36 kick, 38 snare, 42 hi-hat, 45 tom-2,
48 tom-1, 49 crash, 51 ride, 41 tom-3.
"""

import json


def generate(ts_events, ppq, ticks_to_ms, max_tick):
    """
    Build the beat list.

    ts_events  – [(tick, numerator, denominator_int), ...] sorted ascending.
                 denominator_int is the actual denominator (e.g. 4 for 4/4,
                 8 for 6/8), NOT a power-of-two index.
    ppq        – ticks per quarter note (MIDI PPQ / chart Resolution)
    ticks_to_ms – callable: tick → float ms
    max_tick   – last note/event tick; beats are generated a few measures past this

    Returns list of {"t": float, "m": int} dicts.
    """
    if not ts_events or ts_events[0][0] != 0:
        ts_events = [(0, 4, 4)] + list(ts_events)

    beats = []
    measure_num = 0
    beat_in_measure = 0
    ts_idx = 0
    numerator, denominator = ts_events[0][1], ts_events[0][2]
    beat_ticks = max(1, ppq * 4 // denominator)  # quarter-note = ppq, eighth = ppq//2 …

    tick = 0
    limit = max_tick + beat_ticks * (numerator * 4)  # ~4 extra measures past end

    while tick <= limit:
        # Advance time-signature on TS change boundaries
        while ts_idx + 1 < len(ts_events) and ts_events[ts_idx + 1][0] <= tick:
            ts_idx += 1
            new_num, new_den = ts_events[ts_idx][1], ts_events[ts_idx][2]
            if (new_num, new_den) != (numerator, denominator):
                numerator, denominator = new_num, new_den
                beat_ticks = max(1, ppq * 4 // denominator)
                beat_in_measure = 0  # realign beat counter on TS change

        t_sec = round(ticks_to_ms(tick) / 1000.0, 3)

        if beat_in_measure == 0:
            beats.append({"time": t_sec, "measure": measure_num})
            measure_num += 1
        else:
            beats.append({"time": t_sec, "measure": -1})

        beat_in_measure = (beat_in_measure + 1) % numerator
        tick += beat_ticks

    return beats


def to_json_bytes(beats):
    return json.dumps(beats, separators=(",", ":")).encode()
