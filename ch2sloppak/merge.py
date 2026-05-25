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
# Audio waveform alignment
# ---------------------------------------------------------------------------
#
# Primary path (drum chart available):
#   Step 1 — 80-pass iterative frequency-adaptive note-to-audio.
#     RS stem is filtered into three bands (kick <150 Hz, snare 150–2500 Hz,
#     cymbal >2500 Hz).  When CH drum stems are present, each note's frequency
#     fingerprint is read directly from the CH band envelopes at that note's
#     chart time — no fixed "kick=low" assumption.  The fingerprint is
#     normalised to a weight vector (wkick, wsnare, wcymbal) and used as a soft
#     blend when scoring that note against each RS position.  Simultaneous hits
#     (within 20 ms) are down-weighted by 1/(n_nearby+1) since co-occurring
#     drums blur the fingerprint.  Without CH audio, weights fall back to
#     one-hot on the declared band.  Search window decays exponentially from
#     ±30 s to ±1 frame over 80 passes.
#   Step 2 — banded CH-audio vs RS-audio xcorr refinement (if CH drums exist).
#     Cross-correlates CH drum bands vs RS bands within ±0.9 s of the note-
#     derived estimate for sub-frame accuracy; accepted if within 0.3 s.
#
# Fallback path (no drum chart): full-song onset xcorr + multi-clip voting.
#
# Sign convention: lag > 0 means CH appears lag frames LATER than RS,
# so offset_s = -lag / rate (what to ADD to CH note times to reach RS time).

_COARSE_RATE       = 50     # Hz — onset envelope rate for all scoring
_COARSE_SEARCH_S   = 30.0   # ±30 s initial search range
_COARSE_ZERO_BIAS  = 1e-4   # per-lag penalty to prefer smaller offsets on ties
_N_ALIGN_PASSES    = 5      # iterative note-to-audio passes
_CH_BAND_SR        = 8000   # sample rate for CH drum extraction (Python IIR filtered)
_CH_BAND_WIN       = _CH_BAND_SR // _COARSE_RATE  # 160 → 50 Hz envelope
_AUDIO_AGREE_S     = 0.3    # max note/audio diff (s) to trust the audio result
_PROFILE_WIN_FRAMES = 20    # ±20 frames = ±400 ms window for CH type-profile peaks

# RS frequency band extraction: ffmpeg filters, SR/win = _COARSE_RATE
_BAND_CFG = {
    "kick":   {"sr": 400,  "af": "lowpass=f=150",                  "win": 8},
    "snare":  {"sr": 4000, "af": "highpass=f=150,lowpass=f=2500",  "win": 80},
    "cymbal": {"sr": 8000, "af": "highpass=f=2500",                "win": 160},
}

# Map (ch_note, cymbal_flag) → frequency band
_DRUM_NOTE_BAND = {
    (0,  False): "kick",   (0,  True):  "kick",
    (32, False): "kick",   (32, True):  "kick",
    (1,  False): "snare",  (1,  True):  "snare",
    (2,  True):  "cymbal", (2,  False): "snare",   # hi-hat or tom_hi
    (3,  True):  "cymbal", (3,  False): "snare",   # ride or tom_mid
    (4,  True):  "cymbal", (4,  False): "snare",   # crash or tom_floor
}

# Fallback xcorr constants (used only when no drum chart is available)
_COARSE_SR       = 200
_COARSE_WIN      = 4
_FINE_SR        = 200
_FINE_CLIP_S    = 60
_FINE_SEARCH_S  = 4.0
_FINE_CONF_MIN  = 0.06
_MULTI_STEP_S   = 50.0
_MULTI_START_S  = 10.0
_MULTI_FULL_S   = 240
_MULTI_MIN_CORR = 0.02
_MULTI_BIN_S    = 0.10
_MULTI_MIN_CLIPS = 2

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


def _xcorr_best(a, b, max_lag, zero_bias=0.0):
    """Cross-correlate a and b; return (best_lag, best_corr).

    zero_bias: subtract this * abs(lag) from each score so that when two
    peaks have similar correlation the one closer to zero wins.  Does not
    affect the returned best_corr value (raw score is reported).
    """
    na, nb = len(a), len(b)
    best_lag, best_corr, best_raw = 0, float("-inf"), float("-inf")
    for lag in range(-max_lag, max_lag + 1):
        a0 = max(0,  lag)
        b0 = max(0, -lag)
        n  = min(na - a0, nb - b0)
        if n < 10:
            continue
        corr = sum(a[a0 + i] * b[b0 + i] for i in range(n)) / n
        biased = corr - zero_bias * abs(lag)
        if biased > best_corr:
            best_corr = biased
            best_raw  = corr
            best_lag  = lag
    return best_lag, best_raw


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


def _extract_pcm_af(audio_path, sample_rate, af, duration_s=None):
    """Extract mono PCM from audio_path with an ffmpeg audio filter applied."""
    fd, raw_path = tempfile.mkstemp(suffix=".raw")
    os.close(fd)
    cmd = ["ffmpeg", "-y", "-i", audio_path]
    if duration_s is not None:
        cmd += ["-t", str(duration_s)]
    cmd += ["-af", af, "-ar", str(sample_rate), "-ac", "1", "-f", "s16le", raw_path]
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


def _extract_band_envs(audio_path):
    """Extract frequency-band onset envelopes (each at _COARSE_RATE Hz) from audio.

    Returns {band: env} with bands 'kick', 'snare', 'cymbal', or None on failure.
    Each env is normalised so peak = 1.0.
    """
    result = {}
    for band, cfg in _BAND_CFG.items():
        pcm = _extract_pcm_af(audio_path, cfg["sr"], cfg["af"])
        if not pcm:
            return None
        env = _onset_envelope(pcm, cfg["win"])
        peak = max(env) if env else 0.0
        result[band] = [v / peak for v in env] if peak > 1e-10 else [0.0] * len(env)
    # Trim all bands to the same (shortest) length
    min_len = min(len(v) for v in result.values())
    return {b: v[:min_len] for b, v in result.items()}


def _drum_hits_to_band_pairs(hits, time_offset_s=0.0):
    """Convert parsed drum hits to [(time_s, declared_band), ...] sorted by time."""
    pairs = []
    for h in hits:
        t    = h["time_ms"] / 1000.0 + time_offset_s
        band = _DRUM_NOTE_BAND.get((h["ch_note"], h.get("cymbal_flag", False)), "snare")
        pairs.append((t, band))
    pairs.sort()
    return pairs


def _one_hot_weights(band):
    """One-hot (wkick, wsnare, wcymbal) tuple for a declared band name."""
    return {"kick": (1.0, 0.0, 0.0),
            "snare": (0.0, 1.0, 0.0),
            "cymbal": (0.0, 0.0, 1.0)}.get(band, (0.0, 1.0, 0.0))


def _build_type_profiles(note_band_pairs, ch_band_envs, coarse_rate):
    """Build per-declared-type frequency profiles from CH band envelopes.

    CH audio timing does not match chart-note timing exactly — intro/outro
    lengths differ, ch_audio_delay_s may be approximate, and minor tempo drift
    accumulates.  Looking up a single frame per note is therefore unreliable.

    Instead, for each note type (kick/snare/cymbal) this function searches in
    a ±_PROFILE_WIN_FRAMES window around each note's chart time and takes the
    peak in each band, then averages across all notes of that type.  Aggregating
    over many hits smooths out individual timing errors and produces a stable
    per-type fingerprint: "what does a kick/snare/cymbal actually sound like in
    this recording?"

    Returns {declared_band: (wkick, wsnare, wcymbal)}, or None on failure.
    """
    if not ch_band_envs:
        return None

    n_ch = len(ch_band_envs["kick"])
    ck, cs, cc = ch_band_envs["kick"], ch_band_envs["snare"], ch_band_envs["cymbal"]
    w    = _PROFILE_WIN_FRAMES

    accum = {"kick": [0.0, 0.0, 0.0], "snare": [0.0, 0.0, 0.0], "cymbal": [0.0, 0.0, 0.0]}
    count = {"kick": 0, "snare": 0, "cymbal": 0}

    for t, declared_band in note_band_pairs:
        if declared_band not in accum:
            continue
        f  = round(t * coarse_rate)
        lo = max(0, f - w)
        hi = min(n_ch, f + w + 1)
        if lo >= hi:
            continue
        accum[declared_band][0] += max(ck[lo:hi])
        accum[declared_band][1] += max(cs[lo:hi])
        accum[declared_band][2] += max(cc[lo:hi])
        count[declared_band]    += 1

    profiles = {}
    for band in ("kick", "snare", "cymbal"):
        n = count[band]
        if n == 0:
            profiles[band] = _one_hot_weights(band)
            continue
        a = accum[band]
        avg_k, avg_s, avg_c = a[0] / n, a[1] / n, a[2] / n
        total = avg_k + avg_s + avg_c
        profiles[band] = ((avg_k / total, avg_s / total, avg_c / total)
                          if total > 1e-10 else _one_hot_weights(band))
    return profiles


def _prep_note_weights(note_band_pairs, ch_band_envs, coarse_rate,
                        simultaneous_ms=20.0):
    """Compute per-note frequency weight tuples.

    Assigns each note its type's aggregate frequency profile (from
    _build_type_profiles), so weights are stable across the song even when
    CH audio timing drifts from chart timing.  Falls back to one-hot weights
    when no CH audio is available.

    Notes with simultaneous hits within ±simultaneous_ms are down-weighted by
    1/(n_nearby+1) since co-occurring drums blur the frequency fingerprint.

    Returns [(orig_frame, (wkick, wsnare, wcymbal), sim_weight), ...].
    """
    type_profiles = _build_type_profiles(note_band_pairs, ch_band_envs, coarse_rate)

    times = [t for t, _ in note_band_pairs]
    sim_s = simultaneous_ms / 1000.0

    result = []
    for i, (t, declared_band) in enumerate(note_band_pairs):
        n_nearby = sum(1 for j, t2 in enumerate(times)
                       if j != i and abs(t2 - t) <= sim_s)
        sim_w   = 1.0 / (n_nearby + 1)
        frame   = round(t * coarse_rate)
        weights = (type_profiles[declared_band]
                   if type_profiles and declared_band in type_profiles
                   else _one_hot_weights(declared_band))
        result.append((frame, weights, sim_w))

    return result


import math as _math

def _iir_lowpass(samples, cutoff_hz, sr):
    """First-order IIR lowpass filter (in-place style, returns new list)."""
    rc = 1.0 / (2.0 * _math.pi * cutoff_hz)
    a  = (1.0 / sr) / (rc + 1.0 / sr)
    b  = 1.0 - a
    y  = 0.0
    out = [0.0] * len(samples)
    for i, x in enumerate(samples):
        y = a * x + b * y
        out[i] = y
    return out


def _iir_highpass(samples, cutoff_hz, sr):
    """First-order IIR highpass filter."""
    rc = 1.0 / (2.0 * _math.pi * cutoff_hz)
    dt = 1.0 / sr
    a  = rc / (rc + dt)
    xp = samples[0] if samples else 0.0
    yp = 0.0
    out = [0.0] * len(samples)
    for i, x in enumerate(samples):
        y = a * (yp + x - xp)
        xp, yp = x, y
        out[i] = y
    return out


def _extract_band_envs_paths(paths):
    """Extract banded onset envelopes from CH drum stems using Python IIR filters.

    Sums all stems at _CH_BAND_SR, then filters into kick/snare/cymbal bands.
    Returns {band: env} normalised to peak=1.0, or None on failure.
    """
    pcm = _sum_pcm(paths, _CH_BAND_SR)
    if not pcm:
        return None
    kick   = _iir_lowpass(pcm, 150, _CH_BAND_SR)
    hp_lo  = _iir_highpass(pcm, 150, _CH_BAND_SR)
    snare  = _iir_lowpass(hp_lo, 2500, _CH_BAND_SR)
    cymbal = _iir_highpass(pcm, 2500, _CH_BAND_SR)
    result = {}
    for band, band_pcm in (("kick", kick), ("snare", snare), ("cymbal", cymbal)):
        env  = _onset_envelope(band_pcm, _CH_BAND_WIN)
        peak = max(env) if env else 0.0
        result[band] = [v / peak for v in env] if peak > 1e-10 else [0.0] * len(env)
    min_len = min(len(v) for v in result.values())
    return {b: v[:min_len] for b, v in result.items()}


def _note_align_iterative(note_frame_weights, band_envs_rs, coarse_rate,
                           max_search_s=30.0, n_passes=80, zero_bias=1e-4):
    """Iteratively refine note-to-audio offset over n_passes.

    Frequency weights are pre-computed once; each pass applies the current
    frame shift, scores against RS bands, and corrects.  Search window decays
    exponentially from ±max_search_s to ±1 frame.  Terminates early on lag=0.

    Returns (offset_s, final_score, passes_taken).
    """
    start_lag = max(1, int(max_search_s * coarse_rate))
    min_lag   = 1
    decay     = (min_lag / start_lag) ** (1.0 / max(n_passes - 1, 1))
    n_rs      = len(next(iter(band_envs_rs.values()))) if band_envs_rs else 0

    offset       = 0.0
    score        = 0.0
    passes_taken = n_passes

    for i in range(n_passes):
        max_lag     = max(min_lag, int(start_lag * decay ** i))
        frame_shift = round(offset * coarse_rate)
        shifted     = [(f + frame_shift, bw, sw)
                       for f, bw, sw in note_frame_weights]
        lag, score  = _score_banded(shifted, band_envs_rs, n_rs,
                                     max_lag, zero_bias=zero_bias)
        offset     += -lag / coarse_rate
        if lag == 0:
            passes_taken = i + 1
            break

    return offset, score, passes_taken


def _banded_xcorr_fine(band_envs_ch, band_envs_rs, coarse_rate,
                        anchor_s, window_s=2.0):
    """Per-band xcorr (CH vs RS) within ±window_s of anchor_s.

    For each band, xcorr the full envelopes restricted to lags near anchor_s.
    Returns the correlation-weighted mean offset, or anchor_s on failure.
    """
    anchor_lag = -round(anchor_s * coarse_rate)
    fine_max   = max(1, int(window_s * coarse_rate))

    results = []
    for band in ("kick", "snare", "cymbal"):
        ch_n = _normalize(band_envs_ch.get(band, []))
        rs_n = _normalize(band_envs_rs.get(band, []))
        if not ch_n or not rs_n:
            continue
        na, nb = len(ch_n), len(rs_n)
        best_lag, best_corr = anchor_lag, float("-inf")
        for lag in range(anchor_lag - fine_max, anchor_lag + fine_max + 1):
            a0 = max(0, lag)
            b0 = max(0, -lag)
            n  = min(na - a0, nb - b0)
            if n < 20:
                continue
            corr = sum(ch_n[a0 + k] * rs_n[b0 + k] for k in range(n)) / n
            if corr > best_corr:
                best_corr = corr
                best_lag  = lag
        results.append((-best_lag / coarse_rate, max(0.0, best_corr)))

    if not results:
        return anchor_s
    total_w = sum(c for _, c in results) or 1e-10
    return sum(o * c for o, c in results) / total_w


def _score_banded(note_frame_weights, rs_band_envs, n_rs, max_lag,
                   zero_bias=0.0):
    """Score each candidate lag using adaptive per-note frequency weights.

    Each note contributes a weighted blend of RS band energies at the candidate
    position, scaled by its simultaneity weight.  The blend weights come from
    CH audio fingerprints (or one-hot fallback), so the score collapses when
    notes land on the wrong energy without hard-coding any drum-to-band mapping.

    Returns (best_lag, best_avg_score).  Sign: offset_s = -lag / coarse_rate.
    """
    rs_kick   = rs_band_envs.get("kick",   [])
    rs_snare  = rs_band_envs.get("snare",  [])
    rs_cymbal = rs_band_envs.get("cymbal", [])

    best_lag, best_score = 0, float("-inf")
    for lag in range(-max_lag, max_lag + 1):
        score   = 0.0
        total_w = 0.0
        count   = 0
        for frame, (wk, ws, wc), sim_w in note_frame_weights:
            idx = frame - lag
            if 0 <= idx < n_rs:
                note_sc  = (wk * rs_kick[idx]
                            + ws * rs_snare[idx]
                            + wc * rs_cymbal[idx])
                score   += note_sc * sim_w
                total_w += sim_w
                count   += 1
        if count < 10:
            continue
        avg = score / total_w - zero_bias * abs(lag)
        if avg > best_score:
            best_score = avg
            best_lag   = lag
    return best_lag, best_score


def _audio_align(ch_dir, rs_sloppak_path, verbose, ch_note_band_pairs=None):
    """Banded two-pass note-to-audio alignment (primary) or onset xcorr (fallback).

    Primary path (drum chart available):
      Extracts the RS stem into three frequency bands (kick/snare/cymbal) and
      scores each candidate offset by matching note types to their expected band.
      Pass 1 searches ±30 s; pass 2 applies that offset and re-checks ±2 s.
      No CH audio is required — only RS audio and chart note data.

    Fallback path (no drum chart): full-song onset xcorr + multi-clip voting.

    Returns (offset_seconds, drum_anchor: bool) or (None, False) on failure.
    """
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

        max_lag = int(_COARSE_SEARCH_S * _COARSE_RATE)

        if ch_note_band_pairs:
            # --- Extract CH drum audio bands early for adaptive note weights ---
            ch_drum_files = _find_ch_drum_files(ch_dir)
            band_envs_ch  = None
            if ch_drum_files:
                if verbose:
                    ch_label = (f"drums ({len(ch_drum_files)} stem"
                                f"{'s' if len(ch_drum_files) > 1 else ''})")
                    print(f"  audio    : extracting CH {ch_label} frequency profile …")
                band_envs_ch = _extract_band_envs_paths(ch_drum_files)

            # Pre-compute per-note frequency weight tuples.
            # When CH audio available: builds per-type aggregate profiles with
            # ±400 ms windowed peak search, robust to intro/outro timing drift.
            note_frame_weights = _prep_note_weights(
                ch_note_band_pairs, band_envs_ch, _COARSE_RATE)
            if verbose and band_envs_ch:
                profiles = _build_type_profiles(
                    ch_note_band_pairs, band_envs_ch, _COARSE_RATE)
                if profiles:
                    def _pct(w): return f"k{w[0]:.0%}/s{w[1]:.0%}/c{w[2]:.0%}"
                    print(f"  audio    : type profiles — "
                          f"kick={_pct(profiles['kick'])}  "
                          f"snare={_pct(profiles['snare'])}  "
                          f"cymbal={_pct(profiles['cymbal'])}")

            # --- 80-pass iterative frequency-adaptive note-to-audio ---
            mode_label = "adaptive" if band_envs_ch else "fixed-band"
            if verbose:
                print(f"  audio    : note→audio ({mode_label}, {_N_ALIGN_PASSES} passes) "
                      f"vs RS {rs_label} …")
            band_envs_rs = _extract_band_envs(rs_tmp)
            if not band_envs_rs:
                if verbose:
                    print("  audio    : RS band extraction failed")
                return None, False

            rs_dur_s = len(next(iter(band_envs_rs.values()))) / _COARSE_RATE
            offset_n, score_n, n_iters = _note_align_iterative(
                note_frame_weights, band_envs_rs, _COARSE_RATE,
                max_search_s=_COARSE_SEARCH_S, n_passes=_N_ALIGN_PASSES,
                zero_bias=_COARSE_ZERO_BIAS)
            if verbose:
                print(f"  audio    : note→audio  {offset_n:+.3f}s  "
                      f"(score {score_n:.4f}, {n_iters}/{_N_ALIGN_PASSES} passes, "
                      f"{len(ch_note_band_pairs)} hits [{mode_label}], RS={rs_dur_s:.0f}s)")

            # --- Banded CH-audio vs RS-audio xcorr fine-tuning ---
            total_s = offset_n
            if band_envs_ch:
                if verbose:
                    print(f"  audio    : banded xcorr within ±{_AUDIO_AGREE_S*3:.1f}s …")
                offset_a = _banded_xcorr_fine(
                    band_envs_ch, band_envs_rs, _COARSE_RATE,
                    anchor_s=offset_n, window_s=_AUDIO_AGREE_S * 3)
                diff = abs(offset_a - offset_n)
                if diff < _AUDIO_AGREE_S:
                    total_s = offset_a
                    if verbose:
                        print(f"  audio    : audio xcorr {offset_a:+.3f}s  "
                              f"(diff {diff:.3f}s ≤ {_AUDIO_AGREE_S}s) → using audio")
                else:
                    if verbose:
                        print(f"  audio    : audio xcorr {offset_a:+.3f}s  "
                              f"(diff {diff:.3f}s > {_AUDIO_AGREE_S}s) → keeping notes")

            return total_s, True

        # --- Fallback: full onset xcorr + multi-clip voting (no drum chart) ---
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

        if verbose:
            print(f"  audio    : CH {ch_label} → RS {rs_label} (xcorr fallback) …")

        rs_coarse_pcm = _extract_pcm(rs_tmp, _COARSE_SR)
        if not rs_coarse_pcm:
            return None, False
        rs_env_raw = _onset_envelope(rs_coarse_pcm, _COARSE_WIN)
        if not rs_env_raw:
            return None, False

        ch_coarse_pcm = _sum_pcm(ch_paths, _COARSE_SR)
        if not ch_coarse_pcm:
            return None, False
        ch_env = _normalize(_onset_envelope(ch_coarse_pcm, _COARSE_WIN))
        rs_env = _normalize(rs_env_raw)
        if not ch_env or not rs_env:
            return None, False

        ch_dur_s  = len(ch_coarse_pcm) / _COARSE_SR
        rs_dur_s  = len(rs_coarse_pcm) / _COARSE_SR
        lag1, _   = _xcorr_best(ch_env, rs_env, max_lag, zero_bias=_COARSE_ZERO_BIAS)
        offset1_s = -lag1 / _COARSE_RATE
        if verbose:
            print(f"  audio    : onset xcorr  CH={ch_dur_s:.0f}s RS={rs_dur_s:.0f}s "
                  f"→ coarse {offset1_s:+.3f}s")

        fine_max_lag = max(1, int(_FINE_SEARCH_S * _FINE_SR))
        clip_samples = int(_FINE_CLIP_S * _FINE_SR)
        ch_full = _sum_pcm(ch_paths, _FINE_SR, duration_s=_MULTI_FULL_S)
        rs_full = _extract_pcm(rs_tmp, _FINE_SR, duration_s=_MULTI_FULL_S)

        vote_results = []
        pos = _MULTI_START_S
        while pos + _FINE_CLIP_S <= _MULTI_FULL_S:
            ch_i = int(pos * _FINE_SR)
            rs_i = int((pos + offset1_s) * _FINE_SR)
            if (0 <= ch_i and ch_i + clip_samples <= len(ch_full) and
                    0 <= rs_i and rs_i + clip_samples <= len(rs_full)):
                ch_clip = _normalize(ch_full[ch_i: ch_i + clip_samples])
                rs_clip = _normalize(rs_full[rs_i: rs_i + clip_samples])
                if ch_clip and rs_clip:
                    lag_f, corr_f = _xcorr_best(ch_clip, rs_clip, fine_max_lag)
                    if corr_f >= _MULTI_MIN_CORR:
                        vote_results.append((-lag_f / _FINE_SR, corr_f))
            pos += _MULTI_STEP_S

        if not vote_results:
            return offset1_s, use_drums

        bins = {}
        for fine_off, corr in vote_results:
            b = round(fine_off / _MULTI_BIN_S)
            bins[b] = bins.get(b, 0.0) + corr
        best_bin    = max(bins, key=bins.get)
        inliers     = [(o, c) for o, c in vote_results
                       if abs(o - best_bin * _MULTI_BIN_S) <= _MULTI_BIN_S]
        n_inliers   = len(inliers)
        cluster_off = sum(o for o, c in inliers) / n_inliers
        avg_corr    = sum(c for o, c in inliers) / n_inliers

        confident = n_inliers >= _MULTI_MIN_CLIPS and avg_corr >= _FINE_CONF_MIN

        if not confident:
            if verbose:
                print(f"  audio    : vote low-conf (n={n_inliers}/{len(vote_results)}, "
                      f"corr={avg_corr:.3f}), coarse {offset1_s:+.3f}s as loose prior")
            return offset1_s, False

        total = offset1_s + cluster_off
        if verbose:
            print(f"  audio    : {n_inliers}/{len(vote_results)} clips → fine {cluster_off:+.3f}s "
                  f"total {total:+.3f}s  (avg corr {avg_corr:.3f})")
        return total, use_drums

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

def merge(ch_dir, rs_sloppak_path, output_path=None, offset_ms=None, nudge_ms=0.0,
          verbose=True, split_drums=False):
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
            # Build (time_s, band) pairs from the best drum difficulty.
            # Times are in CH audio time (chart_time + ch_audio_delay_s) so they
            # align with the RS audio timeline being scored against.
            ch_note_band_pairs = None
            if "drums" in parsed["tracks"] and parsed["tracks"]["drums"]:
                drum_track = parsed["tracks"]["drums"]
                best_diff  = max(drum_track.keys())
                ch_note_band_pairs = _drum_hits_to_band_pairs(
                    drum_track[best_diff], time_offset_s=ch_audio_delay_s)

            audio_offset_s, drum_anchor = _audio_align(
                ch_dir, rs_sloppak_path, verbose, ch_note_band_pairs)
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
    # Guitar/bass tracks always get "-gamepad" IDs; drums deferred in split mode.
    _GAMEPAD_IDS    = {"lead": "lead-gamepad", "bass": "bass-gamepad"}
    _DIFF_CAP       = {3: "Expert", 2: "Hard", 1: "Medium", 0: "Easy"}
    ch_arrangements = {}
    drums_diff_dict_ch = None

    for track_id, diff_dict in parsed["tracks"].items():
        if not diff_dict:
            continue
        if track_id == "drums":
            drums_diff_dict_ch = diff_dict
            if not split_drums:
                ch_arrangements["drums"]       = arrangement_writer.convert_drums(diff_dict)
                ch_arrangements["drums_score"] = arrangement_writer.convert_drums_score(diff_dict)
        elif track_id == "keys":
            ch_arrangements["keys"] = arrangement_writer.convert_keys(diff_dict)
        else:
            out_id = _GAMEPAD_IDS.get(track_id, track_id)
            ch_arrangements[out_id] = arrangement_writer.convert_guitar(
                diff_dict, arrangement_name=out_id)

    # --- drum_tabs (non-split only; split path handles per-file below) ---
    drum_tabs = {}
    if not split_drums and "drums" in parsed["tracks"] and parsed["tracks"]["drums"]:
        dt = drum_tab_writer.convert(parsed["tracks"]["drums"])
        if dt:
            drum_tabs["drums"] = dt

    # Retime non-drum CH arrangements and stamp RS beats
    for arr in ch_arrangements.values():
        _retime_arrangement(arr, shift_fn)
        if rs["rs_beats"]:
            arr["beats"] = rs["rs_beats"]

    # Retime drum_tab hits (non-split)
    for tab in drum_tabs.values():
        for hit in tab["hits"]:
            hit["t"] = round(shift_fn(hit["t"]), 3)
        tab["hits"].sort(key=lambda h: h["t"])
    if verbose and drum_tabs:
        total_hits = sum(len(t["hits"]) for t in drum_tabs.values())
        print(f"  drum_tab : {total_hits} hits")

    # --- lyrics: prefer RS; fall back to CH ---
    lyrics_data = []
    if not rs["has_lyrics"]:
        raw = parsed.get("lyrics", [])
        if raw:
            lyrics_data = lyrics_writer.convert(raw)
            if verbose and lyrics_data:
                print(f"  lyrics   : {len(lyrics_data)} syllables (from CH)")

    # --- split-drums: one merged file per difficulty ---
    if split_drums and drums_diff_dict_ch:
        if output_path:
            output_base = os.path.splitext(output_path)[0]
        else:
            output_base = os.path.splitext(rs_sloppak_path)[0] + "+ch"

        rs_meta = rs["manifest"]
        outputs = []
        for diff, hits in sorted(drums_diff_dict_ch.items(), reverse=True):
            diff_name = _DIFF_CAP.get(diff, str(diff))
            diff_out  = f"{output_base} ({diff_name}).sloppak"

            drums_arr       = arrangement_writer.convert_drums({diff: hits})
            drums_score_arr = arrangement_writer.convert_drums_score({diff: hits})
            _retime_arrangement(drums_arr, shift_fn)
            _retime_arrangement(drums_score_arr, shift_fn)
            if rs["rs_beats"]:
                drums_arr["beats"]       = rs["rs_beats"]
                drums_score_arr["beats"] = rs["rs_beats"]

            dt = drum_tab_writer.convert({diff: hits})
            diff_drum_tabs = {}
            if dt:
                for hit in dt["hits"]:
                    hit["t"] = round(shift_fn(hit["t"]), 3)
                dt["hits"].sort(key=lambda h: h["t"])
                diff_drum_tabs["drums"] = dt

            diff_ch = dict(ch_arrangements)
            diff_ch["drums"]       = drums_arr
            diff_ch["drums_score"] = drums_score_arr

            diff_merged = dict(rs["arrangements"])
            diff_added, diff_skipped = [], []
            for tid, arr in diff_ch.items():
                if tid in diff_merged:
                    diff_skipped.append(tid)
                else:
                    diff_merged[tid] = arr
                    diff_added.append(tid)

            if verbose:
                if diff_added:
                    print(f"  [{diff_name}] added   : {', '.join(diff_added)}")
                if diff_skipped:
                    print(f"  [{diff_name}] skipped : {', '.join(diff_skipped)} (RS exists)")

            diff_manifest = manifest_writer.build(
                metadata={
                    "name":       rs_meta.get("title",  "Unknown"),
                    "artist":     rs_meta.get("artist", ""),
                    "album":      rs_meta.get("album",  ""),
                    "year_clean": str(rs_meta.get("year", "")),
                },
                arrangement_ids = list(diff_merged.keys()),
                stem_ids        = rs["stem_ids"],
                cover_filename  = rs["cover_name"],
                has_lyrics      = rs["has_lyrics"] or bool(lyrics_data),
                rs_arrangements = rs_meta.get("arrangements", []),
                drum_tabs       = {"drums": "drum_tab_drums.json"} if diff_drum_tabs else {},
            )
            _write_merged(
                output_path         = diff_out,
                manifest_yaml       = manifest_writer.to_yaml_string(diff_manifest),
                merged_arrangements = diff_merged,
                rs_sloppak_path     = rs_sloppak_path,
                rs_data             = rs,
                lyrics_data         = lyrics_data,
                drum_tabs           = diff_drum_tabs,
            )
            if verbose:
                size_kb = os.path.getsize(diff_out) // 1024
                print(f"  → {diff_out}  ({size_kb} KB)")
            outputs.append(diff_out)
        return outputs

    # --- merge (non-split): RS wins on ID collision ---
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
