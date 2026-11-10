/**
 * Comprehensive field descriptions for hover tooltips in the WSI_Hub frontend.
 * These descriptions explain what each field represents in the context of WSI datasets and papers.
 */

export const FIELD_DESCRIPTIONS: Record<string, string> = {
  // =============================================================================
  // CORE PAPER METADATA
  // =============================================================================
  url: "URL link to the original paper or publication",
  title: "Title of the research paper",
  authors: "List of authors who contributed to the paper",
  citations: "Number of times this paper has been cited by other publications",
  year: "Year the paper or dataset was published",
  journal: "Academic journal where the paper was published",
  abstract: "Summary of the paper's research objectives, methods, and findings",
  methods: "Detailed description of the methodology used in the study",

  // =============================================================================
  // DATASET METADATA
  // =============================================================================
  slug: "Unique identifier (URL-safe name) for the dataset",
  dataset_name: "Human-readable name of the dataset",
  datasets_used: "Names of datasets used or referenced in the study",
  description: "Brief summary of the dataset's purpose, contents, and intended use cases",
  is_public: "Whether the dataset is publicly accessible without restrictions",

  // =============================================================================
  // URLS & RESOURCES
  // =============================================================================
  code_url: "Link to the code repository (e.g., GitHub) with implementation details",
  download_url: "Direct link to download the dataset files",
  home_page_url: "Official homepage or landing page for the dataset",
  resource_urls: "Additional resource links such as documentation, tutorials, or related tools",

  // =============================================================================
  // ORGANIZATION & ACCESS
  // =============================================================================
  institutions: "Research institutions or organizations that created or maintain the dataset",
  license: "License governing data use and distribution (e.g., CC-BY-4.0, CC0, MIT, proprietary)",
  dataset_access: "How to access the data: open (freely available), restricted (requires approval), on-request (contact authors), or proprietary",

  // =============================================================================
  // DOMAIN & MODALITY
  // =============================================================================
  domain: "Primary imaging domain: histopathology (tissue slides), cytology (cell samples), or molecular-pathology",
  biological_domain: "Medical specialty area: oncology (cancer), immunology, neurology, cardiology, dermatology, etc.",
  modality: "Imaging technique used: brightfield microscopy, fluorescence, IHC-DAB staining, multiplex immunofluorescence, etc.",
  wsi_or_patch: "Data format: WSI (whole-slide images), patch/tile (extracted regions), or both",
  other_modalities: "Additional data types beyond histopathology: radiology scans, genomic data, clinical notes, etc.",

  // =============================================================================
  // TISSUE & SAMPLE INFORMATION
  // =============================================================================
  tissue_site: "Anatomical origin of tissue samples (e.g., lung, breast, colon, liver, brain, skin)",
  species: "Biological species of the samples: human, mouse, rat, or other model organisms",
  sample_prep: "How tissue was prepared: FFPE (formalin-fixed paraffin-embedded), frozen section, fresh tissue",
  staining_type: "Staining protocol applied: H&E (hematoxylin & eosin), IHC (immunohistochemistry), PAS, Masson's trichrome, etc.",

  // =============================================================================
  // IMAGE PROPERTIES
  // =============================================================================
  mag_min: "Lowest magnification level available (e.g., 5x, 10x)",
  mag_max: "Highest magnification level available (e.g., 20x, 40x)",
  magnification: "Range of magnification levels available in the dataset",
  mpp: "Microns per pixel (MPP) - spatial resolution where lower values mean higher resolution (e.g., 0.25 MPP ≈ 40x)",
  scanner: "Slide scanner manufacturer and model used for digitization (e.g., Aperio, Hamamatsu, Leica)",
  image_format: "File format for storing images: SVS, NDPI, TIFF, MRXS, or other proprietary formats",
  approx_size_gb: "Approximate total storage size of the dataset in gigabytes",

  // =============================================================================
  // TILE/PATCH PROPERTIES
  // =============================================================================
  tile_size_px: "Dimensions of extracted image tiles/patches in pixels (e.g., 256, 512, 1024)",
  tile_dimensions: "Width × height of tiles in pixels",
  tiling_strategy: "Method for extracting tiles from WSIs: grid-based, tissue-mask guided, hotspot detection, or random sampling",

  // =============================================================================
  // DATASET SIZE & SCALE
  // =============================================================================
  n_patients: "Number of unique patients represented in the dataset",
  n_slides: "Total count of whole-slide images in the dataset",
  n_tiles: "Total number of extracted tiles/patches",
  n_centers: "Number of medical centers or institutions contributing data (indicates multi-site diversity)",
  class_counts: "Distribution of samples across different classes or categories",

  // =============================================================================
  // TASK & ANNOTATIONS
  // =============================================================================
  task: "Primary machine learning task: classification (categorizing), segmentation (pixel-level labeling), detection (locating objects), regression (predicting values), or survival analysis",
  labels: "Class labels or categories used for training and evaluation",
  annotation_level: "Granularity of annotations: slide-level (one label per slide), ROI-level (region of interest), or pixel-level (dense segmentation)",
  annotation_types: "Types of annotation formats: polygons, binary masks, point markers, bounding boxes, or categorical labels",
  annotation_format: "File format for storing annotations: JSON, XML, GeoJSON, CSV, or custom formats",

  // =============================================================================
  // ANNOTATION QUALITY
  // =============================================================================
  n_annotators: "Number of human annotators who labeled the data",
  annotator_expertise: "Qualification level of annotators: pathologist, resident, trained technician, or crowdsourced",
  annotator_consensus: "How disagreements between annotators were resolved: majority vote, expert adjudication, or consensus discussion",

  // =============================================================================
  // STUDY DESIGN & VALIDATION
  // =============================================================================
  split_level: "Unit of data splitting to prevent leakage: patient-level (recommended), case-level, slide-level, or tile-level",
  split_method: "Data splitting strategy: holdout (train/val/test), k-fold cross-validation, leave-one-out, or stratified sampling",
  external_validation: "Whether model performance was validated on data from independent external sources",

  // =============================================================================
  // CLINICAL CONTEXT
  // =============================================================================
  outcome_endpoint: "Clinical outcome being predicted: diagnosis, tumor grade, cancer stage, survival time, recurrence risk, or treatment response",

  // =============================================================================
  // PREPROCESSING & AUGMENTATION
  // =============================================================================
  preprocessing_methods: "Data preprocessing steps applied: tissue detection, background removal, color/stain normalization, artifact filtering",
  augmentation_methods: "Data augmentation techniques used during training: geometric transforms (flips, rotations), color jitter, elastic deformation, mixup",

  // =============================================================================
  // MODEL ARCHITECTURE
  // =============================================================================
  model_family: "General category of model architecture: CNN-based, Transformer-based, MIL (multiple instance learning), hybrid",
  models_used: "Specific model architectures used in the study (e.g., ResNet-50, ViT-B/16, CLAM)",
  model_type: "Architecture paradigm: CNN, Vision Transformer, Graph Neural Network, or hybrid approaches",
  backbone: "Feature extraction backbone network: ResNet, DenseNet, EfficientNet, ViT, Swin Transformer, etc.",
  pretraining_strategy: "How the model was pretrained: ImageNet (natural images), pathology-specific SSL, or foundation model",
  foundation_model: "Pathology foundation model used: UNI, CONCH, Prov-GigaPath, CTransPath, REMEDIS, Phikon, etc.",

  // =============================================================================
  // MULTIMODAL FLAGS
  // =============================================================================
  uses_radiology: "Whether radiology images (CT, MRI, X-ray) were incorporated",

  // =============================================================================
  // PERFORMANCE METRICS
  // =============================================================================
  results: "Summary of key experimental results and performance metrics",

  // =============================================================================
  // PAPER METADATA
  // =============================================================================
  paper_doi: "Digital Object Identifier (DOI) providing a permanent link to the publication",
  paper_venue: "Journal name or conference where the paper was published",

  // =============================================================================
  // PROCESSING PIPELINE
  // =============================================================================
  processing_pipeline: "Sequence of processing steps from raw data to model input",

  // =============================================================================
  // COMPUTED/DERIVED FIELDS
  // =============================================================================
  num_papers: "Count of papers in the database that reference or use this dataset",
  datasets_used_detailed: "Detailed information about datasets used including specific configurations",
  models_used_detailed: "Detailed information about models including hyperparameters and training details",

  // =============================================================================
  // PERFORMANCE METRICS - CLASSIFICATION
  // =============================================================================
  auc: "Area Under the ROC Curve - measures discrimination ability (0.5 = random, 1.0 = perfect)",
  accuracy: "Proportion of correct predictions out of total predictions",
  acc: "Accuracy - proportion of correct predictions out of total predictions",
  f1: "F1 Score - harmonic mean of precision and recall, balances both metrics",
  precision: "Proportion of true positives among all positive predictions (how many selected items are relevant)",
  prec: "Precision - proportion of true positives among all positive predictions",
  recall: "Proportion of true positives among all actual positives (how many relevant items are selected)",
  sensitivity: "Same as recall - proportion of actual positives correctly identified",
  specificity: "Proportion of actual negatives correctly identified",

  // =============================================================================
  // PERFORMANCE METRICS - SEGMENTATION
  // =============================================================================
  dice: "Dice coefficient - overlap between predicted and ground truth segmentation (0-1, higher is better)",
  iou: "Intersection over Union (Jaccard index) - overlap metric for segmentation tasks",
  pixel_accuracy: "Proportion of correctly classified pixels in segmentation",
  px_acc: "Pixel Accuracy - proportion of correctly classified pixels",
  hd95: "95th percentile Hausdorff Distance - measures boundary accuracy in segmentation (lower is better)",

  // =============================================================================
  // PERFORMANCE METRICS - DETECTION
  // =============================================================================
  map: "Mean Average Precision - average precision across all object classes",
  mAP: "Mean Average Precision - average precision across all object classes",
  ap50: "Average Precision at IoU threshold 0.50 - detection accuracy with lenient overlap requirement",
  ap75: "Average Precision at IoU threshold 0.75 - detection accuracy with strict overlap requirement",

  // =============================================================================
  // PERFORMANCE METRICS - SURVIVAL ANALYSIS
  // =============================================================================
  c_index: "Concordance Index - measures ranking accuracy for survival predictions (0.5 = random, 1.0 = perfect)",
  ibs: "Integrated Brier Score - measures calibration and discrimination for survival models (lower is better)",

  // =============================================================================
  // DATASET STATISTICS DISPLAY LABELS
  // =============================================================================
  patients: "Number of unique patients in the dataset",
  slides: "Number of whole-slide images in the dataset",
  tiles: "Number of extracted image tiles/patches",
  centers: "Number of contributing medical centers",
  size: "Total storage size of the dataset",

  // =============================================================================
  // BOOLEAN FLAGS & VALIDATION
  // =============================================================================
  validation: "Validation approach used to assess model performance",

  // =============================================================================
  // ADDITIONAL DISPLAY FIELDS
  // =============================================================================
  access: "Data access level and availability",
  staining: "Staining protocol used for tissue samples",
  type: "Type of imaging data (WSI or patches)",
  preprocessing: "Data preprocessing steps applied before model training",
  augmentation: "Data augmentation techniques used during training",
  annotation_quality: "Quality indicators for the annotation process",
}

/**
 * Get the description for a field, with fallback to formatted field name
 */
export function getFieldDescription(fieldName: string): string | undefined {
  return FIELD_DESCRIPTIONS[fieldName]
}

/**
 * Check if a field has a description
 */
export function hasFieldDescription(fieldName: string): boolean {
  return fieldName in FIELD_DESCRIPTIONS
}
