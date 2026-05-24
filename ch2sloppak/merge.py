"""Merge a Clone Hero song folder with an existing Rocksmith .sloppak package.

The CH note data is re-timed to the RS audio via inter-beat-interval
cross-correlation of the two beat maps, then written into a new sloppak
alongside the RS audio stems and existing arrangements.

CH audio stems are dropped; RS stems pass through unchanged.
RS arrangements win on ID collision (e.g. 'lead', 'bass').
"""

import json
import os
import re
import struct
import subprocess
import sys
import tempfile
import zipfile

import yaml

if not getattr(sys, "frozen", False):
    sys.path.insert(0, os.path.dirname(__file__))

from parsers import chart as chart_parser
from parsers import mid as mid_parser
from parsers import song_ini
from writers import arrangement as arrangement_writer
from writers import beats as beats_writer
from writers import drum_tab as drum_tab_writer
from writers import lyrics as lyrics_writer
from writers import manifest as manifest_writer


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------

def _find(directory, *names):
    for name in names:
        p = os.path.join(directory, name)
        if os.path.isfile(p):
            return p
    return None


# ---------------------------------------------------------------------------
# RS sloppak reader
# ---------------------------------------------------------------------------

def _read_sloppak(path):
    """
    Read manifest, arrangements, and beat data from a .sloppak zip.

    Returns dict with keys:
      manifest, arrangements, rs_beats, has_lyrics, stem_ids, cover_name
    """
    with zipfile.ZipFile(path, "r") as zf:
        names = set(zf.namelist())
        manifest = yaml.safe_load(zf.read("manifest.yaml"))

        arrangements = {}
        for name in names:
            if name.startswith("arrangements/") and name.endswith(".json"):
                tid = name[len("arrangements/"):-len(".json")]
                arrangements[tid] = json.loads(zf.read(name))

        has_lyrics = "lyrics.json" in names
        rs_lyrics  = json.loads(zf.read("lyrics.json")) if has_lyrics else []
        stem_ids   = [s["id"] for s in manifest.get("stems", [])]

        cover_name = next(
            (n for n in names if n.startswith("cover.")), None
        )

    rs_beats = next(
        (arr["beats"] for arr in arrangements.values() if arr.get("beats")),
        None,
    )

    return {
        "manifest":     manifest,
        "arrangements": arrangements,
        "rs_beats":     rs_beats or [],
        "has_lyrics":   has_lyrics,
        "rs_lyrics":    rs_lyrics,
        "stem_ids":     stem_ids,
        "cover_name":   cover_name,
    }


# ---------------------------------------------------------------------------
# Lyrics alignment
# ---------------------------------------------------------------------------

def _norm_lyric(text):
    text = text.lower()
    text = text.strip("-+= \t")
    text = re.sub(r"[^\w]", "", text)
    return text


def _lyrics_align(raw_ch_lyrics, rs_lyrics, verbose):
    """
    Find alignment offset using paired lyric timestamps.

    raw_ch_lyrics: [{time_ms, text}, ...] from the CH parser (CH-audio time)
    rs_lyrics:     [{"t": float, "w": str}, ...] from the RS sloppak (RS-audio time)

    Normalises syllable text, matches by content, bins the per-pair offsets
    into 50 ms buckets, and returns the mean of the winning bucket.
    Returns offset_seconds or None.
    """
    if not raw_ch_lyrics or not rs_lyrics:
        return None

    # Build RS lookup: normalised_text → [t_seconds, ...]
    rs_by_text = {}
    for entry in rs_lyrics:
        key = _norm_lyric(entry.get("w", ""))
        if key:
            rs_by_text.setdefault(key, []).append(entry["t"])

    offsets = []
    for lyric in raw_ch_lyrics:
        key = _norm_lyric(lyric.get("text", ""))
        if not key or key not in rs_by_text:
            continue
        ch_t = lyric["time_ms"] / 1000.0
        for rs_t in rs_by_text[key]:
            offsets.append(rs_t - ch_t)

    if not offsets:
        if verbose:
            print("  lyrics   : no text matches found")
        return None

    # Vote in 50 ms bins; find the winning cluster
    bin_s = 0.05
    bins  = {}
    for off in offsets:
        b = round(off / bin_s)
        bins[b] = bins.get(b, 0) + 1

    best_bin   = max(bins, key=bins.get)
    inliers    = [o for o in offsets if abs(o - best_bin * bin_s) <= bin_s]
    offset_s   = sum(inliers) / len(inliers)

    if verbose:
        print(f"  lyrics   : {len(inliers)}/{len(offsets)} matches → offset {offset_s:+.3f} s")

    # Require at least 4 inliers to trust the result
    return offset_s if len(inliers) >= 4 else None


# ---------------------------------------------------------------------------
# Audio waveform alignment (two-level)
# ---------------------------------------------------------------------------
#
# Level 1 – coarse: energy envelope at 50 Hz, ±60 s search → ~20 ms accuracy
# Level 2 – fine:   raw waveform at 200 Hz in ±1.5 s window  → ~5 ms accuracy
#
# Both recordings are assumed to be the same take; the waveform correlation
# peak is very sharp for identical audio and gives reliable sub-10 ms results.
#
# Sign convention: lag > 0 means CH content appears lag frames LATER than RS,
# so offset_s = -lag / rate (what to ADD to CH note times to reach RS time).

_COARSE_SR    = 200   # Hz for coarse extraction
_COARSE_WIN   = 4     # samples per RMS window → 50 Hz envelope
_COARSE_RATE  = _COARSE_SR // _COARSE_WIN
_COARSE_SECS  = 90    # seconds to extract for coarse step

_FINE_SR      = 200   # Hz for fine waveform correlation
_FINE_CLIP_S  = 15    # seconds of audio to correlate
_FINE_START_S = 30    # CH start time of fine clip (past most intros)
_FINE_SEARCH_S = 1.5  # ±seconds fallback (used when beat interval unknown)


_DRUM_AUDIO_EXTS = (".ogg", ".mp3", ".opus", ".wav", ".flac")


def _find_ch_drum_files(ch_dir):
    """Return a list of CH drum stem paths (merged stem, or splits 1-4).

    Drum audio has sharp, distinctive transients that produce much cleaner
    onset-envelope correlation than a full mix.  Returns [] if none found.
    """
    # Merged drum stem
    for ext in _DRUM_AUDIO_EXTS:
        p = os.path.join(ch_dir, "drums" + ext)
        if os.path.isfile(p):
            return [p]
    # Individual splits (drums_1 … drums_4)
    splits = []
    for i in range(1, 5):
        for ext in _DRUM_AUDIO_EXTS:
            p = os.path.join(ch_dir, f"drums_{i}{ext}")
            if os.path.isfile(p):
                splits.append(p)
                break
    return splits


def _find_ch_audio(ch_dir):
    for name in ("song.ogg", "song.mp3", "song.opus",
                 "audio.ogg", "guitar.ogg", "guitar.mp3"):
        p = os.path.join(ch_dir, name)
        if os.path.isfile(p):
            return p
    audio_exts = {".ogg", ".mp3", ".opus", ".wav", ".flac", ".m4a"}
    for fname in sorted(os.listdir(ch_dir)):
        if os.path.splitext(fname)[1].lower() in audio_exts:
            return os.path.join(ch_dir, fname)
    return None


def _extract_pcm(audio_path, sample_rate, start_s=0.0, duration_s=None):
    """Return raw mono PCM as a list of int16 values via ffmpeg."""
    fd, raw_path = tempfile.mkstemp(suffix=".raw")
    os.close(fd)
    cmd = ["ffmpeg", "-y"]
    if start_s > 0:
        cmd += ["-ss", str(start_s)]
    cmd += ["-i", audio_path]
    if duration_s is not None:
        cmd += ["-t", str(duration_s)]
    cmd += ["-ar", str(sample_rate), "-ac", "1", "-f", "s16le", raw_path]
    try:
        subprocess.run(cmd, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with open(raw_path, "rb") as f:
            data = f.read()
    finally:
        try:
            os.unlink(raw_path)
        except OSError:
            pass
    n = len(data) // 2
    if not n:
        return []
    return list(struct.unpack(f"<{n}h", data))


def _rms_envelope(samples, window):
    env = []
    for i in range(0, len(samples) - window, window):
        chunk = samples[i: i + window]
        env.append((sum(s * s for s in chunk) / window) ** 0.5)
    return env


def _onset_envelope(samples, window):
    """Positive first-order difference of RMS energy.
    Peaks at attack transients (drum hits, strums), much more distinctive
    than raw energy for constant-loudness repeating songs.
    """
    rms = _rms_envelope(samples, window)
    return [max(0.0, rms[i] - rms[i - 1]) if i > 0 else 0.0
            for i in range(len(rms))]


def _normalize(sig):
    n = len(sig)
    if not n:
        return sig
    mean = sum(sig) / n
    centered = [x - mean for x in sig]
    var = sum(x * x for x in centered) / n
    std = max(var ** 0.5, 1e-10)
    return [x / std for x in centered]


def _xcorr_best(a, b, max_lag):
    """Cross-correlate a and b; return (best_lag, best_corr)."""
    na, nb = len(a), len(b)
    best_lag, best_corr = 0, float("-inf")
    for lag in range(-max_lag, max_lag + 1):
        a0 = max(0,  lag)
        b0 = max(0, -lag)
        n  = min(na - a0, nb - b0)
        if n < 10:
            continue
        corr = sum(a[a0 + i] * b[b0 + i] for i in range(n)) / n
        if corr > best_corr:
            best_corr = corr
            best_lag  = lag
    return best_lag, best_corr


def _sum_pcm(paths, sample_rate, start_s=0.0, duration_s=None):
    """Extract PCM from each path and return a sample-wise sum (list of int).

    Truncates all signals to the shortest one so they can be summed directly.
    Returns [] if no valid signal is found.
    """
    signals = []
    for p in paths:
        pcm = _extract_pcm(p, sample_rate, start_s=start_s, duration_s=duration_s)
        if pcm:
            signals.append(pcm)
    if not signals:
        return []
    min_len = min(len(s) for s in signals)
    return [sum(s[i] for s in signals) for i in range(min_len)]


def _pick_rs_stem(rs_sloppak_path):
    """Return (zip_entry_name, label) preferring the drums stem."""
    with zipfile.ZipFile(rs_sloppak_path, "r") as zf:
        stems = [n for n in zf.namelist()
                 if n.startswith("stems/") and n.endswith(".ogg")]
    if not stems:
        return None, None
    drums = next((n for n in stems if n == "stems/drums.ogg"), None)
    if drums:
        return drums, "drums"
    full = next((n for n in stems if n in ("stems/full.ogg", "stems/song.ogg")), None)
    return (full or stems[0]), "mix"


def _audio_align(ch_dir, rs_sloppak_path, verbose):
    """Two-level onset alignment.

    Uses CH drum stems (split or merged) when available — drum transients
    are far more distinctive than a full mix and produce cleaner correlation.
    Falls back to any CH audio if no drum stems are found.  On the RS side,
    prefers stems/drums.ogg over the full mix for the same reason.

    Returns (offset_seconds, used_drums: bool) or (None, False) on failure.
    """
    # Choose CH source: drum stems > full mix
    ch_drum_files = _find_ch_drum_files(ch_dir)
    if ch_drum_files:
        ch_paths  = ch_drum_files
        ch_label  = f"drums ({len(ch_drum_files)} stem{'s' if len(ch_drum_files) > 1 else ''})"
        use_drums = True
    else:
        ch_audio = _find_ch_audio(ch_dir)
        if not ch_audio:
            if verbose:
                print("  audio    : no CH audio found")
            return None, False
        ch_paths  = [ch_audio]
        ch_label  = "mix"
        use_drums = False

    rs_stem_name, rs_label = _pick_rs_stem(rs_sloppak_path)
    if not rs_stem_name:
        if verbose:
            print("  audio    : no RS stems found")
        return None, False

    rs_tmp = None
    try:
        with zipfile.ZipFile(rs_sloppak_path, "r") as zf:
            fd, rs_tmp = tempfile.mkstemp(suffix=".ogg")
            os.close(fd)
            with open(rs_tmp, "wb") as f:
                f.write(zf.read(rs_stem_name))

        if verbose:
            print(f"  audio    : CH {ch_label} → RS {rs_label} …")

        # --- Level 1: coarse onset-envelope correlation (50 Hz) ---
        # Sum PCM across CH drum splits so every hit contributes to the signal.
        ch_coarse_pcm = _sum_pcm(ch_paths, _COARSE_SR, duration_s=_COARSE_SECS)
        rs_coarse_pcm = _extract_pcm(rs_tmp, _COARSE_SR, duration_s=_COARSE_SECS)

        ch_env = _normalize(_onset_envelope(ch_coarse_pcm, _COARSE_WIN))
        rs_env = _normalize(_onset_envelope(rs_coarse_pcm, _COARSE_WIN))

        if not ch_env or not rs_env:
            return None, False

        lag1, _ = _xcorr_best(ch_env, rs_env, int(60 * _COARSE_RATE))
        offset1_s = -lag1 / _COARSE_RATE

        if verbose:
            print(f"  audio    : coarse {offset1_s:+.2f} s")

        # --- Level 2: fine PCM correlation (200 Hz, ±1.5 s window) ---
        # Summing raw PCM from drum splits amplifies transients further.
        rs_fine_start = max(0.0, _FINE_START_S + offset1_s)
        ch_fine_pcm   = _sum_pcm(ch_paths, _FINE_SR,
                                  start_s=_FINE_START_S, duration_s=_FINE_CLIP_S)
        rs_fine_pcm   = _extract_pcm(rs_tmp, _FINE_SR,
                                      start_s=rs_fine_start, duration_s=_FINE_CLIP_S)

        ch_fine = _normalize(ch_fine_pcm)
        rs_fine = _normalize(rs_fine_pcm)

        if ch_fine and rs_fine:
            lag2, corr2 = _xcorr_best(
                ch_fine, rs_fine, max(1, int(_FINE_SEARCH_S * _FINE_SR)))
            offset2_s = -lag2 / _FINE_SR
            total     = offset1_s + offset2_s
            if verbose:
                print(f"  audio    : fine {offset2_s:+.3f} s "
                      f"(±{_FINE_SEARCH_S * 1000:.0f} ms) → "
                      f"total {total:+.3f} s  (corr {corr2:.3f})")
            return total, use_drums

        return offset1_s, use_drums

    except Exception as exc:
        if verbose:
            print(f"  audio    : failed ({exc})")
        return None, False
    finally:
        if rs_tmp:
            try:
                os.unlink(rs_tmp)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Beat alignment
# ---------------------------------------------------------------------------

def _ibi_cross_correlate(ch_times, rs_times, max_k=128, prior_offset_s=None,
                         prior_scale=2.0):
    """
    Find the integer index shift K that minimises IBI MSE.
    When prior_offset_s is supplied, a soft penalty discourages K values
    whose implied mean offset differs from the prior by more than prior_scale
    seconds.  Use a small prior_scale (e.g. 0.25) to anchor tightly to a
    drum-derived offset and prevent IBI from picking a neighbouring beat.
    Returns (best_k, ibi_rms_ms).
    """
    ch_ibis = [ch_times[i + 1] - ch_times[i] for i in range(len(ch_times) - 1)]
    rs_ibis = [rs_times[i + 1] - rs_times[i] for i in range(len(rs_times) - 1)]

    if not ch_ibis or not rs_ibis:
        return 0, float("inf")

    best_k, best_score = 0, float("inf")
    search = min(max_k, len(ch_ibis) - 1, len(rs_ibis) - 1)

    for k in range(-search, search + 1):
        c0 = max(0,  k)
        r0 = max(0, -k)
        n  = min(len(ch_ibis) - c0, len(rs_ibis) - r0, 64)
        if n < 4:
            continue
        ibi_mse = sum(
            (ch_ibis[c0 + i] - rs_ibis[r0 + i]) ** 2 for i in range(n)
        ) / n
        score = ibi_mse
        if prior_offset_s is not None:
            np_ = min(len(ch_times) - c0, len(rs_times) - r0, 64)
            if np_ > 0:
                mean_off = sum(
                    rs_times[r0 + i] - ch_times[c0 + i] for i in range(np_)
                ) / np_
                score += ((mean_off - prior_offset_s) / prior_scale) ** 2
        if score < best_score:
            best_score = score
            best_k     = k

    c0 = max(0,  best_k); r0 = max(0, -best_k)
    n  = min(len(ch_ibis) - c0, len(rs_ibis) - r0, 64)
    ibi_mse = sum((ch_ibis[c0+i] - rs_ibis[r0+i])**2 for i in range(n)) / max(n, 1)
    return best_k, (ibi_mse ** 0.5) * 1000.0


def _build_shift_fn(ch_times, rs_times, k):
    """
    Build a piecewise-linear ch_time → rs_time function from aligned beat pairs.
    """
    c0 = max(0,  k)
    r0 = max(0, -k)
    n  = min(len(ch_times) - c0, len(rs_times) - r0)
    ch_k = ch_times[c0: c0 + n]
    rs_k = rs_times[r0: r0 + n]

    if not ch_k:
        return lambda t: t

    if n == 1:
        off = rs_k[0] - ch_k[0]
        return lambda t: t + off

    def shift_fn(t):
        if t <= ch_k[0]:
            return t + (rs_k[0] - ch_k[0])
        if t >= ch_k[-1]:
            return t + (rs_k[-1] - ch_k[-1])
        lo, hi = 0, n - 2
        while lo < hi:
            mid = (lo + hi) // 2
            if ch_k[mid + 1] <= t:
                lo = mid + 1
            else:
                hi = mid
        span = ch_k[lo + 1] - ch_k[lo]
        frac = (t - ch_k[lo]) / span if span > 0 else 0.0
        return rs_k[lo] + frac * (rs_k[lo + 1] - rs_k[lo])

    return shift_fn


def _auto_align(ch_beats, rs_beats, prior_offset_s=None, prior_scale=2.0):
    """
    Auto-align CH beat list to RS beat list, optionally biased by an audio
    cross-correlation prior.  Returns (shift_fn, info_dict).
    prior_scale controls how tightly the beat K is pinned to prior_offset_s:
    small value (e.g. 0.25) = drum-anchor mode; large (2.0) = loose.
    """
    ch_times = [b["time"] for b in ch_beats]
    rs_times = [b["time"] for b in rs_beats]

    if not ch_times or not rs_times:
        return lambda t: t, {"k": 0, "ibi_rms_ms": None, "mean_offset_ms": 0.0, "n_pairs": 0}

    k, ibi_rms_ms = _ibi_cross_correlate(ch_times, rs_times,
                                          prior_offset_s=prior_offset_s,
                                          prior_scale=prior_scale)

    c0 = max(0,  k)
    r0 = max(0, -k)
    n  = min(len(ch_times) - c0, len(rs_times) - r0)
    ch_k = ch_times[c0: c0 + n]
    rs_k = rs_times[r0: r0 + n]
    mean_off_ms = (
        sum(rs_k[i] - ch_k[i] for i in range(n)) / n * 1000.0
    ) if n else 0.0

    return _build_shift_fn(ch_times, rs_times, k), {
        "k":              k,
        "ibi_rms_ms":     ibi_rms_ms,
        "mean_offset_ms": mean_off_ms,
        "n_pairs":        n,
    }


# ---------------------------------------------------------------------------
# Retiming
# ---------------------------------------------------------------------------

def _retime_note(note, shift_fn):
    new_t   = shift_fn(note["t"])
    new_end = shift_fn(note["t"] + note["sus"])
    note["t"]   = round(new_t, 3)
    note["sus"] = round(max(0.0, new_end - new_t), 3)


def _retime_arrangement(arr, shift_fn):
    for note in arr.get("notes") or []:
        _retime_note(note, shift_fn)

    for phrase in arr.get("phrases") or []:
        phrase["start_time"] = round(shift_fn(phrase["start_time"]), 3)
        phrase["end_time"]   = round(shift_fn(phrase["end_time"]),   3)
        for level in phrase.get("levels") or []:
            for note in level.get("notes") or []:
                _retime_note(note, shift_fn)
            for anchor in level.get("anchors") or []:
                anchor["time"] = round(shift_fn(anchor["time"]), 3)

    for anchor in arr.get("anchors") or []:
        anchor["time"] = round(shift_fn(anchor["time"]), 3)


# ---------------------------------------------------------------------------
# Output writer
# ---------------------------------------------------------------------------

def _write_merged(output_path, manifest_yaml, merged_arrangements,
                  rs_sloppak_path, rs_data, lyrics_data, drum_tabs=None):
    """
    Write the merged sloppak.  RS zip entries (stems, cover, lyrics) are
    streamed through unchanged; arrangements are all rewritten from dicts.
    """
    drum_tabs = drum_tabs or {}

    with zipfile.ZipFile(rs_sloppak_path, "r") as zf_in, \
         zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf_out:

        zf_out.writestr("manifest.yaml", manifest_yaml)

        for tid, arr in merged_arrangements.items():
            zf_out.writestr(
                f"arrangements/{tid}.json",
                json.dumps(arr, separators=(",", ":")),
            )

        if lyrics_data:
            zf_out.writestr(
                "lyrics.json",
                json.dumps(lyrics_data, ensure_ascii=False, separators=(",", ":")),
            )

        for arr_id, tab in drum_tabs.items():
            zf_out.writestr(
                f"drum_tab_{arr_id}.json",
                json.dumps(tab, separators=(",", ":")),
            )

        # Pass through everything from RS that we haven't rewritten
        new_drum_tab_names = {f"drum_tab_{aid}.json" for aid in drum_tabs}
        skip_prefixes = {"manifest.yaml", "arrangements/"}
        skip_exact    = {"lyrics.json"} if lyrics_data else set()
        skip_exact   |= new_drum_tab_names

        for name in zf_in.namelist():
            if name in skip_exact:
                continue
            if any(name.startswith(p) for p in skip_prefixes):
                continue
            zf_out.writestr(name, zf_in.read(name))


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def merge(ch_dir, rs_sloppak_path, output_path=None, offset_ms=None, nudge_ms=0.0, verbose=True):
    """
    Merge a CH song folder with an RS .sloppak.
    Returns the path of the written package.
    """
    ch_dir          = os.path.abspath(ch_dir)
    rs_sloppak_path = os.path.abspath(rs_sloppak_path)

    if not os.path.isdir(ch_dir):
        raise FileNotFoundError(f"CH folder not found: {ch_dir}")
    if not os.path.isfile(rs_sloppak_path):
        raise FileNotFoundError(f"RS sloppak not found: {rs_sloppak_path}")

    # --- parse CH ---
    mid_path   = _find(ch_dir, "notes.mid",   "Notes.mid")
    chart_path = _find(ch_dir, "notes.chart", "Notes.chart")
    ini_path   = _find(ch_dir, "song.ini",    "Song.ini")

    if not mid_path and not chart_path:
        raise FileNotFoundError(f"No notes.mid or notes.chart in {ch_dir}")

    if mid_path:
        if verbose:
            print(f"  CH midi  : {mid_path}")
        try:
            parsed = mid_parser.parse(mid_path)
        except Exception as exc:
            if chart_path:
                if verbose:
                    print(f"  WARNING: MIDI failed ({exc}); falling back to .chart")
                parsed = chart_parser.parse(chart_path)
            else:
                raise
    else:
        if verbose:
            print(f"  CH chart : {chart_path}")
        parsed = chart_parser.parse(chart_path)

    # CH audio delay relative to chart notes: audio starts this many seconds
    # after the chart's t=0.  Positive = audio is late relative to notes.
    # song.ini `delay` (ms) and chart `[Song] Offset` (s) express the same
    # concept; add both in case both are present (normally only one is set).
    ini_meta = song_ini.parse(ini_path) if ini_path else {}
    ch_audio_delay_s = 0.0
    try:
        ch_audio_delay_s += float(ini_meta.get("delay", 0) or 0) / 1000.0
    except (ValueError, TypeError):
        pass
    ch_audio_delay_s += float(parsed.get("offset", 0.0) or 0.0)
    if verbose and ch_audio_delay_s:
        print(f"  CH delay : {ch_audio_delay_s * 1000:+.1f} ms (audio relative to notes)")

    # --- CH beats ---
    ch_beats = beats_writer.generate(
        ts_events   = parsed.get("ts_events", []),
        ppq         = parsed["resolution"],
        ticks_to_ms = parsed["ticks_to_ms"],
        max_tick    = parsed.get("max_tick", 0),
    )

    # --- read RS sloppak ---
    if verbose:
        print(f"  RS       : {os.path.basename(rs_sloppak_path)}")
    rs = _read_sloppak(rs_sloppak_path)

    # --- compute alignment ---
    if offset_ms is not None:
        total_ms = offset_ms + (nudge_ms or 0.0)
        shift_fn = lambda t, s=total_ms: t + s / 1000.0
        if verbose:
            if nudge_ms:
                print(f"  align    : manual offset {offset_ms:+.1f} ms + nudge {nudge_ms:+.1f} ms = {total_ms:+.1f} ms")
            else:
                print(f"  align    : manual offset {offset_ms:+.1f} ms")
    else:
        # Step 1: best available coarse offset estimate (lyrics > audio > none)
        raw_ch_lyrics = parsed.get("lyrics", [])
        prior_offset_s = _lyrics_align(raw_ch_lyrics, rs["rs_lyrics"], verbose)
        drum_anchor = False

        if prior_offset_s is None:
            audio_offset_s, drum_anchor = _audio_align(ch_dir, rs_sloppak_path, verbose)
            if audio_offset_s is not None:
                # Audio correlation gives CH-audio → RS-audio offset.
                # CH audio position = CH chart time + ch_audio_delay_s
                # (positive delay = audio has that many seconds of pre-roll before notes).
                # So CH-chart → RS-chart offset = audio_corr + delay.
                prior_offset_s = audio_offset_s + ch_audio_delay_s
                if verbose and drum_anchor:
                    print("  align    : drum stems used as master reference")

        if nudge_ms:
            prior_offset_s = (prior_offset_s or 0.0) + nudge_ms / 1000.0
            if verbose:
                print(f"  nudge    : {nudge_ms:+.1f} ms applied to prior")

        # Step 2: beat-map IBI → piecewise refinement biased by audio/lyrics prior.
        # When drum stems provided the prior, use a tight scale (0.25 s) so the
        # beat K cannot drift more than ~250 ms from the drum-derived anchor —
        # the drums ARE the timing reference; IBI only handles tempo stretch.
        prior_scale = 0.25 if drum_anchor else 2.0
        if ch_beats and rs["rs_beats"]:
            shift_fn, info = _auto_align(
                ch_beats, rs["rs_beats"],
                prior_offset_s=prior_offset_s,
                prior_scale=prior_scale,
            )
            if verbose:
                k   = info["k"]
                off = info["mean_offset_ms"]
                rms = info["ibi_rms_ms"]
                n   = info["n_pairs"]
                k_desc  = f"K={k:+d}" if k != 0 else "K=0"
                rms_str = f"{rms:.1f} ms" if rms is not None and rms < 9999 else "n/a"
                anchor  = " [drum-anchored]" if drum_anchor else ""
                print(f"  beats    : {k_desc}, mean offset {off:+.1f} ms, IBI RMS {rms_str} ({n} pairs){anchor}")
                if rms is not None and rms > 20.0:
                    print(f"  WARNING  : high IBI residual — try --offset if notes feel off")
        elif prior_offset_s is not None:
            shift_fn = lambda t, p=prior_offset_s: t + p
            if verbose:
                src = "drum" if drum_anchor else "audio"
                print(f"  align    : {src} offset {prior_offset_s * 1000:+.1f} ms (no beat map)")
        else:
            shift_fn = lambda t: t
            if verbose:
                print("  align    : no signal found; zero offset applied")

    # --- convert CH arrangements ---
    # Guitar/bass tracks are always written with "-gamepad" IDs so they never
    # shadow RS arrangements and both can coexist in the merged package.
    _GAMEPAD_IDS = {"lead": "lead-gamepad", "bass": "bass-gamepad"}

    ch_arrangements = {}
    for track_id, diff_dict in parsed["tracks"].items():
        if not diff_dict:
            continue
        if track_id == "drums":
            ch_arrangements["drums"]       = arrangement_writer.convert_drums(diff_dict)
            ch_arrangements["drums_score"] = arrangement_writer.convert_drums_score(diff_dict)
        elif track_id == "keys":
            ch_arrangements["keys"] = arrangement_writer.convert_keys(diff_dict)
        else:
            out_id = _GAMEPAD_IDS.get(track_id, track_id)
            ch_arrangements[out_id] = arrangement_writer.convert_guitar(
                diff_dict, arrangement_name=out_id)

    # --- drum_tabs (for slopsmith-plugin-drum-highway-3d) ---
    # Only real drums get a drum_tab file.  Guitar/bass/rhythm gamepad tracks
    # display on the guitar highway via GUITAR_LANE_MAP (string colors match).
    drum_tabs = {}
    if "drums" in parsed["tracks"] and parsed["tracks"]["drums"]:
        dt = drum_tab_writer.convert(parsed["tracks"]["drums"])
        if dt:
            drum_tabs["drums"] = dt

    # Retime and stamp RS beats onto each CH arrangement
    for arr in ch_arrangements.values():
        _retime_arrangement(arr, shift_fn)
        if rs["rs_beats"]:
            arr["beats"] = rs["rs_beats"]

    # Retime drum_tab hits
    if "drums" in drum_tabs:
        for hit in drum_tabs["drums"]["hits"]:
            hit["t"] = round(shift_fn(hit["t"]), 3)
        drum_tabs["drums"]["hits"].sort(key=lambda h: h["t"])
        if verbose:
            print(f"  drum_tab : {len(drum_tabs['drums']['hits'])} hits")

    # --- merge: RS wins on ID collision ---
    merged = dict(rs["arrangements"])
    added, skipped = [], []
    for tid, arr in ch_arrangements.items():
        if tid in merged:
            skipped.append(tid)
        else:
            merged[tid] = arr
            added.append(tid)

    if verbose:
        if added:
            print(f"  added    : {', '.join(added)}")
        if skipped:
            print(f"  skipped  : {', '.join(skipped)} (RS arrangement exists)")

    # --- lyrics: prefer RS; fall back to CH ---
    lyrics_data = []
    if not rs["has_lyrics"]:
        raw = parsed.get("lyrics", [])
        if raw:
            lyrics_data = lyrics_writer.convert(raw)
            if verbose and lyrics_data:
                print(f"  lyrics   : {len(lyrics_data)} syllables (from CH)")

    # --- manifest: use RS metadata ---
    rs_meta = rs["manifest"]
    arrangement_drum_tabs = {aid: f"drum_tab_{aid}.json" for aid in drum_tabs}
    manifest_dict = manifest_writer.build(
        metadata={
            "name":       rs_meta.get("title",  "Unknown"),
            "artist":     rs_meta.get("artist", ""),
            "album":      rs_meta.get("album",  ""),
            "year_clean": str(rs_meta.get("year", "")),
        },
        arrangement_ids = list(merged.keys()),
        stem_ids        = rs["stem_ids"],
        cover_filename  = rs["cover_name"],
        has_lyrics      = rs["has_lyrics"] or bool(lyrics_data),
        rs_arrangements = rs_meta.get("arrangements", []),
        drum_tabs       = arrangement_drum_tabs,
    )
    manifest_yaml = manifest_writer.to_yaml_string(manifest_dict)

    # --- output path ---
    if output_path is None:
        base        = os.path.splitext(rs_sloppak_path)[0]
        output_path = base + "+ch.sloppak"

    _write_merged(
        output_path         = output_path,
        manifest_yaml       = manifest_yaml,
        merged_arrangements = merged,
        rs_sloppak_path     = rs_sloppak_path,
        rs_data             = rs,
        lyrics_data         = lyrics_data,
        drum_tabs           = drum_tabs,
    )

    if verbose:
        size_kb = os.path.getsize(output_path) // 1024
        print(f"  → {output_path}  ({size_kb} KB)")

    return output_path
