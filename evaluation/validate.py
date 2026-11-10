"""Development and stored-corpus validation used in Tables 2 and 3; no inference calls."""
import ast
import json
import re
from collections import defaultdict
import numpy as np
from pipeline.src.push_to_supabase import _slug_forms, canonicalize_url
from evaluation.common import DATA, VALIDATION, FIELD_MAP, _get, _same, load_gold, report, boot, same_dataset, read_data

TASKS = ('classification','segmentation','detection','regression','survival')

def development_extraction(rng):
    saved = report("smoke_human_labels_2026-10-28_gpt-6-luna.json")
    raw = saved["models"]["gpt-6-luna"]["raw"]
    gold = load_gold()
    per_paper, per_field, per_run = defaultdict(lambda: [0, 0]), defaultdict(lambda: defaultdict(lambda: [0, 0])), defaultdict(lambda: [0, 0])
    # a label that names a correct answer ("match") vs one that only names a wrong answer to avoid
    per_kind = {"match": defaultdict(lambda: [0, 0]), "avoid": defaultdict(lambda: [0, 0])}
    per_field_match = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    for key, rec in raw.items():
        _, rest = key.split(":", 1)
        url, run = rest.rsplit(":", 1)
        out = rec.get("parsed")
        if out is None or url not in gold:
            continue
        for field, (kind, want) in gold[url].items():
            got = _get(out, FIELD_MAP[field])
            hit = (not _same(field, got, want)) if kind == "avoid" else _same(field, got, want)
            accs = [per_paper[url], per_field[field][url], per_run[run], per_kind[kind][url]]
            if kind == "match":
                accs.append(per_field_match[field][url])
            for acc in accs:
                acc[0] += hit
                acc[1] += 1
    return {"all": boot(list(per_paper.values()), rng),
            "positive_labels": boot(list(per_kind["match"].values()), rng),
            "avoid_labels": {"k": sum(v[0] for v in per_kind["avoid"].values()),
                             "n": sum(v[1] for v in per_kind["avoid"].values()), "units": len(per_kind["avoid"])},
            "fields": {f: boot(list(v.values()), rng) for f, v in per_field.items()},
            "fields_positive_labels": {f: boot(list(v.values()), rng) for f, v in per_field_match.items()},
            "per_run": {r: {"k": v[0], "n": v[1], "p": v[0] / v[1]} for r, v in sorted(per_run.items())}}

def development_metrics(rng, report_name="metrics_rescore.json"):
    """Confirmed values recovered in place, and values an earlier version missed that are now found,
    per paper (the report's detail lines start with the paper title's first 40 characters)."""
    gold = VALIDATION["labels"]["metrics"]
    rep = report(report_name)["report"]["per_task"]
    misses, missed = defaultdict(int), defaultdict(lambda: [0, 0])
    numeric, dataset_only = defaultdict(lambda: [0, 0]), defaultdict(lambda: [0, 0])
    for line in rep["detail"]:
        title = line.split(":", 1)[0]
        if line.endswith("no longer in place"):
            misses[title] += 1
        elif ": missing " in line:
            title, detail = line.split(": missing ", 1)
            annotation, outcome = detail.rsplit(": ", 1)
            target = ast.literal_eval(annotation)
            hit = outcome == "found"
            bucket = numeric if target.get("metric") and target.get("value") not in (None, "") else dataset_only
            for acc in [missed[title], bucket[title]]:
                acc[0] += hit
                acc[1] += 1
    units = []
    for g in gold:
        n = len(g.get("confirmed") or [])
        if n:
            units.append((n - misses.get(g["title"][:40], 0), n))
    return {"in_place": boot(units, rng), "missed_now_found": boot(list(missed.values()), rng),
            "missed_numeric_now_found": boot(list(numeric.values()), rng),
            "missed_dataset_now_found": {"k": sum(u[0] for u in dataset_only.values()),
                                         "n": sum(u[1] for u in dataset_only.values()),
                                         "units": len(dataset_only), "interval": None}}

def development_usage(rng):
    rows = report("dataset_usage_rescore.json")["rows"]
    units = [(len(r["you"]) - len(r["uncovered"]), len(r["you"])) for r in rows if r["you"]]
    linkable = [(len(r["you"]) - len(r["uncovered"]), len(r["you"]) - len(r["unreachable"])) for r in rows
                if len(r["you"]) - len(r["unreachable"]) > 0]
    return {"all_uses": boot(units, rng), "linkable_uses": boot(linkable, rng)}

def development_metadata(rng):
    rows = report("metadata_rescore.json")
    units = []
    for r in rows:
        f = [v for v in (r.get("fields") or {}).values() if v.get("findable")]
        if f:
            units.append((sum(bool(v.get("ok")) for v in f), len(f)))
    return boot(units, rng)

def gold_items(name):
    return VALIDATION['labels'][name.removesuffix('_gold')]

def stored_cu(u):
    return canonicalize_url(u) or u

def stored_num(v):
    try:
        f = float(str(v).strip().rstrip("%"))
    except (TypeError, ValueError):
        return None
    return f / 100 if 1 < f <= 100 else f

def stored_gate(run):
    status = {}
    for r in VALIDATION['gate_records']:
        status.setdefault(stored_cu(r['key']),r['status'])
    tp = fn = tn = fp = absent = unsure = 0
    for g in gold_items("relevance_gold"):
        s = status.get(stored_cu(g["key"]))
        if s not in ("kept", "rejected"):
            absent += 1
            continue
        if g["truth"] not in ("yes", "no"):  # "unsure" is neither a positive nor a negative label
            unsure += 1
            continue
        if g["truth"] == "yes":
            tp += s == "kept"
            fn += s == "rejected"
        else:
            tn += s == "rejected"
            fp += s == "kept"
    return {"labeled": len(gold_items("relevance_gold")), "judged_in_run": tp + fn + tn + fp, "not_judged_in_run": absent,
            "unsure_labels_in_run": unsure,
            "recall": {"k": tp, "n": tp + fn}, "specificity": {"k": tn, "n": tn + fp}}

def stored_extraction(papers, rng):
    gold = load_gold()
    per_paper, per_field = defaultdict(lambda: [0, 0]), defaultdict(lambda: defaultdict(lambda: [0, 0]))
    for url, fields in gold.items():
        p = papers.get(stored_cu(url))
        if not p:
            continue
        out = (p.get("extraction") or {}).get("paper_classification_prompt.md") or {}
        for field, (kind, want) in fields.items():
            if kind != "match":
                continue
            hit = _same(field, _get(out, FIELD_MAP[field]), want)
            for acc in (per_paper[url], per_field[field][url]):
                acc[0] += hit
                acc[1] += 1
    return {"positive_labels": boot(list(per_paper.values()), rng),
            "fields": {f: boot(list(v.values()), rng) for f, v in per_field.items()}}

def stored_tasks(run):
    tc = VALIDATION['stored_tasks']
    ok = n = 0
    for g in gold_items("task_gold"):
        got = tc.get(stored_cu(g["key"]))
        if got is None:
            continue
        n += 1
        ok += all(bool(got.get(t)) == bool(g["tasks"].get(t)) for t in TASKS)
    return {"papers": n, "all_five_right": ok}

def stored_metrics(run, rng):
    rows = defaultdict(list)
    for r in read_data("metrics",run):
        rows[stored_cu(r["paper_id"])].append(r)
    by_value, by_context, by_full, by_strict = [], [], [], []
    level_missing = 0
    for g in gold_items("metrics_gold"):
        mine = rows.get(stored_cu(g["key"]))
        conf = g.get("confirmed") or []
        if mine is None or not conf:
            continue
        hv = hc = hf = hs = 0
        for c in conf:
            want = stored_num(c["value"])
            cands = [r for r in mine if want is not None and stored_num((r["metrics"] or {}).get(c["metric"])) is not None
                     and abs(stored_num(r["metrics"][c["metric"]]) - want) < 1e-6]
            hv += bool(cands)
            # partial context: split and subset agree, and the level agrees when both records give one
            part = [r for r in cands if (r.get("split") or "") == (c.get("split") or "")
                    and ((r.get("level") or "") == (c.get("level") or "") or not r.get("level") or not c.get("level"))
                    and (r.get("subset") or "").strip().lower() == (c.get("subset") or "").strip().lower()]
            hc += bool(part)
            level_missing += bool(part) and not any((r.get("level") or "") == (c.get("level") or "") for r in part)
            # full context: also the same task and the same dataset (names compared as the scorer does)
            full = [r for r in part if r.get("task_type") == c.get("task") and same_dataset(r.get("dataset") or "", c.get("dataset") or "")]
            hf += bool(full)
            # strict: the evaluation level must also be stated and equal on both sides
            hs += any(r.get("level") and c.get("level") and r["level"] == c["level"] for r in full)
        by_value.append((hv, len(conf)))
        by_context.append((hc, len(conf)))
        by_full.append((hf, len(conf)))
        by_strict.append((hs, len(conf)))
    return {"papers": len(by_value), "value_found": boot(by_value, rng),
            "value_found_partial_context": boot(by_context, rng),
            "partial_context_matches_missing_a_level": level_missing,
            "value_found_full_context": boot(by_full, rng),
            "value_found_strict_context": boot(by_strict, rng)}

def stored_dataset_use(run, rng):
    """A labeled use is found if the run links the paper to one of the use's catalog slugs (the
    labels list every slug the dataset went by) or to a record with the same name."""
    ds = [{"slug":s,"dataset_name":n} for s,n in VALIDATION["stored_dataset_names"].items()]
    slugs = {d["slug"] for d in ds}
    name_of = {d["slug"]: re.sub(r"[^a-z0-9]+", " ", d["dataset_name"].lower()).strip() for d in ds}
    links = defaultdict(set)
    for m in read_data('links',run):
        found = _slug_forms(m.get("dataset_name", "")) & slugs
        if m.get("is_match") and found:
            links[stored_cu(m["paper_id"])].add(sorted(found)[0])
    papers = {p["url"] for p in read_data("papers",run)}
    units, units_exact = [], []
    for g in gold_items("dataset_usage_gold"):
        u = stored_cu(g["key"])
        if u not in papers:
            continue
        got = links.get(u, set())
        got_names = {name_of[s] for s in got}
        key = lambda n: re.sub(r"[^a-z0-9]+", " ", n.lower()).strip()
        uses = [(set(x.get("member_slugs") or []), key(x["name"])) for x in g.get("used") or []]
        # public datasets the paper used that the labeling catalog did not hold: matched by name and slug forms
        uses += [({s for s in _slug_forms(n) if s in slugs}, key(n)) for n in g.get("public_missing") or []]
        if not uses:
            continue
        exact = [bool(ms & got or nm in got_names) for ms, nm in uses]
        # a use of a collection (TCGA) is implied by a link to one of its projects (TCGA-BRCA), as the scorer counts it
        implied = [e or any(n.startswith(nm + " ") for n in got_names) for e, (_, nm) in zip(exact, uses)]
        units_exact.append((sum(exact), len(uses)))
        units.append((sum(implied), len(uses)))
    # Precision is not reported: the run links several duplicate records of one dataset, so a
    # per-link count would not mean what the rerun's precision means.
    return {"papers": len(units), "recall_with_implied": boot(units, rng), "recall_exact": boot(units_exact, rng)}

def run_development():
    rng=np.random.default_rng(0)
    return {'extraction':development_extraction(rng),'metrics':development_metrics(rng),
        'metrics_pipeline_tasks':development_metrics(rng,'metrics_rescore_pipeline_tasks.json'),
        'dataset_use_recall':development_usage(rng),'metadata_fields':development_metadata(rng)}

def run_stored(root=DATA):
    rng=np.random.default_rng(0)
    return {'gate':stored_gate(root),'extraction':stored_extraction(VALIDATION['stored_papers'],rng),
        'tasks':stored_tasks(root),'metrics':stored_metrics(root,rng),'dataset_use':stored_dataset_use(root,rng)}
