You are given a small group of dataset entries that a similarity search flagged as possibly naming the same dataset. Decide which entries are the same underlying dataset, and give each resulting dataset one canonical name.

## Same dataset (merge)
- Naming variants of one release: `Kather 100k`, `NCT-CRC-HE-100K`, `100,000 Histological Images of Human Colorectal Cancer and Healthy Tissue`
- Acronym vs spelled-out: `TCGA-LUAD` and `The Cancer Genome Atlas Lung Adenocarcinoma`
- Challenge naming: `BACH`, `ICIAR 2018 Grand Challenge on Breast Cancer Histology Images`
- Spelling, casing, punctuation, or a trailing "dataset" / "challenge"

## Different datasets (keep separate)
- Different releases or years: `CAMELYON16` vs `CAMELYON17`, `MIDOG 2021` vs `MIDOG 2022`
- Different cohorts of one program: `TCGA-LUAD` vs `TCGA-LUSC` vs `TCGA-BRCA`
- A combined benchmark that papers name as one thing is its own dataset: `TCGA-NSCLC` (LUAD + LUSC) and `TCGA-RCC` (KIRC + KIRP + KICH) stay separate from their cohorts
- A dataset derived from another one (new annotations, curated subsets, patch extractions, QC masks): `PatchCamelyon` is not `CAMELYON16`; `TCGA TIL maps` is not `TCGA-BRCA`
- An entry that names a whole program without a cohort (`TCGA`, `The Cancer Genome Atlas`) is its own group, not merged into a cohort
- A vague organ-level program name means that organ's standard benchmark: `TCGA lung` / `TCGA lung cancer` = `TCGA-NSCLC`, `TCGA kidney` = `TCGA-RCC`, `TCGA brain` / `TCGA glioma` = `TCGA-GBM/LGG`. Merge it there, not into a single cohort
- Catch-all program names (`TCGA multi`, `TCGA pan-cancer`, `The Cancer Genome Atlas (TCGA)`) merge with the plain program entry (`TCGA`)

- An entry that is a description, split or subset rather than a dataset name (`tcga glioma wsi dataset`, `lks liver kidney stomach`, `... testing data subset`) belongs to the dataset it describes or comes from: merge it there when that dataset is in the group (a TCGA description goes to the cohort or family it names, by the organ rule above). If it is not, keep it on its own and add a `note` saying it is not a dataset name.

When unsure, keep entries separate. Merging two real datasets corrupts both records; leaving a duplicate only costs a few extra calls.

## Canonical name
Use the name the field uses most, with its standard acronym when there is one: `TCGA-LUAD`, `CAMELYON16`, `PANDA`, `GlaS`, `MoNuSeg`, `NCT-CRC-HE-100K`. A canonical name is always a dataset's published name, never a description of the data.

## Output
Every input id must appear in exactly one group. Add a `note` only when the decision was a judgment call a human should check.

```json
{
  "groups": [
    {"canonical_name": "TCGA-LUAD", "members": [3, 7, 12]},
    {"canonical_name": "TCGA-NSCLC", "members": [4, 9], "note": "LUAD+LUSC combined benchmark kept separate from its cohorts"},
    {"canonical_name": "PatchCamelyon", "members": [5]}
  ]
}
```

Return only the JSON object.
