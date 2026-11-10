Extract every dataset that the paper explicitly uses or introduces, subject to strict inclusion rules. Return one JSON object with two keys: `datasets` — an array of public datasets — and `private_datasets` — an array of the non-public data the paper uses (see the last section).

## Inclusion rules

A dataset qualifies **only** if all of these hold:
1. It has a **precise official name** (e.g., "Camelyon17", "TCGA-BRCA", "PANDA").
2. It is **publicly available**.
3. The paper provides — or the dataset is widely known to have — at least one valid public URL among `code_url`, `download_url`, `home_page_url`. The URL must point to a code repo, dataset host (Zenodo, GitHub, Kaggle, Synapse, institutional repository), or official project page — **never** a paper URL or publisher domain.

If a paper newly introduces a dataset, include it only when a public URL is supplied; set `is_public: true`.

**Used means this paper's own experiments use it** — it trains, validates or tests on it, or deposits its data in it (below). A dataset named only in the introduction or related work, in a description of what *other* methods were trained or evaluated on ("with the MoNuSeg dataset, the reported networks were trained..."), or only in the reference list, does not qualify.

**Data-availability statements.** When the paper says its own data is deposited in, or available from, a public dataset or repository (e.g. "an open-source repository of SRH images can be found at OpenSRH"), that dataset is the data the paper used: list it.

**`dataset_name` is the dataset's published name** — `CAMELYON16`, `PANDA`, `BreakHis`, `TCGA-LGG`, or the title its authors released it under. Never:
- a description of the data (`TCGA glioma WSI dataset`, `liver kidney stomach images`, `H&E breast patches`);
- a split or subset of a dataset (`testing data subset`, `validation fold`) — name the dataset it comes from;
- a paper-specific cohort label.
For TCGA use the project code, `TCGA-<CODE>` (`TCGA-LGG`, `TCGA-KICH`), one entry per cohort; use `TCGA` alone only when the paper names no cohort. A public dataset you can only describe, never name, does not qualify.

## Per-dataset field guidance

Schema is `dataset_fields_schema.json`. Use schema enums/patterns for: `domain`, `modality`, `tissue_site`, `species`, `sample_prep`, `staining_type`, `image_format`, `split_level`, `split_method`, `dataset_access`, `license`, all `annotation_*` fields.

- `staining_type`: `h&e | ihc | pap-stain | giemsa | pas | trichrome | silver | multiplex-if | unstained | other`, pipe-separated in alphabetical order. `other` = a stated stain not in this list; `""` = the source does not say.
- Multi-value fields use a pipe-delimited string (`"kidney|breast"`).
- Numeric fields default to `0` if unknown; `n_patients` may use the literal string `"nan"` only when the count is genuinely unstated.
- `mpp` is in µm/pixel.
- `tile_size_px` is a single integer (no width/height split).
- Counts: `n_slides`, `n_patients`, `n_tiles` (do not use `num_*` variants).
- `scanner`: exact WSI scanner vendor + model as printed (e.g., "Aperio ScanScope XT", "Hamamatsu NanoZoomer 2.0-HT"). Pipe-separated for multiples. Only digital slide scanners — exclude generic microscopes. `""` if not reported.
- `labels`: pipe-separated discrete label names used in the main task(s) — classification labels (`"benign|malignant"`) or segmentation regions (`"tumor-core|edema|necrosis"`). Never include organ names (those go in `tissue_site`). Use `"na"` for self-supervised / regression-only datasets.
- Optional fields to include if present: `resource_urls`, `paper.doi`, `paper.venue` (copy venue verbatim from the paper, do not infer).

## Output shape

```json
{
  "datasets": [
    {
      "is_public": true,
      "slug": "short-kebab-name-or-empty",
      "dataset_name": "",
      "year": 0,
      "description": "<= 280 chars",
      "code_url": "",
      "download_url": "",
      "home_page_url": "",
      "domain": "histopathology",
      "modality": "brightfield",
      "tissue_site": "kidney|breast|lung|brain|skin|blood|multi-organ|colon|colorectal|gastrointestinal|stomach|gastric|uterus|lymph-node|placenta|bone-marrow|cervix|esophagus|ovary|liver|prostate|eye|head-neck|oral-cavity|pancreas|bone|bladder|thyroid|other",
      "species": "human|mouse|other",
      "sample_prep": "ffpe|frozen|other",
      "staining_type": "h&e|ihc|other",
      "scanner": "",
      "mag_min": 0,
      "mag_max": 0,
      "mpp": 0.0,
      "image_format": "svs|ndpi|tiff|other",
      "wsi_or_patch": "wsi|patch|both",
      "tile_size_px": 0,
      "approx_size_gb": 0.0,
      "n_slides": 0,
      "n_patients": "nan",
      "n_tiles": 0,
      "task": "classification|segmentation|regression|other",
      "labels": "",
      "annotation_level": "slide|region|tile|cell|pixel|object|patient|other",
      "annotation_types": "polygon|mask|point|box|label|landmark points|roi crop|heat map|cell-graph|text|other",
      "annotation_format": "xml|json|coco|txt|png|tiff|dicom-seg|xls|csv|sqlite db|mat / hdf5|.mat|other",
      "split_level": "patient|case|slide|tile",
      "split_method": "holdout|kfold|loo|stratified|other",
      "external_validation": false,
      "institutions": "",
      "dataset_access": "open|restricted|proprietary|on-request|unknown",
      "license": "CC0|CC-BY|CC-BY-NC|custom|unknown",
      "resource_urls": "",
      "paper": {"doi": "", "venue": ""}
    }
  ],
  "private_datasets": [
    {"name": "", "institution": "", "n_slides": 0, "n_patients": 0, "description": "<= 200 chars"}
  ]
}
```

Respect types: booleans for `is_public` / `external_validation`; integers for magnifications, year, tile size, counts; numbers for `mpp` / `approx_size_gb`. For each dataset, `wsi_or_patch` is the **format of the dataset's images** (as in step 2.2), not how this paper trained on them: `wsi` = whole-slide images; `patch` = pre-cut patches/tiles or fixed-size microscope field images; `both` = both. Set it when the paper says what kind of images the dataset contains; leave `""` if it does not. Return only JSON — no commentary, no code fences.

Do **not** include generic descriptors ("WSI dataset", "internal cohort", "private data"). Do **not** point any URL field at the paper or publisher page (`ieeexplore.ieee.org`, `nature.com`, `frontiersin.org`, `elsevier.com`, `sciencedirect.com`, `springer.com`). Do **not** invent counts, mpp, scanner, or venue values.

## Private / in-house data (`private_datasets`)
List the data the paper trains, validates or tests on that is **not** publicly available and so cannot go in `datasets`: hospital or institutional cohorts, consortium data without an open download, data "available on request". One entry per cohort:
- `name`: as the paper names it (e.g. "MPUH data set"); otherwise "in-house cohort, <institution>".
- `institution`, `n_slides`, `n_patients`: only when the paper states them (`""` / `0` otherwise).
- `description`: what the data is, in at most 200 characters.

Use `[]` when the paper uses only public data. The rule against generic descriptors applies to `datasets`, not here.
