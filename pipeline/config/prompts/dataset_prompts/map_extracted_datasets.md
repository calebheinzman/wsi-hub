A research paper's extraction lists the datasets the paper reports using. Map that list onto the catalog entries it refers to, using the paper excerpts to resolve ambiguity.

## Mapping rules
- Map on identity, never on topic. A breast dataset does not map to another breast dataset because both are breast.
- A listed dataset can map to several entries when it combines them, and to none when the catalog does not contain it.
- Map only datasets the paper USES (trains, validates, tests or benchmarks on). Ignore datasets the paper only cites or discusses.
- The extraction list can miss datasets, especially in papers that evaluate on many. Each catalog entry comes with passages where its own name appears: an entry the passages show the paper using counts even if the extraction did not list it. That needs a passage naming it as a dataset the paper trains, validates or tests on (e.g. "brain subtyping (Ebrains)", "evaluated on CAMELYON16"). It does not count when the name only appears in the reference list or related work, or when the passage is ordinary wording that happens to share words with a descriptive catalog name (a passage about "liver, kidney and stomach" tissue is not evidence for an entry called "lks liver kidney stomach").

## TCGA and other multi-cohort programs
- Map to the specific cohort entries the paper names: `TCGA-KIRC`, `TCGA-READ`, `TCGA-LUAD`. Check the excerpts for the cohort codes or organs.
- A combined benchmark entry (`TCGA-NSCLC` = LUAD + LUSC, `TCGA-RCC` = KIRC + KIRP + KICH) applies only when the paper uses that combination. A paper that used only LUAD maps to `TCGA-LUAD`, not `TCGA-NSCLC`.
- Map to a generic program entry (`TCGA`, `The Cancer Genome Atlas`) only when the paper uses the program without naming any cohort.
- A dataset derived from TCGA (new annotations, TIL maps, curated patch sets) maps to its own entry only when the paper names that derived dataset.

## Output
```json
{"catalog_ids": [3, 7], "unmatched": ["names from the list with no catalog entry"]}
```
Return only the JSON object.
