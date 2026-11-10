"""Shared input loading and scoring definitions used by the paper results."""
import gzip
import json
import re
from pathlib import Path
import numpy as np
import pandas as pd
from rapidfuzz import fuzz

DATA = Path(__file__).with_name('data')
REPS = 5000
FIELD_MAP = {'wsi_or_patch':('wsi_or_patch',),'tissue_site':('tissue_site',),
    'staining_type':('staining_type',),'modality':('modality',),'species':('species',),
    'domain':('domain',),'journal':('paper','venue')}

def read_data(name, root=DATA):
    with gzip.open(Path(root)/(name+'.json.gz'),'rt',encoding='utf-8') as f:
        return json.load(f)

def load_run(root):
    return tuple(pd.DataFrame(read_data(n,root)) for n in ['datasets','papers','metrics'])

VALIDATION = read_data('validation')

def load_gold():
    return VALIDATION['labels']['extraction']

def report(name):
    return VALIDATION['reports'][name.removesuffix('.json')]

def _get(obj, path):
    for k in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(k)
    return obj

def _norm(v) -> str:
    return "" if v is None else str(v).strip().lower()

def _same(field: str, got, want) -> bool:
    g, w = _norm(got), _norm(want)
    if field == "journal":
        if not g or not w:
            return g == w
        return g in w or w in g or fuzz.token_set_ratio(g, w) >= 80
    if "|" in g or "|" in w:
        return set(filter(None, g.split("|"))) == set(filter(None, w.split("|")))
    return g == w

def same_dataset(a: str, b: str) -> bool:
    """Loose match of two dataset names as written (\"CoNIC (Lizard dataset)\" ~ \"CoNIC\")."""
    na, nb = (re.sub(r"[^a-z0-9]+", " ", (x or "").lower()).split() for x in (a, b))
    return bool(na and nb) and (na == nb or set(na) <= set(nb) or set(nb) <= set(na))

def boot(units, rng):
    """units: list of (hits, total) per paper. Pooled rate and a percentile interval over papers."""
    h = np.array([u[0] for u in units], dtype=float)
    t = np.array([u[1] for u in units], dtype=float)
    idx = rng.integers(0, len(units), size=(REPS, len(units)))
    rates = h[idx].sum(1) / np.maximum(t[idx].sum(1), 1)
    return {"k": int(h.sum()), "n": int(t.sum()), "units": len(units), "p": float(h.sum() / t.sum()),
            "lo": float(np.percentile(rates, 2.5)), "hi": float(np.percentile(rates, 97.5))}
