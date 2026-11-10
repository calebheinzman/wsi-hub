-- Where each metric row comes from (pipeline 7a7256f, metrics.md): run once in the Supabase
-- SQL editor BEFORE pushing a run made with it. Safe to re-run. A database created from
-- 0001_init.sql already has these columns, so on it this is a no-op.
ALTER TABLE filtered_metrics ADD COLUMN IF NOT EXISTS method TEXT;       -- the paper's name for the method or variant this row is for
ALTER TABLE filtered_metrics ADD COLUMN IF NOT EXISTS n_eval TEXT;       -- size of the evaluation set as the paper states it ("129 slides")
ALTER TABLE filtered_metrics ADD COLUMN IF NOT EXISTS uncertainty TEXT;  -- CI or ± as written
ALTER TABLE filtered_metrics ADD COLUMN IF NOT EXISTS evidence TEXT;     -- table and row, or the sentence, the numbers come from
