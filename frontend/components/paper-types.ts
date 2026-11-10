export type PaperEntry = {
  // Core paper metadata
  url: string
  title?: string | null
  authors?: string | null
  citations?: number | null
  year?: number | null
  journal?: string | null
  abstract?: string | null
  
  // From extraction schema
  methods?: string | null
  datasets_used?: string | null
  preprocessing_methods?: string | null
  augmentation_methods?: string | null
  models_used?: string | null
  model_family?: string | null
  results?: string | null
  processing_pipeline?: any | null
  // What the fields were extracted from: full_text | abstract | partial | title_snippet
  text_source?: string | null
  uses_private_data?: boolean | null
  private_datasets?: string | null  // pipe-separated names
  
  // From classification schema - study characteristics
  domain?: string | null
  biological_domain?: string | null
  modality?: string | null
  wsi_or_patch?: string | null
  tissue_site?: string | null
  species?: string | null
  staining_type?: string | null
  task?: string | null
  annotation_types?: string | null
  labels?: string | null
  
  // Image/scan properties
  mpp?: number | null
  scanner?: string | null
  tile_size_px?: number | null
  tiling_strategy?: string | null
  
  // Dataset size
  n_patients?: number | null
  n_slides?: number | null
  n_tiles?: number | null
  n_centers?: number | null
  class_counts?: any | null
  
  // Study design
  split_level?: string | null
  split_method?: string | null
  external_validation?: boolean | null
  
  // Annotation details (flattened from annotation_provenance)
  n_annotators?: number | null
  annotator_expertise?: string | null
  annotator_consensus?: string | null
  
  // Clinical context
  outcome_endpoint?: string | null
  
  // Preprocessing (flattened from preprocessing)
  
  // Model details (flattened from model_details)
  model_type?: string | null
  backbone?: string | null
  pretraining_strategy?: string | null
  foundation_model?: string | null
  
  // Multimodal flags (flattened from multimodal_flags)
  uses_radiology?: boolean | null
  other_modalities?: string | null
  
  // Data access
  dataset_access?: string | null
  license?: string | null
  resource_urls?: string | null
  
  // Paper metadata (flattened from paper object)
  paper_doi?: string | null
  paper_venue?: string | null
  
  // Detailed extraction arrays (JSONB)
  datasets_used_detailed?: any | null
  models_used_detailed?: any | null
  
  [key: string]: string | number | boolean | any | null | undefined
}

export type PaperFilterState = {
  [key: string]: string[]
}

export type DatasetEntry = {
  // Core dataset metadata
  slug: string
  parent_slug?: string | null  // the dataset this one is a subset of (TCGA-KICH -> TCGA-RCC)
  dataset_name?: string | null
  year?: number | null
  description?: string | null
  is_public?: boolean | null
  
  // URLs
  code_url?: string | null
  download_url?: string | null
  home_page_url?: string | null
  resource_urls?: string | null
  
  // Organization
  institutions?: string | null
  
  // Data access
  license?: string | null
  dataset_access?: string | null
  
  // Domain and modality
  domain?: string | null
  modality?: string | null
  wsi_or_patch?: string | null
  
  // Tissue information
  tissue_site?: string | null
  species?: string | null
  sample_prep?: string | null
  staining_type?: string | null
  
  // Image properties
  mag_min?: number | null
  mag_max?: number | null
  mpp?: number | null
  scanner?: string | null
  image_format?: string | null
  approx_size_gb?: number | null
  
  // Patch/tile properties
  tile_size_px?: number | null
  tiling_strategy?: string | null
  
  // Dataset size
  n_slides?: number | null
  n_patients?: number | null
  n_tiles?: number | null
  n_centers?: number | null
  
  // Task and annotations
  task?: string | null
  labels?: string | null
  annotation_level?: string | null
  annotation_types?: string | null
  annotation_format?: string | null
  
  // Study design
  split_level?: string | null
  split_method?: string | null
  external_validation?: boolean | null
  
  // Paper metadata (flattened from paper object)
  paper_doi?: string | null
  paper_venue?: string | null
  
  // Computed/derived fields
  num_papers?: number
  magnification?: string
  tile_dimensions?: string
  
  [key: string]: string | number | boolean | null | undefined
}

export type FilterState = {
  [key: string]: string[]
}

export type MetricEntry = {
  paper_url: string
  dataset_slug: string
  task: string
  // Classification metrics
  accuracy?: number | null
  f1_score?: number | null
  precision?: number | null
  recall?: number | null
  specificity?: number | null
  npv?: number | null
  auc_roc?: number | null
  auprc?: number | null
  balanced_accuracy?: number | null
  mcc?: number | null
  kappa?: number | null
  macro_f1?: number | null
  micro_f1?: number | null
  weighted_f1?: number | null
  // Segmentation metrics
  dice_coefficient?: number | null
  iou?: number | null
  pixel_accuracy?: number | null
  hd95?: number | null
  aji?: number | null
  // Detection metrics
  map?: number | null
  ap50?: number | null
  ap75?: number | null
  froc?: number | null
  sensitivity_at_1_fp_per_image?: number | null
  sensitivity_at_2_fp_per_image?: number | null
  // Regression metrics
  mae?: number | null
  rmse?: number | null
  r2?: number | null
  spearman_r?: number | null
  pearson_r?: number | null
  // Survival metrics
  c_index?: number | null
  hazard_ratio?: number | null
  time_dependent_auc?: string | null
  ibs?: number | null
}
