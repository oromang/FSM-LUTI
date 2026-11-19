"""Explicit file provenance, stable identifiers and portable project paths."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import numpy as np


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(2**20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    def convert(item):
        if isinstance(item, np.ndarray): return item.tolist()
        if isinstance(item, np.generic): return item.item()
        if isinstance(item, Path): return str(item)
        raise TypeError(type(item).__name__)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, default=convert, allow_nan=False)+"\n", encoding="utf-8")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def aligned_indices(source_ids, target_ids):
    source = [str(x) for x in source_ids]
    target = [str(x) for x in target_ids]
    if len(set(source)) != len(source) or len(set(target)) != len(target):
        raise ValueError("Zone IDs must be unique")
    lookup = {v:i for i,v in enumerate(source)}
    missing = [v for v in target if v not in lookup]
    if missing: raise ValueError(f"Missing {len(missing)} zone IDs, e.g. {missing[:5]}")
    return np.array([lookup[v] for v in target], dtype=int)

