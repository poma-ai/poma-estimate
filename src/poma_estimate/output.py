"""Output formatting: human-readable table + JSON.

Display intent: report a single rounded unit count per file and per
total. Internal low/expected/high bands stay in the JSON output for
power users who want to see uncertainty, but the human table reads as
"~25 units" — honest about the precision the underlying heuristics
actually have. See credit_math.round_estimate for the rounding ladder.
"""

import json
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

from poma_estimate import measures as M
from poma_estimate.credit_math import CreditBand, round_estimate
from poma_estimate.scan import FileResult, ScanReport



def _approx(n: int) -> str:
    """Render a credit count with the ~ prefix and locale grouping."""
    rounded = round_estimate(n)
    if rounded == 0:
        return "0"
    return f"~{rounded:,}"


def _fmt_tokens(d: dict) -> str:
    exp = d.get("tokens_expected", 0)
    return f"{exp:,}"


def headline(est) -> str:
    """'~740,000 units'; '(±3%)' while it is still a sample; '(rough guess)' first."""
    n = _approx(int(est.expected))
    if est.measured == 0:
        return f"{n} units (rough guess)"
    if est.complete or est.rel_err < 0.005:
        return f"{n} units"
    return f"{n} units (±{max(1, round(est.rel_err * 100))}%)"


def stage_line(est) -> str:
    """One progress line per stage, printed as the estimate homes in."""
    if est.measured == 0:
        how = "size-based guess"
    else:
        how = f"{est.measured:,}/{est.total:,} files measured"
    return f"  {est.elapsed:6.1f}s  {est.stage:<40} {headline(est):<30} {how}"


COUNTS_SCHEMA = "counts-1"  # version of the per-group count fields in --json


def render_two_numbers(est, console: Console | None = None) -> None:
    """Default output on stdout: the unit count, then one line per file-type group with plain counts
    (files, size, pages, pages without a text layer, text tokens, images, ...). Plain writes, not Rich,
    so a narrow terminal cannot wrap them."""
    lines = [f"units {round_estimate(int(est.expected)):,}"] + M.describe(est.counts or {})
    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()


def render_summary(report: ScanReport, est, root: Path, console: Console) -> None:
    """Headline numbers plus context lines (used by --verbose)."""
    errored = est.unreadable_files

    console.print()
    console.print(f"[bold]{headline(est)}[/bold]")
    console.print()

    bits = [f"{est.total:,} files counted"]
    if not est.complete:
        bits.append(
            f"estimated from a random sample of {est.measured:,} "
            f"({est.measured_bytes / max(1, est.total_bytes):.0%} of the data)"
        )
    if est.approx_files:
        bits.append(f"{est.approx_files:,} large or media files estimated from partial reads")
    if report.unsupported:
        bits.append(f"{report.unsupported:,} ignored (other file types)")
    if errored:
        bits.append(f"{errored:,} unreadable")
    if report.unreadable_dirs:
        bits.append(f"{report.unreadable_dirs:,} folders or files could not be inspected, so the total is incomplete")
    if report.walk_incomplete:
        bits.append("folder listing stopped at the time limit, so the total is incomplete")
    if report.icloud_skipped:
        bits.append(
            f"{len(report.icloud_skipped):,} iCloud placeholders skipped "
            "(use --include-icloud to download + scan)"
        )
    console.print("[dim]" + ", ".join(bits) + "[/dim]")
    if est.stopped:
        console.print(f"[dim]stopped early ({est.stopped}); the figure is the best estimate so far[/dim]")
    lo, hi = _approx(int(est.low)), _approx(int(est.high))
    if est.expected and lo != hi and (est.high - est.low) > 0.03 * est.expected:
        console.print(f"[dim]likely range: {lo} to {hi} units[/dim]")


def render_table(report: ScanReport, est, root: Path, console: Console) -> None:
    """Verbose per-file table — only used when --verbose is passed."""
    results = report.results
    table = Table(title=f"Estimate — {root}", show_lines=False)
    table.add_column("File", overflow="fold")
    table.add_column("Type", justify="left")
    table.add_column("Pages", justify="right")
    table.add_column("Tokens", justify="right")
    table.add_column("Units (est.)", justify="right", style="bold")
    table.add_column("Notes", overflow="fold")

    for r in results:
        notes_parts = []
        if r.detail.get("scanned"):
            notes_parts.append("scanned")
        if r.detail.get("presplit_adjusted"):
            raw = r.detail.get("pages_raw")
            if raw is not None:
                notes_parts.append(f"presplit {raw}→{r.detail['pages']}")
        n_total = r.detail.get("n_images_total")
        n_unique = r.detail.get("n_images_unique")
        if n_total:
            if n_unique is not None and n_unique != n_total:
                notes_parts.append(f"{n_unique}u/{n_total} img")
            else:
                notes_parts.append(f"{n_total} img")
        elif r.detail.get("n_images"):
            notes_parts.append(f"{r.detail['n_images']} img")
        if r.detail.get("real_ext") and r.detail.get("claimed_ext"):
            notes_parts.append(
                f"misnamed (.{r.detail['claimed_ext']} → .{r.detail['real_ext']})"
            )
        if r.error:
            notes_parts.append(f"err: {r.error}")
        table.add_row(
            str(r.path.relative_to(root) if r.path != root else r.path.name),
            r.detail.get("kind", r.ext or "?"),
            str(r.detail.get("pages", "?")),
            _fmt_tokens(r.detail),
            _approx(r.band.expected),
            ", ".join(notes_parts) or "",
        )

    console.print(table)
    render_summary(report, est, root, console)


def archive_summary(report: ScanReport) -> dict:
    """Archives found among the measured files: they count by their estimated contents (POMA
    does not unpack them, so upload them unpacked); `unpacked_*` is that size-based guess."""
    arch = [r.detail for r in report.results if (r.detail or {}).get("kind") == "archive"]
    states = {}
    for d in arch:
        s = d.get("archive_toc", "unreadable")
        states[s] = states.get(s, 0) + 1
    return {
        "measured": len(arch),
        "listed": states.get("listed", 0),
        "not_listed": len(arch) - states.get("listed", 0),  # unreadable, too large, compressed tar, rar/7z, truncated
        "listing_states": states,
        "members": sum(d.get("archive_members", 0) for d in arch),
        "member_bytes": sum(d.get("archive_member_bytes", 0) for d in arch),
        "supported_members": sum(d.get("archive_supported_members", 0) for d in arch),
        "unpacked_units_est": round(sum(d.get("unpacked_units_est", 0) for d in arch)),
        "estimated_from_size": len(arch) - states.get("listed", 0),
    }


def render_json(report: ScanReport, est, root: Path) -> str:
    """JSON output keeps raw low/expected/high (`files` lists the measured files only) so downstream code can do
    its own rounding or uncertainty propagation.
    """
    from poma_estimate.progressive import rough_units
    rows = []
    for r in report.results:
        group, counts = M.counts_for_result(r)
        u = round_estimate(r.band.expected) if not r.error else round_estimate(int(round(rough_units(r.ext, r.size_bytes))))
        rows.append({
            "path": str(r.path),
            "ext": r.ext,
            "size_bytes": r.size_bytes,
            "units": u,  # an unreadable file is counted by its size, like in the totals
            "units_raw": {
                "low": r.band.low,
                "expected": r.band.expected,
                "high": r.band.high,
            },
            "group": group,
            "counts": counts,
            "detail": r.detail,
            "error": r.error,
        })
    payload = {
        "root": str(root),
        "archives": archive_summary(report),
        "total_units": round_estimate(int(est.expected)),
        "total_units_raw": {
            "low": round(est.low),
            "expected": round(est.expected),
            "high": round(est.high),
        },
        "counts_schema": COUNTS_SCHEMA,
        "counts": est.counts or {},
        "complete": est.complete,
        "exact": est.exact,
        "approximate_files": est.approx_files,
        "unreadable_folders": report.unreadable_dirs,
        "relative_error_95": round(est.rel_err, 4),
        "files_counted": est.total,
        "files_measured": est.measured,
        "bytes_counted": est.total_bytes,
        "bytes_measured": est.measured_bytes,
        "walk_incomplete": report.walk_incomplete,
        "files": rows,
        "icloud_skipped": [str(p) for p in report.icloud_skipped],
        "ignored_files": report.unsupported,
    }
    return json.dumps(payload, indent=2, ensure_ascii=True)  # safe on any console encoding (e.g. Windows cp1252)
