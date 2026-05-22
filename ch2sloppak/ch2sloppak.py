#!/usr/bin/env python3
"""ch2sloppak – Convert a Clone Hero song folder into a Slopsmith .sloppak package.

Usage:
  ch2sloppak.py <song_folder> [-o output.sloppak] [-q]

Supports:
  • notes.mid  (tried first)  – Expert/Hard/Medium/Easy, pro drums, lyrics
  • notes.chart               – Expert/Hard/Medium/Easy, pro drums, lyrics
  • Multiple audio stems with ffmpeg merge when no pre-mix exists
"""

import argparse
import os
import re
import sys

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
from writers import sloppak as sloppak_writer
import merge as merge_mod
import batch as batch_mod


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------

def _find(song_dir, *names):
    for name in names:
        p = os.path.join(song_dir, name)
        if os.path.isfile(p):
            return p
    return None


def _find_chart(song_dir):
    return _find(song_dir, "notes.chart", "Notes.chart")


def _find_mid(song_dir):
    return _find(song_dir, "notes.mid", "Notes.mid")


def _find_song_ini(song_dir):
    return _find(song_dir, "song.ini", "Song.ini")


# ---------------------------------------------------------------------------
# Metadata merge
# ---------------------------------------------------------------------------

def _merge_metadata(ini_meta, chart_song_meta):
    merged = {}
    for k, v in chart_song_meta.items():
        merged[k.lower()] = v
    for k, v in ini_meta.items():
        if v:
            merged[k.lower()] = v
    # Normalise year: strip ", " artefact from .chart [Song] section
    year_raw = merged.get("year", "").strip().strip('"')
    merged["year_clean"] = re.sub(r"^,\s*", "", year_raw).strip()
    return merged


# ---------------------------------------------------------------------------
# Main conversion
# ---------------------------------------------------------------------------

def convert(song_dir, output_path=None, verbose=True):
    """
    Convert a CH song folder to a .sloppak file.
    Returns the path of the written package.
    """
    song_dir = os.path.abspath(song_dir)
    if not os.path.isdir(song_dir):
        raise FileNotFoundError(f"Song folder not found: {song_dir}")

    mid_path   = _find_mid(song_dir)
    chart_path = _find_chart(song_dir)
    ini_path   = _find_song_ini(song_dir)

    if not mid_path and not chart_path:
        raise FileNotFoundError(f"No notes.mid or notes.chart found in {song_dir}")

    # --- parse note data ---
    if mid_path:
        if verbose:
            print(f"  midi  : {mid_path}")
        try:
            parsed = mid_parser.parse(mid_path)
        except Exception as exc:
            if chart_path:
                if verbose:
                    print(f"  WARNING: MIDI parse failed ({exc}); falling back to .chart")
                parsed = chart_parser.parse(chart_path)
            else:
                raise
    else:
        if verbose:
            print(f"  chart : {chart_path}")
        parsed = chart_parser.parse(chart_path)

    # --- parse metadata (song.ini overrides [Song]) ---
    ini_meta = song_ini.parse(ini_path) if ini_path else {}
    metadata = _merge_metadata(ini_meta, parsed["song_meta"])

    title  = metadata.get("name") or metadata.get("title") or os.path.basename(song_dir)
    artist = metadata.get("artist") or ""

    if verbose:
        print(f"  title : {title}")
        if artist:
            print(f"  artist: {artist}")

    # --- convert arrangements ---
    arrangements = {}
    for track_id, diff_dict in parsed["tracks"].items():
        if not diff_dict:
            continue
        if track_id == "drums":
            arr = arrangement_writer.convert_drums(diff_dict)
            arrangements["drums_score"] = arrangement_writer.convert_drums_score(diff_dict)
        elif track_id == "keys":
            arr = arrangement_writer.convert_keys(diff_dict)
        else:
            arr = arrangement_writer.convert_guitar(diff_dict, arrangement_name=track_id)

        total_notes = sum(len(d) for d in diff_dict.values())
        if verbose:
            diffs_present = sorted(diff_dict.keys(), reverse=True)
            diff_names = {3: "E", 2: "H", 1: "M", 0: "Ey"}
            label = "/".join(diff_names[d] for d in diffs_present)
            print(f"  [{track_id}] {total_notes} notes ({label})"
                  + (" + phrases" if arr.get("phrases") else ""))
        arrangements[track_id] = arr

    if not arrangements and verbose:
        print("  WARNING: no note data found")

    # --- beats (for slopsmith-plugin-tabview measure structure) ---
    # Slopsmith reads beats from data["beats"] inside each arrangement JSON.
    beats_data = beats_writer.generate(
        ts_events  = parsed.get("ts_events", []),
        ppq        = parsed["resolution"],
        ticks_to_ms = parsed["ticks_to_ms"],
        max_tick   = parsed.get("max_tick", 0),
    )
    if beats_data:
        for arr in arrangements.values():
            arr["beats"] = beats_data
    if verbose and beats_data:
        print(f"  beats : {len(beats_data)} beats")

    # --- lyrics ---
    raw_lyrics  = parsed.get("lyrics", [])
    lyrics_data = lyrics_writer.convert(raw_lyrics)
    if verbose and lyrics_data:
        print(f"  lyrics: {len(lyrics_data)} syllables")

    # --- drum_tabs (for slopsmith-plugin-drum-highway-3d) ---
    # Only real drums get a drum_tab file.  Guitar/bass/rhythm gamepad tracks
    # display on the guitar highway via GUITAR_LANE_MAP (string colors match).
    drum_tabs = {}
    if "drums" in parsed["tracks"] and parsed["tracks"]["drums"]:
        dt = drum_tab_writer.convert(parsed["tracks"]["drums"])
        if dt:
            drum_tabs["drums"] = dt
    if verbose and drum_tabs:
        print(f"  drum_tab: {len(drum_tabs['drums']['hits'])} hits")

    # --- stems + cover (discover before manifest so both are accurate) ---
    stem_ids   = sloppak_writer.find_stem_ids(song_dir)
    cover_path = sloppak_writer.find_cover(song_dir)
    cover_filename = ("cover" + os.path.splitext(cover_path)[1].lower()
                      if cover_path else None)

    # --- build manifest ---
    arrangement_drum_tabs = {
        aid: f"drum_tab_{aid}.json" for aid in drum_tabs
    }
    manifest_dict = manifest_writer.build(
        metadata={**metadata, "name": title, "artist": artist},
        arrangement_ids=list(arrangements.keys()),
        stem_ids=stem_ids,
        cover_filename=cover_filename,
        has_lyrics=bool(lyrics_data),
        drum_tabs=arrangement_drum_tabs,
    )
    manifest_yaml = manifest_writer.to_yaml_string(manifest_dict)

    # --- output path ---
    if output_path is None:
        safe = re.sub(r'[<>:"/\\|?*]', "", f"{artist} - {title}" if artist else title)
        safe = safe.strip(". ") or "output"
        output_path = os.path.join(os.path.dirname(song_dir), f"{safe}.sloppak")

    # --- package ---
    sloppak_writer.write(
        output_path=output_path,
        manifest_yaml=manifest_yaml,
        arrangements=arrangements,
        lyrics_data=lyrics_data,
        song_dir=song_dir,
        cover_path=cover_path,
        verbose=verbose,
        drum_tabs=drum_tabs,
    )

    if verbose:
        size_kb = os.path.getsize(output_path) // 1024
        print(f"  → {output_path}  ({size_kb} KB)")

    return output_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Convert or merge Clone Hero songs into Slopsmith .sloppak packages."
    )
    sub = parser.add_subparsers(dest="command")

    # --- convert (default, backwards-compatible) ---
    conv = sub.add_parser("convert", help="Convert a CH folder to a new .sloppak")
    conv.add_argument("song_folder", help="Path to the CH song folder")
    conv.add_argument("-o", "--output", metavar="FILE",
                      help="Output .sloppak path (default: auto-named next to folder)")
    conv.add_argument("-q", "--quiet", action="store_true",
                      help="Suppress progress output")

    # --- merge ---
    mrg = sub.add_parser(
        "merge",
        help="Merge CH note data into an existing RS .sloppak (keeps RS audio)",
    )
    mrg.add_argument("ch_folder",    help="Path to the CH song folder")
    mrg.add_argument("rs_sloppak",   help="Path to the existing RS .sloppak")
    mrg.add_argument("-o", "--output", metavar="FILE",
                     help="Output path (default: <rs_sloppak_base>+ch.sloppak)")
    mrg.add_argument("--offset", metavar="MS", type=float,
                     help="Manual time offset in ms to add to all CH note times "
                          "(skips auto-alignment)")
    mrg.add_argument("--nudge", metavar="MS", type=float, default=0.0,
                     help="Extra ms to add on top of auto-alignment "
                          "(negative = shift earlier). "
                          "Tip: 2 quarter notes earlier at 120 BPM ≈ --nudge -1000")
    mrg.add_argument("-q", "--quiet", action="store_true",
                     help="Suppress progress output")

    # --- batch ---
    bat = sub.add_parser(
        "batch",
        help="Convert all CH song folders found under a root directory",
    )
    bat.add_argument("root_dir", help="Root directory to scan recursively")
    bat.add_argument("-o", "--output", metavar="DIR",
                     help="Directory for output .sloppak files "
                          "(default: next to each source folder)")
    bat.add_argument("--library", metavar="DIR",
                     help="Sloppak library directory — when provided, matching "
                          "songs are merged instead of converted standalone")
    bat.add_argument("--force", action="store_true",
                     help="Overwrite existing output files (default: skip them)")
    bat.add_argument("-q", "--quiet", action="store_true",
                     help="Suppress progress output")

    args = parser.parse_args()

    # Backwards-compatible: no subcommand → treat first positional as song_folder
    if args.command is None:
        if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
            # Re-parse as implicit convert
            args.command    = "convert"
            args.song_folder = sys.argv[1]
            args.output     = None
            args.quiet      = "--quiet" in sys.argv or "-q" in sys.argv
        else:
            parser.print_help()
            sys.exit(1)

    try:
        if args.command == "convert":
            out = convert(song_dir=args.song_folder,
                          output_path=args.output,
                          verbose=not args.quiet)
            print(f"Done: {out}")

        elif args.command == "merge":
            out = merge_mod.merge(
                ch_dir          = args.ch_folder,
                rs_sloppak_path = args.rs_sloppak,
                output_path     = args.output,
                offset_ms       = args.offset,
                nudge_ms        = args.nudge,
                verbose         = not args.quiet,
            )
            print(f"Done: {out}")

        elif args.command == "batch":
            batch_mod.run(
                root_dir    = args.root_dir,
                output_dir  = args.output,
                library_dir = args.library,
                force       = args.force,
                verbose     = not args.quiet,
            )

    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"Unexpected error: {exc}", file=sys.stderr)
        raise


if __name__ == "__main__":
    main()
