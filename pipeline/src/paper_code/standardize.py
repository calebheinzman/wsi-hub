"""
Standardization utilities for journals, labels, and other metadata.
"""

import re
from typing import Optional, List


# Journal name standardization mappings
JOURNAL_ABBREVIATIONS = {
    # Nature journals
    'nat med': 'Nature Medicine',
    'nature med': 'Nature Medicine',
    'nat. med.': 'Nature Medicine',
    'nat commun': 'Nature Communications',
    'nature commun': 'Nature Communications',
    'nat. commun.': 'Nature Communications',
    'nat methods': 'Nature Methods',
    'nat. methods': 'Nature Methods',
    'nat biomed eng': 'Nature Biomedical Engineering',
    'nat. biomed. eng.': 'Nature Biomedical Engineering',
    
    # Medical imaging journals
    'ieee tmi': 'IEEE Transactions on Medical Imaging',
    'ieee trans med imaging': 'IEEE Transactions on Medical Imaging',
    'ieee trans. med. imaging': 'IEEE Transactions on Medical Imaging',
    'med image anal': 'Medical Image Analysis',
    'med. image anal.': 'Medical Image Analysis',
    'j pathol inform': 'Journal of Pathology Informatics',
    'j. pathol. inform.': 'Journal of Pathology Informatics',
    
    # Pathology journals
    'am j pathol': 'American Journal of Pathology',
    'am. j. pathol.': 'American Journal of Pathology',
    'j pathol': 'Journal of Pathology',
    'j. pathol.': 'Journal of Pathology',
    'mod pathol': 'Modern Pathology',
    'mod. pathol.': 'Modern Pathology',
    'arch pathol lab med': 'Archives of Pathology & Laboratory Medicine',
    'arch. pathol. lab. med.': 'Archives of Pathology & Laboratory Medicine',
    
    # Computer vision/ML journals
    'ieee tpami': 'IEEE Transactions on Pattern Analysis and Machine Intelligence',
    'ieee trans pattern anal mach intell': 'IEEE Transactions on Pattern Analysis and Machine Intelligence',
    'ieee trans. pattern anal. mach. intell.': 'IEEE Transactions on Pattern Analysis and Machine Intelligence',
    'int j comput vis': 'International Journal of Computer Vision',
    'int. j. comput. vis.': 'International Journal of Computer Vision',
    'ijcv': 'International Journal of Computer Vision',
    
    # Medical journals
    'lancet': 'The Lancet',
    'lancet oncol': 'The Lancet Oncology',
    'lancet digit health': 'The Lancet Digital Health',
    'jama': 'JAMA',
    'jama oncol': 'JAMA Oncology',
    'nejm': 'New England Journal of Medicine',
    'n engl j med': 'New England Journal of Medicine',
    'n. engl. j. med.': 'New England Journal of Medicine',
    
    # Cancer journals
    'cancer res': 'Cancer Research',
    'cancer cell': 'Cancer Cell',
    'clin cancer res': 'Clinical Cancer Research',
    'clin. cancer res.': 'Clinical Cancer Research',
    
    # AI/ML journals
    'nat mach intell': 'Nature Machine Intelligence',
    'nat. mach. intell.': 'Nature Machine Intelligence',
    
    # Conference proceedings (common venues)
    'cvpr': 'CVPR',
    'iccv': 'ICCV',
    'eccv': 'ECCV',
    'nips': 'NeurIPS',
    'neurips': 'NeurIPS',
    'icml': 'ICML',
    'iclr': 'ICLR',
    'aaai': 'AAAI',
    'miccai': 'MICCAI',
    'midl': 'MIDL',
    'isbi': 'ISBI',
    'ipmi': 'IPMI',
}


def standardize_journal(journal: Optional[str]) -> Optional[str]:
    """
    Standardize journal names to canonical forms.
    
    Args:
        journal: Raw journal name from paper metadata
        
    Returns:
        Standardized journal name or original if no mapping found
    """
    if not journal:
        return None
    
    # Clean the input
    cleaned = journal.strip()
    if not cleaned:
        return None
    
    # Normalize for lookup
    normalized = cleaned.lower()
    normalized = re.sub(r'\s+', ' ', normalized)  # Collapse whitespace
    normalized = re.sub(r'[,;]', '', normalized)  # Remove punctuation
    
    # Check for exact match in abbreviations
    if normalized in JOURNAL_ABBREVIATIONS:
        return JOURNAL_ABBREVIATIONS[normalized]
    
    # Check for partial matches (e.g., "proceedings of CVPR 2023" -> "CVPR")
    for abbrev, full_name in JOURNAL_ABBREVIATIONS.items():
        if abbrev in normalized:
            return full_name
    
    # Return original with cleaned whitespace if no match
    return cleaned


# Label standardization mappings
LABEL_STANDARDIZATIONS = {
    # Tissue types
    'breast': 'breast',
    'lung': 'lung',
    'colon': 'colon',
    'colorectal': 'colorectal',
    'prostate': 'prostate',
    'liver': 'liver',
    'kidney': 'kidney',
    'brain': 'brain',
    'skin': 'skin',
    'lymph node': 'lymph node',
    'lymph nodes': 'lymph node',
    'bone marrow': 'bone marrow',
    
    # Cancer types
    'adenocarcinoma': 'adenocarcinoma',
    'squamous cell carcinoma': 'squamous cell carcinoma',
    'scc': 'squamous cell carcinoma',
    'carcinoma': 'carcinoma',
    'melanoma': 'melanoma',
    'glioma': 'glioma',
    'glioblastoma': 'glioblastoma',
    'gbm': 'glioblastoma',
    'lymphoma': 'lymphoma',
    'leukemia': 'leukemia',
    
    # Diagnostic labels
    'benign': 'benign',
    'malignant': 'malignant',
    'normal': 'normal',
    'tumor': 'tumor',
    'tumour': 'tumor',
    'cancer': 'cancer',
    'metastasis': 'metastasis',
    'metastatic': 'metastatic',
    
    # Grading
    'grade 1': 'grade 1',
    'grade 2': 'grade 2',
    'grade 3': 'grade 3',
    'grade 4': 'grade 4',
    'low grade': 'low grade',
    'high grade': 'high grade',
    'well differentiated': 'well differentiated',
    'moderately differentiated': 'moderately differentiated',
    'poorly differentiated': 'poorly differentiated',
    
    # Staging
    'stage i': 'stage I',
    'stage ii': 'stage II',
    'stage iii': 'stage III',
    'stage iv': 'stage IV',
    'stage 1': 'stage I',
    'stage 2': 'stage II',
    'stage 3': 'stage III',
    'stage 4': 'stage IV',
}


def standardize_label(label: Optional[str]) -> Optional[str]:
    """
    Standardize label names to canonical forms.
    
    Args:
        label: Raw label from dataset or paper
        
    Returns:
        Standardized label or original if no mapping found
    """
    if not label:
        return None
    
    # Clean the input
    cleaned = label.strip()
    if not cleaned:
        return None
    
    # Normalize for lookup
    normalized = cleaned.lower()
    normalized = re.sub(r'\s+', ' ', normalized)  # Collapse whitespace
    normalized = re.sub(r'[_-]', ' ', normalized)  # Replace underscores/hyphens with spaces
    
    # Check for exact match
    if normalized in LABEL_STANDARDIZATIONS:
        return LABEL_STANDARDIZATIONS[normalized]
    
    # Check for partial matches
    for pattern, standard in LABEL_STANDARDIZATIONS.items():
        if pattern in normalized:
            return standard
    
    # Return original with cleaned whitespace if no match
    return cleaned


def standardize_labels_list(labels: Optional[List[str]]) -> Optional[List[str]]:
    """
    Standardize a list of labels.
    
    Args:
        labels: List of raw labels
        
    Returns:
        List of standardized labels with duplicates removed
    """
    if not labels:
        return None
    
    standardized = []
    seen = set()
    
    for label in labels:
        std_label = standardize_label(label)
        if std_label and std_label.lower() not in seen:
            standardized.append(std_label)
            seen.add(std_label.lower())
    
    return standardized if standardized else None


def standardize_pipe_separated_labels(labels_str: Optional[str]) -> Optional[str]:
    """
    Standardize pipe-separated labels string.
    
    Args:
        labels_str: Pipe-separated labels string
        
    Returns:
        Pipe-separated standardized labels
    """
    if not labels_str:
        return None
    
    labels = [l.strip() for l in labels_str.split('|') if l.strip()]
    standardized = standardize_labels_list(labels)
    
    if standardized:
        return '|'.join(standardized)
    
    return None


# The one staining_type vocabulary, shared by papers (1.4 classification), datasets
# (1.4 dataset_details and 2.2 metadata) and models. The schemas' patterns list the
# same values; test_staining_type.py checks they agree.
STAINING_TYPES = ("h&e", "ihc", "pap-stain", "giemsa", "pas", "trichrome", "silver",
                  "multiplex-if", "unstained", "other")

# Spellings seen in extractions -> the shared value. Keys are lowercased with
# everything except letters, digits and '&' removed.
STAINING_SYNONYMS = {
    "he": "h&e", "hande": "h&e", "hematoxylinandeosin": "h&e",
    "haematoxylinandeosin": "h&e", "hematoxylineosin": "h&e", "haematoxylineosin": "h&e",
    "hes": "h&e", "hematoxylineosinsaffron": "h&e",
    "immunohistochemistry": "ihc", "immunohistochemical": "ihc", "ihcdab": "ihc", "dab": "ihc",
    "pap": "pap-stain", "papstain": "pap-stain", "papanicolaou": "pap-stain", "papanicolaoustain": "pap-stain",
    "multiplexif": "multiplex-if", "mif": "multiplex-if", "multipleximmunofluorescence": "multiplex-if",
    "multiplexedimmunofluorescence": "multiplex-if",
    "periodicacidschiff": "pas",
    "masson": "trichrome", "massontrichrome": "trichrome", "massonstrichrome": "trichrome", "mt": "trichrome",
    "silverstain": "silver", "pasm": "silver", "jones": "silver", "gms": "silver", "grocott": "silver",
    "labelfree": "unstained", "nostain": "unstained",
}
# Words that mean "the source did not say" -- empty, not 'other'.
_STAINING_UNSTATED = {"", "na", "nan", "none", "null", "unknown", "unspecified", "notreported", "notstated"}


def _stain_key(token: str) -> str:
    return re.sub(r"[^a-z0-9&]", "", token.lower())


_STAINING_BY_KEY = {_stain_key(v): v for v in STAINING_TYPES}
_STAINING_BY_KEY.update({_stain_key(k): v for k, v in STAINING_SYNONYMS.items()})


def normalize_staining_type(value) -> str:
    """Map a raw staining_type (pipe string or list) onto STAINING_TYPES.

    Known spellings map to their shared value, a stated stain outside the list becomes
    'other', and unstated stays ''. Values are deduplicated and sorted alphabetically.
    """
    if value is None:
        return ""
    parts = value if isinstance(value, (list, tuple)) else re.split(r"[|,;/]+", str(value))
    out = set()
    for part in parts:
        key = _stain_key(str(part))
        if key in _STAINING_UNSTATED:
            continue
        out.add(_STAINING_BY_KEY.get(key, "other"))
    return "|".join(sorted(out))
