// The metric columns of filtered_metrics, and how to label them. Every other column of a
// metrics row describes the evaluation (dataset, split, level, subset, source), not a score.

export const METRIC_LABELS: Record<string, string> = {
  // classification
  auc_roc: "AUC",
  accuracy: "Acc",
  balanced_accuracy: "Bal Acc",
  f1_score: "F1",
  macro_f1: "Macro F1",
  micro_f1: "Micro F1",
  weighted_f1: "Weighted F1",
  precision: "Prec",
  recall: "Recall",
  specificity: "Spec",
  npv: "NPV",
  auprc: "AUPRC",
  mcc: "MCC",
  kappa: "Kappa",
  // segmentation
  dice_coefficient: "Dice",
  iou: "IoU",
  pixel_accuracy: "Px Acc",
  aji: "AJI",
  hd95: "HD95",
  // detection
  map: "mAP",
  ap50: "AP50",
  ap75: "AP75",
  froc: "FROC",
  sensitivity_at_1_fp_per_image: "Sens @1 FP",
  sensitivity_at_2_fp_per_image: "Sens @2 FP",
  // regression
  mae: "MAE",
  rmse: "RMSE",
  r2: "R²",
  pearson_r: "Pearson r",
  spearman_r: "Spearman",  // not "ρ": the cards uppercase labels, and a capital rho reads as P
  // survival
  c_index: "C-Index",
  hazard_ratio: "HR",
  time_dependent_auc: "td-AUC",
  ibs: "IBS",
}

// Headline metrics first for each task; any other reported metric follows.
const TASK_ORDER: Record<string, string[]> = {
  classification: ["auc_roc", "accuracy", "balanced_accuracy", "f1_score", "macro_f1", "precision", "recall",
                   "specificity", "npv"],
  segmentation: ["dice_coefficient", "iou", "aji", "pixel_accuracy", "hd95"],
  detection: ["map", "ap50", "ap75", "froc", "f1_score", "precision", "recall"],
  regression: ["r2", "pearson_r", "spearman_r", "mae", "rmse"],
  survival: ["c_index", "hazard_ratio", "time_dependent_auc", "ibs"],
}

export const isMetricKey = (key: string): boolean => key in METRIC_LABELS

export const hasMetricValue = (value: unknown): boolean => {
  if (value === null || value === undefined) return false
  const s = String(value).trim().toLowerCase()
  return s !== "" && !["nan", "none", "null", "unknown", "n/a", "na"].includes(s)
}

/** The metrics a row reports, as [column, label, value], headline metrics for its task first. */
export function reportedMetrics(row: Record<string, any>, task?: string | null): [string, string, any][] {
  const first = TASK_ORDER[(task || "").toLowerCase()] || []
  const keys = [...first, ...Object.keys(METRIC_LABELS).filter((k) => !first.includes(k))]
  return keys.filter((k) => hasMetricValue(row[k])).map((k) => [k, METRIC_LABELS[k], row[k]])
}

export const SPLIT_LABELS: Record<string, string> = {
  test: "Test",
  external_test: "External test",
  cross_validation: "Cross-validation",
  validation: "Validation",
  train: "Train",
  other: "Other",
}

/** A note for results not read from the full paper, or "" for full text. */
export function textSourceNote(source?: string | null): string {
  if (!source || source === "full_text") return ""
  if (source === "abstract") return "From the abstract only"
  if (source === "partial") return "From partial text"
  return "From the title and snippet only"
}
