"""Progressive estimation: walk, rough size-based guess, then ever larger
random samples (stratified by type and size) until the estimate is tight
enough, the time budget is spent, or every file has been measured.

Every stage reports (elapsed, estimate, error bar, share measured), so the
caller can print the estimate homing in and the user can stop whenever it
is exact enough.
"""

import itertools
import math
import multiprocessing
import os
import random
import signal
import sys
import time
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, ThreadPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from pathlib import Path

from poma_estimate import measures as M
from poma_estimate.credit_math import KB_PER_CREDIT, CreditBand
from poma_estimate.scan import FileResult, ScanReport, _UF_DATALESS, _classify, estimate_one

FRACTIONS = (0.01, 0.03, 0.10, 0.30, 1.0)
MIN_PER_STRATUM = 3
Z95 = 1.96
FLOOR_REL = 0.35  # unseen files of a measured stratum may differ from its sample by this share (see combine)
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


def _terminate_children_of(pids: list[int]) -> None:
    """SIGTERM the direct children of the given processes (POSIX; best effort)."""
    if os.name != "posix" or not pids:
        return
    import subprocess
    try:
        out = subprocess.run(["pgrep", "-P", ",".join(map(str, pids))], capture_output=True,
                             text=True, timeout=5).stdout
    except Exception:
        return
    for line in out.split():
        try:
            os.kill(int(line), signal.SIGTERM)
        except (ValueError, OSError):
            pass


# Which task each worker is running and since when, so the ETA can count files still in flight.
# Process workers write into a shared array (pairs of task id, start time; slot per worker); thread
# workers into _THREAD_RUNNING. time.monotonic() is system-wide on macOS, Linux and Windows.
_SLOTS = None
_SLOT = None
_THREAD_RUNNING: dict[int, float] = {}
_TASK_IDS = itertools.count()


def _quiet_libraries() -> None:
    """The readers' diagnostics (malformed PDF objects, odd image metadata) are not actionable for an estimate."""
    import logging
    import warnings
    logging.getLogger("pdfminer").setLevel(logging.ERROR)
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    warnings.filterwarnings("ignore", category=UserWarning, module="openpyxl")
    warnings.filterwarnings("ignore", module="PIL")


def _start_quiet_resource_tracker() -> None:
    """Stopping at the time limit terminates busy workers, after which multiprocessing's resource
    tracker prints a "leaked semaphore objects" warning about what it then cleans up itself. Start the
    tracker (POSIX only) with its stderr on the null device; it has nothing else to say. Not via -W
    options: the one-file binary would read them as its own arguments."""
    if os.name != "posix":
        return
    try:
        from multiprocessing import resource_tracker
        sys.stderr.flush()
        saved = os.dup(2)
        null = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(null, 2)
            resource_tracker.ensure_running()
        finally:
            os.dup2(saved, 2)
            os.close(saved)
            os.close(null)
    except Exception:
        pass


class _TooManyCrashes(Exception):
    pass


def _kill_pool(pool) -> None:
    """Shut a process pool down now: its workers' children (e.g. a pdftotext) first, while they can
    still be found, then the workers, waiting at most 2 s for them to go."""
    procs = list((getattr(pool, "_processes", None) or {}).values())  # shutdown() drops this mapping
    pool.shutdown(wait=False, cancel_futures=True)
    _terminate_children_of([p.pid for p in procs if p.pid])
    for p in procs:
        try:
            p.terminate()
        except Exception:
            pass
    end = time.monotonic() + 2.0
    for p in procs:
        try:
            p.join(max(0.0, end - time.monotonic()))
        except Exception:
            pass


def _worker_init(slots=None, counter=None) -> None:
    """Process-pool workers leave Ctrl+C to the parent, which stops and reports the estimate so far."""
    global _SLOTS, _SLOT
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    _quiet_libraries()  # spawned from source, workers never import the CLI that sets these
    if slots is not None and counter is not None:
        with counter.get_lock():
            _SLOT = counter.value
            counter.value += 1
        _SLOTS = slots if 2 * _SLOT + 1 < len(slots) else None  # a replacement worker beyond the slots goes untracked


def _timed(path: Path, fast: bool, tid: int = -1) -> tuple[FileResult, float]:
    """estimate_one plus the seconds it took in its worker."""
    t = time.monotonic()
    if _SLOTS is not None:
        _SLOTS[2 * _SLOT + 1] = t  # start first, id second: a reader never pairs a new id with no start
        _SLOTS[2 * _SLOT] = tid
    else:
        _THREAD_RUNNING[tid] = t
    try:
        r = estimate_one(path, fast)
    finally:
        if _SLOTS is not None:
            _SLOTS[2 * _SLOT] = -1
        else:
            _THREAD_RUNNING.pop(tid, None)
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
        # Zero observed variance proves nothing about unseen files: add a floor of FLOOR_REL of the
        # unmeasured files' units, valued at this stratum's measured mean. Not at the size model:
        # that ran ~10x high on a real folder and kept the band at ±100% while the estimate had
        # settled. 0.35: resampling a fully measured 850-file mix (PDF, text, images) gave 96-97%
        # coverage at 1-30% sampled (0.2 gave 82-89%).
        var_floor += (FLOOR_REL * (N - n) * mean) ** 2
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


def _interrupt_once(signum, frame):
    """First Ctrl+C stops the scan; repeats are ignored until it has wrapped up (the one-file binary's
    launcher forwards Ctrl+C to the scanner, which therefore gets it twice from one key press)."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    raise KeyboardInterrupt


def scan_progressive(*args, **kwargs) -> tuple[ScanReport, Estimate]:
    import threading
    if threading.current_thread() is not threading.main_thread() or signal.getsignal(signal.SIGINT) is not signal.default_int_handler:
        return _scan_progressive(*args, **kwargs)
    signal.signal(signal.SIGINT, _interrupt_once)
    try:
        return _scan_progressive(*args, **kwargs)
    finally:
        signal.signal(signal.SIGINT, signal.default_int_handler)


def _scan_progressive(
    root: Path,
    *,
    recursive: bool = True,
    include_icloud: bool = False,
    fast: bool = False,
    workers: int = 8,
    budget: float = 600.0,
    on_stage: Callable[[Estimate], None] | None = None,
    processes: bool = False,
) -> tuple[ScanReport, Estimate]:
    """`processes=True` measures in worker processes: the PDF reader is pure Python, so threads
    share one core (measured 3.7x slower on 120 PDFs with 8 workers). The CLI uses processes;
    the default stays threads so tests can monkeypatch the estimators."""
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

    if processes:
        _start_quiet_resource_tracker()
        if os.name == "nt":
            workers = min(workers, 61)  # the Windows process pool's limit
        ctx = multiprocessing.get_context("spawn")
        slots = ctx.Array("d", [-1.0] * (2 * workers * 4), lock=False)

        def new_pool():
            return ProcessPoolExecutor(max_workers=workers, mp_context=ctx, initializer=_worker_init,
                                       initargs=(slots, ctx.Value("i", 0)))
        pool = new_pool()
    else:
        slots = None
        pool = ThreadPoolExecutor(max_workers=workers)
    in_flight: dict[int, tuple] = {}  # task id -> (stratum, submit time) for every submitted, unfinished file

    def running_starts() -> list[tuple]:
        """(stratum, start time) of each file a worker is measuring right now (queued files excluded)."""
        if slots is not None:
            pairs = [(int(slots[i]), slots[i + 1]) for i in range(0, len(slots), 2)]
        else:
            pairs = list(_THREAD_RUNNING.items())
        out = []
        for tid, t in pairs:
            meta = in_flight.get(tid)
            if meta is not None:
                out.append((meta[0], max(t, meta[1])))  # never before it was submitted
        return out
    import threading
    shield = processes and threading.current_thread() is threading.main_thread()

    def submit(key: tuple, path: Path):
        """Workers are spawned inside submit(): ignore Ctrl+C for that moment so they inherit the
        ignored disposition and a Ctrl+C during their start-up prints no tracebacks from them."""
        tid = next(_TASK_IDS)
        in_flight[tid] = (key, time.monotonic())
        old = signal.signal(signal.SIGINT, signal.SIG_IGN) if shield else None
        try:
            return pool.submit(_timed, path, fast, tid), tid
        finally:
            if shield:
                signal.signal(signal.SIGINT, old)
    last_emit = time.monotonic()
    cost: dict[tuple, list] = defaultdict(lambda: [0.0, 0])  # per stratum: worker-seconds, files timed
    stop = None  # None | "time budget reached" | "stopped by user"
    sampled_keys = [k for k in strata if _classify(k[0]) != "media"]
    sampled_total = sum(len(strata[k]) for k in sampled_keys)

    def snapshot(label: str, running: list[tuple] | None = None) -> Estimate:
        now = time.monotonic()
        e = combine(strata, measured, failed, label, now - t0, report)
        # Remaining work = files left in each stratum x that stratum's mean worker-seconds per file
        # (the overall mean where none was timed yet), spread over the workers. Per stratum, because
        # costs differ by orders of magnitude between types and sizes; worker time, so idle time and
        # strata already finished do not distort it.
        # Files still running count with the time they have run so far: fast files finish first,
        # so finished files alone make the remaining work look cheaper than it is.
        cost_now = {k: [c[0], c[1]] for k, c in cost.items()}
        for k, t in (running_starts() if running is None else running):
            cost_now.setdefault(k, [0.0, 0])[0] += max(0.0, now - t)
        t_all = sum(c[0] for c in cost_now.values())
        n_all = sum(c[1] for c in cost_now.values())
        if n_all < 2 or t_all <= 0:
            return e
        mean_all = t_all / n_all
        media_s = docs_s = 0.0
        docs_left = 0
        for k, ents in strata.items():
            left = len(ents) - len(measured.get(k, ())) - len(failed.get(k, ()))
            if left <= 0:
                continue
            c = cost_now.get(k)
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

    pending: dict = {}  # carried across stages, so the next stage fills the queue behind a stage's slow files
    retry: list[tuple] = []  # files in flight when a worker process died, measured once more
    suspects: dict[Path, int] = defaultdict(int)
    rebuilds = 0

    def retry_or_fail(key: tuple, ent: Entry) -> None:
        suspects[ent.path] += 1
        if suspects[ent.path] >= 2:  # crashed again when measured alone: unreadable, counted by its size
            measured[key].append(FileResult(path=ent.path, ext=ent.ext, size_bytes=ent.size,
                                            band=CreditBand(0, 0, 0),
                                            detail={"kind": _classify(ent.ext), "error": "the reader crashed"},
                                            error="the reader crashed on this file"))
        else:
            retry.append((key, ent))

    def recover() -> None:
        """A worker died (native crash, killed for memory): every in-flight file fails with it. Kill
        what is left of the pool, start a new one and measure those files once more."""
        nonlocal pool, rebuilds
        for f, (key, ent, tid) in list(pending.items()):
            in_flight.pop(tid, None)
            retry_or_fail(key, ent)
        pending.clear()
        _kill_pool(pool)
        rebuilds += 1
        if rebuilds > 5:
            raise _TooManyCrashes
        pool = new_pool()
    running_at_stop: list[tuple] | None = None
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
            it = iter(todo)
            exhausted = False
            last_stage = frac == FRACTIONS[-1]
            while pending or retry or not exhausted:
                if exhausted and not retry and not last_stage and not stop:
                    break  # this stage is all submitted: start queueing the next one without waiting
                while (retry or not exhausted) and len(pending) < workers * 4 and time.monotonic() < deadline:
                    solo = bool(retry)
                    if solo and pending:
                        break  # files in flight at a worker death are retried one at a time, alone
                    nxt = retry.pop() if solo else next(it, None)
                    if nxt is None:
                        exhausted = True
                        break
                    try:
                        f, tid = submit(nxt[0], nxt[1].path)
                    except BrokenProcessPool:
                        retry.append(nxt)
                        recover()
                        continue
                    pending[f] = (nxt[0], nxt[1], tid)
                    if solo:
                        break
                if time.monotonic() >= deadline:
                    stop = "time budget reached"
                    exhausted = True
                if not pending:
                    if retry and not stop:
                        continue
                    break
                done, _ = wait(pending, timeout=1.0, return_when=FIRST_COMPLETED)
                broken = False
                for f in done:
                    key, ent, tid = pending.pop(f)
                    in_flight.pop(tid, None)
                    try:
                        r, dt = f.result()
                    except BrokenProcessPool:
                        broken = True
                        retry_or_fail(key, ent)
                        continue
                    except Exception:
                        failed[key].add(ent.path)
                        continue
                    cost[key][0] += dt
                    cost[key][1] += 1
                    if r.detail.get("kind") == "skipped":
                        failed[key].add(ent.path)
                    else:
                        measured[key].append(r)
                if broken:
                    recover()
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
    except _TooManyCrashes:
        stop = "stopped: the measuring processes kept crashing"
    finally:
        running_at_stop = running_starts()  # before the workers go: the final ETA still counts them
        if processes and pending:  # a worker stuck in a broken file must not outlive the scan
            _kill_pool(pool)
        elif pending:  # threads cannot be killed: abandon them (the CLI exits the process)
            pool.shutdown(wait=False, cancel_futures=True)
        else:  # nothing in flight: wait, so a worker still starting up is not cut off mid-start (traceback)
            pool.shutdown(wait=True, cancel_futures=True)
    est = snapshot(est.stage + (f" ({stop})" if stop else ""), running=running_at_stop)
    pending.clear()
    in_flight.clear()
    est.stopped = stop
    est.rel_err_at_budget = None  # nothing more will be measured
    if stop:
        emit(est)
    report.results = sorted((r for v in measured.values() for r in v), key=lambda r: str(r.path))
    return report, est
