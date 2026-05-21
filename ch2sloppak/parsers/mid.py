"""Parse Clone Hero .mid files into the same multi-difficulty structure as chart.py.

Pure-Python binary MIDI parser — no external dependencies.

MIDI encoding reference (Clone Hero / Guitar Hero / Rock Band):
  Guitar/Bass per difficulty:
    Expert base 96: Green=96, Red=97, Yellow=98, Blue=99, Orange=100,
                    Open=95, ForceHOPO=101, ForceStrum=102
    Hard   base 84 / Medium base 72 / Easy base 60  (same offsets)
    Tap phrase = note 104 (sustained, applies to all difficulties)

  Drums per difficulty (4-lane Pro):
    Expert base 96: Kick=96, Red=97, Yellow=98, Blue=99, Green=100, 2xKick=95
    Hard base 84 / Medium base 72 / Easy base 60
    Tom markers (sustained): 110=Yellow tom, 111=Blue tom, 112=Green tom
    Default is CYMBAL; tom marker active → that lane is TOM for that tick.

  Lyrics: Lyric meta events (0x05) on track "PART VOCALS" or "VOCALS".
"""

import struct
from bisect import bisect_right

HOPO_THRESHOLD_FACTOR = 65 / 192


# ---------------------------------------------------------------------------
# Binary MIDI reader
# ---------------------------------------------------------------------------

def _read_vlq(data, pos):
    """Read a MIDI variable-length quantity. Returns (value, new_pos)."""
    val = 0
    while pos < len(data):
        b = data[pos]; pos += 1
        val = (val << 7) | (b & 0x7F)
        if not (b & 0x80):
            return val, pos
    return val, pos


def _parse_midi_binary(path):
    """
    Parse a .mid file.

    Returns:
      ppq        – ticks per quarter note
      raw_tracks – list of event lists, one per MIDI track
                   each event: ('tempo'|'name'|'lyric'|'text'|'note_on'|'note_off', abs_tick, ...)
    """
    with open(path, "rb") as f:
        data = f.read()

    pos = 0

    # --- MThd header ---
    if data[pos:pos+4] != b"MThd":
        raise ValueError("Not a MIDI file (missing MThd)")
    pos += 4
    hdr_len = struct.unpack(">I", data[pos:pos+4])[0]; pos += 4
    _fmt, n_trk, ppq = struct.unpack(">HHH", data[pos:pos+6])
    if ppq & 0x8000:
        raise ValueError("SMPTE timecode MIDI not supported")
    pos += hdr_len

    raw_tracks = []

    for _ in range(n_trk):
        if pos + 8 > len(data) or data[pos:pos+4] != b"MTrk":
            break
        pos += 4
        trk_len = struct.unpack(">I", data[pos:pos+4])[0]; pos += 4
        trk_end = pos + trk_len
        trk_data = data[pos:trk_end]; pos = trk_end

        events = []
        tp, abs_tick, status = 0, 0, 0

        while tp < len(trk_data):
            delta, tp = _read_vlq(trk_data, tp)
            if tp >= len(trk_data):
                break
            abs_tick += delta
            b = trk_data[tp]

            if b == 0xFF:  # meta event
                tp += 1
                if tp >= len(trk_data): break
                mtype = trk_data[tp]; tp += 1
                mlen, tp = _read_vlq(trk_data, tp)
                mdata = trk_data[tp:tp+mlen]; tp += mlen
                status = 0  # meta clears running status

                if mtype == 0x51 and mlen >= 3:
                    tempo = (mdata[0] << 16) | (mdata[1] << 8) | mdata[2]
                    events.append(("tempo", abs_tick, tempo))
                elif mtype == 0x58 and mlen >= 2:
                    num = mdata[0]
                    den = 2 ** mdata[1]  # denominator stored as power of 2
                    events.append(("timesig", abs_tick, num, den))
                elif mtype == 0x03:
                    events.append(("name", abs_tick, mdata.decode("utf-8", "replace").rstrip("\x00")))
                elif mtype == 0x05:
                    events.append(("lyric", abs_tick, mdata.decode("utf-8", "replace").rstrip("\x00")))
                elif mtype == 0x01:
                    events.append(("text", abs_tick, mdata.decode("utf-8", "replace").rstrip("\x00")))
                elif mtype == 0x2F:
                    break  # end of track

            elif b in (0xF0, 0xF7):  # sysex
                tp += 1
                slen, tp = _read_vlq(trk_data, tp)
                tp += slen
                status = 0

            else:  # MIDI channel event (possibly with running status)
                if b & 0x80:
                    status = b; tp += 1

                if not status or tp >= len(trk_data):
                    tp += 1; continue

                st = status & 0xF0
                if st in (0x80, 0x90):
                    if tp + 1 >= len(trk_data): break
                    note, vel = trk_data[tp], trk_data[tp+1]; tp += 2
                    on = (st == 0x90 and vel > 0)
                    events.append(("note_on" if on else "note_off", abs_tick, note, vel))
                elif st in (0xA0, 0xB0, 0xE0):
                    tp += 2
                elif st in (0xC0, 0xD0):
                    tp += 1
                else:
                    tp += 1  # unknown, skip

        raw_tracks.append(events)

    return ppq, raw_tracks


# ---------------------------------------------------------------------------
# Tempo / timing
# ---------------------------------------------------------------------------

def _build_converter(all_tracks, ppq):
    """Build a tick→ms converter from tempo events."""
    tempos = []
    for events in all_tracks:
        for evt in events:
            if evt[0] == "tempo":
                tempos.append((evt[1], evt[2]))
    tempos.sort()
    if not tempos or tempos[0][0] != 0:
        tempos.insert(0, (0, 500_000))

    ticks_list, us_list, cum_list = [], [], []
    cum_ms = 0.0
    for i, (tick, tempo_us) in enumerate(tempos):
        if i > 0:
            pt, pu = tempos[i-1]
            cum_ms += (tick - pt) / ppq * (pu / 1000.0)
        ticks_list.append(tick)
        us_list.append(tempo_us)
        cum_list.append(cum_ms)

    def ticks_to_ms(tick):
        idx = max(0, bisect_right(ticks_list, tick) - 1)
        base = ticks_list[idx]
        return round(cum_list[idx] + (tick - base) / ppq * (us_list[idx] / 1000.0), 3)

    return ticks_to_ms


# ---------------------------------------------------------------------------
# Sustained-note range tracker
# ---------------------------------------------------------------------------

class _ActiveNotes:
    """Track note on/off pairs to produce (start_tick, end_tick) ranges."""
    def __init__(self):
        self._active = {}        # note → start_tick
        self._ranges = {}        # note → [(start, end)]

    def on(self, tick, note):
        self._active[note] = tick

    def off(self, tick, note):
        start = self._active.pop(note, None)
        if start is not None:
            self._ranges.setdefault(note, []).append((start, tick))

    def close_all(self, end_tick):
        for note, start in list(self._active.items()):
            self._ranges.setdefault(note, []).append((start, end_tick))
        self._active.clear()

    def active_at(self, note, tick):
        for s, e in self._ranges.get(note, []):
            if s <= tick < e:
                return True
        return False


# ---------------------------------------------------------------------------
# Drum extractor
# ---------------------------------------------------------------------------

# Difficulty base MIDI notes (kick note)
DRUM_BASES = {3: 96, 2: 84, 1: 72, 0: 60}
# offset within difficulty: kick=0, red=1, yellow=2, blue=3, green=4, 2x=-1
# Tom marker notes (sustained): 110=yellow, 111=blue, 112=green
TOM_MARKERS = {110: 2, 111: 3, 112: 4}  # note → lane index


def _build_note_to_diff_lane_drums():
    """Return dict: midi_note → (difficulty, lane_index)."""
    m = {}
    for diff, base in DRUM_BASES.items():
        for lane in range(5):           # 0=kick,1=red,2=yellow,3=blue,4=green
            m[base + lane] = (diff, lane)
        m[base - 1] = (diff, 32)        # 2x kick → ch_note 32
    return m


_DRUM_NOTE_MAP = _build_note_to_diff_lane_drums()
_TOM_MARKER_FOR_LANE = {2: 110, 3: 111, 4: 112}


def _extract_drum_notes(events, ticks_to_ms):
    """
    Returns {diff_int: [drum_hit_dict, ...]}

    In MIDI 4-lane pro drums: yellow/blue/green are CYMBALS by default.
    A sustained tom marker note (110/111/112) active at a drum hit tick
    flips that lane to TOM for that tick.
    """
    tracker = _ActiveNotes()

    # First pass: collect tom marker ranges
    for evt in events:
        if evt[0] == "note_on" and evt[2] in TOM_MARKERS:
            tracker.on(evt[1], evt[2])
        elif evt[0] == "note_off" and evt[2] in TOM_MARKERS:
            tracker.off(evt[1], evt[2])
    tracker.close_all(999_999_999)

    result = {}
    for evt in events:
        if evt[0] != "note_on":
            continue
        _, tick, note, _vel = evt
        if note not in _DRUM_NOTE_MAP:
            continue
        diff, lane = _DRUM_NOTE_MAP[note]

        if lane == 32:      # 2x kick
            ch_note, cymbal_flag = 32, False
        elif lane in (0, 1):  # kick, red snare – no cymbal variant
            ch_note, cymbal_flag = lane, False
        else:               # yellow(2), blue(3), green(4)
            ch_note = lane
            # MIDI default is CYMBAL; active tom marker → TOM
            tom_note = _TOM_MARKER_FOR_LANE[lane]
            cymbal_flag = not tracker.active_at(tom_note, tick)

        result.setdefault(diff, []).append({
            "tick":        tick,
            "time_ms":     ticks_to_ms(tick),
            "ch_note":     ch_note,
            "cymbal_flag": cymbal_flag,
        })

    for diff in result:
        result[diff].sort(key=lambda h: (h["tick"], h["ch_note"]))
    return result


# ---------------------------------------------------------------------------
# Guitar / bass extractor
# ---------------------------------------------------------------------------

# Difficulty base MIDI notes (green note) for guitar/bass
GUITAR_BASES = {3: 96, 2: 84, 1: 72, 0: 60}
# offsets: green=0, red=1, yellow=2, blue=3, orange=4, open=-1
#          force_hopo=+5, force_strum=+6
TAP_NOTE = 104  # single sustained note covering tap region (all difficulties)


def _build_note_to_diff_lane_guitar():
    m = {}
    for diff, base in GUITAR_BASES.items():
        for lane in range(5):   # 0=green .. 4=orange
            m[base + lane] = (diff, lane)
        m[base - 1] = (diff, 7)  # open → lane 7
    return m


_GUITAR_NOTE_MAP = _build_note_to_diff_lane_guitar()


def _extract_guitar_notes(events, ppq, ticks_to_ms):
    """
    Returns {diff_int: [guitar_note_dict, ...]}
    """
    hopo_threshold = int(HOPO_THRESHOLD_FACTOR * ppq)

    # Collect sustained phrase marker ranges
    phrase_tracker   = _ActiveNotes()
    tap_tracker      = _ActiveNotes()
    note_end_tracker = _ActiveNotes()

    for evt in events:
        tick, note = evt[1], evt[2]
        if evt[0] == "note_on":
            phrase_tracker.on(tick, note)
            note_end_tracker.on(tick, note)
            if note == TAP_NOTE:
                tap_tracker.on(tick, note)
        elif evt[0] == "note_off":
            phrase_tracker.off(tick, note)
            note_end_tracker.off(tick, note)
            if note == TAP_NOTE:
                tap_tracker.off(tick, note)

    phrase_tracker.close_all(999_999_999)
    tap_tracker.close_all(999_999_999)
    note_end_tracker.close_all(999_999_999)

    # Collect note events per difficulty, grouped by tick
    by_diff_tick = {}   # {diff: {tick: [lane, ...]}}
    for evt in events:
        if evt[0] != "note_on":
            continue
        _, tick, note, _vel = evt
        if note not in _GUITAR_NOTE_MAP:
            continue
        diff, lane = _GUITAR_NOTE_MAP[note]
        by_diff_tick.setdefault(diff, {}).setdefault(tick, []).append(lane)

    result = {}

    for diff, by_tick in by_diff_tick.items():
        force_hopo_note  = GUITAR_BASES[diff] + 5
        force_strum_note = GUITAR_BASES[diff] + 6

        prev_tick, prev_frets = None, []
        diff_notes = []

        for tick in sorted(by_tick):
            frets = sorted(set(by_tick[tick]))

            # Sustain: find latest note_off for any lane in this tick
            end_ticks = []
            for lane in frets:
                for s, e in note_end_tracker._ranges.get(GUITAR_BASES[diff] + (lane if lane < 5 else -1), []):
                    if s == tick:
                        end_ticks.append(e)
            max_end = max(end_ticks) if end_ticks else tick
            sustain_ms = round(ticks_to_ms(max_end) - ticks_to_ms(tick), 3)

            is_tap      = tap_tracker.active_at(TAP_NOTE, tick)
            force_hopo  = phrase_tracker.active_at(force_hopo_note, tick)
            force_strum = phrase_tracker.active_at(force_strum_note, tick)

            auto_hopo = False
            if not is_tap and not force_hopo and not force_strum:
                if prev_tick is not None and (tick - prev_tick) <= hopo_threshold:
                    if len(frets) == 1 and frets != prev_frets:
                        auto_hopo = True

            diff_notes.append({
                "tick":         tick,
                "time_ms":      ticks_to_ms(tick),
                "frets":        frets,
                "length_ticks": max(0, max_end - tick),
                "sustain_ms":   sustain_ms,
                "ho":           force_hopo or auto_hopo or is_tap,
                "tap":          is_tap,
            })
            prev_tick, prev_frets = tick, frets

        if diff_notes:
            result[diff] = diff_notes

    return result


# ---------------------------------------------------------------------------
# Pro keys extractor
# ---------------------------------------------------------------------------

def _extract_piano_notes(events, ticks_to_ms):
    """
    Extract piano notes from a PART REAL_KEYS_* track.
    Returns [{time_ms, pitch, sustain_ms}] sorted by time.
    """
    active = {}  # pitch → start_tick
    notes = []
    for evt in events:
        if evt[0] == "note_on":
            _, tick, pitch, _vel = evt
            if 21 <= pitch <= 108:  # A0–C8 piano range
                active[pitch] = tick
        elif evt[0] == "note_off":
            _, tick, pitch, _vel = evt
            if pitch in active:
                start = active.pop(pitch)
                time_ms = ticks_to_ms(start)
                sustain_ms = max(0.0, round(ticks_to_ms(tick) - time_ms, 3))
                notes.append({"time_ms": time_ms, "pitch": pitch, "sustain_ms": sustain_ms})
    notes.sort(key=lambda n: n["time_ms"])
    return notes


# ---------------------------------------------------------------------------
# Lyrics extractor
# ---------------------------------------------------------------------------

def _extract_lyrics(events, ticks_to_ms):
    lyrics = []
    for evt in events:
        if evt[0] != "lyric":
            continue
        _, tick, text = evt
        text = text.strip()
        # Skip section/phrase markers that sometimes appear as lyric events
        if not text or text.startswith("[") or text.startswith("("):
            continue
        lyrics.append({"tick": tick, "time_ms": ticks_to_ms(tick), "text": text})
    return sorted(lyrics, key=lambda l: l["tick"])


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse(filepath):
    """
    Parse a .mid file.  Returns the same structure as chart.parse():
      {
        "resolution":  int (PPQ),
        "offset":      float (0.0 – .mid has no explicit offset),
        "song_meta":   dict (empty – metadata comes from song.ini),
        "ticks_to_ms": callable,
        "tracks": {
          "lead":  {diff_int: [guitar_note_dict, ...]},
          "bass":  {diff_int: [guitar_note_dict, ...]},
          "drums": {diff_int: [drum_hit_dict,   ...]},
        },
        "lyrics": [{tick, time_ms, text}, ...],
      }
    """
    ppq, raw_tracks = _parse_midi_binary(filepath)
    ticks_to_ms = _build_converter(raw_tracks, ppq)

    # Extract time-signature events from all tracks
    ts_events = []
    max_tick   = 0
    for events in raw_tracks:
        for evt in events:
            if evt[0] == "timesig":
                ts_events.append((evt[1], evt[2], evt[3]))
            if evt[1] > max_tick:
                max_tick = evt[1]
    ts_events.sort()

    # Index tracks by name
    named = {}
    for events in raw_tracks:
        name = None
        for evt in events:
            if evt[0] == "name":
                name = evt[2].strip()
                break
        if name:
            named[name] = events

    tracks = {}

    # Guitar / bass
    guitar_aliases = ["PART GUITAR", "T1 GEMS"]
    bass_aliases   = ["PART BASS", "PART RHYTHM"]
    drums_aliases  = ["PART DRUMS"]
    vocals_aliases = ["PART VOCALS", "VOCALS"]

    for alias in guitar_aliases:
        if alias in named:
            notes = _extract_guitar_notes(named[alias], ppq, ticks_to_ms)
            if notes:
                tracks["lead"] = notes
            break

    for alias in bass_aliases:
        if alias in named:
            notes = _extract_guitar_notes(named[alias], ppq, ticks_to_ms)
            if notes:
                tracks["bass"] = notes
            break

    pro_keys_diffs = {3: "PART REAL_KEYS_X", 2: "PART REAL_KEYS_H",
                      1: "PART REAL_KEYS_M", 0: "PART REAL_KEYS_E"}
    keys_by_diff = {}
    for diff, track_name in pro_keys_diffs.items():
        if track_name in named:
            notes = _extract_piano_notes(named[track_name], ticks_to_ms)
            if notes:
                keys_by_diff[diff] = notes
    if keys_by_diff:
        tracks["keys"] = keys_by_diff

    for alias in drums_aliases:
        if alias in named:
            notes = _extract_drum_notes(named[alias], ticks_to_ms)
            if notes:
                tracks["drums"] = notes
            break

    lyrics = []
    for alias in vocals_aliases:
        if alias in named:
            lyrics = _extract_lyrics(named[alias], ticks_to_ms)
            break

    return {
        "resolution":  ppq,
        "offset":      0.0,
        "song_meta":   {},
        "ticks_to_ms": ticks_to_ms,
        "ts_events":   ts_events,   # [(tick, numerator, denominator_int), ...]
        "max_tick":    max_tick,
        "tracks":      tracks,
        "lyrics":      lyrics,
    }
