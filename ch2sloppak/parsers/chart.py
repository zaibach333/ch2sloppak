"""Parse Clone Hero .chart files into structured multi-difficulty note data.

Sections handled:
  [Song]       – resolution, offset, inline metadata
  [SyncTrack]  – BPM (B) events → tick→ms converter
  [Events]     – lyric syllables (E "lyric …")
  [Expert/Hard/Medium/Easy][Single/Bass/Drums]  – note events per difficulty
"""

import re
from bisect import bisect_right

# ---------------------------------------------------------------------------
# Section → (track_id, difficulty_int) maps
# ---------------------------------------------------------------------------

GUITAR_SECTIONS = {
    "ExpertSingle":    ("lead", 3),
    "HardSingle":      ("lead", 2),
    "MediumSingle":    ("lead", 1),
    "EasySingle":      ("lead", 0),
    "ExpertBass":      ("bass", 3),
    "HardBass":        ("bass", 2),
    "MediumBass":      ("bass", 1),
    "EasyBass":        ("bass", 0),
    "ExpertDoubleBass":("bass", 3),  # some co-op charts use this for bass
    "ExpertRhythm":    ("bass", 3),  # rhythm guitar → bass track
}
DRUMS_SECTIONS = {
    "ExpertDrums":  ("drums", 3),
    "HardDrums":    ("drums", 2),
    "MediumDrums":  ("drums", 1),
    "EasyDrums":    ("drums", 0),
}

# Modifier note numbers for 5-fret guitar
FORCE_STRUM_NOTE = 5
FORCE_HOPO_NOTE  = 5   # same byte, direction depends on current note type — treated as toggle
FORCE_TAP_NOTE   = 6

# Cymbal modifier note numbers for pro drums: modifier → lane index (2/3/4)
CYMBAL_FLAG_TO_LANE = {66: 2, 67: 3, 68: 4}

# HOPO auto-detect threshold fraction of resolution
HOPO_THRESHOLD_FACTOR = 65 / 192


# ---------------------------------------------------------------------------
# Section splitter
# ---------------------------------------------------------------------------

def _split_sections(content):
    sections = {}
    current, lines = None, []
    for raw in content.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            if current is not None:
                sections[current] = lines
            current, lines = line[1:-1], []
        elif line not in ("{", "}") and line:
            lines.append(line)
    if current is not None:
        sections[current] = lines
    return sections


# ---------------------------------------------------------------------------
# Timing helpers
# ---------------------------------------------------------------------------

def _parse_sync_track(sync_lines, resolution):
    """Return (bpm_events, ts_events) from SyncTrack lines."""
    bpm_events = []
    ts_events  = []
    for line in sync_lines:
        m = re.match(r"(\d+)\s*=\s*B\s+(\d+)", line)
        if m:
            bpm_events.append((int(m.group(1)), int(m.group(2)) / 1000.0))
            continue
        m = re.match(r"(\d+)\s*=\s*TS\s+(\d+)(?:\s+(\d+))?", line)
        if m:
            tick = int(m.group(1))
            num  = int(m.group(2))
            den_pow = int(m.group(3)) if m.group(3) else 2  # default: quarter note
            ts_events.append((tick, num, 2 ** den_pow))
    bpm_events.sort()
    ts_events.sort()
    return bpm_events, ts_events


def _build_time_map(sync_lines, resolution):
    bpm_events, _ts = _parse_sync_track(sync_lines, resolution)
    if not bpm_events or bpm_events[0][0] != 0:
        bpm_events.insert(0, (0, 120.0))

    ticks, bpms, cumulative = [], [], []
    cum_ms = 0.0
    for i, (tick, bpm) in enumerate(bpm_events):
        if i > 0:
            pt, pb = bpm_events[i - 1]
            cum_ms += (tick - pt) / resolution * (60_000.0 / pb)
        ticks.append(tick)
        bpms.append(bpm)
        cumulative.append(cum_ms)
    return ticks, bpms, cumulative


def _make_converter(ticks, bpms, cumulative, resolution):
    def ticks_to_ms(tick):
        idx = max(0, bisect_right(ticks, tick) - 1)
        base_tick = ticks[idx]
        return round(cumulative[idx] + (tick - base_tick) / resolution * (60_000.0 / bpms[idx]), 3)
    return ticks_to_ms


# ---------------------------------------------------------------------------
# Raw event parser
# ---------------------------------------------------------------------------

def _parse_raw_note_events(lines):
    events = []
    for line in lines:
        m = re.match(r"(\d+)\s*=\s*N\s+(\d+)\s+(\d+)", line)
        if m:
            events.append((int(m.group(1)), int(m.group(2)), int(m.group(3))))
    return sorted(events)


def _group_by_tick(events):
    grouped = {}
    for tick, note_type, length in events:
        grouped.setdefault(tick, []).append((note_type, length))
    return grouped


# ---------------------------------------------------------------------------
# Guitar processing (single difficulty)
# ---------------------------------------------------------------------------

def _process_guitar(raw_events, ticks_to_ms, resolution):
    grouped = _group_by_tick(raw_events)
    hopo_threshold = int(HOPO_THRESHOLD_FACTOR * resolution)
    result = []
    prev_tick, prev_frets = None, []

    for tick in sorted(grouped):
        events_at_tick = grouped[tick]
        note_types = {nt for nt, _ in events_at_tick}

        force_hopo = FORCE_HOPO_NOTE in note_types
        is_tap     = FORCE_TAP_NOTE  in note_types

        fret_events = [(nt, ln) for nt, ln in events_at_tick if nt in (0, 1, 2, 3, 4, 7)]
        if not fret_events:
            continue

        frets = sorted({nt for nt, _ in fret_events})
        max_length = max(ln for _, ln in fret_events)

        auto_hopo = False
        if prev_tick is not None and not is_tap and not force_hopo:
            if (tick - prev_tick) <= hopo_threshold:
                if len(frets) == 1 and frets != prev_frets:
                    auto_hopo = True

        result.append({
            "tick":        tick,
            "time_ms":     ticks_to_ms(tick),
            "frets":       frets,
            "length_ticks": max_length,
            "sustain_ms":  round(ticks_to_ms(tick + max_length) - ticks_to_ms(tick), 3),
            "ho":          is_tap or force_hopo or auto_hopo,
            "tap":         is_tap,
        })
        prev_tick, prev_frets = tick, frets

    return result


# ---------------------------------------------------------------------------
# Drums processing (single difficulty)
# ---------------------------------------------------------------------------

def _process_drums(raw_events, ticks_to_ms):
    grouped = _group_by_tick(raw_events)
    result = []

    for tick in sorted(grouped):
        events_at_tick = grouped[tick]
        note_types = {nt for nt, _ in events_at_tick}
        cymbal_lanes = {lane for flag, lane in CYMBAL_FLAG_TO_LANE.items() if flag in note_types}

        for note_type, _length in events_at_tick:
            if note_type in CYMBAL_FLAG_TO_LANE:
                continue
            if note_type not in (0, 1, 2, 3, 4, 32):
                continue
            result.append({
                "tick":        tick,
                "time_ms":     ticks_to_ms(tick),
                "ch_note":     note_type,
                "cymbal_flag": note_type in cymbal_lanes,
            })

    result.sort(key=lambda e: (e["tick"], e["ch_note"]))
    return result


# ---------------------------------------------------------------------------
# Lyrics processing
# ---------------------------------------------------------------------------

def _parse_lyrics(events_lines, ticks_to_ms):
    lyrics = []
    for line in events_lines:
        # Match: TICK = E "lyric <text>"
        m = re.match(r'(\d+)\s*=\s*E\s+"lyric\s+(.+?)"', line, re.IGNORECASE)
        if m:
            lyrics.append({
                "tick":    int(m.group(1)),
                "time_ms": ticks_to_ms(int(m.group(1))),
                "text":    m.group(2).strip(),
            })
    return sorted(lyrics, key=lambda l: l["tick"])


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse(filepath):
    """
    Parse a .chart file.  Returns:
      {
        "resolution": int,
        "offset":     float,
        "song_meta":  dict,
        "ticks_to_ms": callable,
        "tracks": {
          "lead":  {diff_int: [guitar_note_dict, ...]},
          "bass":  {diff_int: [guitar_note_dict, ...]},
          "drums": {diff_int: [drum_hit_dict,   ...]},
        },
        "lyrics": [{tick, time_ms, text}, ...],
      }
    """
    with open(filepath, "r", encoding="utf-8-sig", errors="replace") as f:
        content = f.read()

    sections = _split_sections(content)

    song_meta = {}
    for line in sections.get("Song", []):
        if "=" in line:
            key, _, val = line.partition("=")
            song_meta[key.strip()] = val.strip().strip('"')

    resolution   = int(song_meta.get("Resolution", 192))
    offset       = float(song_meta.get("Offset", 0))
    sync_lines   = sections.get("SyncTrack", [])
    _bpm_events, ts_events = _parse_sync_track(sync_lines, resolution)
    ticks_arr, bpms_arr, cumul_arr = _build_time_map(sync_lines, resolution)
    ticks_to_ms  = _make_converter(ticks_arr, bpms_arr, cumul_arr, resolution)

    tracks = {}

    # Guitar / bass sections
    for section_name, (track_id, diff) in {**GUITAR_SECTIONS}.items():
        if section_name not in sections:
            continue
        raw = _parse_raw_note_events(sections[section_name])
        events = _process_guitar(raw, ticks_to_ms, resolution)
        if events:
            tracks.setdefault(track_id, {})[diff] = events

    # Drums sections
    for section_name, (track_id, diff) in DRUMS_SECTIONS.items():
        if section_name not in sections:
            continue
        raw = _parse_raw_note_events(sections[section_name])
        events = _process_drums(raw, ticks_to_ms)
        if events:
            tracks.setdefault(track_id, {})[diff] = events

    lyrics = _parse_lyrics(sections.get("Events", []), ticks_to_ms)

    # Largest tick seen anywhere — used by ch2sloppak.py to bound beat generation
    max_tick = 0
    for section_lines in sections.values():
        for line in section_lines:
            m = re.match(r"(\d+)", line)
            if m:
                t = int(m.group(1))
                if t > max_tick:
                    max_tick = t

    return {
        "resolution":  resolution,
        "offset":      offset,
        "song_meta":   song_meta,
        "ticks_to_ms": ticks_to_ms,
        "ts_events":   ts_events,   # [(tick, numerator, denominator_int), ...]
        "max_tick":    max_tick,
        "tracks":      tracks,
        "lyrics":      lyrics,
    }
