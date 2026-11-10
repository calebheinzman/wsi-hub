# WSI Hub

Code and saved inputs for the WSI Hub paper.

## Reproduce the paper

From this directory, with Python 3.12:

```sh
pip install -e ".[analysis]"
python reproduce.py
```

This rebuilds the corpus analyses, both validation tables, screening counts, and Figures 2–6 without network access or API keys. It checks the numerical results against the paper's saved outputs. Results are written to `results/results.json`, `results/tables/`, and `results/figures/`. With `pdflatex` installed, the same command also compiles the pipeline diagram (Figure 1).

`evaluation/` contains only the analysis, scoring definitions, figure code, and inputs used for these results. Its compressed data files hold the structured corpus fields, reference labels, saved development predictions, screening decisions, and numeric run events. Fields used only for presence counts retain a presence marker. Paper full texts, abstracts, fetched pages, and private credentials are excluded. The saved development evaluations are not a new held-out test or a rerun of model inference.

## Pipeline, database, and website

These implement the system described in the paper; they are not invoked by `reproduce.py`.

- `pipeline/`: discovery, screening, extraction, matching, and export. Prompts and schemas are in `pipeline/config/`.
- `supabase/migrations/`: database schema and migrations, in numeric order.
- `frontend/`: the Next.js catalog interface.

To run a new extraction, copy `.env.example` to `.env`, supply the service keys, and run `python -m pipeline.src.cli papers` in an empty `pipeline/data/`. This makes paid API calls and is separate from reproducing the saved paper results. Use `python -m pipeline.src.cli --help` for the run and database-export options.

For the website, set `NEXT_PUBLIC_SUPABASE_URL` and `NEXT_PUBLIC_SUPABASE_ANON_KEY` in `frontend/.env.local`, then run `npm ci` and `npm run dev` from `frontend/`.
