"""Cached aggregate host metrics; no process, path, or identity data is public."""
from __future__ import annotations

import math
import os
from pathlib import Path
import shutil
import threading
import time


def cpu_counters(text):
    """Use aggregate ticks, excluding guest ticks already counted in user/nice."""
    fields = text.splitlines()[0].split()
    if not fields or fields[0] != 'cpu' or len(fields) < 5:
        raise ValueError('Missing aggregate CPU counters')
    values = [int(value) for value in fields[1:9]]
    if any(value < 0 for value in values):
        raise ValueError('Invalid CPU counter')
    return tuple(values + [0] * (8 - len(values)))


def cpu_delta(previous, current):
    if previous is None or current is None:
        return None
    differences = [new - old for old, new in zip(previous, current)]
    # Linux iowait can decrease; it is idle time, and never negative work.
    if any(value < 0 for index, value in enumerate(differences) if index != 4):
        return None
    differences[4] = max(0, differences[4])
    total = sum(differences)
    if total <= 0:
        return None
    busy = total - differences[3] - differences[4]
    return round(max(0.0, min(100.0, busy * 100.0 / total)), 2)


def usage(total=None, used=None):
    if total is None or total < 0:
        return {'used_bytes': None, 'total_bytes': None, 'percent': None}
    if used is None or not 0 <= used <= total:
        return {'used_bytes': None, 'total_bytes': total, 'percent': None}
    return {'used_bytes': used, 'total_bytes': total,
            'percent': round(used * 100.0 / total, 2) if total else None}


def memory_usage(text):
    values = {}
    for line in text.splitlines():
        fields = line.split()
        if not fields or fields[0] not in {'MemTotal:', 'MemAvailable:'}:
            continue
        if len(fields) != 3 or fields[2] != 'kB':
            raise ValueError('Invalid memory counter')
        values[fields[0][:-1]] = int(fields[1]) * 1024
    total, available = values.get('MemTotal'), values.get('MemAvailable')
    used = total - available if total is not None and available is not None and available >= 0 else None
    return usage(total, used)


class HostStatsSampler:
    """One two-second sampler; concurrent requests read immutable snapshots."""
    def __init__(self, data_path, *, interval=2.0, proc_root='/proc', start=True):
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError('Sampling interval must be positive')
        self.data_path = Path(data_path)
        self.proc_root = Path(proc_root)
        self.interval = float(interval)
        self._stop = threading.Event()
        self._cache_lock = threading.Lock()
        self._sample_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._previous_cpu = None
        self._previous_monotonic = None
        self._thread = None
        self._cache = None
        self.sample()
        if start:
            self.start()

    def sample(self):
        with self._sample_lock:
            try:
                with (self.proc_root / 'stat').open() as stream:
                    current_cpu = cpu_counters(stream.readline())
            except (OSError, ValueError, IndexError):
                current_cpu = None
            monotonic = time.monotonic()
            elapsed = None
            if current_cpu is not None and self._previous_cpu is not None:
                elapsed = monotonic - self._previous_monotonic
                if elapsed <= 0:
                    elapsed = None
            cpu_percent = cpu_delta(self._previous_cpu, current_cpu) if elapsed is not None else None
            self._previous_cpu, self._previous_monotonic = current_cpu, monotonic
            try:
                memory = memory_usage((self.proc_root / 'meminfo').read_text())
            except (OSError, ValueError):
                memory = usage()
            try:
                disk = shutil.disk_usage(self.data_path)
                disk = usage(disk.total, disk.used)
            except (OSError, ValueError):
                disk = usage()
            try:
                load = list(os.getloadavg())
                if len(load) != 3 or any(not math.isfinite(value) or value < 0 for value in load):
                    load = None
            except (AttributeError, OSError, ValueError):
                load = None
            try:
                count = os.cpu_count()
            except OSError:
                count = None
            snapshot = {'cpu_percent': cpu_percent, 'cpu_count': count if count and count > 0 else None,
                        'memory': memory, 'disk': disk, 'load_average': load,
                        'collected_at': time.time(), 'sample_seconds': round(elapsed, 3) if elapsed is not None else None}
            with self._cache_lock:
                self._cache = snapshot

    def snapshot(self):
        with self._cache_lock:
            return {**self._cache, 'memory': dict(self._cache['memory']), 'disk': dict(self._cache['disk']),
                    'load_average': list(self._cache['load_average']) if self._cache['load_average'] is not None else None}

    def _run(self):
        while not self._stop.wait(self.interval):
            self.sample()

    def start(self):
        with self._lifecycle_lock:
            if self._thread is None and not self._stop.is_set():
                self._thread = threading.Thread(target=self._run, name='host-stats', daemon=True)
                self._thread.start()

    def close(self):
        self._stop.set()
        with self._lifecycle_lock:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1)
