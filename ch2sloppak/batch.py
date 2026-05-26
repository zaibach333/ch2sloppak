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
import zipfile
from difflib import SequenceMatcher

import yaml


# Mirrors merge.py's _GAMEPAD_IDS renaming — keep in sync if that changes.
_GAMEPAD_IDS = {"lead": "lead-gamepad", "bass": "bass-gamepad"}

_DIFF_NAMES = {3: "expert", 2: "hard", 1: "medium", 0: "easy"}
_DIFF_CAPS  = {3: "Expert", 2: "Hard", 1: "Medium", 0: "Easy"}

# Fuzzy match thresholds (SequenceMatcher ratio, 0–1).
_FUZZY_COMBINED_THRESHOLD = 0.85   # artist+title sequence similarity
_FUZZY_DEEP_THRESHOLD     = 0.82   # artist+title after noise-token stripping
_FUZZY_TITLE_MIN          = 0.60   # title-alone similarity floor — prevents same-artist
                                   # cross-song matches (e.g. "Siva" matching "Today")

# Common noise tokens stripped before deep comparison.
_NOISE = re.compile(
    r"\b(feat|ft|featuring|the|a|an|and|remaster|remastered|live|official|"
    r"version|deluxe|edition|ep|lp|single|radio|edit|cover|acoustic|demo)\b"
)


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


def find_all_matches(artist, title, library_index):
    """
    Find all matching sloppaks for the given artist+title.

    Matching tiers (all candidates above threshold are returned):
      1. Exact normalized key — all files sharing that key
      2. Sequence fuzzy on artist+title            ≥ FUZZY_COMBINED_THRESHOLD
      3. Sequence fuzzy deep (noise tokens stripped) ≥ FUZZY_DEEP_THRESHOLD

    Returns list of (path, manifest, score_description), best text match first.
    Empty list if no match found.
    """
    norm_artist = _normalize(artist)
    norm_title  = _normalize(title)
    key         = (norm_artist + " " + norm_title).strip()
    deep_key    = (_normalize_deep(artist) + " " + _normalize_deep(title)).strip()

    # Pre-compute per-candidate normalised strings once (flatten all files)
    flat = []
    for cand_key, entries in library_index.items():
        for path, manifest in entries:
            cand_deep = (_normalize_deep(manifest.get("artist", "")) + " " +
                         _normalize_deep(manifest.get("title", ""))).strip()
            flat.append((path, manifest, cand_key, cand_deep))

    # 1. Exact
    if key in library_index:
        return [(path, manifest, "exact")
                for path, manifest in library_index[key]]

    deep_title = _normalize_deep(title)

    # 2. Sequence fuzzy on normalized artist+title (title floor applied)
    scored = []
    for path, manifest, cand_norm, cand_deep in flat:
        cand_title = _normalize(manifest.get("title", ""))
        if _sim(norm_title, cand_title) < _FUZZY_TITLE_MIN:
            continue
        sc = _sim(key, cand_norm)
        scored.append((path, manifest, sc, cand_deep))
    hits = [(path, manifest, f"fuzzy {sc:.0%}")
            for path, manifest, sc, _ in scored if sc >= _FUZZY_COMBINED_THRESHOLD]
    if hits:
        return sorted(hits, key=lambda x: float(x[2].split()[1].rstrip("%")), reverse=True)

    # 3. Sequence fuzzy after noise-token stripping (title floor applied to deep titles)
    hits = []
    for path, manifest, _, cand_deep in flat:
        cand_title_deep = _normalize_deep(manifest.get("title", ""))
        if _sim(deep_title, cand_title_deep) < _FUZZY_TITLE_MIN:
            continue
        sc = _sim(deep_key, cand_deep)
        if sc >= _FUZZY_DEEP_THRESHOLD:
            hits.append((path, manifest, f"fuzzy-deep {sc:.0%}"))
    if hits:
        return sorted(hits, key=lambda x: float(x[2].split()[1].rstrip("%")), reverse=True)

    return []


def find_match(artist, title, library_index):
    """Return (path, manifest, score_description) for the best text match, or (None, None, None)."""
    matches = find_all_matches(artist, title, library_index)
    if matches:
        return matches[0]
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
    Returns { normalize(artist+" "+title): [(path, manifest), ...] }.
    All files with the same normalised key are stored (sorted filenames).
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
        index.setdefault(key, []).append((path, manifest))
    return index


def predict_merge_ids(tracks_dict, split_drums=False):
    """
    Given a parsed tracks dict, return the arrangement IDs a merge would add.
    Mirrors merge.py track→arrangement mapping including gamepad renaming.
    With split_drums, each file has standard 'drums'/'drums_score' IDs.
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


def run(root_dir, output_dir=None, library_dir=None, force=False, verbose=True,
        split_drums=False):
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
                candidates = find_all_matches(artist, title, library_index)
                if candidates:
                    if len(candidates) == 1:
                        rs_path, rs_manifest, match_desc = candidates[0]
                        if verbose:
                            print(f"  Match ({match_desc}): {os.path.basename(rs_path)}")
                    else:
                        # Multiple RS sloppaks match — score each by audio xcorr
                        if verbose:
                            print(f"  {len(candidates)} candidates — scoring by audio …")
                        best_score = -1.0
                        rs_path, rs_manifest, match_desc = candidates[0]
                        for cand_path, cand_manifest, cand_desc in candidates:
                            _, sc = merge_mod.score_candidate(ch_dir, cand_path)
                            if verbose:
                                print(f"    {os.path.basename(cand_path)}: "
                                      f"xcorr={sc:.4f}")
                            if sc > best_score:
                                best_score = sc
                                rs_path, rs_manifest, match_desc = (
                                    cand_path, cand_manifest, cand_desc)
                        if verbose:
                            print(f"  Best ({match_desc}): {os.path.basename(rs_path)}")

            if rs_path:
                candidate_ids = predict_merge_ids(parsed["tracks"], split_drums=split_drums)

                rs_base    = re.sub(r'[<>:"/\\|?*]', "",
                                    os.path.splitext(os.path.basename(rs_path))[0])
                out_dir    = output_dir or os.path.dirname(rs_path)
                drums_track = parsed["tracks"].get("drums", {})
                use_split   = split_drums and bool(drums_track)

                if use_split:
                    diffs     = sorted(drums_track.keys(), reverse=True)
                    out_paths = [
                        os.path.join(out_dir,
                                     f"{rs_base}+ch ({_DIFF_CAPS.get(d, str(d))}).sloppak")
                        for d in diffs
                    ]
                    out_path  = out_paths[0]

                    if not force and all(os.path.exists(p) for p in out_paths):
                        reason = "all split files already exist"
                        if verbose:
                            print(f"  SKIP: {reason}")
                        skipped_entries.append({"artist": artist, "title": title,
                                                "ch_dir": ch_dir,
                                                "output": ", ".join(out_paths),
                                                "reason": reason})
                        skipped += 1
                        continue
                    missing = candidate_ids   # all tracks are new per-file
                else:
                    out_path = os.path.join(out_dir, f"{rs_base}+ch.sloppak")
                    out_paths = [out_path]

                    # Check for missing tracks in the manifest
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
                    if use_split:
                        action = ("Re-merging"
                                  if any(os.path.exists(p) for p in out_paths)
                                  else "Merging")
                    else:
                        out_basename = os.path.basename(out_path)
                        action = ("Re-merging"
                                  if os.path.exists(out_path) and "+ch" in out_basename
                                  else "Merging")
                    print(f"  {action} with {os.path.basename(rs_path)}")
                    print(f"  Adding: {', '.join(missing)}")

                # For split mode, pass a base output_path so merge() can derive
                # per-diff filenames; for non-split pass out_path directly.
                merge_out_path = (
                    os.path.join(out_dir, f"{rs_base}+ch.sloppak")
                    if use_split else out_path
                )
                out = merge_mod.merge(
                    ch_dir=ch_dir,
                    rs_sloppak_path=rs_path,
                    output_path=merge_out_path,
                    verbose=verbose,
                    split_drums=split_drums,
                )
                out_str = ", ".join(out) if isinstance(out, list) else out
                merge_entries.append({
                    "artist": artist, "title": title,
                    "ch_dir": ch_dir, "rs_path": rs_path,
                    "added_tracks": missing, "output": out_str,
                })
                merged += 1

            else:
                # No library match — standalone convert
                safe = re.sub(r'[<>:"/\\|?*]', "",
                              f"{artist} - {title}" if artist else title)
                safe = safe.strip(". ") or "output"
                base_dir     = output_dir or os.path.dirname(ch_dir)
                drums_track  = parsed["tracks"].get("drums", {})
                use_split    = split_drums and bool(drums_track)

                if use_split:
                    diffs     = sorted(drums_track.keys(), reverse=True)
                    out_paths = [
                        os.path.join(base_dir,
                                     f"{safe} ({_DIFF_CAPS.get(d, str(d))}).sloppak")
                        for d in diffs
                    ]
                    out_path  = out_paths[0]
                    if not force and all(os.path.exists(p) for p in out_paths):
                        reason = "all split files already exist"
                        if verbose:
                            print(f"  SKIP: {reason}")
                        skipped_entries.append({"artist": artist, "title": title,
                                                "ch_dir": ch_dir,
                                                "output": ", ".join(out_paths),
                                                "reason": reason})
                        skipped += 1
                        continue
                    # Pass base path; convert() strips ext and appends " (Diff)"
                    conv_out_path = os.path.join(base_dir, f"{safe}.sloppak")
                else:
                    out_path      = os.path.join(base_dir, f"{safe}.sloppak")
                    conv_out_path = out_path
                    if not force and os.path.exists(out_path):
                        reason = f"output already exists: {out_path}"
                        if verbose:
                            print(f"  SKIP: {reason}")
                        skipped_entries.append({"artist": artist, "title": title,
                                                "ch_dir": ch_dir, "output": out_path,
                                                "reason": reason})
                        skipped += 1
                        continue

                main_mod.convert(song_dir=ch_dir, output_path=conv_out_path,
                                 verbose=verbose, split_drums=split_drums)
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
