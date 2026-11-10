"""Corpus statistics, dataset grouping, sensitivity analyses and Figures 2-6.

Inputs are structured saved-run records; this module does not rerun inference.
"""

import json
import math
import re
from urllib.parse import urlsplit, unquote
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from evaluation.common import DATA, load_run, read_data
from pipeline.src.push_to_supabase import _slug_forms, canonicalize_url, resolve_metric_dataset

LAST_FULL_YEAR = 2025  # the run ended 2026-10-31, so 2026 is a part year
# Categorical colors for series and an ordered blue ramp for publication periods.
INK, INK2, GRID, SURFACE = "#0b0b0b", "#52514e", "#e6e5e1", "#fcfcfb"
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]  # blue, orange, aqua, yellow
ORDINAL = ["#86b6ef", "#5598e7", "#256abf", "#104281"]  # light -> dark blue, for ordered periods
NEUTRAL = "#a8a7a2"
MUTED, ACCENT, ACCENT2 = INK2, SERIES[0], SERIES[1]


def split_multi(v):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return []
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if str(x).strip()]
    return [x.strip() for x in str(v).split("|") if x.strip()]


def is_known(v):
    toks = [t.lower() for t in split_multi(v)]
    return bool(toks) and not all(t in {"unknown", "na", "n/a", "none", "other", "not reported", "nan"} for t in toks)


def gini(x):
    x = np.sort(np.asarray(x, dtype=float))
    if x.sum() == 0:
        return 0.0
    n = len(x)
    return float((2 * np.arange(1, n + 1) - n - 1).dot(x) / (n * x.sum()))


def wilson(k, n, z=1.96):
    if n == 0:
        return (float("nan"),) * 3
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return p, c - h, c + h


def num(v):
    try:
        f = float(str(v).strip().rstrip("%"))
    except (TypeError, ValueError):
        return None
    if math.isnan(f):
        return None
    return f / 100 if f > 1.0 and f <= 100 else f


def style():
    plt.rcParams.update({
        "font.family": "DejaVu Sans", "font.size": 10, "axes.titlesize": 11, "axes.titleweight": "bold",
        "axes.titlelocation": "left", "axes.titlepad": 8, "axes.labelsize": 10,
        "axes.edgecolor": INK2, "axes.linewidth": 0.6, "axes.labelcolor": INK2,
        "xtick.color": INK2, "ytick.color": INK2, "xtick.labelsize": 9.5, "ytick.labelsize": 9.5,
        "xtick.major.width": 0.6, "ytick.major.width": 0.6, "xtick.major.size": 3, "ytick.major.size": 3,
        "axes.spines.top": False, "axes.spines.right": False, "savefig.dpi": 300,
        "legend.frameon": False, "legend.fontsize": 9, "axes.grid": True, "axes.axisbelow": True,
        "grid.color": GRID, "grid.linewidth": 0.6, "lines.linewidth": 2, "lines.markersize": 4,
        "axes.facecolor": "white", "figure.facecolor": "white", "text.color": INK,
    })


def ygrid_only(ax):
    ax.grid(False, axis="x")
    ax.grid(True, axis="y")


def xgrid_only(ax):
    ax.grid(False, axis="y")
    ax.grid(True, axis="x")


def end_labels(ax, ends, gap):
    """Label each line just right of its last point. ends: [(name, x, y)]; labels closer than
    `gap` (data units) are pushed apart so they don't overlap. Text stays in ink, not the series color."""
    placed = []
    for name, x, y in sorted(ends, key=lambda t: t[2]):
        y_ = max(y, placed[-1] + gap) if placed else y
        placed.append(y_)
        ax.annotate(name, (x, y), xytext=(x + 0.35, y_), textcoords="data", va="center", fontsize=9, color=INK,
                    annotation_clip=False, linespacing=0.95)


DISPLAY = {"camelyon16": "CAMELYON16", "camelyon17": "CAMELYON17", "breakhis": "BreakHis", "bach": "BACH",
           "bracs": "BRACS", "panda": "PANDA", "lc25000": "LC25000", "patchcamelyon": "PatchCamelyon",
           "nct-crc-he-100k": "NCT-CRC-HE-100K", "pannuke": "PanNuke", "monuseg": "MoNuSeg", "glas": "GlaS",
           "the cancer genome atlas (tcga)": "TCGA (unspecified)", "cptac luad": "CPTAC-LUAD", "pathmnist": "PathMNIST",
           "clinical proteomic tumor analysis consortium (cptac)": "CPTAC"}


def label(n):
    n = str(n)
    if n.lower() in DISPLAY:
        return DISPLAY[n.lower()]
    if n.lower().startswith(("tcga-", "cptac-")):
        return n.upper()
    return n[:28]


def save(fig, out, name):
    for ext in ("pdf", "png"):
        fig.savefig(out / f"{name}.{ext}", bbox_inches="tight")
    plt.close(fig)


ALIASES = Path(__file__).resolve().parent / "dataset_aliases.json"
PERIODS = {"<=2018": (0, 2018), "2019-2021": (2019, 2021), "2022-2023": (2022, 2023), "2024-2025": (2024, 2025)}


def name_key(s):
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


def resource_key(value):
    """A catalog resource URL; preserve deposit IDs, versions, paths and query parameters."""
    if not isinstance(value, str) or not value.strip():
        return ""
    url = urlsplit(value.strip())
    host = (url.hostname or "").lower().removeprefix("www.")
    path = unquote(url.path).rstrip("/")
    # A Zenodo record URL and its DOI identify the same deposit.
    zenodo = re.fullmatch(r"/(?:record|records)/(\d+)", path) if host == "zenodo.org" else None
    doi = re.fullmatch(r"/10\.5281/zenodo\.(\d+)", path, flags=re.I) if host == "doi.org" else None
    if zenodo or doi:
        return "zenodo:" + (zenodo or doi).group(1)
    if not host:
        return ""
    return host + path + ("?" + url.query if url.query else "")


def build_canon(ds):
    """Use documented aliases and matching names/resource URLs; names alone are insufficient.
    The representative of a name group is the member with a parent, else the shortest slug."""
    cfg = json.loads(ALIASES.read_text(encoding="utf-8"))
    hand = {k: v for k, v in cfg["merge"].items() if k in ds.index and v in ds.index}
    # versions of a dataset that share a name with it (MIDOG++ named "midog") stay their own dataset
    separate = [[s for s in g if s in ds.index] for g in cfg.get("keep_separate", [])]
    held = {s for g in separate for s in g}
    groups = defaultdict(list)
    for s, n in ds["dataset_name"].items():
        if s not in held:
            resource = resource_key(ds.at[s, "home_page_url"])
            # Missing names/URLs cannot provide evidence of identity. DOI slugs remain separate.
            identity = resource if resource and name_key(n) and not s.startswith("10.") else "slug:" + s
            groups[(name_key(n), identity)].append(s)
    has_parent = ds["parent_slug"].map(lambda p: isinstance(p, str) and p in ds.index)
    rep = {}
    for members in [*groups.values(), *[g for g in separate if g]]:
        r = sorted(members, key=lambda s: (not has_parent[s], len(s), s))[0]
        for s in members:
            rep[s] = r

    def canon(s):
        for _ in range(10):
            t = rep.get(hand.get(s, s), hand.get(s, s))
            if t == s:
                return s
            s = t
        return s
    return canon, hand, cfg["root_patterns"]


def paper_aliases(run_dir):
    data = read_data('paper_routes', run_dir)
    return data['aliases'], {k:set(v) for k,v in data['routes'].items()}


def term_or_citation(routes):
    return bool(routes & {"search", "cites"})


def wilson_d(k, n):
    p, lo, hi = wilson(k, n)
    return {"k": int(k), "n": int(n), "p": p, "lo": lo, "hi": hi}





def run(run_dir=DATA, out_dir=Path('results')):
    from types import SimpleNamespace
    a = SimpleNamespace(run_dir=Path(run_dir), out_dir=Path(out_dir))
    figs = a.out_dir / "figures"
    figs.mkdir(parents=True, exist_ok=True)
    style()
    rng = np.random.default_rng(0)
    N = {}

    ds, pp, mt = load_run(a.run_dir)
    ds = ds.drop_duplicates("slug").set_index("slug", drop=False)
    pp = pp.drop_duplicates("url").set_index("url", drop=False)
    pp["year_i"] = pd.to_numeric(pp["year"], errors="coerce")
    slugs = set(ds.index)

    # ---- post-hoc merge of catalog entries that are the same dataset
    canon, hand, root_patterns = build_canon(ds)
    ds["canon"] = [canon(s) for s in ds.index]
    reps = sorted(set(ds["canon"]))
    dsm = ds.loc[reps].copy()  # one row per merged dataset, its fields resolved across the members below
    conflicts = Counter()
    conflicted = set()  # merged groups whose records disagree on year, tissue or image unit
    groups = ds.groupby("canon")
    review = []
    for r, g in groups:
        if len(g) < 2:
            continue
        yrs = pd.to_numeric(g["year"], errors="coerce").dropna()
        if yrs.nunique() > 1:
            conflicts["year"] += 1
            conflicted.add(r)
        if len({tuple(sorted(split_multi(v))) for v in g["tissue_site"] if split_multi(v)}) > 1:
            conflicted.add(r)
        if len({str(v).lower() for v in g["wsi_or_patch"] if isinstance(v, str)}) > 1:
            conflicted.add(r)
        if len(yrs):
            dsm.at[r, "year"] = yrs.min()  # release year = the earliest any member states
        for col in ["tissue_site", "species", "staining_type", "modality", "domain"]:
            toks = sorted({t for v in g[col] for t in split_multi(v)})
            if len({tuple(sorted(split_multi(v))) for v in g[col] if split_multi(v)}) > 1:
                conflicts[col] += 1
            if toks:
                dsm.at[r, col] = "|".join(toks)  # union of what the members say
        units = {str(v).lower() for v in g["wsi_or_patch"] if isinstance(v, str)}
        if len(units) > 1:
            conflicts["wsi_or_patch"] += 1
            dsm.at[r, "wsi_or_patch"] = "Both" if {"wsi", "patch"} & units else dsm.at[r, "wsi_or_patch"]
        for s, row in g.iterrows():
            review.append({"group": r, "slug": s, "dataset_name": row["dataset_name"], "year": row["year"],
                           "wsi_or_patch": row["wsi_or_patch"], "tissue_site": row["tissue_site"],
                           "home_page_url": row["home_page_url"], "merged_by": "documented alias" if s in hand else "name and resource URL",
                           "decision (alias / version / subset / different)": "operational alias; metadata not adjudicated"})
    review_path = a.out_dir / "merge_groups_for_review.csv"
    pd.DataFrame(review).to_csv(review_path, index=False, encoding="utf-8")
    N["catalog_n"] = len(ds)
    N["catalog_merge"] = {"entries": len(ds), "merged_datasets": len(dsm), "entries_merged_away": len(ds) - len(dsm),
                          "hand_merges": len(hand), "merged_groups": int(groups.size().gt(1).sum()),
                          "groups_with_conflicting_field": dict(conflicts)}
    parent = {}
    for s, p in ds["parent_slug"].items():
        if isinstance(p, str) and p in slugs and canon(p) != canon(s):
            parent.setdefault(canon(s), canon(p))
    name = dsm["dataset_name"].to_dict()
    pat = {r: re.compile(rx) for r, rx in root_patterns.items() if r in slugs}

    def root(s):
        seen = set()
        while parent.get(s) and s not in seen:
            seen.add(s)
            s = parent[s]
        if s not in pat:
            for r, rx in pat.items():
                if rx.search(str(name.get(s, s)).lower()):
                    return canon(r)
        return s

    # ---- paper -> catalog dataset links, as push maps them (paper aliases, slug forms)
    alias, source = paper_aliases(a.run_dir)

    def paper_url(pid):
        return canonicalize_url(alias.get(pid, pid)) or alias.get(pid, pid)
    links = defaultdict(set)
    links_raw = defaultdict(set)  # the same links on the run's own catalog records, before merging
    n_rows, no_slug, no_paper = 0, 0, 0
    for m in read_data("links", a.run_dir):
        if not m.get("is_match"):
            continue
        n_rows += 1
        found = _slug_forms(m.get("dataset_name", "")) & slugs
        url = paper_url(m.get("paper_id"))
        if not found:
            no_slug += 1
        elif url not in pp.index:
            no_paper += 1
        else:
            links[url].add(canon(sorted(found)[0]))
            links_raw[url].add(sorted(found)[0])
    pairs = [(u, s) for u, ss in links.items() for s in ss]
    N["links"] = {"match_rows": n_rows, "rows_no_catalog_slug": no_slug, "rows_paper_not_in_hub": no_paper,
                  "paper_dataset_pairs": len(pairs), "papers_linked": len(links), "papers_total": len(pp),
                  "frac_papers_linked": len(links) / len(pp),
                  "links_per_linked_paper_median": float(np.median([len(v) for v in links.values()]))}

    use = Counter(s for _, s in pairs)
    use_root = Counter(r for u, ss in links.items() for r in {root(s) for s in ss})
    counts = np.array([use.get(s, 0) for s in reps])
    roots = sorted({root(s) for s in reps})
    rcounts = np.array([use_root.get(r, 0) for r in roots])

    def conc(c):
        c_sorted = np.sort(c)[::-1]
        tot = c_sorted.sum()
        return {"n": int(len(c)), "used": int((c > 0).sum()), "unused": int((c == 0).sum()),
                "used_once": int((c == 1).sum()), "gini": gini(c), "top10_share": float(c_sorted[:10].sum() / tot),
                "datasets_for_half_of_uses": int(np.searchsorted(np.cumsum(c_sorted), tot / 2) + 1),
                "used_ge10": int((c >= 10).sum()), "used_ge100": int((c >= 100).sum())}
    N["usage_entry"] = conc(counts)
    N["usage_root"] = conc(rcounts)
    N["top_datasets"] = [{"slug": s, "name": name.get(s, s), "papers": c} for s, c in use.most_common(25)]
    N["top_roots"] = [{"slug": s, "name": name.get(s, s), "papers": c} for s, c in use_root.most_common(15)]
    tcga = canon("tcga") if "tcga" in slugs else None
    N["tcga_papers"] = use_root.get(tcga, 0)
    N["tcga_frac_of_linked_papers"] = use_root.get(tcga, 0) / len(links)
    N["tcga_frac_of_all_papers"] = use_root.get(tcga, 0) / len(pp)

    # concentration by discovery route: the dataset-name search (1.6) finds papers by dataset name,
    # so it can only add uses of datasets already known; recompute without its papers
    by_route = {}
    routes_of = lambda u: source.get(u, set())
    for label_, keep in {"term_and_citation": term_or_citation,
                         "dataset_search_only": lambda r: bool(r) and r <= {"dataset_search"}}.items():
        sub = [s for u, s in pairs if keep(routes_of(u))]
        cu = Counter(sub)
        c = np.array([cu.get(s, 0) for s in reps])
        papers_ = {u for u, _ in pairs if keep(routes_of(u))}
        tc = sum(1 for u in papers_ if tcga in {root(s) for s in links[u]})
        by_route[label_] = {**conc(c), "pairs": len(sub), "papers_linked": len(papers_),
                            "papers_in_route": int(sum(keep(routes_of(u)) for u in pp.index)),
                            "tcga_frac_of_linked": tc / max(1, len(papers_))}
    N["papers_without_route"] = int(sum(not routes_of(u) for u in pp.index))

    # sensitivity: without the post-hoc merge (the run's 1,770 records as they are), and
    # restricted to papers read in full text
    use_raw = Counter(s for ss in links_raw.values() for s in ss)
    N["usage_unmerged"] = conc(np.array([use_raw.get(s, 0) for s in ds.index]))
    full_urls = set(pp.index[pp["text_source"].eq("full_text")])
    use_full = Counter(s for u, ss in links.items() if u in full_urls for s in ss)
    N["usage_full_text_only"] = {**conc(np.array([use_full.get(s, 0) for s in reps])),
                                 "papers_linked": int(sum(u in full_urls for u in links)),
                                 "tcga_frac_of_linked": sum(1 for u in links if u in full_urls and tcga in {root(s) for s in links[u]})
                                 / max(1, sum(u in full_urls for u in links))}
    N["usage_by_route"] = by_route

    # term search only: no citation snowballing from highly cited seeds, no dataset-name search
    term_only = {u for u in pp.index if routes_of(u) == {"search"}}
    use_term = Counter(s for u, ss in links.items() if u in term_only for s in ss)
    N["usage_term_search_only"] = {**conc(np.array([use_term.get(s, 0) for s in reps])),
                                   "papers": len(term_only), "papers_linked": int(sum(u in term_only for u in links))}

    # how link and text coverage change by year: missed links would bias trends if they moved
    yr = pp["year_i"]
    N["coverage_by_year"] = {int(y): {"papers": int((yr == y).sum()),
                                      "full_text": float(pp.loc[yr == y, "text_source"].eq("full_text").mean()),
                                      "linked": float(np.mean([u in links for u in pp.index[yr == y]]))}
                             for y in range(2016, LAST_FULL_YEAR + 1)}

    # stress test: stored-record link recall was 22/40 on labeled papers. Suppose every missed link
    # went to the least-used datasets (the case most against concentration) and recompute.
    recall = 22 / 40
    missing = int(round(len(pairs) * (1 / recall - 1)))
    c_obs = np.array([use.get(s, 0) for s in reps], dtype=float)
    order = np.argsort(c_obs, kind="stable")  # least-used first
    c_tail = c_obs.copy()
    k = 0
    while k < missing:  # level up the tail one link at a time, lowest first
        level = c_tail[order].min()
        idx = order[c_tail[order] == level]
        take = min(len(idx), missing - k)
        c_tail[idx[:take]] += 1
        k += take
    c_prop = c_obs * (1 / recall)  # missed links in proportion to observed use
    N["missed_link_stress"] = {"assumed_recall": recall, "added_links": missing,
                               "tail_case": conc(c_tail), "proportional_case": conc(c_prop)}

    # WSI-level datasets only (the catalog also holds patch, cytology and spatial transcriptomics sets)
    wsi = dsm["wsi_or_patch"].fillna("").str.lower().isin(["wsi", "both"])
    N["wsi_subset"] = {"datasets": int(wsi.sum()), "patch": int(dsm["wsi_or_patch"].fillna("").str.lower().eq("patch").sum()),
                       "unknown": int(dsm["wsi_or_patch"].isna().sum()),
                       "usage": conc(np.array([use.get(s, 0) for s in dsm.index[wsi]]))}

    # ---- catalog composition (merged datasets)
    def dist(series, top_k=12):
        return Counter(t.lower() for v in series for t in split_multi(v)).most_common(top_k)
    N["catalog_composition"] = {col: dist(dsm[col]) for col in
                                ["tissue_site", "species", "staining_type", "modality", "domain", "task", "dataset_access",
                                 "license", "wsi_or_patch", "annotation_level", "image_format"]}
    dyear = pd.to_numeric(dsm["year"], errors="coerce")
    N["catalog_year"] = {"known": int(dyear.notna().sum()), "median": float(dyear.median()),
                         "since_2020": int((dyear >= 2020).sum())}
    N["catalog_license_known"] = float(dsm["license"].map(is_known).mean())
    N["catalog_download_url"] = float(dsm["download_url"].notna().mean())
    N["catalog_scanner_known"] = float(dsm["scanner"].map(is_known).mean())
    N["catalog_mpp_known"] = float(pd.to_numeric(dsm["mpp"], errors="coerce").notna().mean())
    nsl = pd.to_numeric(dsm["n_slides"], errors="coerce")
    N["catalog_n_slides"] = {"known": int(nsl.notna().sum()), "frac_known": float(nsl.notna().mean()), "median": float(nsl.median()),
                             "q1": float(nsl.quantile(.25)), "q3": float(nsl.quantile(.75)), "ge_1000": int((nsl >= 1000).sum())}
    ncent = pd.to_numeric(dsm["n_centers"], errors="coerce")
    N["catalog_n_centers"] = {"known": int(ncent.notna().sum()), "single": int((ncent == 1).sum()), "multi": int((ncent > 1).sum())}

    # ---- the paper corpus
    years = pp["year_i"]
    N["papers_n"] = len(pp)
    N["papers_year"] = {int(k): int(v) for k, v in years.value_counts().sort_index().items() if pd.notna(k)}
    N["papers_text_source"] = dict(Counter(pp["text_source"].fillna("unknown")))
    N["papers_year_quality"] = {"missing": int(years.isna().sum()), "before_1990": int((years < 1990).sum()),
                                "after_2026": int((years > 2026).sum())}

    # catalog vs paper-topic composition by tissue site: human histopathology on both sides
    def share(series):
        c = Counter(t.lower() for v in series for t in split_multi(v) if t.lower() not in {"unknown", "other", "na"})
        tot = sum(c.values())
        return {k: v / tot for k, v in c.items()}, c
    is_hist = lambda col: col.fillna("").str.lower().str.contains("histopathology")
    is_human = lambda col: col.fillna("").str.lower().str.contains("human")
    human_ds = is_human(dsm["species"]) & is_hist(dsm["domain"])
    human_pp = is_human(pp["species"]) & is_hist(pp["domain"])

    def composition(ds_mask):
        s_ds, c_ds = share(dsm.loc[ds_mask, "tissue_site"])
        s_pp, c_pp = share(pp.loc[human_pp, "tissue_site"])
        sites = [k for k, _ in Counter({**c_ds, **c_pp}).most_common() if c_ds.get(k, 0) + c_pp.get(k, 0) >= 40][:18]
        return {"datasets_n": int(ds_mask.sum()), "papers_n": int(human_pp.sum()),
                "sites": {k: {"datasets": c_ds.get(k, 0), "papers": c_pp.get(k, 0),
                              "ds_share": s_ds.get(k, 0), "paper_share": s_pp.get(k, 0)} for k in sites}}
    N["composition_tissue"] = composition(human_ds)
    # sensitivity: leave out merged groups whose records disagree (their tissue is a union)
    N["composition_tissue_no_conflicts"] = composition(human_ds & ~dsm.index.isin(list(conflicted)))

    # ---- reporting practice in full-text papers, by period (extractor output)
    yes = lambda v: v is True or str(v).strip().lower() in {"true", "yes"}
    numeric = lambda v: pd.notna(pd.to_numeric(v, errors="coerce"))
    flags = {
        "external_validation": pp["external_validation"].map(yes),
        "uses_private_data": pp["uses_private_data"].map(yes),
        "reports_n_slides": pp["n_slides"].map(numeric),
        "reports_n_patients": pp["n_patients"].map(numeric),
        "reports_n_centers": pp["n_centers"].map(numeric),
        "reports_scanner": pp["scanner"].map(is_known),
        "reports_mpp": pp["mpp"].map(numeric),
        "reports_split_level": pp["split_level"].map(is_known),
        "patient_level_split": pp["split_level"].map(lambda v: str(v).lower() in {"patient", "case"}),
        "reports_n_annotators": pp["n_annotators"].map(lambda v: (pd.to_numeric(v, errors="coerce") or 0) > 0),
        "foundation_model": pp["foundation_model"].map(lambda v: bool(split_multi(v))),
    }
    full = pp["text_source"].eq("full_text")
    R = {}
    for k, s in flags.items():
        s = s.reindex(pp.index).fillna(False).astype(bool)
        row = {"all": float(s.mean()), "full_text": float(s[full].mean())}
        for pn, (lo, hi) in PERIODS.items():
            m = years.between(lo, hi) & full
            row[pn] = wilson_d(int(s[m].sum()), int(m.sum()))
        R[k] = row
    N["reporting"] = R
    # sensitivity: full-text histopathology papers that work on whole slides, where scanner,
    # resolution and centres apply most directly (patch-only and cytology papers often lack them)
    wsi_hist = full & is_hist(pp["domain"]) & pp["wsi_or_patch"].fillna("").str.lower().isin(["wsi", "both"])
    R2 = {}
    for k in ["reports_scanner", "reports_mpp", "reports_n_centers", "reports_split_level", "external_validation"]:
        s = flags[k].reindex(pp.index).fillna(False).astype(bool)
        R2[k] = {pn: wilson_d(int(s[years.between(lo, hi) & wsi_hist].sum()), int((years.between(lo, hi) & wsi_hist).sum()))
                 for pn, (lo, hi) in PERIODS.items()}
    N["reporting_wsi_histopathology"] = R2
    N["full_text_papers"] = int(full.sum())
    N["full_text_share_by_period"] = {pn: float(full[years.between(lo, hi)].mean()) for pn, (lo, hi) in PERIODS.items()}

    # ---- metric rows (paper aliases and dataset placement as push does)
    mt = mt.copy()
    mt["url"] = mt["paper_id"].map(paper_url)
    N["metrics_rows_raw"] = len(mt)
    mt = mt[mt["url"].isin(pp.index)]
    raw_links = defaultdict(set)  # resolve_metric_dataset needs the unmerged slugs it can name-match
    for m in read_data("links", a.run_dir):
        found = _slug_forms(m.get("dataset_name", "")) & slugs
        if m.get("is_match") and found:
            raw_links[paper_url(m.get("paper_id"))].add(sorted(found)[0])
    mt["slug"] = [resolve_metric_dataset(r or "", sorted(raw_links.get(u, []))) for r, u in zip(mt["dataset"].fillna(""), mt["url"])]
    mt["slug"] = mt["slug"].map(lambda s: canon(s) if isinstance(s, str) else None)
    mt["year_i"] = mt["url"].map(pp["year_i"])
    nonempty = lambda col: mt[col].fillna("").astype(str).str.strip().ne("")
    N["metrics"] = {"rows": len(mt), "papers": int(mt["url"].nunique()), "on_catalog_dataset": float(mt["slug"].notna().mean()),
                    "task_type": dict(Counter(mt["task_type"])),
                    "split": dict(Counter(mt["split"].fillna("").str.lower()).most_common(8)),
                    "has_uncertainty": float(nonempty("uncertainty").mean()), "has_n": float(nonempty("n").mean()),
                    "has_evidence": float(nonempty("evidence").mean()), "primary": float(mt["is_primary"].fillna(False).astype(bool).mean())}
    prim = mt[mt["is_primary"].fillna(False).astype(bool)]
    unc = prim.assign(u=nonempty("uncertainty").reindex(prim.index)).groupby("url")["u"].any()
    uy = unc.index.map(pp["year_i"])
    N["uncertainty_by_period"] = {pn: wilson_d(int(unc[(uy >= lo) & (uy <= hi)].sum()), int(((uy >= lo) & (uy <= hi)).sum()))
                                  for pn, (lo, hi) in PERIODS.items()}
    N["uncertainty_papers_primary"] = {"papers": int(len(unc)), "any": float(unc.mean())}

    # ---- reported AUC on the same dataset
    def auc_of(d):
        return num((d or {}).get("auc_roc"))
    cls = mt[mt["task_type"].eq("classification")].copy()
    cls["auc"] = cls["metrics"].map(auc_of)
    N["auc_rows_outside_range"] = {"below_0.3": int((cls["auc"] < 0.3).sum()), "above_1": int((cls["auc"] > 1).sum()),
                                   "in_range": int(cls["auc"].between(0.3, 1.0).sum())}
    cls = cls[cls["auc"].between(0.3, 1.0)]
    cls["split_l"] = cls["split"].fillna("").str.lower()
    cls["primary"] = cls["is_primary"].fillna(False).astype(bool)
    oncat = cls[cls["slug"].notna() & cls["split_l"].isin(["test", "external_test"])]
    # main: the paper's own primary result rows on that dataset (median if several)
    main_ = oncat[oncat["primary"]].groupby(["slug", "url"]).agg(auc=("auc", "median"), year=("year_i", "first")).reset_index()
    # sensitivity: the best test row of any kind (what a leaderboard would show)
    best = oncat.groupby(["slug", "url"]).agg(auc=("auc", "max"), year=("year_i", "first")).reset_index()

    def bench_of(t):
        b = t.groupby("slug").agg(papers=("url", "nunique"), median=("auc", "median"),
                                  q1=("auc", lambda s: s.quantile(.25)), q3=("auc", lambda s: s.quantile(.75)),
                                  ge95=("auc", lambda s: (s >= .95).mean()))
        return b[b["papers"] >= 15].sort_values("papers", ascending=False)
    bench = bench_of(main_)
    bench_best = bench_of(best)
    rowify = lambda b: [{"slug": s, "name": name.get(s, s), **{k: float(v) for k, v in r.items()}} for s, r in b.iterrows()]
    N["auc_primary"] = {"pairs": len(main_), "median": float(main_["auc"].median()), "ge95": float((main_["auc"] >= .95).mean()),
                        "ge99": float((main_["auc"] >= .99).mean()), "benchmarks": rowify(bench)}
    N["auc_best"] = {"pairs": len(best), "median": float(best["auc"].median()), "ge95": float((best["auc"] >= .95).mean()),
                     "ge99": float((best["auc"] >= .99).mean()), "benchmarks": rowify(bench_best)}

    def summ(t):
        return {"pairs": len(t), "median": float(t["auc"].median()), "ge95": float((t["auc"] >= .95).mean())}
    keys_ = main_.set_index(["slug", "url"]).index
    best_same = best.set_index(["slug", "url"]).loc[keys_].reset_index()  # best row, on the primary analysis's pairs
    full_urls_ = set(pp.index[pp["text_source"].eq("full_text")])
    allc = mt[mt["task_type"].eq("classification")].copy()
    allc["auc"] = allc["metrics"].map(auc_of)
    any01 = allc[allc["slug"].notna() & allc["auc"].between(0, 1) & allc["is_primary"].fillna(False).astype(bool)
                 & allc["split"].fillna("").str.lower().isin(["test", "external_test"])]
    any01 = any01.groupby(["slug", "url"])["auc"].median().reset_index()
    N["auc_sensitivity"] = {
        "best_on_primary_pairs": summ(best_same),
        "primary_full_text_only": summ(main_[main_["url"].isin(full_urls_)]),
        "primary_test_split_only": summ(oncat[oncat["primary"] & oncat["split_l"].eq("test")].groupby(["slug", "url"])["auc"].median().reset_index()),
        "primary_slide_or_patient_level": summ(oncat[oncat["primary"] & oncat["level"].fillna("").str.lower().isin(["slide", "patient"])]
                                               .groupby(["slug", "url"])["auc"].median().reset_index()),
        "primary_all_auc_0_to_1": summ(any01)}

    # ---- external vs internal within a paper: rows sharing method variant, evaluation level and subset.
    # The prediction target is not a field, so this is not a matched comparison. The strict version keeps
    # groups with a stated level and at most two rows per side, so one group can't pool many endpoints.
    cls["method_l"] = cls["method"].fillna("").str.strip().str.lower()
    cls["level_l"] = cls["level"].fillna("").str.lower()
    cls["subset_l"] = cls["subset"].fillna("").str.strip().str.lower()
    cls["kind"] = cls["split_l"].map(lambda s: "external" if "external" in s else ("internal" if s in {"test", "cross_validation"} else None))
    cm = cls[cls["kind"].notna() & cls["method_l"].ne("")]
    keys = ["url", "method_l", "level_l", "subset_l"]
    agg = cm.groupby(keys + ["kind"])["auc"].agg(["mean", "size"]).unstack("kind").dropna()

    strict = agg[(agg.index.get_level_values("level_l") != "") & (agg[("size", "internal")] <= 2) & (agg[("size", "external")] <= 2)]
    N["external_result_groups"] = {"groups": int(len(strict)), "papers": int(strict.index.get_level_values("url").nunique())}

    # ---- age of datasets at use (link-weighted and paper-weighted), by paper year
    rel = dyear.to_dict()
    # sensitivity: the latest year any member states, instead of the earliest
    rel_latest = dict(rel)
    for r, g in groups:
        yrs = pd.to_numeric(g["year"], errors="coerce").dropna()
        if len(g) > 1 and len(yrs):
            rel_latest[r] = yrs.max()

    def age_series(rel_):
        ages, neg = defaultdict(list), 0
        per_paper_age = defaultdict(list)
        for u, s in pairs:
            y, r = pp.at[u, "year_i"], rel_.get(s)
            if pd.isna(y) or pd.isna(r) or not 2016 <= y <= LAST_FULL_YEAR:
                continue
            if y < r:
                neg += 1
                continue
            ages[int(y)].append((int(y) - int(r), root(s) == tcga))
            per_paper_age[(int(y), u)].append((int(y) - int(r), root(s) == tcga))
        out = {}
        for y in sorted(ages):
            allv = [a_ for a_, _ in ages[y]]
            ex = [a_ for a_, t in ages[y] if not t]
            pw = [np.mean([a_ for a_, _ in v]) for (yy, _), v in per_paper_age.items() if yy == y]
            pw_ex = [np.mean(e) for (yy, _), v in per_paper_age.items() if yy == y for e in [[a_ for a_, t in v if not t]] if e]
            out[y] = {"links": len(allv), "mean": float(np.mean(allv)), "median": float(np.median(allv)),
                      "excl_tcga_links": len(ex), "excl_tcga_mean": float(np.mean(ex)), "excl_tcga_median": float(np.median(ex)),
                      "paper_weighted_median": float(np.median(pw)), "paper_weighted_median_excl_tcga": float(np.median(pw_ex))}
        return out, neg
    N["age_at_use"], N["age_at_use_negative_dropped"] = age_series(rel)
    # sensitivity: leave out merged groups whose records disagree
    N["age_at_use_no_conflicts"], _ = age_series({s: (None if s in conflicted else y) for s, y in rel.items()})
    N["age_at_use_latest_year"], _ = age_series(rel_latest)
    N["links_with_release_year"] = float(np.mean([pd.notna(rel.get(s)) for _, s in pairs]))

    # ---- uptake in a fixed window: linked papers in the release year and the two after it, from the
    # term and citation searches only (the dataset-name search looks for uses directly), also per
    # 1,000 such papers published in the window, since the literature grew ~7-fold over the period
    yr_of = pp["year_i"].to_dict()
    tc_year = Counter(int(yr_of[u]) for u in pp.index if term_or_citation(routes_of(u)) and pd.notna(yr_of[u]))
    papers_by_ds = defaultdict(list)
    for u, s in pairs:
        if term_or_citation(routes_of(u)):
            papers_by_ds[s].append(yr_of.get(u))
    cohorts = {"2010-2012": (2010, 2012), "2013-2015": (2013, 2015), "2016-2018": (2016, 2018),
               "2019-2021": (2019, 2021), "2022-2023": (2022, LAST_FULL_YEAR - 2)}
    N["uptake_3yr_window"] = {}
    for label_, (c0, c1) in cohorts.items():
        n_, rate = [], []
        for s in reps:
            r = rel.get(s)
            if pd.notna(r) and c0 <= r <= c1:
                k = sum(1 for y in papers_by_ds.get(s, []) if pd.notna(y) and r <= y <= r + 2)
                base = sum(tc_year.get(int(r) + i, 0) for i in range(3))
                n_.append(k)
                rate.append(1000 * k / base if base else 0)
        if n_:
            N["uptake_3yr_window"][label_] = {"datasets": len(n_), "zero": float(np.mean(np.array(n_) == 0)),
                                              "ge5": float(np.mean(np.array(n_) >= 5)), "mean": float(np.mean(n_)),
                                              "mean_per_1000_papers": float(np.mean(rate))}

    # ---- Table 4: the most-used datasets, with what the catalog says about them
    auc_by = main_.groupby("slug")["auc"].agg(["size", "median"])
    def rng_str(vals):  # one value, or the range the merged entries state
        vals = sorted({int(v) for v in vals})
        if not vals:
            return None
        return f"\\num{{{vals[0]}}}" if len(vals) == 1 else f"\\num{{{vals[0]}}}--\\num{{{vals[-1]}}}"

    def lic_str(v):
        return v.upper().replace("CC-BY", "CC BY") if v.lower().startswith("cc") else v.capitalize()
    rows = []
    for s, c in use.most_common(15):
        g = ds[ds["canon"] == s]
        lics = sorted({lic_str(t) for v in g["license"] for t in split_multi(v) if is_known(t)})
        rows.append({"slug": s, "name": label(name.get(s, s)), "papers": c,
                     "year": rng_str(pd.to_numeric(g["year"], errors="coerce").dropna()),
                     "unit": str(dsm.at[s, "wsi_or_patch"]) if isinstance(dsm.at[s, "wsi_or_patch"], str) else None,
                     "license": " / ".join(lics) or None,
                     "n_slides": rng_str(pd.to_numeric(g["n_slides"], errors="coerce").dropna()),
                     "auc_papers": int(auc_by.at[s, "size"]) if s in auc_by.index else 0,
                     "auc_median": float(auc_by.at[s, "median"]) if s in auc_by.index else None})
    N["top_datasets_table"] = rows
    cell = lambda v: "--" if v is None else str(v)
    lines = [" & ".join([cell(r["name"]), f"\\num{{{r['papers']}}}", cell(r["year"]).replace("\\num", ""), cell(r["unit"]),
                         cell(r["n_slides"]), cell(r["license"]),
                         (f"{r['auc_median']:.2f} ({r['auc_papers']})" if r["auc_papers"] >= 5 else "--")]) + " \\\\"
             for r in rows]
    (a.out_dir / "tables").mkdir(exist_ok=True)
    (a.out_dir / "tables" / "table_4_top_datasets.tex").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # ---- figures
    fig_concentration_aging(counts, N["age_at_use"], N["uptake_3yr_window"], figs)
    fig_supply_demand(N["composition_tissue"]["sites"], figs)
    fig_reporting(R, figs)
    fig_auc(main_, bench, name, figs)

    # ---- methods over time, full-text papers 2016-2025.
    # Extractor output, not labeled: shares are of papers whose record fills the field.
    ft = pp[pp["text_source"].eq("full_text") & years.between(2016, LAST_FULL_YEAR)]
    first = lambda v: (split_multi(v) or [""])[0].lower()
    fam_map = {"cnn": "CNN", "mil": "MIL", "transformer": "Transformer"}
    fam = ft["model_type"].map(first).map(lambda t: fam_map.get(t, "Other") if t else None)
    pre_map = {"imagenet": "ImageNet", "pathology-ssl": "Pathology SSL", "foundation-model": "Foundation model",
               "other": "Other"}
    pre = ft["pretraining_strategy"].map(first).map(lambda t: pre_map.get(t))
    tasks_ = ft["task"].fillna("").str.lower()
    trends = {}

    def shares(series, cats):
        out, ns = {}, {}
        for y in range(2016, LAST_FULL_YEAR + 1):
            s = series[ft["year_i"] == y].dropna()
            ns[y] = int(len(s))
            out[y] = {c: float((s == c).mean()) if len(s) else 0.0 for c in cats}
        return {"share": out, "n": ns}
    trends["model_family"] = shares(fam, ["CNN", "MIL", "Transformer", "Other"])
    trends["pretraining"] = shares(pre, ["ImageNet", "Pathology SSL", "Foundation model", "Other"])
    task_share, task_n = {}, {}
    for y in range(2016, LAST_FULL_YEAR + 1):
        t = tasks_[ft["year_i"] == y]
        task_n[y] = int(len(t))
        task_share[y] = {"Classification": float(t.str.contains("classification").mean()),
                         "Segmentation": float(t.str.contains("segmentation").mean()),
                         "Survival": float(t.str.contains("survival").mean()),
                         "Detection or regression": float(t.str.contains("detection|regression").mean())}
    trends["task"] = {"share": task_share, "n": task_n, "denominator": "all full-text papers"}
    trends["model_family_known"] = float(fam.notna().mean())
    trends["pretraining_known"] = float(pre.notna().mean())
    N["methods_trends"] = trends
    fig_methods(trends, figs)

    return N



def fig_concentration_aging(counts, age, uptake, figs):
    fig, ax = plt.subplots(2, 2, figsize=(6.0, 6.2), gridspec_kw={"wspace": 0.58, "hspace": 0.75})
    a, b, c, d = ax.ravel()

    # A: use per dataset, datasets with no linked paper left off the log axis (the caption counts them)
    used = np.sort(counts[counts > 0])[::-1]
    a.loglog(np.arange(1, len(used) + 1), used, color=SERIES[0])
    half = int(np.searchsorted(np.cumsum(used), used.sum() / 2) + 1)
    a.axvline(half, color=INK2, lw=0.8, ls=(0, (3, 3)))
    a.annotate(f"{half} groups:\nhalf of links", (half, used[half - 1]), xytext=(10, 18),
               textcoords="offset points", fontsize=9, color=INK)
    a.set_xlabel("Dataset rank")
    a.set_ylabel("Linked papers")
    a.set_title("A  Use per dataset")

    # B: age of linked datasets at use
    yrs = sorted(int(y) for y in age)
    get = lambda k: [age[y if y in age else str(y)][k] for y in yrs]
    lines = [("All links (mean)", get("mean")), ("Excl. TCGA (mean)", get("excl_tcga_mean")),
             ("Excl. TCGA, per\npaper (median)", get("paper_weighted_median_excl_tcga"))]
    for (nm, ys), col in zip(lines, SERIES):
        b.plot(yrs, ys, color=col, marker="o", markersize=3.5, label=nm.replace("All links (mean)", "All links mean").replace("Excl. TCGA (mean)", "Non-TCGA mean").replace("Excl. TCGA, per\npaper (median)", "Non-TCGA median"))
    b.set_xticks([2016, 2019, 2022, 2025])
    b.set_xlim(2015.5, 2025.5)
    b.set_ylim(0, 14)
    b.set_xlabel("Publication year")
    b.set_ylabel("Dataset age at use (years)")
    b.set_title("B  Dataset age")
    b.legend(loc="upper left", fontsize=10, labelspacing=0.3)
    ygrid_only(b)

    # C and D: uptake in the release year and the two after it (two panels, one scale each)
    cohorts = list(uptake)
    x = np.arange(len(cohorts))
    ticks = [f"{k[2:4]}-{k[-2:]}" for k in cohorts]
    c.bar(x, [100 * uptake[k]["zero"] for k in cohorts], width=0.62, color=SERIES[0], edgecolor="white", linewidth=1)
    for xi, k in zip(x, cohorts):
        c.text(xi, 100 * uptake[k]["zero"] + 1.5, f"{100 * uptake[k]['zero']:.0f}%", ha="center", fontsize=9, color=INK)
    c.set_xticks(x, ticks, rotation=45, ha="right")
    c.set_ylim(0, 80)
    c.set_ylabel("Datasets without a link (%)")
    c.set_xlabel("Release cohort (20xx)")
    c.set_title("C  No early link")
    ygrid_only(c)
    rate = [uptake[k]["mean_per_1000_papers"] for k in cohorts]
    d.semilogy(x, rate, color=SERIES[1], marker="o")
    for xi, r in zip(x, rate):
        d.annotate(f"{r:.1f}" if r < 10 else f"{r:.0f}", (xi, r), xytext=(0, 7), textcoords="offset points",
                   ha="center", fontsize=9, color=INK)
    d.set_xticks(x, ticks, rotation=45, ha="right")
    d.set_ylim(0.2, 200)
    d.set_ylabel("Uses per 1,000 papers (log)")
    d.set_xlabel("Release cohort (20xx)")
    d.set_title("D  Early use rate")
    ygrid_only(d)
    for axis in ax.ravel():
        axis.tick_params(labelsize=10.5)
        for text in axis.texts:
            text.set_fontsize(10.5)
    save(fig, figs, "fig_concentration_aging")


def fig_supply_demand(sd, figs):
    items = sorted(sd.items(), key=lambda kv: kv[1]["paper_share"])
    fig, ax = plt.subplots(figsize=(5.8, 4.5))
    y = np.arange(len(items))
    h = 0.38
    ax.barh(y + h / 2, [v["paper_share"] * 100 for _, v in items], h, color=SERIES[0], edgecolor="white",
            linewidth=1, label="Papers (topic)")
    ax.barh(y - h / 2, [v["ds_share"] * 100 for _, v in items], h, color=SERIES[1], edgecolor="white",
            linewidth=1, label="Catalog datasets")
    nice = {"multi-organ": "Multi-organ", "lymph-node": "Lymph node", "head-neck": "Head and neck",
            "oral-cavity": "Oral cavity"}
    ax.set_yticks(y, [nice.get(k, k.replace("-", " ").capitalize()) for k, _ in items])
    ax.tick_params(axis="y", length=0)
    ax.set_xlabel("Share of tissue-site mentions (%)")
    ax.legend(loc="lower right")
    xgrid_only(ax)
    fig.tight_layout()
    save(fig, figs, "fig_supply_demand_tissue")


def fig_reporting(R, figs):
    keys = [("reports_split_level", "Split unit\nstated"), ("reports_n_slides", "Slide\ncount"),
            ("reports_n_patients", "Patient\ncount"), ("patient_level_split", "Patient-level\nsplit"),
            ("external_validation", "External\nvalidation"), ("reports_n_centers", "Number of\ncentres"),
            ("reports_scanner", "Scanner"), ("reports_mpp", "Resolution\n(\u00b5m/px)"),
            ("reports_n_annotators", "Number of\nannotators"), ("foundation_model", "Foundation\nmodel")]
    periods = ["<=2018", "2019-2021", "2022-2023", "2024-2025"]
    fig, axes = plt.subplots(2, 1, figsize=(5.8, 5.4), gridspec_kw={"hspace": 0.65})
    w = 0.2
    for ax, panel in zip(axes, [keys[:5], keys[5:]]):
        x = np.arange(len(panel))
        for i, p in enumerate(periods):
            vals = [R[k][p] for k, _ in panel]
            ax.bar(x + (i - 1.5) * w, [100 * v["p"] for v in vals], w,
                   color=ORDINAL[i], edgecolor="white", linewidth=0.8,
                   yerr=[[max(0, 100 * (v["p"] - v["lo"])) for v in vals],
                         [max(0, 100 * (v["hi"] - v["p"])) for v in vals]],
                   error_kw={"elinewidth": 0.7, "ecolor": INK2, "capsize": 0},
                   label=f"{p.replace('<=', '<=')} (n={vals[0]['n']:,})")
        ax.set_xticks(x, [l for _, l in panel], fontsize=9)
        ax.tick_params(axis="x", length=0)
        ax.set_ylim(0, 100)
        ax.set_ylabel("Item captured (%)")
        ygrid_only(ax)
    axes[0].legend(ncol=2, loc="lower center", bbox_to_anchor=(0.5, 1.02), fontsize=9)
    save(fig, figs, "fig_reporting_practice")



def fig_auc(best, bench, name, figs):
    fig, ax = plt.subplots(figsize=(5.8, 0.27 * len(bench) + 0.9))
    order = list(bench.index)
    data = [best.loc[best["slug"] == s, "auc"].values for s in order]
    ax.boxplot(data, orientation="horizontal", widths=0.55, showfliers=True, patch_artist=True,
               flierprops={"marker": "o", "markersize": 2.5, "markerfacecolor": NEUTRAL, "markeredgecolor": "none"},
               boxprops={"facecolor": "#cde2fb", "edgecolor": SERIES[0], "linewidth": 0.9},
               medianprops={"color": "#104281", "linewidth": 1.8},
               whiskerprops={"color": SERIES[0], "linewidth": 0.9}, capprops={"color": SERIES[0], "linewidth": 0.9})
    ax.set_yticks(range(1, len(order) + 1), [f"{label(name.get(s, s))}  ({int(bench.at[s, 'papers'])})" for s in order])
    ax.tick_params(axis="y", length=0)
    ax.invert_yaxis()
    ax.set_xlabel("Primary test-set AUC per paper (papers in brackets)")
    ax.set_xlim(0.3, 1.01)
    xgrid_only(ax)
    fig.tight_layout()
    save(fig, figs, "fig_auc_spread")


def fig_methods(trends, figs):
    fig, ax = plt.subplots(3, 1, figsize=(4.9, 5.5), gridspec_kw={"hspace": 0.55})
    fig.subplots_adjust(left=0.15, right=0.75)
    panels = [("model_family", "A  Model family", ["CNN", "MIL", "Transformer", "Other"]),
              ("pretraining", "B  Pretraining source", ["ImageNet", "Pathology SSL", "Foundation\nmodel", "Other"]),
              ("task", "C  Task", ["Classification", "Segmentation", "Survival", "Detection or regression"])]
    for axis, (key, title, cats) in zip(ax, panels):
        t = trends[key]
        # years with fewer than 50 papers stating the field are left out (too few to show a share)
        yrs = [y for y in sorted(int(y) for y in t["share"]) if t["n"][str(y) if str(y) in t["n"] else y] >= 50]
        ends = []
        for cat, col in zip(cats, SERIES):
            ys = [100 * t["share"][str(y) if str(y) in t["share"] else y][cat.replace("\n", " ")] for y in yrs]
            axis.plot(yrs, ys, color=col, marker="o", markersize=3, label=cat.replace("\n", " "))
            ends.append((cat, yrs[-1], ys[-1]))
        axis.set_xticks([2016, 2019, 2022, 2025])
        axis.set_xlim(2015.5, 2025.5)
        axis.set_ylim(0, 100)
        axis.set_ylabel("Share (%)")
        axis.set_xlabel("Publication year" if key == "task" else "", fontsize=11)
        axis.tick_params(labelsize=10.5)
        axis.set_title(title, fontsize=11.5)
        axis.legend(loc="upper left", bbox_to_anchor=(1.02, 1), fontsize=10.5, handlelength=1.3)
        ygrid_only(axis)
    save(fig, figs, "fig_methods_trends")


