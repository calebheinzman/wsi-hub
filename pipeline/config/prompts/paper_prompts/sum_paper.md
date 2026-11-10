Extract structured paper metadata from the provided full text. Return one JSON object with these five required keys: `Journal`, `Abstract`, `Methods`, `Results`, `Processing Pipeline`.

## Field guidance

**Journal** — the **exact venue name** as printed in the paper (e.g., "Nature Medicine", "IEEE Transactions on Medical Imaging", "Proceedings of CVPR 2023", "Medical Image Analysis"). Copy the venue string verbatim from the masthead, header, or citation block. Do not infer, abbreviate, or guess from the publisher domain. If the venue is not stated in the text, return `""`.

**Abstract** — the **complete abstract verbatim** as it appears in the paper. Do not paraphrase, summarize, shorten, or rewrite. Preserve original sentences and punctuation. If no abstract is present in the text, return `""`. Do not invent one.

**Methods** — a markdown bullet-list string (3-6 bullets) summarizing methodology. Format:
```
- **Data**: ...
- **Model**: ...
- **Training**: ...
- **Preprocessing**: ...
- **Evaluation**: ...
```

**Results** — a markdown bullet-list string (3-5 bullets) summarizing key findings. Format:
```
- **Metric**: value (context)
- **Key finding**: ...
```

**Processing Pipeline** — a 2D JSON array. Each inner array is one ordered pipeline of 3-6 short step labels (1-3 words each). Use one inner array unless the paper contrasts distinct pipelines (e.g., baseline vs. proposed).

## Output shape

```json
{
  "Journal": "Nature Medicine",
  "Abstract": "Verbatim abstract text from the paper...",
  "Methods": "- **Data**: 1,200 H&E WSIs from 3 centers\n- **Model**: ResNet-50 with attention pooling\n- **Training**: 5-fold CV, Adam\n- **Evaluation**: AUC-ROC, sensitivity, specificity",
  "Results": "- **Classification**: AUC 0.94 (95% CI 0.91-0.97)\n- **Generalization**: External AUC 0.89\n- **Key finding**: Outperformed pathologist baseline by 8%",
  "Processing Pipeline": [["Patch extraction", "Preprocessing", "MIL training", "Prediction agg", "WSI output"]]
}
```

## Defaults if a field is genuinely absent
- `Journal`: `""`
- `Abstract`: `""` (only if no abstract appears in the text)
- `Methods`: `"- **Not specified**: Methodology details not available"`
- `Results`: `"- **Not specified**: Results not available"`
- `Processing Pipeline`: `[]`

Return only the JSON object — no prose, no code fences. **Methods and Results must be raw markdown strings, not nested JSON.** Do **not** paraphrase the abstract. Do **not** guess the venue from the URL or publisher.
