Extract three URL types for the named dataset from the provided search results. Return one JSON object.

Fields:
- **code_url** — code repository (GitHub, GitLab, Bitbucket, etc.)
- **download_url** — direct dataset download (Zenodo, Synapse, institutional repo, direct file)
- **home_page_url** — official dataset homepage / landing / documentation site: a data-repository record, challenge or project site, GitHub repository, or for a TCGA cohort its GDC project page (`https://portal.gdc.cancer.gov/projects/TCGA-XXXX`) — which is also its `download_url`, since the slides are downloaded from GDC. **Never** the paper that introduced the dataset — no article, abstract or publisher page (sciencedirect.com, springer.com, nature.com, pubmed/PMC, arxiv.org/abs).

A single URL may serve multiple roles (e.g., a GitHub repo can be both `code_url` and `home_page_url` if it is the project's main page). Prefer official sources over third-party mirrors. When no official download exists in the results, a public mirror of the same dataset (Kaggle, Hugging Face, Zenodo, figshare) is a valid `download_url`.

When the input says how research papers describe the dataset, pick URLs for THAT release: if the papers use whole-slide images, choose where the slides are hosted (e.g. the GDC portal or Imaging Data Commons for a TCGA cohort), not a radiology collection of the same cohort.

```json
{
  "code_url": "https://github.com/org/dataset-repo",
  "download_url": "https://zenodo.org/record/12345",
  "home_page_url": "https://dataset-project.org"
}
```

Use `""` for any URL you cannot find. If the dataset does not appear public, if no result clearly matches the named dataset, or if the dataset is not WSI / pathology related, return all three fields as `""`.

Return only the JSON object — no prose, no code fences.
