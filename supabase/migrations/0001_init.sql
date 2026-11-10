-- WSI Hub schema: the three tables `python -m pipeline.src.cli push` writes and the frontend reads.
-- Applied by `push --init-db-url` (pipeline/src/push_to_supabase.py:init_database) or the SQL editor.
-- Plain CREATE TABLE: on a database that already has the tables this fails with "already exists"
-- and nothing is applied; upgrade such a database with 0002 instead.

-- Datasets (pipeline step 2.2, datasets_seed_step2.json). Pipe-separated multi-value text.
CREATE TABLE final_datasets (
  slug TEXT PRIMARY KEY,
  dataset_name TEXT,
  year INTEGER,
  description TEXT,
  code_url TEXT,
  download_url TEXT,
  home_page_url TEXT,
  institutions TEXT,
  license TEXT,
  dataset_access TEXT,
  is_public BOOLEAN,
  wsi_or_patch TEXT,
  tissue_site TEXT,
  domain TEXT,
  modality TEXT,
  species TEXT,
  staining_type TEXT,
  sample_prep TEXT,
  task TEXT,
  annotation_level TEXT,
  annotation_types TEXT,
  annotation_format TEXT,
  labels TEXT,
  mag_min INTEGER,
  mag_max INTEGER,
  mpp NUMERIC,
  scanner TEXT,
  tile_size_px INTEGER,
  tiling_strategy TEXT,
  image_format TEXT,
  approx_size_gb NUMERIC,
  n_slides INTEGER,
  n_patients INTEGER,
  n_tiles BIGINT,
  n_centers INTEGER,
  split_level TEXT,
  split_method TEXT,
  external_validation BOOLEAN,
  resource_urls TEXT,
  paper_doi TEXT,
  paper_venue TEXT,
  parent_slug TEXT  -- dataset this one is a subset of (TCGA-KICH -> TCGA-RCC); no FK, the parent may not be in the catalog
);

-- Papers (pipeline final_extracted.json), keyed by canonical paper URL.
CREATE TABLE papers_dct (
  url TEXT PRIMARY KEY,
  title TEXT,
  authors TEXT,
  year INTEGER,
  journal TEXT,
  citations INTEGER,
  abstract TEXT,
  methods TEXT,
  datasets_used TEXT,
  uses_private_data BOOLEAN,  -- NULL = extracted before private_datasets existed
  private_datasets TEXT,      -- pipe-separated names of non-public data
  preprocessing_methods TEXT,
  augmentation_methods TEXT,
  models_used TEXT,
  model_family TEXT,
  results TEXT,
  processing_pipeline JSONB,  -- 2D list of step labels (sum_paper "Processing Pipeline")
  wsi_or_patch TEXT,
  tissue_site TEXT,
  domain TEXT,
  biological_domain TEXT,
  modality TEXT,
  species TEXT,
  staining_type TEXT,
  text_source TEXT,           -- what extraction read: full_text | abstract | partial | title_snippet
  task TEXT,
  annotation_types TEXT,
  labels TEXT,
  mpp NUMERIC,
  scanner TEXT,
  tile_size_px INTEGER,
  tiling_strategy TEXT,
  n_patients INTEGER,
  n_slides INTEGER,
  n_tiles BIGINT,
  n_centers INTEGER,
  class_counts JSONB,
  split_level TEXT,
  split_method TEXT,
  external_validation BOOLEAN,
  n_annotators INTEGER,
  annotator_expertise TEXT,
  annotator_consensus TEXT,
  outcome_endpoint TEXT,
  time_horizon_months NUMERIC,
  model_type TEXT,
  backbone TEXT,
  pretraining_strategy TEXT,
  foundation_model TEXT,
  uses_radiology BOOLEAN,
  other_modalities TEXT,
  dataset_access TEXT,
  license TEXT,
  resource_urls TEXT,
  paper_doi TEXT,
  paper_venue TEXT,
  datasets_used_detailed JSONB,
  models_used_detailed JSONB
);

-- Metrics (pipeline metrics_final.json): one row per paper, dataset, task, split, evaluation
-- level and subset. metric_key = md5 of those fields (push_to_supabase.metric_key).
CREATE TABLE filtered_metrics (
  metric_key TEXT,
  paper_url TEXT NOT NULL,
  dataset_slug TEXT,          -- NULL for private cohorts or results that match no linked catalog dataset
  task TEXT NOT NULL,
  dataset_reported TEXT,      -- dataset as the paper names it
  split TEXT,                 -- test | external_test | cross_validation | validation | train | other
  eval_level TEXT,            -- patch | tile | slide | patient | pixel | object
  subset TEXT,                -- NULL = overall; else the class or subgroup
  is_primary BOOLEAN DEFAULT TRUE,  -- headline result for its dataset
  text_source TEXT,           -- full_text | abstract | partial: what the numbers were read from
  method TEXT,                -- the paper's name for the method or variant this row is for
  n_eval TEXT,                -- size of the evaluation set as the paper states it ("129 slides")
  uncertainty TEXT,           -- CI or ± as written
  evidence TEXT,              -- table and row, or the sentence, the numbers come from
  -- Classification metrics
  accuracy NUMERIC,
  f1_score NUMERIC,
  precision NUMERIC,
  recall NUMERIC,
  specificity NUMERIC,
  npv NUMERIC,
  auc_roc NUMERIC,
  auprc NUMERIC,
  balanced_accuracy NUMERIC,
  mcc NUMERIC,
  kappa NUMERIC,
  macro_f1 NUMERIC,
  micro_f1 NUMERIC,
  weighted_f1 NUMERIC,
  -- Segmentation metrics
  dice_coefficient NUMERIC,
  iou NUMERIC,
  pixel_accuracy NUMERIC,
  hd95 NUMERIC,
  aji NUMERIC,
  -- Detection metrics
  map NUMERIC,
  ap50 NUMERIC,
  ap75 NUMERIC,
  froc NUMERIC,
  sensitivity_at_1_fp_per_image NUMERIC,
  sensitivity_at_2_fp_per_image NUMERIC,
  -- Regression metrics
  mae NUMERIC,
  rmse NUMERIC,
  r2 NUMERIC,
  spearman_r NUMERIC,
  pearson_r NUMERIC,
  -- Survival metrics
  c_index NUMERIC,
  hazard_ratio NUMERIC,
  time_dependent_auc TEXT,    -- may carry labels, e.g. "AUC@1yr=0.74"
  ibs NUMERIC,
  CONSTRAINT filtered_metrics_metric_key_pkey PRIMARY KEY (metric_key),
  FOREIGN KEY (paper_url) REFERENCES papers_dct(url),
  FOREIGN KEY (dataset_slug) REFERENCES final_datasets(slug)
);

-- Indexes
CREATE INDEX idx_datasets_tissue_site ON final_datasets(tissue_site);
CREATE INDEX idx_datasets_year ON final_datasets(year);
CREATE INDEX idx_papers_year ON papers_dct(year);
CREATE INDEX idx_papers_citations ON papers_dct(citations);
CREATE INDEX idx_metrics_paper_url ON filtered_metrics(paper_url);
CREATE INDEX idx_metrics_dataset_slug ON filtered_metrics(dataset_slug);
CREATE INDEX idx_metrics_primary ON filtered_metrics(dataset_slug) WHERE is_primary;

-- Public read-only access through the Data API; the push writes with the service-role key.
ALTER TABLE final_datasets ENABLE ROW LEVEL SECURITY;
ALTER TABLE papers_dct ENABLE ROW LEVEL SECURITY;
ALTER TABLE filtered_metrics ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Allow public read access" ON final_datasets
  FOR SELECT USING (true);

CREATE POLICY "Allow public read access" ON papers_dct
  FOR SELECT USING (true);

CREATE POLICY "Allow public read access" ON filtered_metrics
  FOR SELECT USING (true);

-- Supabase projects may not grant new public tables to the API roles by default.
-- Skipped on a plain Postgres without those roles.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon')
     AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated')
     AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
    GRANT SELECT ON final_datasets, papers_dct, filtered_metrics TO anon, authenticated;
    GRANT SELECT, INSERT, UPDATE, DELETE ON final_datasets, papers_dct, filtered_metrics TO service_role;
  END IF;
END $$;
