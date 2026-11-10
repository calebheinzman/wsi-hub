{prompt_base}

Extract the performance results the paper reports for its own method, one entry per evaluation the paper reports.

Dataset: {dataset_info}

## Output shape
Return a JSON object `{{"results": [ ... ]}}`. Each entry is one evaluation:

```json
{{
  "dataset": "dataset name as the paper writes it (e.g. TCGA-NSCLC, CAMELYON16, in-house cohort from Hospital X)",
  "split": "test | external_test | validation | cross_validation | train | other",
  "level": "evaluation unit if stated: patch | tile | slide | patient | pixel | object | \"\"",
  "subset": "\"\" for the overall result; otherwise the class or subgroup this row is for",
  "is_primary": true,
  "method": "\"\" for the paper's main model; otherwise the variant or configuration this row is for, as the paper names it",
  "n": "size of the evaluation set as the paper states it, with its unit (e.g. \"129 slides\", \"48 patients\"), or \"\"",
  "uncertainty": "any confidence interval or ± the paper gives for these numbers, as written (e.g. \"AUC 95% CI 0.91-0.96\"), or \"\"",
  "evidence": "where the numbers are: the table or figure and its row or column label (e.g. \"Table 3, row 'Ours (UNI)'\"), or the sentence they come from",
  "metrics": {json_template}
}}
```

## Which entries to write
- One entry per (dataset, split, level) the paper reports a result for. A paper evaluated on two datasets, or on an internal test set and an external test set, gets separate entries — never merge numbers from different datasets or splits into one entry.
- `is_primary: true` on exactly one overall entry (`subset` = "") per dataset: the paper's headline result for that dataset. Prefer, in order: test > external_test > cross_validation > validation. A validation-set number is never primary when a test number for the same dataset exists.
- Per-class results: only when the paper reports a per-class table or list. Write the averaged result as the overall entry (`subset` = "") and each class as its own entry with `subset` set to the class name and `is_primary: false`. At most 10 class entries per dataset.
- Cross-validation: one entry with the mean over folds (the "average" or "mean ± sd" the paper reports), never one entry per fold. Individual fold numbers are not results.
- Report only the paper's own method. Skip baselines, ablations and other papers' numbers.

## Metric values
- Metric keys must be **exactly** the schema keys above — do not add any other keys. Leave a metric `""` if it is not reported for that entry.
- Fill **every** schema metric the paper reports for that entry, not only the headline one: when a results row gives Dice, IoU, precision, recall and accuracy, all five go in the entry.
- Synonyms map to the same field: AUROC = ROC AUC; AUPRC = PR AUC = Average Precision; Dice = Sørensen Dice; IoU = Jaccard; sensitivity = recall; PPV = precision; NPV = npv; specificity = TNR; a Cox hazard ratio (HR) for the model's risk groups or score = hazard_ratio.
- Macro / micro / weighted F1 variants go in `macro_f1` / `micro_f1` / `weighted_f1`, never in `f1_score`.
- Detection: keep mAP / AP50 / AP75 distinct from classification metrics.
- Survival: if time-dependent AUC is reported with a horizon (e.g., "AUC@1yr=0.74"), include the horizon label in the value.
- Each value is **either** a single floating-point number (e.g., `0.95`) **or** an empty string. No percent signs, units, ranges or confidence intervals: store the point estimate, and put the interval or ± in `uncertainty`. Convert percentages to fractions: 95.3% → `0.953`.
- If you are not certain a number is this method's result for this dataset and split, leave it out. Do **not** guess.

Return only the JSON object. If the paper reports no results for this task, return `{{"results": []}}`.
