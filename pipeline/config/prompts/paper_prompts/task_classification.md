Classify which computational tasks the paper actually performs and reports results for. Return one JSON object with five boolean keys.

Task types:
- **classification** — categorizing images/regions (e.g., tumor vs normal, grading, subtyping). Evidence: AUC, F1, accuracy.
- **segmentation** — delineating boundaries (tissue, cell, tumor). Evidence: Dice, IoU, pixel-level accuracy/F1. Also counts when the paper scores any of its outputs (e.g. attention or heat maps of a classifier) against segmentation ground truth with overlap metrics, even if the model was not trained for segmentation. Not segmentation: heat maps only shown as a visual aid, and segmentation done by an existing tool as a step of the pipeline, when no overlap score is reported for it.
- **detection** — locating objects (mitoses, nuclei, lesions). Evidence: mAP, AP50, FROC.
- **regression** — predicting continuous values (cell counts, biomarker levels). Evidence: MAE, RMSE, R².
- **survival** — outcome / time-to-event prediction, including prognostic models evaluated with Cox regression. Evidence: C-index, time-dependent AUC, hazard ratios, Kaplan–Meier / log-rank separation of predicted risk groups.

Decide by what the reported result measures, not by the paper's wording ("detect", "segment") or the model's architecture:
- **Classification vs detection.** A label per image, patch, slide or patient is classification, even when the paper calls it "cancer detection" or "metastasis detection" and reports AUC or accuracy. Detection needs predicted locations (points, boxes, lesions) matched to annotated ones — mAP, FROC, sensitivity at N false positives, or an F1 where a hit is a prediction within some distance of an annotated object (mitoses, nuclei).
- **Segmentation vs detection.** Regions or masks scored by overlap (Dice, IoU, pixel accuracy/F1, AJI, PQ, instance masks) are segmentation; points or boxes scored by matching are detection. A paper that reports both kinds of result gets both flags.
- **Typing detected objects** (e.g. nucleus types): detection, plus classification only when a separate type-classification result is reported.
- **Counts and other continuous values** (cell counts, percent tumour or Gleason pattern, biomarker levels) scored by MAE, RMSE, R² or correlation are regression, in addition to whatever model produced them.

A paper may perform multiple tasks — set every flag that applies.

Output shape:
```json
{"classification": false, "segmentation": false, "detection": false, "regression": false, "survival": false}
```

Set a flag to `true` **only** if the paper actively performs and reports results for that task. Tangential mentions, related-work citations, or future-work statements do not qualify. Return only the JSON object.
