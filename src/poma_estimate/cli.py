"""poma-estimate CLI entrypoint."""

import argparse
import logging
import os
import re
import sys
import warnings
from pathlib import Path

from rich.console import Console

# pdfminer + pypdf are noisy by default on malformed/encrypted PDFs.
# Their warnings are diagnostic, not actionable for a credit estimate.
logging.getLogger("pdfminer").setLevel(logging.ERROR)
logging.getLogger("pypdf").setLevel(logging.ERROR)
warnings.filterwarnings("ignore", category=UserWarning, module="openpyxl")

from poma_estimate import __version__
from poma_estimate.output import archive_summary, render_json, render_table, render_two_numbers, stage_line
from poma_estimate.progressive import scan_progressive


def _positive_int(v: str) -> int:
    n = int(v)
    if n < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return n


def _positive_float(v: str) -> float:
    f = float(v)
    if not f > 0 or f == float("inf"):
        raise argparse.ArgumentTypeError("must be a positive number")
    return f


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="poma-estimate",
        description=(
            "Scan a folder (or single file) and estimate its size in "
            "POMA units. Hidden files and symlinks are skipped."
        ),
    )
    p.add_argument("path", type=Path, nargs="?", help="folder or file to scan (asked for if omitted)")
    p.add_argument(
        "--no-recurse",
        action="store_true",
        help="do not descend into subfolders",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="emit JSON instead of a human-readable table",
    )
    p.add_argument(
        "--include-icloud",
        action="store_true",
        help=(
            "include macOS iCloud Drive placeholder files (will trigger "
            "a download for each). Default: skip them."
        ),
    )
    p.add_argument(
        "--workers",
        type=_positive_int,
        default=min(8, os.cpu_count() or 1),
        help=(
            "parallel worker threads for per-file estimation. "
            "Default: min(8, CPU count)."
        ),
    )
    p.add_argument(
        "--budget",
        type=_positive_float,
        default=600.0,
        help=(
            "maximum seconds to spend (default 600). Large folders are "
            "estimated from growing random samples and the best estimate "
            "so far is reported when time runs out."
        ),
    )
    p.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help=(
            "show per-file breakdown table in addition to the summary "
            "total. Default: summary only."
        ),
    )
    p.add_argument(
        "--version",
        action="version",
        version=f"poma-estimate {__version__}",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.path is None:
        if not sys.stdin.isatty():
            print("error: no folder given. Usage: poma-estimate <folder>", file=sys.stderr)
            return 2
        print("Drag the folder to scan into this window, then press Enter: ", end="", file=sys.stderr, flush=True)
        raw = input()
        raw = raw.strip().strip("'\"")
        if os.name != "nt":  # POSIX drag-and-drop escapes spaces etc.; Windows paths use backslashes as separators
            raw = re.sub(r"\\(.)", r"\1", raw)
        args.path = Path(raw).expanduser()
    root: Path = args.path.resolve()
    if not root.exists():
        print(f"error: path does not exist: {root}", file=sys.stderr)
        return 2

    console = Console(stderr=False)
    err = Console(stderr=True, highlight=False, width=200)
    if not args.json:
        err.print(f"Scanning {root}…", style="dim", markup=False)

    def on_stage(est) -> None:
        err.print(stage_line(est), style="dim", markup=False)

    report, est = scan_progressive(
        root,
        recursive=not args.no_recurse,
        include_icloud=args.include_icloud,
        workers=args.workers,
        budget=args.budget,
        on_stage=on_stage,
    )

    if args.json:
        print(render_json(report, est, root))
        return 0
    if args.verbose:
        render_table(report, est, root, err)  # the table is context: stderr, so stdout stays two lines
    render_two_numbers(est, console)
    if report.unreadable_dirs or report.walk_incomplete:
        err.print(f"warning: {report.unreadable_dirs:,} folder(s) could not be read"
                  + (" and the folder walk was cut short" if report.walk_incomplete else "")
                  + "; files in them are not counted.", markup=False)
    failed = sum(1 for r in report.results if r.error)
    if failed:
        err.print(f"{failed:,} file(s) could not be read (for example password-protected or damaged) and are "
                  f"counted by size only (details with --json or -v).", markup=False)
    a = archive_summary(report)
    if a["measured"]:
        msg = (f"{a['measured']:,} archive(s) are counted by their estimated contents "
               f"(~{a['unpacked_units_est']:,} units); POMA does not unpack archives, so unpack them before upload")
        if a["not_listed"]:
            msg += f"; {a['not_listed']:,} could not be listed and are estimated from their size"
        err.print(msg + ".", style="dim", markup=False)
    return 0


def _reap_children() -> None:
    """SIGTERM direct children (e.g. a stuck pdftotext) so none outlive us."""
    if os.name != "posix":
        return
    import signal
    import subprocess
    try:
        out = subprocess.run(["pgrep", "-P", str(os.getpid())], capture_output=True,
                             text=True, timeout=5).stdout
    except Exception:
        return
    for line in out.split():
        try:
            os.kill(int(line), signal.SIGTERM)
        except (ValueError, OSError):
            pass


def run() -> None:
    """Entry point: exit hard after flushing, so a worker thread stuck in a
    broken file can never keep the process alive past the time budget."""
    rc = 1
    try:
        try:
            rc = main()
        except SystemExit as e:  # argparse --help / --version / usage errors
            rc = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        except KeyboardInterrupt:
            rc = 130
        except BrokenPipeError:
            rc = 141
        except Exception as e:
            print(f"error: {e.__class__.__name__}: {e}", file=sys.stderr)
            rc = 1
        for f in (sys.stdout, sys.stderr):
            try:
                f.flush()
            except Exception:
                pass
        _reap_children()
    finally:
        os._exit(rc)


if __name__ == "__main__":
    run()
