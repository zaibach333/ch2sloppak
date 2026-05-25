# ch2sloppak

Convert a Clone Hero song folder into a [Slopsmith](https://github.com/byrongamatos/slopsmith) `.sloppak` package, or merge CH note data into an existing Rocksmith `.sloppak`.

## Requirements

- Python 3.9+
- `pip install -r requirements.txt`
- `ffmpeg` on PATH — required for MP3→OGG conversion and multi-stem merging

## Commands

### convert

Convert a CH song folder to a new `.sloppak`:

```bash
python ch2sloppak.py convert <path/to/ch-song-folder>
# → writes "<Artist - Title>.sloppak" next to the song folder

python ch2sloppak.py convert <path/to/ch-song-folder> -o my_song.sloppak

# Split drums into one arrangement per difficulty instead of a slider:
python ch2sloppak.py convert <path/to/ch-song-folder> --split-drums
# → drums-expert, drums-hard, drums-medium, drums-easy (whichever exist)

# Old-style invocation still works:
python ch2sloppak.py <path/to/ch-song-folder>
```

### merge

Merge CH note data into an existing Rocksmith `.sloppak`, keeping the RS audio:

```bash
python ch2sloppak.py merge <path/to/ch-song-folder> <path/to/song.sloppak>
# → writes "song+ch.sloppak" next to the RS sloppak

python ch2sloppak.py merge <ch-folder> <rs.sloppak> -o merged.sloppak

# Override auto-detected timing alignment with a manual offset:
python ch2sloppak.py merge <ch-folder> <rs.sloppak> --offset 1234.5

# Fine-tune on top of auto-alignment (negative = shift earlier):
python ch2sloppak.py merge <ch-folder> <rs.sloppak> --nudge -500

# Split drums into one arrangement per difficulty:
python ch2sloppak.py merge <ch-folder> <rs.sloppak> --split-drums
```

The merge command auto-aligns CH note timing to the RS audio in three stages:

1. **80-pass iterative frequency-adaptive note-to-audio** — when a drum chart
   is present, the RS stem is filtered into three frequency bands (kick <150 Hz,
   snare 150–2500 Hz, cymbal >2500 Hz). If CH drum stems are available, a
   per-type frequency fingerprint is built by searching a ±400 ms window around
   each note's chart time and averaging the peak band energies across all notes
   of that type. Aggregating over the whole song makes the fingerprint robust to
   intro/outro length differences and timing drift between CH audio and chart.
   Each note is then scored against RS using its type's learned weight vector
   rather than a fixed frequency assumption. Notes with simultaneous hits within
   20 ms are down-weighted by 1/(n_nearby+1). Without CH audio, weights fall
   back to one-hot on the declared band type. The search window decays
   exponentially from ±30 s to ±1 frame over 80 passes.
2. **Banded CH-audio xcorr refinement** — if CH drum stems are present, they
   are cross-correlated per-band against the RS bands within ±0.9 s of the
   note-derived estimate. If the result agrees within 0.3 s it replaces the
   note estimate for sub-frame accuracy; otherwise the note-derived offset is
   kept. Falls back to full-song onset-envelope cross-correlation when no drum
   chart is available at all.
3. **Beat IBI** — piecewise beat-map cross-correlation handles residual tempo
   stretch within the aligned window.

The verbose output shows the note score, convergence pass count, audio xcorr
result (accepted/rejected), beat shift (K), mean time offset, and IBI residual
— if the residual is high (> 20 ms), use `--offset` to correct manually.

RS arrangements win on ID collision (e.g. if RS already has `lead`/`bass`,
the CH gamepad versions are skipped). RS audio stems pass through unchanged;
CH audio is discarded. All original RS manifest fields are preserved.

### batch

Convert all CH song folders found recursively under a root directory:

```bash
python ch2sloppak.py batch <path/to/ch-songs-root>
# → converts each CH folder, writes .sloppak next to each source folder

python ch2sloppak.py batch <root> -o <output-dir>
# → all converted and merged outputs go to the specified directory

# Auto-merge with a sloppak library — songs matched by artist+title are
# merged instead of converted standalone:
python ch2sloppak.py batch <root> --library <path/to/sloppaks>
# → merged +ch.sloppak files land next to their originals in the library

python ch2sloppak.py batch <root> --library <path/to/sloppaks> -o <output-dir>
# → merged and converted outputs all go to output-dir

# Overwrite existing output files (default is to skip them):
python ch2sloppak.py batch <root> --library <path/to/sloppaks> -o <output-dir> --force

# Split drums into per-difficulty arrangements across the whole batch:
python ch2sloppak.py batch <root> --library <path/to/sloppaks> --split-drums
# Duplicate detection uses difficulty-specific IDs (drums-expert, etc.) so
# songs already converted without --split-drums will be re-merged/re-converted.
```

**Default skip behaviour**: if a `.sloppak` output file already exists it is
skipped. Use `--force` to overwrite. Songs skipped because all their CH tracks
are already present in the matched sloppak are also skipped silently.

**Log files** written to the calling directory when applicable:

| File | Written when |
|---|---|
| `mergelog.txt` | At least one merge was performed |
| `skipped.txt` | At least one output file was skipped due to already existing |

Library matching normalises artist and title (lowercase, punctuation stripped)
for comparison. First `.sloppak` match per song wins.

## What gets converted

### Chart formats
| Source | Notes |
|---|---|
| `notes.mid` | Tried first; falls back to `.chart` on parse error |
| `notes.chart` | Used when no `.mid` present |

### Arrangements
| CH track | Arrangement ID | Display name | Type | Notes |
|---|---|---|---|---|
| `[Expert…Easy]Single` | `lead` | Lead Gamepad | guitar | 5-lane color-fret highway |
| `[Expert…Easy]Bass` | `bass` | Bass Gamepad | guitar | 5-lane color-fret highway |
| `[Expert…Easy]Drums` (4-lane Pro) | `drums` | Drums | drums | drum highway encoding |
| same | `drums_score` | Drums Score | drums_score | treble-clef staff encoding for tab view |
| `PART REAL_KEYS_X/H/M/E` (.mid only) | `keys` | Keys | piano | actual MIDI pitches, piano layout |

The **Type** column matches the `type` field written to `manifest.yaml` — used by
Slopsmith plugins to route each arrangement to the correct view (guitar highway,
drum highway, piano roll, or tab view).

**Drum difficulties — two modes:**

| Mode | Arrangement IDs | Behavior |
|---|---|---|
| Default (slider) | `drums`, `drums_score` | One arrangement; difficulty slider spans all available levels |
| `--split-drums` | `drums-expert`, `drums-hard`, `drums-medium`, `drums-easy` (present diffs only) + `drums_score-*` equivalents | Separate selectable arrangement per difficulty |

Keys and guitar/bass always use the slider mode regardless of `--split-drums`.

When 2+ difficulties are present in slider mode, a difficulty slider is enabled
(top-level `notes` = highest difficulty; `phrases` array carries all levels).

In a **merge**, CH guitar/bass tracks are renamed `lead-gamepad` / `bass-gamepad`
to avoid colliding with RS `lead` / `bass` arrangements.

### Audio stems
| Source | Slopsmith output |
|---|---|
| `song.ogg` / `song.mp3` | `stems/full.ogg` |
| `guitar.ogg`, `bass.ogg`, `drums.ogg`, `vocals.ogg`, `keys.ogg` | individual stems |
| `drums_1.ogg` … `drums_4.ogg` | merged into `stems/drums.ogg` |
| `.mp3` / `.opus` variants | converted to OGG via ffmpeg |

### Other
| Source | Slopsmith output |
|---|---|
| `song.ini` + `[Song]` section | `manifest.yaml` |
| `[Events]` lyric events / MIDI VOCALS track | `lyrics.json` |
| `album.png` / `album.jpg` | `cover.<ext>` |
| Drums track — default | `drum_tab_drums.json` |
| Drums track — `--split-drums` | `drum_tab_drums-expert.json`, `drum_tab_drums-hard.json`, … |

## Drum encoding (4-lane Pro)

Two arrangements are written for every drums track:

**`drums`** — highway encoding: `string = GM_MIDI // 24`, `fret = GM_MIDI % 24`

**`drums_score`** — treble-clef staff encoding for the tab view plugin.
Notes are mapped to standard percussion staff positions with cymbal hits
marked muted (`mt: true`) to render as × noteheads.

| CH note | Flag | Drum part | Staff position |
|---|---|---|---|
| 0 / 32 | — | Kick / 2× Kick | between lines 1–2 |
| 1 | — | Snare | between lines 3–4 |
| 2 | cymbal | Hi-Hat | above staff (×) |
| 2 | tom | Yellow Tom | between lines 4–5 |
| 3 | cymbal | Ride | line 5 (×) |
| 3 | tom | Blue Tom | line 4 |
| 4 | cymbal | Crash | ledger line above staff (×) |
| 4 | tom | Floor Tom | between lines 2–3 |

`.chart`: lanes 2/3/4 are toms by default; cymbal flags (66/67/68) mark cymbals.  
`.mid`: lanes 2/3/4 are cymbals by default; tom-marker notes (110/111/112) mark toms.

A `drum_tab_drums.json` file (or per-difficulty files with `--split-drums`) is
written for every song with drums and referenced in the manifest — consumed by
the `slopsmith-plugin-drum-highway-3d` plugin.

## Guitar / Bass encoding

CH lanes map to Slopsmith strings and frets to match color-coded highway lanes:

| CH lane | Color | String | Fret |
|---|---|---|---|
| 0 | Green | 0 | 5 |
| 1 | Red | 1 | 1 |
| 2 | Yellow | 2 | 2 |
| 3 | Blue | 3 | 3 |
| 4 | Orange | 4 | 4 |
| 7 | Open | 5 | 1 |

HOPO and tap flags are preserved.

## Pro Keys encoding

`PART REAL_KEYS_X/H/M/E` tracks contain raw MIDI pitches (A0–C8).
Each note is mapped to a string/fret pair using a piano-octave layout
(6 strings × 12 frets, one octave C–B per string, covering display range C2–C7).
The tuning offset compensates for alphaTab's standard guitar +12 display transposition.
Routed to the piano roll via `type: piano` in the manifest.

## Limitations

- No `.chart` support for Pro Keys (`.mid` only)
- No 5-lane drums decoding (Pro drums only)
- No chord-template grouping (simultaneous notes written as individual notes)
- Drum highway difficulty slider (default mode) shows Expert only in the 3D highway — use `--split-drums` to expose each difficulty as a separate selectable arrangement instead
