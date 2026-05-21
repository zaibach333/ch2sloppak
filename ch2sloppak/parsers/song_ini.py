"""Parse song.ini metadata files from Clone Hero song folders."""


def parse(filepath):
    """Return a dict of lowercased key → string value from song.ini."""
    metadata = {}
    try:
        with open(filepath, "r", encoding="utf-8-sig", errors="replace") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return metadata

    in_section = False
    for line in lines:
        line = line.strip()
        if not line or line.startswith(";") or line.startswith("#"):
            continue
        if line.startswith("["):
            in_section = True
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        metadata[key.strip().lower()] = value.strip()

    return metadata


def get_str(meta, *keys, default=""):
    for k in keys:
        v = meta.get(k, "").strip().strip('"')
        if v:
            return v
    return default


def get_int(meta, *keys, default=0):
    for k in keys:
        raw = meta.get(k, "").strip()
        try:
            return int(raw)
        except (ValueError, TypeError):
            pass
    return default


def get_year(meta):
    """Extract year; .chart stores it as ', 2000' sometimes."""
    raw = meta.get("year", "").strip().strip('"')
    raw = raw.lstrip(", ").strip()
    return raw
