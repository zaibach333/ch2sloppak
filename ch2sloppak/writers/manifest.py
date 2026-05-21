"""Build and serialise the Slopsmith manifest.yaml for a sloppak package."""

import yaml

_ARRANGEMENT_DISPLAY = {
    "lead":          "Lead Gamepad",   # standalone CH convert
    "lead-gamepad":  "Lead Gamepad",   # CH track in a merge
    "bass":          "Bass Gamepad",   # standalone CH convert
    "bass-gamepad":  "Bass Gamepad",   # CH track in a merge
    "keys":          "Keys",
    "drums":         "Drums",
    "drums_score":   "Drums Score",
}
_ARRANGEMENT_TUNING = {
    "lead":          [0, 0, 0, 0, 0, 0],
    "lead-gamepad":  [0, 0, 0, 0, 0, 0],
    "bass":          [0, 0, 0, 0, 0, 0],
    "bass-gamepad":  [0, 0, 0, 0, 0, 0],
    "keys":          [-16, -9, -2, 5, 13, 20],  # PIANO_TUNING — one octave per string
    "drums":         [0, 0, 0, 0, 0, 0],
    "drums_score":   [0, 0, 0, 0, 0, 0],
}
# Explicit type hint per arrangement ID — consumed by plugins/app for view routing.
# guitar → 2D/3D guitar highway   drums → drum highway   drums_score → tab view
# keys   → piano roll
_ARRANGEMENT_TYPE = {
    "lead":          "guitar",
    "lead-gamepad":  "guitar",
    "bass":          "guitar",
    "bass-gamepad":  "guitar",
    "rhythm":        "guitar",
    "keys":          "piano",
    "drums":         "drums",
    "drums_score":   "drums_score",
}


def build(metadata, arrangement_ids, stem_ids, cover_filename, has_lyrics,
          rs_arrangements=None, has_drum_tab=False, drum_tabs=None):
    """
    Build a manifest dict.

    Args:
      metadata:          unified metadata dict (lowercased keys)
      arrangement_ids:   ordered list of track keys in the package
      stem_ids:          list of stem ids that will be in stems/ (all get default: 'on')
      cover_filename:    basename of the cover file (e.g. 'cover.jpg') or None
      has_lyrics:        True if lyrics.json is included
      rs_arrangements:   list of arrangement dicts from the RS sloppak manifest
                         (used to pass name/tuning/capo through for RS tracks)
      drum_tabs:         {arrangement_id: filename} for per-arrangement drum_tab files
    """
    title  = metadata.get("name") or metadata.get("title") or "Unknown"
    artist = metadata.get("artist") or "Unknown"
    album  = metadata.get("album") or ""
    year   = metadata.get("year_clean") or metadata.get("year") or ""

    duration = 0.0
    try:
        duration = round(float(metadata.get("song_length") or
                               metadata.get("duration") or 0) / 1000.0, 3)
    except (TypeError, ValueError):
        pass

    rs_by_id  = {a["id"]: a for a in (rs_arrangements or [])}
    drum_tabs = drum_tabs or {}

    arrangements = []
    for aid in arrangement_ids:
        rs = rs_by_id.get(aid, {})
        entry = {
            "id":     aid,
            "name":   rs.get("name") or _ARRANGEMENT_DISPLAY.get(aid, aid.replace("-", " ").title()),
            "file":   f"arrangements/{aid}.json",
            "tuning": rs.get("tuning") or _ARRANGEMENT_TUNING.get(aid, [0, 0, 0, 0, 0, 0]),
            "capo":   rs.get("capo", 0),
        }
        # Pass through any extra fields from the original RS manifest entry
        # (e.g. path, type, or anything else plugins rely on for matchesArrangement).
        for k, v in rs.items():
            if k not in entry:
                entry[k] = v
        # Always set type: our value wins for CH arrangements; RS value already
        # passed through above, so only set if not already present.
        if "type" not in entry:
            arr_type = _ARRANGEMENT_TYPE.get(aid)
            if arr_type:
                entry["type"] = arr_type
        if aid in drum_tabs:
            entry["drum_tab"] = drum_tabs[aid]
        arrangements.append(entry)

    stems = [
        {"id": sid, "file": f"stems/{sid}.ogg", "default": "on"}
        for sid in stem_ids
    ]

    manifest = {
        "title":        title,
        "artist":       artist,
        "stems":        stems,
        "arrangements": arrangements,
    }
    if album:
        manifest["album"] = album
    if year:
        manifest["year"] = year
    if duration:
        manifest["duration"] = duration
    if cover_filename:
        manifest["cover"] = cover_filename
    if has_lyrics:
        manifest["lyrics"] = "lyrics.json"
    if "drums" in drum_tabs:
        manifest["drum_tab"] = drum_tabs["drums"]

    return manifest


def to_yaml_string(manifest):
    return yaml.dump(
        manifest,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    )
