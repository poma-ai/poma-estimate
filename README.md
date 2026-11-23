# poma-estimate

Point it at a folder and it prints, before anything is ingested, the **POMA
units** for the contents and plain counts per file type (pages, pages without
a text layer, text tokens, images, slides). It reads files locally and never
uploads anything.

## Run it from source

Needs [uv](https://docs.astral.sh/uv/) (installs Python itself):

```
git clone https://github.com/poma-ai/poma-estimate.git
cd poma-estimate
uv run poma-estimate /path/to/a/folder
```

The first run downloads the dependencies (about a minute). `uv run pytest` runs the tests.

## Install and run (3 steps, no admin rights)

These one-line installers download the released binaries.

**macOS / Linux**

1. Open the *Terminal* app.
2. Paste this line and press Enter:

   ```
   curl -fsSL https://raw.githubusercontent.com/poma-ai/poma-estimate/main/install.sh | sh
   ```
3. When asked, drag the folder you want to check into the Terminal window and press Enter.

**Windows**

1. Open *PowerShell*.
2. Paste this line and press Enter:

   ```
   irm https://raw.githubusercontent.com/poma-ai/poma-estimate/main/install.ps1 | iex
   ```
3. When asked, paste the folder path and press Enter.

The installer downloads one program (about 40 MB) into your user folder
(`~/.poma-estimate`, or `%LOCALAPPDATA%\poma-estimate` on Windows), checks its
checksum and starts it. To run it again later:
`~/.poma-estimate/poma-estimate <folder>`. Nothing is installed system-wide and
nothing leaves your computer.

## Usage

```
poma-estimate <path> [--budget SECONDS] [--no-recurse] [--json] [-v]
                     [--workers N] [--include-icloud]
```

The run is fully automatic and homes in on the answer, printing each stage
to stderr as it goes (Ctrl-C once it is exact enough), then the result on
stdout (illustrative output):

```
     0.4s  walked, size-based guess                     ~2,100,000 units (rough guess)
     9.0s  1.4% measured (560/40,000 files)             ~760,000 units (±42%)          ~±12% by the time limit · all files in ~31 min
    40.2s  6.1% measured (2,440/40,000 files)           ~750,000 units (±27%)          ~±11% by the time limit · all files in ~27 min
   ...
   608.3s  48.0% measured (19,200/40,000 files) (time budget reached) ~740,000 units (±10%)  all files in ~11 min
units 740,000
PDF: ...  (one line of counts per file type, see below)
```

`units` is the billing count (pages, minutes, or 1,000-token blocks, whichever
is more, per file). The lines after it are plain counts per
file type that you can check against your own files, for example:

```
units 5,100
PDF: 93 files, 312.4 MB, 1,629 pages, 591 pages without a text layer, 905,104 text tokens, 940 images
Presentations (PPTX, PPT, ODP, Key): 4 files, 20.4 MB, 32 slides, 27 slides with visual content, 27 images
Markdown: 9 files, 138.0 KB, 35,928 text tokens, 14 images
```

"Images" are the ones that get described (in PDFs, drawn at 5 % of the page or
more; elsewhere at least 100 x 100 px and 5 KB). Archives (`.zip`, `.tar`, …)
are counted by what they contain (zip listings are read, never extracted;
other archives by their size), and POMA does not unpack archives, so unpack
them before upload. `--json` gives the same counts under `counts`, per file and
in total.

1. Walk the tree (hidden files skipped, symlinks not followed) and make a
   quick guess from file counts and sizes.
2. Measure growing random samples (stratified by file type and size) and
   extrapolate, with a 95% error bar.
3. Stop when every file has been measured or when `--budget` seconds
   (default 600) are used up, whichever comes first. Pressing Ctrl-C stops
   early and prints the best estimate so far. Every progress line forecasts
   the error bar at the time limit and how long measuring every file would
   take (from the time each file type took so far), so you can decide to
   wait, stop, or re-run with a larger `--budget`. Small folders are measured
   completely. While sampling, the error bar assumes no extreme outlier
   files among the unmeasured ones; very large text files and audio/video
   without duration headers are always estimated, never read in full.

- `--json` — machine-readable result on stdout (stages go to stderr), with
  the counts per file and per file type.
- `-v` — per-file table for the files that were measured, plus context lines.
- `--include-icloud` — macOS only. iCloud placeholders are skipped by
  default because reading them triggers downloads.

Benchmark: a folder with 165k supported files (17 GB) took 8.5 min
measured completely; the estimate was within 3% after 3 min.

## What it measures

Covers the formats POMA ingests. Aliases route to the
same parser (e.g. `htm`/`xhtml`/`shtml`/`mhtml`/`aspx` all use the
HTML/markdownify path; `yml` uses the YAML path; `jpe` uses the JPEG path).

| Family | Extensions | Pages | Tokens |
|---|---|---|---|
| PDF | `pdf` | `pypdf` page count, with oversized-page presplit emulated (height >1000pt → split into 2000pt chunks) | `pdfminer` text → o200k_base tokenizer + per-unique-image description band |
| Office OOXML | `docx`, `dotx`, `pptx`, `potx`, `ppsx`, `xlsx`, `xltx` | DOCX: the larger of Word's stored page count / page breaks and characters ÷ 2,500. PPTX: slide count (zip+xml fallback handles slideshow/template variants python-pptx rejects). XLSX: 1. | text + per-image band |
| Legacy Excel | `xls` (xlrd), `xlsb` (pyxlsb) | 1 | all cells across all sheets |
| Legacy Word | `doc` (olefile) | char-heuristic | naive OLE `WordDocument` stream extraction |
| OpenDocument | `odt`, `ods`, `odp` | text doc paginated, presentation = slide count, spreadsheet = 1 | text + embedded image band |
| HTML | `html`, `htm`, `xhtml`, `shtml`, `mhtml`, `aspx` | 1 | BeautifulSoup → markdownify + per-`<img>` band |
| Plain text / markdown | `txt`, `log`, `text`, `md`, `markdown`, `mdown`, `mkd`, `mkdn` | 1 | tokenized raw text |
| Structured data | `xml`, `xsd`, `xsl`, `xslt`, `rss`, `atom`, `kml`, `opml`, `gpx`, `tcx`, `json`, `json5`, `geojson`, `yaml`, `yml`, `toml`, `ini`, `cfg`, `conf`, `env` | 1 | tokenized text (XML-family gets markup-stripped) |
| SPICE netlist | `cir`, `sp`, `spice`, `ckt` | 1 | tokenized text |
| Tabular | `csv`, `tsv`, `tab` | 1 | tokenized raw text |
| EPUB | `epub` | rendered pages: reading-spine text length, one page per spine document at least, plus explicit page breaks (approximate) | spine text + image band |
| RTF | `rtf` | 1 | stripped text |
| Audio / video | `mp3`, `wav`, `m4a`, `aac`, `flac`, `ogg`, `opus`, `mp4`, `mov`, `mkv`, `avi`, `webm`, … | one unit per started minute | duration from the file header where possible (MP4/MOV/M4A, WAV, MP3), else estimated from file size with a wide band |
| Image-as-document | `png`, `jpg`, `jpeg`, `jpe`, `gif`, `bmp`, `heic`, `heif`, `svg`, `ico`, `tiff`, `tif`, `webp`, `psd`, `ai`, `eps` | 1 (multi-page TIFF: one per frame) | image-as-document detailed-description band (tiny <100px or <5KB images collapse to icon band) |

## Band semantics & calibration

When the unit count for a file can be predicted exactly (a PDF with
extractable text and no embedded images, a plain text file), the band
collapses to a single number. When vision is in play, the `--json` output
carries a low/expected/high range (`units_raw`) from real description-token
measurements; the printed total is the rounded expected value.

**Calibrated 2026-05-19** against 72 image descriptions across 21
real `.poma` archives:

| Percentile | Embedded image description tokens |
|---|---|
| p50 | 48 |
| p75 | 161 |
| p90 | 245 |
| p95 | 264 |
| p99 | 296 |
| max | 342 |

Aggregate finding: image-description tokens are **0.9% of total tokens**
on average. Page count dominates the unit math in 20 of 21 sampled
archives; tokens dominate only on text-dense single-image documents and
very long Georgian/CJK text where tokenization is
inefficient.

Validated against ground truth in 3 paired processing archives where the
source PDF was available locally: estimator's "expected" band hits the
true unit count exactly (114→114, 98→98, 252→252).

## Build (developers)

Requires `uv` (Python 3.11+):

```bash
uv sync --group dev
./build.sh    # produces dist/poma-estimate-<platform>
```

Windows builds run in CI — `.github/workflows/build.yml`.

## Limits, known gaps

- Scanned PDFs assume ~850 tokens/page (no local OCR). Real OCR'd text
  may push the per-page unit count higher on text-dense scans.
- Image-description bands for **image-as-document** inputs are
  extrapolated, not measured; the calibration corpus is all PDFs.
- `.doc` (Word 97 and later) text is read from the document's piece table;
  token counts match LibreOffice's text export within ~1–3% on the files
  tested, page counts are estimated from text length. Older Word 6/95 files
  fall back to a rough scan and are marked approximate; encrypted ones are
  counted by size.
- Not yet supported (would need heavyweight dependencies for marginal
  accuracy): `ppt`, `pps`, `pub` (legacy MS Office binary), `pages` /
  `numbers` / `key` (iWork), `vsd` / `vsdx` (Visio), `dwg` / `dxf` (CAD),
  `djvu`, `mobi`, `xps` / `oxps`, `ps`. They are not counted.
- The tool does not connect to the POMA API; the result is an estimate.
