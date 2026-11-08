"""Progressive estimation: walk, rough size-based guess, then ever larger
random samples (stratified by type and size) until the estimate is tight
enough, the time budget is spent, or every file has been measured.

Every stage reports (elapsed, estimate, error bar, share measured), so the
caller can print the estimate homing in and the user can stop whenever it
is exact enough.
"""

import math
import os
import random
import time
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path

from poma_estimate import measures as M
from poma_estimate.credit_math import KB_PER_CREDIT
from poma_estimate.scan import FileResult, ScanReport, _UF_DATALESS, _classify, estimate_one

FRACTIONS = (0.01, 0.03, 0.10, 0.30, 1.0)
MIN_PER_STRATUM = 3
Z95 = 1.96
EMIT_INTERVAL = 1.5  # seconds between progress lines (the last one always prints)
PROGRESS_INTERVAL = 30.0  # seconds between progress lines inside one long sampling stage


@dataclass
class Entry:
    path: Path
    ext: str
    size: int


@dataclass
class Estimate:
    expected: float
    low: float
    high: float
    rel_err: float  # 95% half-width of the sampling error, relative; 0 when complete
    complete: bool  # every discovered file was visited and the walk finished
    exact: bool  # complete, nothing sampled/size-modelled, tight band
    measured: int
    total: int
    measured_bytes: int
    total_bytes: int
    stage: str
    elapsed: float
    approx_files: int = 0
    unreadable_dirs: int = 0
    unreadable_files: int = 0
    stopped: str | None = None
    counts: dict | None = None  # per file-type group counts (measures.py), same extrapolation as `expected`
    # Projections from the measuring rate so far (None until there is a rate): seconds until every
    # file is measured, and the error bar expected when the time budget runs out.
    eta_all: float | None = None
    rel_err_at_budget: float | None = None
    var_sampling: float = 0.0  # variance from sampling within measured strata: shrinks ~ (1 - n/N) / n
    var_floor: float = 0.0  # size-model floors and unmeasured strata: shrink ~ (1 - n/N)^2


def project(est: Estimate, rate: float, seconds_left: float, done: int | None = None,
            total: int | None = None) -> None:
    """Fill est.eta_all / est.rel_err_at_budget from the measuring rate so far, scaling each
    variance part as if files keep being measured uniformly at random (stratum shares ignored).
    `done`/`total` and `rate` should cover only the sampled files: media files are all measured
    in the first stage and are cheap, so counting them would overstate the throughput left."""
    done = est.measured if done is None else done
    total = est.total if total is None else total
    if est.complete or rate <= 0 or done < 2 or total <= done or not est.expected:
        return
    est.eta_all = (total - done) / rate
    n2 = min(total, done + rate * max(0.0, seconds_left))
    if n2 >= total:
        est.rel_err_at_budget = 0.0
        return
    f1, f2 = done / total, n2 / total
    var = (est.var_sampling * ((1 - f2) / n2) / ((1 - f1) / done)
           + est.var_floor * ((1 - f2) / (1 - f1)) ** 2)
    est.rel_err_at_budget = Z95 * math.sqrt(var) / est.expected


def _timed(path: Path, fast: bool) -> tuple[FileResult, float]:
    """estimate_one plus the seconds it took in its worker thread."""
    t = time.monotonic()
    r = estimate_one(path, fast)
    return r, time.monotonic() - t


def walk(root: Path, recursive: bool, include_icloud: bool, report: ScanReport,
         deadline: float = float("inf"), out: list[Entry] | None = None) -> list[Entry]:
    """Collect supported files with their sizes in one pass: no hidden
    entries, no symlinks, one stat per file."""
    out = [] if out is None else out
    if root.is_file():
        ext = root.suffix.lstrip(".").lower()
        st = root.stat()
        if _classify(ext) is None:
            report.unsupported += 1
            return out
        if not include_icloud and getattr(st, "st_flags", 0) & _UF_DATALESS:
            report.icloud_skipped.append(root)
            return out
        out.append(Entry(root, ext, st.st_size))
        return out
    stack = [str(root)]
    while stack:
        if time.monotonic() > deadline:
            report.walk_incomplete = True
            break
        d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            report.unreadable_dirs += 1
            continue
        with it:
            for e in it:
                if time.monotonic() > deadline:
                    report.walk_incomplete = True
                    return out
                if e.name.startswith("."):
                    continue
                try:
                    if e.is_symlink():
                        continue
                    if e.is_dir(follow_symlinks=False):
                        if recursive:
                            stack.append(e.path)
                        continue
                    if not e.is_file(follow_symlinks=False):
                        continue
                    ext = e.name.rpartition(".")[2].lower() if "." in e.name else ""
                    if _classify(ext) is None:
                        report.unsupported += 1
                        continue
                    st = e.stat(follow_symlinks=False)
                    if not include_icloud and getattr(st, "st_flags", 0) & _UF_DATALESS:
                        report.icloud_skipped.append(Path(e.path))
                        continue
                    out.append(Entry(Path(e.path), ext, st.st_size))
                except OSError:
                    report.unreadable_dirs += 1  # entry we could not inspect
                    continue
    return out


def rough_units(ext: str, size: int) -> float:
    """Size-only guess used for the first stage and for empty strata."""
    kind = _classify(ext)
    if kind == "archive":
        return rough_units("pdf", size)  # contents unknown until listed: as PDFs of that size
    if kind == "image":
        return 1.0
    if kind == "media":
        return max(1.0, size / 10e6)  # ~10 MB per minute as a blunt middle
    kb = KB_PER_CREDIT.get(M.canonical(ext), 4)  # ~4 KB of plain text per 1,000 tokens
    return max(1.0, size / 1024 / kb)


def _bucket(size: int) -> int:
    return size.bit_length() // 2  # base-4 size classes


def _stratify(entries: list[Entry], seed: int = 0) -> dict[tuple, list[Entry]]:
    rng = random.Random(seed)
    strata: dict[tuple, list[Entry]] = defaultdict(list)
    for e in entries:
        strata[(e.ext, _bucket(e.size))].append(e)
    ordered: dict[tuple, list[Entry]] = {}
    for key in sorted(strata, key=str):  # sorted first: directory listing order differs between file systems
        v = sorted(strata[key], key=lambda e: str(e.path))
        rng.shuffle(v)
        ordered[key] = v
    return ordered


def combine(
    strata: dict[tuple, list[Entry]],
    measured: dict[tuple, list[FileResult]],
    failed: dict[tuple, set],
    stage: str,
    elapsed: float,
    report: ScanReport,
) -> Estimate:
    exp = lo = hi = var = var_floor = 0.0
    totals: dict = {}
    n_meas = n_tot = n_done = b_meas = b_tot = approx = errs = 0
    n_failed = sum(len(v) for v in failed.values())
    for key, ents in strata.items():
        res = measured.get(key, [])
        n = len(res)
        bad = failed.get(key, set())
        nf = len(bad)
        N = len(ents) - nf  # files that vanished or crashed leave the population
        ents = [e for e in ents if e.path not in bad]
        n_tot += len(ents) + nf
        n_done += n + nf
        b_tot += sum(e.size for e in ents)
        n_meas += n
        b_meas += sum(r.size_bytes for r in res)
        approx += sum(1 for r in res if r.detail.get("sampled") or r.detail.get("approx")
                      or r.detail.get("method") == "size")
        errs += sum(1 for r in res if r.error)
        if N <= 0:
            continue
        if n == 0:
            g = sum(rough_units(e.ext, e.size) for e in ents)
            exp += g
            lo += g * 0.5
            hi += g * 2
            var_floor += (0.5 * g) ** 2
            for e in ents:
                M.add_unmeasured(totals, M.group_of(e.ext, _classify(e.ext)), 1, e.size)
            continue
        # files that could not be read count by size only: units from the size guess, counts as "size only"
        ys = [r.band.expected if not r.error else rough_units(r.ext, r.size_bytes) for r in res]
        mean = sum(ys) / n
        exp += N * mean
        exact_bytes = sum(e.size for e in ents)  # sizes are known for every file, measured or not
        g = None
        for r in res:
            g, m = M.counts_for_result(r)
            m = dict(m, bytes=0)
            M.add(totals, g, m, N / n)  # each measured file stands for N/n files of its stratum
        if g is not None:
            M.add(totals, g, {"bytes": exact_bytes})
        lo += N * sum((r.band.low if not r.error else 0.5 * y) for r, y in zip(res, ys)) / n
        hi += N * sum((r.band.high if not r.error else 2.0 * y) for r, y in zip(res, ys)) / n
        if n >= N:
            continue
        s2 = sum((y - mean) ** 2 for y in ys) / (n - 1) if n >= 2 else mean * mean
        # Few identical samples prove little: assume at least ~50%/sqrt(n) spread.
        s2 = max(s2, 0.25 * mean * mean / n)
        var += N * N * (1 - n / N) * s2 / n
        # Zero observed variance proves nothing about unseen files: add a
        # size-based floor (20% of what the rough model says is unmeasured).
        unseen = sum(rough_units(e.ext, e.size) for e in ents) * (1 - n / N)
        var_floor += (0.2 * unseen) ** 2
    half = Z95 * math.sqrt(var + var_floor)
    complete = n_done >= n_tot and not report.walk_incomplete and report.unreadable_dirs == 0
    tight = exp > 0 and (hi - lo) <= 0.01 * exp
    return Estimate(
        expected=exp, low=max(0.0, lo - (0 if complete else half)),
        high=hi + (0 if complete else half),
        rel_err=0.0 if complete else (half / exp if exp else 0.0),
        complete=complete, exact=complete and approx == 0 and errs == 0 and n_failed == 0 and (tight or exp == 0),
        measured=n_meas, total=n_tot,
        measured_bytes=b_meas, total_bytes=b_tot, stage=stage, elapsed=elapsed,
        approx_files=approx, unreadable_dirs=report.unreadable_dirs,
        unreadable_files=errs + n_failed, counts=M.rounded(totals),
        var_sampling=var, var_floor=var_floor,
    )


def scan_progressive(
    root: Path,
    *,
    recursive: bool = True,
    include_icloud: bool = False,
    fast: bool = False,
    workers: int = 8,
    budget: float = 600.0,
    on_stage: Callable[[Estimate], None] | None = None,
) -> tuple[ScanReport, Estimate]:
    t0 = time.monotonic()
    deadline = t0 + budget
    report = ScanReport()
    emit = on_stage or (lambda e: None)

    entries: list[Entry] = []
    try:
        walk(root, recursive, include_icloud, report, deadline, entries)
    except KeyboardInterrupt:
        report.walk_incomplete = True
        strata = _stratify(entries)
        est = combine(strata, defaultdict(list), defaultdict(set), "discovery (stopped by user)",
                      time.monotonic() - t0, report)
        est.stopped = "stopped by user"
        return report, est
    strata = _stratify(entries)
    measured: dict[tuple, list[FileResult]] = defaultdict(list)
    failed: dict[tuple, set] = defaultdict(set)
    pos: dict[tuple, int] = defaultdict(int)  # next unsubmitted index per stratum
    est = combine(strata, measured, failed, "walked, size-based guess", time.monotonic() - t0, report)
    emit(est)
    if not entries:
        return report, est

    pool = ThreadPoolExecutor(max_workers=workers)
    last_emit = time.monotonic()
    cost: dict[tuple, list] = defaultdict(lambda: [0.0, 0])  # per stratum: worker-seconds, files timed
    stop = None  # None | "time budget reached" | "stopped by user"
    sampled_keys = [k for k in strata if _classify(k[0]) != "media"]
    sampled_total = sum(len(strata[k]) for k in sampled_keys)

    def snapshot(label: str) -> Estimate:
        now = time.monotonic()
        e = combine(strata, measured, failed, label, now - t0, report)
        # Remaining work = files left in each stratum x that stratum's mean worker-seconds per file
        # (the overall mean where none was timed yet), spread over the workers. Per stratum, because
        # costs differ by orders of magnitude between types and sizes; worker time, so idle time and
        # strata already finished do not distort it.
        t_all = sum(c[0] for c in cost.values())
        n_all = sum(c[1] for c in cost.values())
        if n_all < 2 or t_all <= 0:
            return e
        mean_all = t_all / n_all
        media_s = docs_s = 0.0
        docs_left = 0
        for k, ents in strata.items():
            left = len(ents) - len(measured.get(k, ())) - len(failed.get(k, ()))
            if left <= 0:
                continue
            c = cost.get(k)
            s_k = left * (c[0] / c[1] if c and c[1] else mean_all) / workers
            if _classify(k[0]) == "media":
                media_s += s_k
            else:
                docs_s += s_k
                docs_left += left
        done = sampled_total - docs_left
        # media are all queued in the first stage, so their remaining time comes off the budget first
        project(e, docs_left / docs_s if docs_s > 0 else 0.0, deadline - now - media_s, done, sampled_total)
        if e.eta_all is not None:
            e.eta_all += media_s
        elif media_s > 0 and not e.complete:
            e.eta_all = media_s
        return e

    try:
        for frac in FRACTIONS:
            todo = []
            for key, ents in strata.items():
                N = len(ents)
                if _classify(key[0]) == "media":
                    want = N  # duration headers are cheap: always exhaustive
                else:
                    want = min(N, max(MIN_PER_STRATUM, math.ceil(frac * N)))
                todo += [(key, e) for e in ents[pos[key]:want]]
                pos[key] = max(pos[key], want)
            random.Random(1).shuffle(todo)
            pending: dict = {}
            it = iter(todo)
            exhausted = False
            while pending or not exhausted:
                while not exhausted and len(pending) < workers * 4 and time.monotonic() < deadline:
                    nxt = next(it, None)
                    if nxt is None:
                        exhausted = True
                        break
                    pending[pool.submit(_timed, nxt[1].path, fast)] = (nxt[0], nxt[1])
                if time.monotonic() >= deadline:
                    stop = "time budget reached"
                    exhausted = True
                if not pending:
                    break
                done, _ = wait(pending, timeout=1.0, return_when=FIRST_COMPLETED)
                for f in done:
                    key, ent = pending.pop(f)
                    try:
                        r, dt = f.result()
                    except Exception:
                        failed[key].add(ent.path)
                        continue
                    cost[key][0] += dt
                    cost[key][1] += 1
                    if r.detail.get("kind") == "skipped":
                        failed[key].add(ent.path)
                    else:
                        measured[key].append(r)
                if stop and time.monotonic() > deadline + 10:
                    break  # in-flight stragglers are abandoned (caller exits the process)
                if not stop and time.monotonic() - last_emit >= PROGRESS_INTERVAL:
                    emit(snapshot("measuring"))  # a long stage still shows the estimate moving
                    last_emit = time.monotonic()
            now = time.monotonic()
            label = f"sample {int(frac * 100)}%" if frac < 1 else "all files"
            est = snapshot(label)
            if now - last_emit >= EMIT_INTERVAL or est.complete or stop:
                emit(est)
                last_emit = now
            if stop or est.complete:
                break
    except KeyboardInterrupt:
        stop = "stopped by user"
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    est = snapshot(est.stage + (f" ({stop})" if stop else ""))
    est.stopped = stop
    est.rel_err_at_budget = None  # nothing more will be measured
    if stop:
        emit(est)
    report.results = sorted((r for v in measured.values() for r in v), key=lambda r: str(r.path))
    return report, est
