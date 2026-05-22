"""Batch conversion and auto-merge for ch2sloppak.

Scans a directory tree for Clone Hero song folders (any folder containing
notes.mid or notes.chart), then either converts each one standalone or
merges it with a matching .sloppak from a library directory.

Matching uses normalised artist+title with fuzzy fallback (difflib).
Songs whose CH tracks are already present in the matched sloppak are skipped.
A mergelog.txt is written to the calling directory listing every merge.
"""

import datetime
import os
import re
import subprocess
import zipfile
from difflib import SequenceMatcher

import yaml


# Mirrors merge.py's _GAMEPAD_IDS renaming — keep in sync if that changes.
_GAMEPAD_IDS = {"lead": "lead-gamepad", "bass": "bass-gamepad"}

# Fuzzy match thresholds (SequenceMatcher ratio, 0–1).
_FUZZY_COMBINED_THRESHOLD = 0.60   # artist+title sequence similarity
_FUZZY_TITLE_THRESHOLD    = 0.72   # title-only sequence similarity
_JACCARD_COMBINED_THRESHOLD = 0.45 # word-overlap on artist+title
_JACCARD_TITLE_THRESHOLD    = 0.55 # word-overlap on title alone

# Duration tolerance for ffprobe-based matching (seconds).
_DURATION_TOLERANCE_S = 15.0
# Minimum text similarity required when duration is the deciding factor —
# prevents matching two songs of the same length but completely different names.
_DURATION_MIN_TEXT_SIM = 0.30

# Common noise tokens stripped before deep comparison.
_NOISE = re.compile(
    r"\b(feat|ft|featuring|the|a|an|and|remaster|remastered|live|official|"
    r"version|deluxe|edition|ep|lp|single|radio|edit|cover|acoustic|demo)\b"
)

# Audio file extensions to probe for CH duration
_AUDIO_EXTS = {".ogg", ".mp3", ".opus", ".wav", ".flac", ".m4a"}


def _normalize(s):
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9\s]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _normalize_deep(s):
    s = _normalize(s)
    s = _NOISE.sub("", s)
    return re.sub(r"\s+", " ", s).strip()


def _sim(a, b):
    return SequenceMatcher(None, a, b).ratio()


def _jaccard(a, b):
    wa, wb = set(a.split()), set(b.split())
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def _content_duration(path):
    """
    Use ffmpeg silencedetect to measure the actual musical content duration,
    trimming leading and trailing silence from both ends.

    Returns content duration in seconds, or None on failure.

    silencedetect noise floor: -45 dB, minimum silence: 0.5 s.
    These values tolerate breath noise and light room tone without
    mis-classifying quiet passages as silence.
    """
    try:
        r = subprocess.run(
            ["ffmpeg", "-i", path,
             "-af", "silencedetect=noise=-45dB:d=0.5",
             "-f", "null", "-"],
            capture_output=True, text=True, timeout=120,
        )
        output = r.stderr  # ffmpeg diagnostic output goes to stderr

        # Total duration from the header line
        total = None
        for line in output.splitlines():
            m = re.search(r"Duration:\s+(\d+):(\d+):(\d+\.?\d*)", line)
            if m:
                total = (float(m.group(1)) * 3600
                         + float(m.group(2)) * 60
                         + float(m.group(3)))
                break
        if total is None:
            return None

        # Collect silence intervals
        starts, ends = [], []
        for line in output.splitlines():
            ms = re.search(r"silence_start:\s*([\d.e+-]+)", line)
            me = re.search(r"silence_end:\s*([\d.e+-]+)", line)
            if ms:
                starts.append(float(ms.group(1)))
            if me:
                ends.append(float(me.group(1)))

        # Leading silence: a silence interval that begins at or before 0.1 s
        content_start = 0.0
        if ends and starts and starts[0] <= 0.1:
            content_start = ends[0]
        elif ends and not starts:
            # silence_end with no preceding silence_start → starts at 0
            content_start = ends[0]

        # Trailing silence: a silence interval with no matching end
        # (extends to EOF) or whose end equals/exceeds total duration
        content_end = total
        if starts:
            last_start = starts[-1]
            last_has_end = len(ends) >= len(starts)
            if not last_has_end or (ends and ends[-1] >= total - 0.1):
                content_end = last_start

        return max(0.0, content_end - content_start)

    except Exception:
        return None


def _ch_audio_duration(ch_dir):
    """Return content duration of the first audio file found in ch_dir, or None."""
    for fname in sorted(os.listdir(ch_dir)):
        if os.path.splitext(fname)[1].lower() in _AUDIO_EXTS:
            return _content_duration(os.path.join(ch_dir, fname))
    return None


def _sloppak_audio_duration(sloppak_path):
    """
    Extract the first stem from the sloppak zip to a temp file,
    measure its content duration, then delete the temp file.
    Returns content duration in seconds or None.
    """
    import tempfile
    import shutil
    try:
        with zipfile.ZipFile(sloppak_path) as zf:
            stems = [n for n in zf.namelist()
                     if n.startswith("stems/") and n.endswith(".ogg")]
            if not stems:
                return None
            tmp = tempfile.mktemp(suffix=".ogg")
            with zf.open(stems[0]) as src, open(tmp, "wb") as dst:
                shutil.copyfileobj(src, dst)
        dur = _content_duration(tmp)
        os.unlink(tmp)
        return dur
    except Exception:
        return None


def find_match(artist, title, library_index, ch_dir=None):
    """
    Find the best matching sloppak for the given artist+title.

    Matching tiers (first hit wins):
      1. Exact normalized key
      2. Sequence fuzzy on artist+title       ≥ FUZZY_COMBINED_THRESHOLD
      3. Sequence fuzzy deep (noise stripped)  ≥ FUZZY_COMBINED_THRESHOLD
      4. Jaccard word-overlap on artist+title  ≥ JACCARD_COMBINED_THRESHOLD
      5. Sequence fuzzy title-only             ≥ FUZZY_TITLE_THRESHOLD
      6. Jaccard word-overlap title-only       ≥ JACCARD_TITLE_THRESHOLD
      7. Audio duration match via ffprobe      within DURATION_TOLERANCE_S
         (requires ch_dir; only fires if text similarity ≥ DURATION_MIN_TEXT_SIM)

    Returns (path, manifest, score_description) or (None, None, None).
    """
    norm_artist = _normalize(artist)
    norm_title  = _normalize(title)
    key         = (norm_artist + " " + norm_title).strip()
    deep_key    = (_normalize_deep(artist) + " " + _normalize_deep(title)).strip()

    # Pre-compute per-candidate normalised strings once
    candidates = []
    for cand_key, (path, manifest) in library_index.items():
        cand_norm   = cand_key
        cand_deep   = (_normalize_deep(manifest.get("artist", "")) + " " +
                       _normalize_deep(manifest.get("title", ""))).strip()
        cand_title  = _normalize(manifest.get("title", ""))
        candidates.append((path, manifest, cand_norm, cand_deep, cand_title))

    # 1. Exact
    if key in library_index:
        return (*library_index[key], "exact")

    # 2 & 3. Sequence fuzzy (normal + deep)
    for q, cand_field, label in [
        (key,      2, "fuzzy"),
        (deep_key, 3, "fuzzy-deep"),
    ]:
        best_score, best_item = 0.0, None
        for path, manifest, cand_norm, cand_deep, _ in candidates:
            cq = cand_norm if cand_field == 2 else cand_deep
            score = _sim(q, cq)
            if score > best_score:
                best_score, best_item = score, (path, manifest)
        if best_item and best_score >= _FUZZY_COMBINED_THRESHOLD:
            return (*best_item, f"{label} {best_score:.0%}")

    # 4. Jaccard on artist+title
    best_score, best_item = 0.0, None
    for path, manifest, _, cand_deep, _ in candidates:
        score = _jaccard(deep_key, cand_deep)
        if score > best_score:
            best_score, best_item = score, (path, manifest)
    if best_item and best_score >= _JACCARD_COMBINED_THRESHOLD:
        return (*best_item, f"jaccard {best_score:.0%}")

    # 5. Sequence fuzzy title-only
    if norm_title:
        best_score, best_item = 0.0, None
        for path, manifest, _, _, cand_title in candidates:
            score = _sim(norm_title, cand_title)
            if score > best_score:
                best_score, best_item = score, (path, manifest)
        if best_item and best_score >= _FUZZY_TITLE_THRESHOLD:
            return (*best_item, f"title-fuzzy {best_score:.0%}")

    # 6. Jaccard title-only
    if norm_title:
        deep_title = _normalize_deep(title)
        best_score, best_item = 0.0, None
        for path, manifest, _, _, cand_title in candidates:
            score = _jaccard(deep_title, _normalize_deep(manifest.get("title", "")))
            if score > best_score:
                best_score, best_item = score, (path, manifest)
        if best_item and best_score >= _JACCARD_TITLE_THRESHOLD:
            return (*best_item, f"title-jaccard {best_score:.0%}")

    # 7. Audio duration fallback (requires ffprobe + ch_dir)
    if ch_dir:
        ch_dur = _ch_audio_duration(ch_dir)
        if ch_dur:
            # Score each candidate by text similarity; only consider those
            # whose manifest duration is close enough.
            best_score, best_item = 0.0, None
            for path, manifest, cand_norm, cand_deep, _ in candidates:
                # Check manifest duration first (cheap); fall back to probing
                # the sloppak audio only when manifest has no duration.
                m_dur = manifest.get("duration")
                if m_dur:
                    m_dur = float(m_dur)
                else:
                    m_dur = _sloppak_audio_duration(path)
                if m_dur is None or abs(m_dur - ch_dur) > _DURATION_TOLERANCE_S:
                    continue
                text_score = max(_sim(key, cand_norm), _sim(deep_key, cand_deep))
                if text_score >= _DURATION_MIN_TEXT_SIM and text_score > best_score:
                    best_score, best_item = text_score, (path, manifest)
            if best_item:
                return (*best_item, f"duration+text {best_score:.0%}")

    return None, None, None


def find_ch_folders(root):
    """Yield paths of CH song folders under root (contain notes.mid/.chart)."""
    for dirpath, dirnames, filenames in os.walk(root):
        lower = {f.lower() for f in filenames}
        if "notes.mid" in lower or "notes.chart" in lower:
            yield dirpath
            dirnames.clear()  # don't recurse into a song folder's subdirs


def _read_sloppak_manifest(sloppak_path):
    try:
        with zipfile.ZipFile(sloppak_path) as zf:
            with zf.open("manifest.yaml") as f:
                return yaml.safe_load(f)
    except Exception:
        return None


def build_library_index(library_dir):
    """
    Scan library_dir for .sloppak files.
    Returns { normalize(artist+" "+title): (path, manifest) }.
    First match per key wins (sorted filenames → deterministic).
    The manifest is stored in the tuple so fuzzy matching can re-normalize
    artist/title fields independently.
    """
    index = {}
    for fname in sorted(os.listdir(library_dir)):
        if not fname.lower().endswith(".sloppak"):
            continue
        path = os.path.join(library_dir, fname)
        manifest = _read_sloppak_manifest(path)
        if not manifest:
            continue
        key = (_normalize(manifest.get("artist", "")) + " " +
               _normalize(manifest.get("title", "")))
        if key not in index:
            index[key] = (path, manifest)
    return index


def predict_merge_ids(tracks_dict):
    """
    Given a parsed tracks dict, return the arrangement IDs a merge would add.
    Mirrors merge.py track→arrangement mapping including gamepad renaming.
    """
    ids = []
    for track_id, diff_dict in tracks_dict.items():
        if not diff_dict:
            continue
        if track_id == "drums":
            ids += ["drums", "drums_score"]
        elif track_id == "keys":
            ids.append("keys")
        else:
            ids.append(_GAMEPAD_IDS.get(track_id, track_id))
    return ids


def missing_from_manifest(manifest, candidate_ids):
    """Return which candidate_ids are not already in the manifest."""
    existing = {a["id"] for a in manifest.get("arrangements", [])}
    return [i for i in candidate_ids if i not in existing]


def write_mergelog(entries, dest_dir):
    """Write mergelog.txt to dest_dir. Returns the written path."""
    path = os.path.join(dest_dir, "mergelog.txt")
    lines = [
        f"ch2sloppak merge log — {datetime.date.today()}",
        "=" * 48,
        "",
    ]
    for e in entries:
        label = f"{e['artist']} — {e['title']}" if e["artist"] else e["title"]
        lines += [
            f"[{label}]",
            f"  CH folder : {e['ch_dir']}",
            f"  Matched   : {os.path.basename(e['rs_path'])}",
            f"  Added     : {', '.join(e['added_tracks'])}",
            f"  Output    : {e['output']}",
            "",
        ]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path


def write_skippedlog(entries, dest_dir):
    """Write skipped.txt to dest_dir. Returns the written path."""
    path = os.path.join(dest_dir, "skipped.txt")
    lines = [
        f"ch2sloppak skipped log — {datetime.date.today()}",
        "=" * 48,
        "",
    ]
    for e in entries:
        label = f"{e['artist']} — {e['title']}" if e["artist"] else e["title"]
        lines += [
            f"[{label}]",
            f"  CH folder : {e['ch_dir']}",
            f"  Reason    : {e['reason']}",
            f"  Output    : {e['output']}",
            "",
        ]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path


def run(root_dir, output_dir=None, library_dir=None, force=False, verbose=True):
    """
    Batch-convert all CH song folders found under root_dir.

    If library_dir is given, each CH folder is matched against .sloppak files
    there by normalised artist+title.  A match triggers a merge instead of a
    standalone convert; folders with no match are converted standalone.
    Folders whose CH tracks are already present in the matched sloppak are
    skipped entirely.

    By default, any output file that already exists on disk is skipped.
    Pass force=True to overwrite existing outputs.

    Writes mergelog.txt to the calling directory whenever merges occur.
    Writes skipped.txt to the calling directory whenever items are skipped.

    Returns (converted, merged, skipped, errors) counts.
    """
    from parsers import chart as chart_parser
    from parsers import mid as mid_parser
    from parsers import song_ini
    import ch2sloppak as main_mod
    import merge as merge_mod

    root_dir = os.path.abspath(root_dir)
    cwd = os.getcwd()

    ch_folders = list(find_ch_folders(root_dir))
    if not ch_folders:
        if verbose:
            print(f"No CH song folders found under {root_dir}")
        return 0, 0, 0, 0

    if verbose:
        print(f"Found {len(ch_folders)} CH song folder(s)")

    library_index = build_library_index(library_dir) if library_dir else {}

    merge_entries = []
    skipped_entries = []
    converted = merged = skipped = errors = 0

    for ch_dir in ch_folders:
        mid_path   = main_mod._find_mid(ch_dir)
        chart_path = main_mod._find_chart(ch_dir)
        ini_path   = main_mod._find_song_ini(ch_dir)

        if verbose:
            print(f"\n[{os.path.basename(ch_dir)}]")

        try:
            if mid_path:
                try:
                    parsed = mid_parser.parse(mid_path)
                except Exception as exc:
                    if chart_path:
                        if verbose:
                            print(f"  WARNING: MIDI parse failed ({exc}); falling back to .chart")
                        parsed = chart_parser.parse(chart_path)
                    else:
                        raise
            elif chart_path:
                parsed = chart_parser.parse(chart_path)
            else:
                if verbose:
                    print("  SKIP: no notes file")
                skipped += 1
                continue

            ini_meta = song_ini.parse(ini_path) if ini_path else {}
            metadata = main_mod._merge_metadata(ini_meta, parsed["song_meta"])
            title  = metadata.get("name") or metadata.get("title") or os.path.basename(ch_dir)
            artist = metadata.get("artist") or ""

            if verbose:
                print(f"  {artist + ' — ' + title if artist else title}")

            # --- library match ---
            rs_path = rs_manifest = None
            if library_index:
                rs_path, rs_manifest, match_desc = find_match(
                    artist, title, library_index, ch_dir=ch_dir)
                if rs_path and verbose:
                    print(f"  Match ({match_desc}): {os.path.basename(rs_path)}")

            if rs_path:
                candidate_ids = predict_merge_ids(parsed["tracks"])

                rs_base = re.sub(r'[<>:"/\\|?*]', "",
                                 os.path.splitext(os.path.basename(rs_path))[0])
                out_path = os.path.join(
                    output_dir or os.path.dirname(rs_path),
                    f"{rs_base}+ch.sloppak",
                )

                # Decide what to check for missing tracks:
                #   • If a genuine +ch output already exists, inspect it — it
                #     may have been partially merged and could be missing tracks.
                #   • If the file at out_path exists but is NOT a +ch file
                #     (naming collision or leftover standalone convert), ignore
                #     it and check the RS source instead — merge should still run.
                #   • Otherwise check the RS source.
                out_basename = os.path.basename(out_path)
                if not force and os.path.exists(out_path) and "+ch" in out_basename:
                    existing_manifest = _read_sloppak_manifest(out_path)
                    check_manifest = existing_manifest or rs_manifest
                    check_name = out_basename
                else:
                    check_manifest = rs_manifest
                    check_name = os.path.basename(rs_path)

                missing = missing_from_manifest(check_manifest, candidate_ids)

                if not missing:
                    reason = f"all CH tracks already present in {check_name}"
                    if verbose:
                        print(f"  SKIP: {reason}")
                    skipped_entries.append({"artist": artist, "title": title,
                                            "ch_dir": ch_dir, "output": out_path,
                                            "reason": reason})
                    skipped += 1
                    continue

                if verbose:
                    action = ("Re-merging"
                              if os.path.exists(out_path) and "+ch" in out_basename
                              else "Merging")
                    print(f"  {action} with {os.path.basename(rs_path)}")
                    print(f"  Adding: {', '.join(missing)}")

                out = merge_mod.merge(
                    ch_dir=ch_dir,
                    rs_sloppak_path=rs_path,
                    output_path=out_path,
                    verbose=verbose,
                )
                merge_entries.append({
                    "artist": artist, "title": title,
                    "ch_dir": ch_dir, "rs_path": rs_path,
                    "added_tracks": missing, "output": out,
                })
                merged += 1

            else:
                # No library match — standalone convert
                safe = re.sub(r'[<>:"/\\|?*]', "",
                              f"{artist} - {title}" if artist else title)
                safe = safe.strip(". ") or "output"
                if output_dir:
                    out_path = os.path.join(output_dir, f"{safe}.sloppak")
                else:
                    out_path = os.path.join(os.path.dirname(ch_dir),
                                            f"{safe}.sloppak")

                if not force and os.path.exists(out_path):
                    reason = f"output already exists: {out_path}"
                    if verbose:
                        print(f"  SKIP: {reason}")
                    skipped_entries.append({"artist": artist, "title": title,
                                            "ch_dir": ch_dir, "output": out_path,
                                            "reason": reason})
                    skipped += 1
                    continue

                main_mod.convert(song_dir=ch_dir, output_path=out_path, verbose=verbose)
                converted += 1

        except Exception as exc:
            if verbose:
                print(f"  ERROR: {exc}")
            errors += 1

    if merge_entries:
        log_path = write_mergelog(merge_entries, cwd)
        if verbose:
            print(f"\nMerge log written: {log_path}")

    if skipped_entries:
        skip_path = write_skippedlog(skipped_entries, cwd)
        if verbose:
            print(f"Skipped log written: {skip_path}")

    if verbose:
        print(f"\nBatch complete — {converted} converted, {merged} merged, "
              f"{skipped} skipped, {errors} errors")

    return converted, merged, skipped, errors
