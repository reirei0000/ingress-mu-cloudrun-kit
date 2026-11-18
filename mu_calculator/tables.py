"""Atomic, fingerprinted direct-mean density tables (never distributed)."""

import concurrent.futures
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import time
from importlib.metadata import version
import numpy as np
import s2sphere

# Manifest schema version.
VERSION = 1
# Bump whenever a change can alter any table value or the table layout:
# geometry.Grump.__init__/pix/sample, geometry.l15_centres, repack, tile_values,
# the S2 cell numbering. The fingerprint does not hash source code, so tables
# built by the old logic are otherwise accepted as current.
SAMPLING_VERSION = "direct-ij-l15-f64-s2id-v1"
LEVEL = 6
COUNT = 6 * 4**LEVEL


def max_level(value=None):
    value = os.environ.get("MU_TABLE_MAX_LEVEL", "6") if value is None else value
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise ValueError("MU_TABLE_MAX_LEVEL must be an integer from 6 to 11.")
    text = str(value).strip()
    if not text.isascii() or not text.isdecimal() or text.lstrip("0") not in {"6", "7", "8", "9", "10", "11"}:
        raise ValueError("MU_TABLE_MAX_LEVEL must be an integer from 6 to 11.")
    return int(text.lstrip("0"))


def table_path(folder, level=6):
    return Path(folder).parent / f"density-l{level}.json"


def count(level):
    return COUNT * 4**(level - LEVEL)


def fingerprint(folder):
    h = hashlib.sha256()
    h.update(f"{SAMPLING_VERSION};numpy={np.__version__};s2sphere={version('s2sphere')}".encode())
    for path in sorted(Path(folder).glob("*up00ag.*")):
        if path.suffix in (".hdr", ".bil"):
            h.update(path.name.encode())
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(4 * 1024**2), b""):
                    h.update(chunk)
    return h.hexdigest()


def checksum(path):
    h = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024**2), b""):
            h.update(chunk)
    return h.hexdigest()


def load_table(folder, signature=None, level=6):
    meta = table_path(folder, level)
    try:
        info = json.loads(meta.read_text())
        if (info["fingerprint"] != (signature or fingerprint(folder))
                or info["version"] != VERSION or info["level"] != level
                or info["count"] != count(level) or info["dtype"] != "<f8"
                or info["complete"] is not True):
            return None
        name = info["file"]
        if not isinstance(name, str) or Path(name).name != name or "/" in name or "\\" in name:
            return None
        path = meta.parent / name
        if path.stat().st_size != info["bytes"] or checksum(path) != info["sha256"]:
            return None
        a = np.load(path, mmap_mode="r", allow_pickle=False)
        if a.shape != (count(level),) or a.dtype != np.dtype("<f8"):
            return None
        if any(not np.isfinite(a[i:i+131072]).all() for i in range(0, len(a), 131072)):
            return None
        return a
    except (OSError, ValueError, KeyError, TypeError, EOFError):
        return None


def _init_worker(folder):
    global _grump
    from .geometry import Grump
    _grump = Grump(folder)


def repack(samples, level):
    """Reduce contiguous cell vectors, preserving the direct NumPy mean order."""
    n = 1 << (level - LEVEL)
    q = 512 // n
    vectors = np.ascontiguousarray(samples.reshape(n, q, n, q).transpose(0, 2, 1, 3))
    return vectors.reshape(n*n, q*q).mean(axis=-1).reshape(n, n)


def tile_values(parent, samples, level):
    means = repack(samples, level)
    _, i, j, _ = parent.to_face_ij_orientation()
    base = 1 << (30 - LEVEL)
    i0, j0 = i // base * base, j // base * base
    shift = 30 - level
    return np.array([means[(ci-i0) >> shift, (cj-j0) >> shift]
                     for child in parent.children(level)
                     for _, ci, cj, _ in [child.to_face_ij_orientation()]], dtype="<f8")


def _build_tile(task):
    from .geometry import l15_centres
    index, levels = task
    parent = s2sphere.CellId((2 * index + 1) << (60 - 2 * LEVEL))
    samples = _grump.sample(*l15_centres(parent.id())).reshape(512, 512)
    values, times = {}, {}
    for level in levels:
        start = time.monotonic()
        values[level] = tile_values(parent, samples, level)
        times[level] = time.monotonic() - start
    return index, values, times


def _atomic_json(path, info):
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w") as stream:
        json.dump(info, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def ensure_tables(folder, workers=None, maximum=None):
    """Synchronously prepare only missing levels, under one volume-wide OS lock."""
    maximum = max_level(maximum)
    signature = fingerprint(folder)
    levels = range(LEVEL, maximum + 1)
    current = {level: load_table(folder, signature, level) for level in levels}
    if all(a is not None for a in current.values()):
        return current
    if not list(Path(folder).glob("*up00ag.hdr")):
        raise RuntimeError("Population rasters are required to build density tables.")
    import fcntl
    with open(str(table_path(folder)) + ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = {level: load_table(folder, signature, level) for level in levels}
        missing = [level for level, a in current.items() if a is None]
        if not missing:
            return current
        if workers is None:
            workers = int(os.environ.get("MU_TABLE_WORKERS", min(6, os.cpu_count() or 1)))
        if workers < 1:
            raise ValueError("MU_TABLE_WORKERS must be positive.")
        start = time.monotonic()
        arrays, states, paths, reduction = {}, {}, {}, {l: 0.0 for l in missing}
        for level in missing:
            target = Path(folder).parent / f"density-l{level}-{signature}.npy"
            partial = target.with_suffix(".part")
            state_path = target.with_suffix(".state")
            states[level] = {}
            try:
                state = json.loads(state_path.read_text())
                a = np.load(partial, mmap_mode="r+", allow_pickle=False)
                if state["fingerprint"] != signature or a.shape != (count(level),) or a.dtype != np.dtype("<f8"):
                    raise ValueError("Obsolete checkpoint")
                width = 4**(level-LEVEL)
                for index, digest in state["tiles"].items():
                    idx = int(index)
                    if 0 <= idx < COUNT and hashlib.sha256(a[idx*width:(idx+1)*width].tobytes()).hexdigest() == digest:
                        states[level][index] = digest
            except (OSError, ValueError, KeyError, TypeError, EOFError):
                a = np.lib.format.open_memmap(partial, mode="w+", dtype="<f8", shape=(count(level),))
                # The file is sparse; reserve its blocks so that a full volume raises
                # ENOSPC here instead of killing the process with SIGBUS on a page write.
                with open(partial, "r+b") as stream:
                    os.posix_fallocate(stream.fileno(), 0, os.fstat(stream.fileno()).st_size)
            arrays[level] = a
            paths[level] = target, partial, state_path

        def checkpoint():
            for level, a in arrays.items():
                a.flush()
                with paths[level][1].open("rb") as stream:
                    os.fsync(stream.fileno())
                _atomic_json(paths[level][2], dict(fingerprint=signature, tiles=states[level]))

        tasks = iter((i, tuple(l for l in missing if str(i) not in states[l])) for i in range(COUNT))
        print(f"Building levels {missing} using {workers} workers", flush=True)
        with concurrent.futures.ProcessPoolExecutor(max_workers=workers,
                mp_context=multiprocessing.get_context("spawn"), initializer=_init_worker,
                initargs=(str(folder),)) as pool:
            pending = set()
            exhausted = False
            done_count = 0
            while pending or not exhausted:
                while not exhausted and len(pending) < workers * 2:
                    task = next(tasks, None)
                    if task is None:
                        exhausted = True
                    elif task[1]:
                        pending.add(pool.submit(_build_tile, task))
                if not pending:
                    continue
                ready, pending = concurrent.futures.wait(pending, return_when=concurrent.futures.FIRST_COMPLETED)
                for future in ready:
                    index, values, times = future.result()
                    for level, vector in values.items():
                        width = len(vector)
                        arrays[level][index*width:(index+1)*width] = vector
                        states[level][str(index)] = hashlib.sha256(vector.tobytes()).hexdigest()
                        reduction[level] += times[level]
                    done_count += 1
                if done_count >= 64:
                    checkpoint()
                    print(f"Density tiles ready: {min(len(s) for s in states.values())}/{COUNT}", flush=True)
                    done_count = 0
        checkpoint()
        for level in missing:
            target, partial, state_path = paths[level]
            a = arrays.pop(level)
            if any(not np.isfinite(a[i:i+131072]).all() for i in range(0, len(a), 131072)):
                raise RuntimeError(f"Non-finite density at level {level}")
            del a
            os.replace(partial, target)
            info = dict(version=VERSION, fingerprint=signature, file=target.name, level=level,
                        count=count(level), dtype="<f8", complete=True,
                        seconds=time.monotonic()-start, reduction_seconds=reduction[level],
                        bytes=target.stat().st_size, sha256=checksum(target))
            _atomic_json(table_path(folder, level), info)
            state_path.unlink(missing_ok=True)
            print(f"Level {level} ready: {info['seconds']:.2f}s shared build, "
                  f"{reduction[level]:.2f}s worker reduction, {info['bytes']} bytes", flush=True)
            current[level] = load_table(folder, signature, level)
            if current[level] is None:
                raise RuntimeError(f"Density table validation failed at level {level}")
        return current


def lookup(table, cell_id):
    level = s2sphere.CellId(cell_id).level()
    return float(table[cell_id >> (61 - 2 * level)])
