"""Package arrangement data, manifest, lyrics, and audio stems into a .sloppak zip.

Stem strategy
─────────────
Each stem is written as stems/<id>.ogg.  All stems are listed in the manifest
with default: 'on' so Slopsmith plays them all simultaneously (same as
Rocksmith-sourced sloppaks).

No merging is performed.  If a song has guitar.ogg + bass.ogg + drums.ogg,
all three are included individually.  If it only has song.ogg, that becomes
stems/full.ogg.

Conversion: any non-OGG audio (mp3, opus, wav, flac…) is converted via ffmpeg.
Split drum stems (drums_1.ogg … drums_4.ogg) are merged into a single drums.ogg
since they represent one instrument's parts, not separate mixable layers.
"""

import json
import os
import shutil
import subprocess
import tempfile
import zipfile

# ---------------------------------------------------------------------------
# Known CH stem filenames
# ---------------------------------------------------------------------------

_NEEDS_CONVERSION = {".mp3", ".opus", ".wav", ".flac", ".m4a", ".aac"}
_AUDIO_EXTS       = {".ogg"} | _NEEDS_CONVERSION

# (filename, stem_id) – first match per stem_id wins
_INDIVIDUAL_STEMS = [
    ("guitar.ogg",   "guitar"), ("guitar.opus",  "guitar"), ("guitar.mp3",  "guitar"),
    ("bass.ogg",     "bass"),   ("bass.opus",    "bass"),   ("bass.mp3",    "bass"),
    ("drums.ogg",    "drums"),  ("drums.opus",   "drums"),  ("drums.mp3",   "drums"),
    ("vocals.ogg",   "vocals"), ("vocals.opus",  "vocals"), ("vocals.mp3",  "vocals"),
    ("keys.ogg",     "keys"),   ("keys.opus",    "keys"),   ("keys.mp3",    "keys"),
    ("rhythm.ogg",   "rhythm"), ("rhythm.opus",  "rhythm"), ("rhythm.mp3",  "rhythm"),
]

# Pre-mixed full-track names (used when no individual stems exist)
_PREMIX_NAMES = ["song.ogg", "song.opus", "song.mp3",
                 "audio.ogg", "audio.opus", "audio.mp3"]


# ---------------------------------------------------------------------------
# ffmpeg helpers
# ---------------------------------------------------------------------------

def _ffmpeg_available():
    try:
        subprocess.run(["ffmpeg", "-version"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


def _to_ogg(src, dst):
    subprocess.run(
        ["ffmpeg", "-y", "-i", src, "-c:a", "libvorbis", "-q:a", "5", dst],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _merge_to_ogg(src_list, dst):
    inputs = []
    for s in src_list:
        inputs += ["-i", s]
    n = len(src_list)
    fc = "".join(f"[{i}]" for i in range(n)) + \
         f"amix=inputs={n}:duration=longest:normalize=0"
    subprocess.run(
        ["ffmpeg", "-y"] + inputs + ["-filter_complex", fc,
         "-c:a", "libvorbis", "-q:a", "5", dst],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


# ---------------------------------------------------------------------------
# Stem discovery
# ---------------------------------------------------------------------------

def _find_stems(song_dir):
    """
    Returns:
      premix      – (abs_path, needs_conversion) or None
      indiv       – {stem_id: (abs_path, needs_conversion)}
      drum_splits – [(abs_path, needs_conversion), ...]
    """
    premix = None
    for name in _PREMIX_NAMES:
        p = os.path.join(song_dir, name)
        if os.path.isfile(p):
            ext = os.path.splitext(name)[1].lower()
            premix = (p, ext in _NEEDS_CONVERSION)
            break

    indiv = {}
    for name, stem_id in _INDIVIDUAL_STEMS:
        if stem_id in indiv:
            continue
        p = os.path.join(song_dir, name)
        if os.path.isfile(p):
            ext = os.path.splitext(name)[1].lower()
            indiv[stem_id] = (p, ext in _NEEDS_CONVERSION)

    drum_splits = []
    for i in range(1, 9):
        for ext in (".ogg", ".opus", ".mp3"):
            p = os.path.join(song_dir, f"drums_{i}{ext}")
            if os.path.isfile(p):
                drum_splits.append((p, ext in _NEEDS_CONVERSION))
                break

    # Fallback: grab any audio file if nothing else found
    if premix is None and not indiv and not drum_splits:
        for fname in sorted(os.listdir(song_dir)):
            ext = os.path.splitext(fname)[1].lower()
            if ext in _AUDIO_EXTS:
                premix = (os.path.join(song_dir, fname), ext in _NEEDS_CONVERSION)
                break

    return premix, indiv, drum_splits


# ---------------------------------------------------------------------------
# Cover art
# ---------------------------------------------------------------------------

def find_cover(song_dir):
    for name in ("album.png", "album.jpg", "album.jpeg", "folder.jpg", "folder.png"):
        p = os.path.join(song_dir, name)
        if os.path.isfile(p):
            return p
    return None


# ---------------------------------------------------------------------------
# Stem resolution (conversion only, no merging of separate instruments)
# ---------------------------------------------------------------------------

def resolve_stems(song_dir, tmp_dir, verbose=False):
    """
    Convert / copy all stems to OGG files in tmp_dir.

    Returns list of (stem_id, ogg_path) in the order they should appear in
    the manifest.  All stems are independent — no full-mix merging.
    """
    ffmpeg_ok = _ffmpeg_available()
    premix, indiv, drum_splits = _find_stems(song_dir)

    def to_ogg(src, stem_id, needs_conv):
        dst = os.path.join(tmp_dir, stem_id + ".ogg")
        if os.path.exists(dst):
            return dst
        if needs_conv:
            if not ffmpeg_ok:
                if verbose:
                    print(f"  WARNING: ffmpeg not found; skipping {os.path.basename(src)}")
                return None
            try:
                _to_ogg(src, dst)
                return dst
            except subprocess.CalledProcessError as e:
                if verbose:
                    print(f"  WARNING: ffmpeg failed for {os.path.basename(src)}: {e}")
                return None
        shutil.copy2(src, dst)
        return dst

    result = []  # [(stem_id, ogg_path)]

    # Merge split drum stems into one drums.ogg
    if drum_splits and "drums" not in indiv:
        ogg_splits = []
        for i, (sp, sc) in enumerate(drum_splits):
            o = to_ogg(sp, f"_drumsplit{i}", sc)
            if o:
                ogg_splits.append(o)
        if ogg_splits:
            if len(ogg_splits) == 1:
                dst = os.path.join(tmp_dir, "drums.ogg")
                shutil.copy2(ogg_splits[0], dst)
                result.append(("drums", dst))
            elif ffmpeg_ok:
                dst = os.path.join(tmp_dir, "drums.ogg")
                try:
                    _merge_to_ogg(ogg_splits, dst)
                    result.append(("drums", dst))
                except subprocess.CalledProcessError as e:
                    if verbose:
                        print(f"  WARNING: drum split merge failed: {e}")

    # Individual named stems
    for stem_id, (src, needs_conv) in indiv.items():
        ogg = to_ogg(src, stem_id, needs_conv)
        if ogg:
            result.append((stem_id, ogg))

    # Pre-mix / fallback (only when no individual stems found)
    if not result and premix:
        ogg = to_ogg(premix[0], "full", premix[1])
        if ogg:
            result.append(("full", ogg))

    return result


def find_stem_ids(song_dir):
    """
    Lightweight discovery of stem IDs — mirrors resolve_stems() logic without
    running ffmpeg.  Used to build the manifest before writing the zip.
    """
    premix, indiv, drum_splits = _find_stems(song_dir)

    ids = []
    if drum_splits and "drums" not in indiv:
        ids.append("drums")
    ids.extend(indiv.keys())

    if not ids and premix:
        ids.append("full")

    return ids


# ---------------------------------------------------------------------------
# Main write function
# ---------------------------------------------------------------------------

def write(output_path, manifest_yaml, arrangements, lyrics_data,
          song_dir, cover_path=None, verbose=False, drum_tab_data=None,
          drum_tabs=None):
    """
    Create a .sloppak zip.

    Args:
      output_path:   destination path
      manifest_yaml: serialised YAML string
      arrangements:  {track_id: arrangement_dict}
      lyrics_data:   list of sloppak lyric dicts, or []
      song_dir:      source CH song folder
      cover_path:    cover image path (None = auto-detect)
      verbose:       print stem progress
      drum_tab_data: legacy single drum_tab dict (writes drum_tab.json)
      drum_tabs:     {arrangement_id: drum_tab_dict} — writes drum_tab_<id>.json per entry
    """
    if cover_path is None:
        cover_path = find_cover(song_dir)

    tmp_dir = tempfile.mkdtemp()
    try:
        stems = resolve_stems(song_dir, tmp_dir, verbose)

        with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("manifest.yaml", manifest_yaml)

            for track_id, arr_dict in arrangements.items():
                zf.writestr(
                    f"arrangements/{track_id}.json",
                    json.dumps(arr_dict, separators=(",", ":")),
                )

            if lyrics_data:
                zf.writestr("lyrics.json",
                            json.dumps(lyrics_data, ensure_ascii=False,
                                       separators=(",", ":")))

            if drum_tabs:
                for arr_id, tab in drum_tabs.items():
                    zf.writestr(f"drum_tab_{arr_id}.json",
                                json.dumps(tab, separators=(",", ":")))
            elif drum_tab_data:
                zf.writestr("drum_tab.json",
                            json.dumps(drum_tab_data, separators=(",", ":")))

            for stem_id, ogg_path in stems:
                if os.path.isfile(ogg_path):
                    zf.write(ogg_path, f"stems/{stem_id}.ogg")

            if cover_path and os.path.isfile(cover_path):
                ext = os.path.splitext(cover_path)[1].lower()
                zf.write(cover_path, f"cover{ext}")

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return output_path
