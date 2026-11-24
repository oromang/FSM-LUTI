"""Run-local transport memoization, with disk storage for large OD arrays.

The directory belongs to one backend/configuration and is emptied when the run
finishes. Only identical year, projects, population and employment are reused.
Pickles are internal temporary files written by this process, not input data.
"""
from pathlib import Path
import hashlib
import pickle
import time
import numpy as np


class TransportMemo:
    def __init__(self, backend, directory, progress=None, shared_entries=None):
        self.backend = backend
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.progress = progress or (lambda row: None)
        # Parent-owned completed references are read-only in project workers.
        # New entries, if any, are written only into this memo's local directory.
        self.entries = {key: Path(value) for key, value in (shared_entries or {}).items()}
        self.hits = 0
        self.solves = 0
        self.solve_seconds = 0.

    @property
    def warm_start_enabled(self):
        return bool(getattr(self.backend, 'warm_start_enabled', False))

    def __call__(self, year, population, employment, project_ids):
        return self.with_initialization(year, population, employment, project_ids)

    def with_initialization(self, year, population, employment, project_ids, *, seed=None, purpose='reference'):
        projects = tuple(sorted(project_ids))
        digest = hashlib.sha256()
        digest.update(repr((float(year), projects)).encode())
        for vector in (population, employment):
            values = np.ascontiguousarray(vector, dtype='<f8')
            digest.update(repr(values.shape).encode())
            digest.update(values.tobytes())
        key = digest.hexdigest()
        if key in self.entries:
            self.hits += 1
            self.progress({'event': 'transport_cache_hit', 'year': float(year), 'projects': projects})
            with self.entries[key].open('rb') as stream:
                return pickle.load(stream)
        self.progress({'event': 'transport_start', 'year': float(year), 'projects': projects})
        started = time.perf_counter()
        method = getattr(self.backend, 'with_initialization', None)
        if method is None:
            outcome = self.backend(year, population, employment, projects)
        else:
            outcome = method(year, population, employment, projects, seed=seed, purpose=purpose)
        elapsed = time.perf_counter() - started
        self.solves += 1
        self.solve_seconds += elapsed
        self.progress({'event': 'transport_done', 'year': float(year), 'projects': projects,
                       'elapsed_seconds': elapsed, 'converged': outcome.metrics.get('converged')})
        # Never recycle a nonconverged assignment as a valid equilibrium.
        # References recur across every project case. Project/LUTI iterates can
        # be numerous and rarely recur; do not fill disk with their OD arrays.
        if not projects and outcome.metrics.get('converged') is True:
            path = self.directory / (key + '.pickle')
            temporary = path.with_suffix('.partial')
            with temporary.open('wb') as stream:
                pickle.dump(outcome, stream, protocol=pickle.HIGHEST_PROTOCOL)
            temporary.replace(path)
            self.entries[key] = path
        return outcome

    def statistics(self):
        return {'unique_transport_solves': self.solves, 'exact_cache_hits': self.hits,
                'transport_solve_seconds': self.solve_seconds}
