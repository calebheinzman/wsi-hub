Classify the paper into the fields below using the allowed values for each field. Return one JSON object with every key present.

## Field schema (aligned to `paper_classification_schema.json`)

| Field | Type | Allowable values / pattern | Multi |
|---|---|---|---|
| domain | string | histopathology \| cytology \| molecular-pathology \| optical-label-free \| gross-pathology \| multimodal | no |
| biological_domain | string | oncology \| immunology \| neurology \| cardiology \| nephrology \| hepatology \| pulmonology \| gastroenterology \| dermatology \| hematology \| infectious-disease \| transplant \| developmental \| toxicology \| other | yes (pipe) |
| modality | string | brightfield \| fluorescence \| ihc-dab \| multiplex-if \| fish \| srh \| oct \| mpm \| tma \| spatial-transcriptomics \| other | yes (pipe) |
| tissue_site | string | kidney \| breast \| lung \| brain \| skin \| blood \| multi-organ \| rectum \| colon \| colorectal \| gastrointestinal \| stomach \| gastric \| uterus \| endometrial \| lymph-node \| placenta \| bone-marrow \| cervix \| esophagus \| ovary \| liver \| prostate \| eye \| head-neck \| oral-cavity \| pancreas \| bone \| bladder \| thyroid \| adrenal \| spleen \| heart \| muscle \| testis \| other | yes (pipe) |
| species | string | human \| mouse \| rat \| dog \| pig \| monkey \| rabbit \| other | yes (pipe) |
| staining_type | string | h&e \| ihc \| pap-stain \| giemsa \| pas \| trichrome \| silver \| multiplex-if \| unstained \| other | yes (pipe) |
| wsi_or_patch | string | wsi \| patch \| both \| `""` (empty if unstated) | no |
| annotation_types | string | polygon \| mask \| point \| box \| label \| landmark-points \| roi-crop \| heatmap \| cell-graph \| text \| other | yes (pipe) |
| labels | string | pipe-separated discrete label names used in the main supervised task(s) | no |
| mpp | number | 0.05–10 µm/pixel; 0 if unstated | no |
| scanner | string | exact WSI scanner vendor + model as printed; pipe-separated if multiple; `""` if not reported | no |
| tile_size_px | integer | 32–32768; 0 if unstated | no |
| tiling_strategy | string | grid \| tissue-mask \| hotspot \| random \| other | no |
| n_patients / n_slides / n_tiles / n_centers | integer | ≥ 0; 0 if unstated | no |
| class_counts | object | `{"<class>": int>=0}` | no |
| split_level | string | patient \| case \| slide \| tile | no |
| split_method | string | holdout \| kfold \| loo \| stratified \| other | no |
| external_validation | boolean | true / false | no |
| annotation_provenance | object | `{ n_annotators: int>=0, expertise: attending\|fellow\|resident\|technologist\|mixed\|unknown, consensus: single\|majority\|adjudicated\|unknown }` | no |
| outcome_endpoint | string | diagnosis \| grade \| stage \| survival \| recurrence \| treatment-response \| none \| other | no |
| model_details | object | `{ model_type: cnn\|transformer\|mil\|graph-nn\|hybrid\|rnn\|other, backbone: resnet\|densenet\|efficientnet\|vgg\|inception\|vit\|swin\|convnext\|deit\|custom\|other, pretraining_strategy: none\|imagenet\|pathology-ssl\|foundation-model\|other, foundation_model: none\|uni\|conch\|prov-gigapath\|remedis\|ctranspath\|phikon\|plip\|virchow\|hibou\|other }` | no |
| multimodal_flags | object | `{ uses_radiology: boolean, other_modalities: string }` | no |
| dataset_access | string | open \| restricted \| proprietary \| on-request \| unknown | no |
| license | string | CC0 \| CC-BY \| CC-BY-NC \| custom \| unknown | no |
| resource_urls | string | free-text URLs | no |
| paper | object | `{ doi: string, venue: string }` — `venue` is the **full name of the journal or conference** the paper appears in (e.g., "Nature Medicine", "Frontiers in Oncology" -- not "Front. Oncol."); never the publisher (Springer, Elsevier, IEEE, Wiley) and never inferred when the paper does not say | no |
| preprocessing_methods | string | tissue-detection \| color-normalization \| stain-normalization \| macenko-normalization \| reinhard-normalization \| vahadane-normalization \| background-removal \| tiling \| patching \| resizing \| rescaling \| grayscale-conversion \| artifact-removal \| blur-detection \| pen-mark-removal \| focus-stacking \| white-balance \| clahe \| histogram-equalization \| contrast-enhancement \| denoising \| sharpening \| otsu-thresholding \| morphological-operations \| roi-extraction \| downsampling \| pyramid-generation \| z-score-normalization \| min-max-normalization \| quality-filtering \| center-cropping \| random-cropping \| stain-separation \| foreground-extraction \| none \| other | yes (pipe) |
| augmentation_methods | string | horizontal-flip \| vertical-flip \| rotation \| random-rotation \| 90-rotation \| 180-rotation \| random-crop \| center-crop \| color-jitter \| brightness \| contrast \| saturation \| hue \| gaussian-blur \| gaussian-noise \| elastic-deformation \| affine-transform \| scale \| shear \| translate \| cutout \| random-erasing \| mixup \| cutmix \| mosaic \| randaugment \| autoaugment \| stain-augmentation \| color-augmentation \| random-resize \| perspective-transform \| grid-distortion \| optical-distortion \| morphological-augmentation \| coarse-dropout \| grid-dropout \| channel-shuffle \| normalize \| random-flip \| random-brightness \| random-contrast \| random-saturation \| additive-noise \| multiplicative-noise \| motion-blur \| median-blur \| jpeg-compression \| none \| other | yes (pipe) |

## Global conventions (apply to every field)

**Ordering of multi-value fields** — for every pipe-separated field, sort the values in **alphabetical order**, with no duplicates and no two terms meaning the same thing. `gastroenterology|oncology`, never `oncology|gastroenterology`.

**`other` versus `""`** — these are not interchangeable:
- `other` — the paper **does** state a value, but it is not one of the allowed values.
- `""` — the paper does not state the field at all.

Never use `other` as a substitute for "unstated", and never use `""` for a stated-but-unlisted value.

## Critical extraction rules

**wsi_or_patch** — decide from the granularity of the **ground-truth labels used to supervise training**. Not the image format, not the model architecture, not whether slides were tiled. Ask two independent questions:

1. Is a **slide-level** label a training target? One diagnosis / grade / status / outcome per whole slide, case or patient. Weakly supervised and MIL setups count: the only annotation is the slide's diagnosis.
2. Is a **patch-level** label a training target? The annotation itself must be sub-slide — each patch/tile/core carries its own class, or pixel/region masks supply per-location targets, or the work trains on a pre-labeled patch dataset. It must be used to **train**: sub-slide annotations described as being for validation, evaluation, testing, or "for assessment only" do **not** count, no matter how prominently the paper discusses them.

Then: `both` if both are training targets, `wsi` if only slide-level, `patch` if only patch-level, `""` if the text never says what supervised the training.

The cases that decide this:
- Slides tiled into patches where each patch inherits the slide's label, or patch labels are latent/inferred (MIL, EM, attention pooling) → `wsi`. The annotation only ever existed at slide level.
- Pathologist region or pixel annotations training a patch/segmentation model, with no slide-level training target → `patch`. A slide-level verdict *computed* by aggregating or thresholding patch predictions is not a slide-level training label.
- Datasets distributed as pre-extracted patches (PatchCamelyon, NCT-CRC-HE-100K, BreakHis) and tissue microarray cores → `patch`.
- A region/patch-annotated model **plus** a slide- or patient-level training target (e.g. annotated tumour-patch detector feeding a slide-level status classifier trained on slide labels) → `both`.
- Patch-level annotations used only for **evaluation or validation** while training uses slide labels → `wsi`. Only training supervision counts.

Do not answer `both` merely because whole-slide images were tiled, nor because the architecture is patch-based.

**modality / staining_type** — for conventional H&E-stained histopathology viewed on a light microscope or slide scanner, use `modality: brightfield` and `staining_type: h&e`. These are the standard defaults for routine histology and should be used whenever the paper describes ordinary stained tissue sections, even if the exact words "brightfield" or "H&E" are not printed. Use `other` / `""` only for genuinely non-standard cases (fluorescence, IHC-DAB, multiplex-IF, unstained, Pap/Giemsa/PAS/etc.) or when the modality/stain is truly indeterminable.

**species** — infer it the same way. Clinical/diagnostic pathology on patient biopsies, resections, or any public human cohort (TCGA, CAMELYON, TMAs of patient tissue) is `human`, even when the word "human" never appears. Name an animal species only when the paper says so.

**biological_domain** — emit exactly **one** domain. If the disease studied is cancer, the domain is `oncology` — never pair it with an organ-system domain and never substitute one (a colorectal-cancer study is `oncology`, not `gastroenterology` and not `gastroenterology|oncology`). Use an organ-system domain only for non-neoplastic disease of that system.

**tissue_site** — emit the **single most specific canonical term** from the list. Do not list synonyms of one site: use `colorectal` (not `colon|colorectal`), `stomach` (not `gastric` or `stomach|gastric`). Pipe-separate only genuinely distinct organs, and only up to three (e.g., `lung|lymph-node`). If the work spans **four or more** organs, output exactly `multi-organ` on its own — never combine it with a specific organ (`lung|multi-organ` is wrong; it is either `lung` or `multi-organ`).

**scanner** — copy the exact vendor + model string as written (e.g., "Aperio ScanScope XT", "Hamamatsu NanoZoomer 2.0-HT", "Philips IntelliSite Ultra Fast Scanner"). Pipe-separate multiples. Only digital slide scanners — exclude generic microscopes. If no scanner is reported anywhere, set `""`.

**paper.venue** — take the venue / journal / conference name from **this paper's own** header, masthead or citation block, written out in full: expand an abbreviated journal name ("Front. Oncol." → "Frontiers in Oncology", "JAMA Netw Open" → "JAMA Network Open"). The venue is never the publisher (Springer, Elsevier, IEEE, Wiley). Do **not** infer it from the URL, publisher, or DOI prefix. Never take a venue from the reference list, a cited work, an acknowledgement, or a thesis/dissertation template — if this paper's own venue is not printed, use `""`. If the text you were given clearly belongs to a different paper than the title, use `""` rather than reporting that other paper's venue.

**labels** — pipe-separated discrete label names used in the main supervised task(s):
- Classification: `"benign|malignant|atypical"`, `"low-grade|high-grade"`, `"tumor|normal"`.
- Segmentation/region: `"tumor-core|edema|necrosis"`, `"gland|stroma|lumen"`.
- Do **not** include organ names (those go in `tissue_site`) or dataset names.
- For purely self-supervised or regression-only work without discrete labels, use `"na"`.

**annotation_types** — only the forms of **ground-truth annotation present in the data**. Exclude anything the model *produces* (a predicted heatmap or attention map is an output, not an annotation) and exclude visualization artifacts. If the only ground truth is a class label per image/slide, that is just `label`. For pixel- or region-level ground truth use `mask`; use `polygon` only if the paper says the annotations are stored as polygons/vertex outlines, and `roi-crop` only when annotation consists of cropping regions of interest rather than delineating them.

**preprocessing_methods / augmentation_methods** — list only methods the paper **explicitly describes applying**, alphabetically. `tiling` and `patching` denote the same operation: use `tiling` only, never both. When a general term and a specific variant both apply, keep only the specific one (`macenko-normalization`, not `color-normalization|macenko-normalization`). Do not add methods that are merely implied by using a dataset or a standard library.

**model_details.model_type** — the single primary architecture family of the paper's **main proposed model**. Any convolutional network — including a modified or custom CNN — is `cnn`, not `other`. If the paper aggregates patch features into a slide-level prediction by multiple-instance learning, that is `mil` even though a CNN encodes the patches. Use `hybrid` only when the paper itself presents the model as combining two families as its central design; a CNN with an attention block is not `hybrid`. Reserve `other` for a genuinely different named paradigm.

**model_details.backbone** — `custom` when the authors built their own network rather than reusing a published backbone; `other` when they name a published backbone that is not in the allowed list; `""` when no backbone is identified.

**model_details.pretraining_strategy** — `imagenet` for ImageNet-pretrained weights, `pathology-ssl` for self-supervised pretraining on pathology images, `foundation-model` when a named pathology foundation model supplies the weights, `none` when the network is trained from scratch, `""` when the paper does not say how the weights were initialized.

**resource_urls** — the URLs this paper gives for its own code or data, separated by `|` if several, `""` if it gives none. Do not include DOI links, citation URLs, or a publisher landing page.

**split_level / split_method** — `split_method` is `holdout` whenever the paper reports separate training and test (or train/val/test) sets at all, even if it gives no further detail; `kfold`/`loo` for cross-validation; `other` for a stated scheme not listed; `""` only when the paper never indicates that the data were split. `split_level` is the unit the split is performed on (`patient` > `case` > `slide` > `tile`); use `""` if the paper never says.

**tiling_strategy** — `tissue-mask` when tiles are taken from a detected tissue/foreground mask; `grid` for exhaustive uniform tiling of the slide; `hotspot` only when specific high-interest regions are deliberately selected; `random` for random sampling; `other` for a stated strategy not listed; `""` when tiling is not described.

**outcome_endpoint** — the clinical question the model predicts: `diagnosis` for presence / type / subtype of disease (including tumor-vs-normal and tissue-type classification), `grade` for grading, `stage` for staging, `survival` / `recurrence` / `treatment-response` for those endpoints, `other` for a stated endpoint not listed. When the work has no clinical endpoint at all (pure segmentation, retrieval, representation learning), use `none` — not `""`. This field is effectively always determinable from the task, so `""` should be rare.

**multimodal_flags.uses_radiology** — `true` only if radiology images or radiology text reports are explicitly used as model inputs, not merely mentioned.

## Sample output

```json
{
  "domain": "histopathology",
  "biological_domain": "oncology",
  "modality": "brightfield",
  "tissue_site": "breast",
  "species": "human",
  "staining_type": "h&e",
  "wsi_or_patch": "wsi",
  "annotation_types": "label",
  "labels": "benign|malignant",
  "mpp": 0.5,
  "scanner": "Aperio ScanScope XT",
  "tile_size_px": 0,
  "tiling_strategy": "",
  "n_patients": 0,
  "n_slides": 0,
  "n_tiles": 0,
  "n_centers": 0,
  "class_counts": {},
  "split_level": "patient",
  "split_method": "holdout",
  "external_validation": false,
  "annotation_provenance": {"n_annotators": 0, "expertise": "unknown", "consensus": "unknown"},
  "outcome_endpoint": "diagnosis",
  "model_details": {"model_type": "cnn", "backbone": "resnet", "pretraining_strategy": "imagenet", "foundation_model": "none"},
  "multimodal_flags": {"uses_radiology": false, "other_modalities": ""},
  "dataset_access": "unknown",
  "license": "unknown",
  "resource_urls": "",
  "paper": {"doi": "", "venue": "Nature Medicine"},
  "preprocessing_methods": "none",
  "augmentation_methods": "none"
}
```

For unknown numeric values use `0` — never a plausible-looking stand-in. In particular, if `mpp` is not stated, use `0`; do **not** fall back to `0.25` or any other typical scan resolution. For unknown strings use `""`. For objects, include the keys with `""` / `0` rather than omitting them.

Return only JSON. Do **not** invent values not in the paper. Do **not** guess the venue from the publisher domain or DOI. For `wsi_or_patch`, follow the decision rule above: identify which label granularities supervised training, and use `""` only when the text never says.
