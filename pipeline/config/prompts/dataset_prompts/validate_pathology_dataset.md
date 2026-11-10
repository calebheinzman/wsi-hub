Decide whether the dataset (given its name and merged search results) actually contains pathology / WSI / microscopy images, and whether it is publicly available.

## Accept (`is_pathology_dataset: true`)
- Histopathology whole-slide images
- Cytology microscopy
- H&E, IHC, multiplex-IF, fluorescence, brightfield microscopy of tissue/cells
- Digital pathology images
- Tissue microarrays (TMA)

## Reject (`is_pathology_dataset: false`)
- MRI, CT, X-ray, ultrasound, dermoscopy, colposcopy, endoscopy
- Gross-pathology photographs without microscopy
- Clinical photographs of skin
- Any radiology images
- Non-medical images
- Pages that only *mention* pathology datasets but contain none
- Software, toolkits, benchmark frameworks or model zoos that download or wrap other datasets (e.g. a benchmarking toolkit) — the toolkit is not itself a dataset

## Which dataset
When the input starts with how research papers describe the dataset, judge THAT dataset. Search results about a different release of the same program (e.g. TCIA's radiology collection for a TCGA cohort whose papers use its whole-slide images) do not make it a radiology dataset.

## Public availability (`appears_public`)
`true` if the data can be downloaded without paying, without an institutional login, and without an approval or data-use committee — from the official site or an official or widely used public mirror (GitHub, Zenodo, figshare, Kaggle, Hugging Face, Mendeley Data), including challenge sites that only need a free account. `false` for "available on request", controlled access (dbGaP, EGA, signed DUA), or no download found. Copyright or reuse terms ("permission required to reproduce", a non-commercial license) limit what users may do with the data, not who can get it: they do not make it non-public.

## Output

```json
{
  "is_pathology_dataset": true,
  "appears_public": true,
  "confidence": "high",
  "rationale": "Brief reason for the pathology and public decisions",
  "disqualifier": "If is_pathology_dataset is false, name the actual dataset type (e.g., 'CT', 'dermoscopy')"
}
```

Return only the JSON object. Do **not** mark `is_pathology_dataset: true` for datasets that merely cite or reference pathology data without containing microscopy images themselves.
