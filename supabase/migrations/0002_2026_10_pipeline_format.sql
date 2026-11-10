-- Pipeline data-format changes from the first human-labelled validation round (2026-10).
-- Run once in the Supabase SQL editor BEFORE pushing a run made with the new pipeline.
-- Safe to re-run: every step is idempotent.
-- A database created from 0001_init.sql already has all of this, so on it this is a no-op.

-- 1. filtered_metrics: one row per paper, dataset, task, split, evaluation level and
--    subset (class/subgroup), instead of one per paper-dataset-task. The old push
--    copied a paper's single metrics dict onto every dataset linked to the paper.
ALTER TABLE filtered_metrics ADD COLUMN IF NOT EXISTS dataset_reported TEXT;  -- dataset as the paper names it
ALTER TABLE filtered_metrics ADD COLUMN IF NOT EXISTS split TEXT;             -- test | external_test | cross_validation | validation | train | other
ALTER TABLE filtered_metrics ADD COLUMN IF NOT EXISTS eval_level TEXT;        -- patch | tile | slide | patient | pixel | object
ALTER TABLE filtered_metrics ADD COLUMN IF NOT EXISTS subset TEXT;            -- NULL = overall; else the class or subgroup
ALTER TABLE filtered_metrics ADD COLUMN IF NOT EXISTS is_primary BOOLEAN DEFAULT TRUE;  -- headline result for its dataset
ALTER TABLE filtered_metrics ADD COLUMN IF NOT EXISTS metric_key TEXT;
ALTER TABLE filtered_metrics ADD COLUMN IF NOT EXISTS text_source TEXT;       -- full_text | abstract | partial: what the numbers were read from

-- Existing rows predate splits: key them on the old identity so the new primary key holds.
UPDATE filtered_metrics
SET metric_key = md5(coalesce(paper_url, '') || '|' || coalesce(dataset_slug, '') || '||' || coalesce(task, '') || '|||')
WHERE metric_key IS NULL;

-- Results on private cohorts (or ambiguous) have no catalog dataset: dataset_slug may be NULL.
ALTER TABLE filtered_metrics ALTER COLUMN dataset_slug DROP NOT NULL;

-- Replace the (paper_url, dataset_slug, task) primary key with metric_key.
DO $$
DECLARE pk TEXT;
BEGIN
  SELECT conname INTO pk FROM pg_constraint
  WHERE conrelid = 'filtered_metrics'::regclass AND contype = 'p';
  IF pk IS NOT NULL AND pk <> 'filtered_metrics_metric_key_pkey' THEN
    EXECUTE format('ALTER TABLE filtered_metrics DROP CONSTRAINT %I', pk);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'filtered_metrics_metric_key_pkey') THEN
    ALTER TABLE filtered_metrics ADD CONSTRAINT filtered_metrics_metric_key_pkey PRIMARY KEY (metric_key);
  END IF;
END $$;
DROP INDEX IF EXISTS idx_metrics_pk;
CREATE INDEX IF NOT EXISTS idx_metrics_primary ON filtered_metrics(dataset_slug) WHERE is_primary;

-- 2. final_datasets: the dataset this one is a subset of (TCGA-KICH -> TCGA-RCC -> TCGA).
--    No foreign key: the parent may not be in the catalog.
ALTER TABLE final_datasets ADD COLUMN IF NOT EXISTS parent_slug TEXT;

-- 3. papers_dct: non-public data the paper uses, which no catalog dataset can link.
ALTER TABLE papers_dct ADD COLUMN IF NOT EXISTS uses_private_data BOOLEAN;  -- NULL = extracted before this existed
ALTER TABLE papers_dct ADD COLUMN IF NOT EXISTS private_datasets TEXT;      -- pipe-separated names

-- 4. Before pushing the first run in the new format, clear the old metric rows: they are
--    one-dict-per-paper copies spread across every linked dataset, and would sit beside
--    the new per-dataset rows. Uncomment when that push is ready.
-- DELETE FROM filtered_metrics WHERE split IS NULL AND dataset_reported IS NULL;

-- What each paper's extraction read: full_text, abstract (only the landing page /
-- abstract was available, mostly paywalled papers), partial, title_snippet.
ALTER TABLE papers_dct ADD COLUMN IF NOT EXISTS text_source TEXT;

-- Metrics with no column before: NPV, and the Cox hazard ratio that 1.7 counts as survival
-- evidence (2026-10-07) -- every HR result was dropped for lack of a field.
ALTER TABLE filtered_metrics ADD COLUMN IF NOT EXISTS npv NUMERIC;
ALTER TABLE filtered_metrics ADD COLUMN IF NOT EXISTS hazard_ratio NUMERIC;

-- The processing pipeline (2D list of step labels) that push writes and the paper detail view
-- reads was never in the schema docs; a database built from them rejected every paper row.
ALTER TABLE papers_dct ADD COLUMN IF NOT EXISTS processing_pipeline JSONB;
