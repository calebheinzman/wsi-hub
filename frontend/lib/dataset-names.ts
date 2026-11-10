import { useEffect, useState } from "react"
import { fetchAllRows } from "@/lib/supabase"

// Display names for datasets. The catalog's dataset_name was lowercase until the 2.2 prompt
// kept capitals (2026-10-28), and papers list datasets by slug ("tcga-brca|imp-crs"), so a
// plain title-caser gave "Acdc@lunghp" and "Tcga-Brca". A name that already has capitals is
// shown as written; a lowercase name or a slug is cased word by word: known dataset names
// and acronyms as their owners write them, vowel-less tokens (bcnb, dhmc) in capitals, the
// rest capitalized.

const KNOWN: Record<string, string> = {
  // TCGA project codes and groupings
  ...Object.fromEntries(
    ("acc blca brca cesc chol coad coadread dlbc esca gbm gbmlgg hnsc kich kirc kirp laml lgg lihc luad lusc " +
      "meso ov paad pcpg prad read sarc skcm stad tgct thca thym ucec ucs uvm nsclc rcc crc")
      .split(" ").map((c) => [c, c.toUpperCase()])
  ),
  tcga: "TCGA", cptac: "CPTAC", tcia: "TCIA", gdc: "GDC", gtex: "GTEx", nlst: "NLST", plco: "PLCO", nci: "NCI",
  hubmap: "HuBMAP", hest: "HEST", kpmp: "KPMP", ebrains: "EBRAINS",
  // datasets and challenges
  aggc: "AGGC", acdc: "ACDC", acrobat: "ACROBAT", aidpath: "AIDPATH", bach: "BACH", bcnb: "BCNB", bcss: "BCSS",
  bracs: "BRACS", breakhis: "BreaKHis", camelyon: "CAMELYON", cobra: "COBRA", cocahis: "CoCaHis", conic: "CoNIC",
  consep: "CoNSeP", crag: "CRAG", dhmc: "DHMC", digestpath: "DigestPath", glas: "GlaS", lizard: "Lizard",
  lunghp: "LungHP", mco: "MCO", mhist: "MHIST", monuseg: "MoNuSeg", nct: "NCT", ocdc: "OCDC", orca: "ORCA",
  paip: "PAIP", panda: "PANDA", pannuke: "PanNuke", pcam: "PCam", sicap: "SICAP", sicapv2: "SICAPv2",
  surgen: "SurGen", tiger: "TIGER", tupac: "TUPAC", wsss4luad: "WSSS4LUAD", xenium: "Xenium", visium: "Visium",
  kpis: "KPIs", imp: "IMP", ipd: "IPD", nmi: "NMI", bccc: "BCCC", isbi: "ISBI", miccai: "MICCAI", icpr: "ICPR",
  iciar: "ICIAR", ash: "ASH", catch: "CATCH", monusac: "MoNuSAC", patchcamelyon: "PatchCamelyon",
  breastpathq: "BreastPathQ", brecahad: "BreCaHAD", bbbc: "BBBC", lc: "LC", unitopatho: "UniToPatho", ubc: "UBC",
  gse: "GSE", empiar: "EMPIAR", dsb: "DSB", brats: "BraTS", cifar: "CIFAR", anhir: "ANHIR", idb: "IDB", imc: "IMC",
  til: "TIL", cmb: "CMB", lca: "LCA", hmu: "HMU", mnist: "MNIST", pathmnist: "PathMNIST", codex: "CODEX",
  shidc: "SHIDC", nucls: "NuCLS", nuinsseg: "NuInsSeg", cryonuseg: "CryoNuSeg", midog: "MIDOG", peso: "PESO",
  puma: "PUMA", oasis: "OASIS", neurips: "NeurIPS", livecell: "LIVECell", tissuenet: "TissueNet",
  deepliif: "DeepLIIF", segpath: "SegPath", tracerx: "TRACERx", ptrc: "PTRC", hgsoc: "HGSOC", icgc: "ICGC",
  liri: "LIRI", prjca: "PRJCA", lynsec: "LyNSeC", lyon: "LYON", herohe: "HEROHE", ihc4bc: "IHC4BC", bci: "BCI",
  bccd: "BCCD", bcdata: "BCData", cnseg: "CNSeg", cpm: "CPM", uzh: "UZH", yeaz: "YeaZ", crchisto: "CRCHisto",
  // general acronyms
  wsi: "WSI", tma: "TMA", ihc: "IHC", he: "HE", ffpe: "FFPE", her2: "HER2", ki67: "Ki-67", ai: "AI", ml: "ML",
  ct: "CT", mri: "MRI", uk: "UK", usa: "USA", roi: "ROI", rois: "ROIs",
}
const SMALL = new Set(["a", "an", "and", "of", "for", "the", "in", "on", "with", "to", "by", "or", "at", "from",
  "et", "al", "vs"])

function caseToken(token: string, first: boolean): string {
  const t = token.toLowerCase()
  if (KNOWN[t]) return KNOWN[t]
  const withNumber = t.match(/^([a-z]+)(\d+[a-z]?)$/) // camelyon16, digestpath2019, crc100k
  if (withNumber && KNOWN[withNumber[1]]) return KNOWN[withNumber[1]] + withNumber[2].toUpperCase()
  if (/^\d+[km]$/.test(t)) return t.toUpperCase() // 100k, 1m
  if (/^\d/.test(t)) return t
  if (!first && SMALL.has(t)) return t
  if (t.length <= 5 && !/[aeiouy]/.test(t)) return t.toUpperCase() // bcnb, dhmc, crs
  return t.charAt(0).toUpperCase() + t.slice(1)
}

const isAcronym = (s: string) => /^[A-Z0-9&]+[a-z]?$/.test(s) && /[A-Z]/.test(s)

/** A dataset slug ("tcga-brca", "xenium-breast-cancer-rep1") as a readable name. */
export function formatDatasetSlug(slug: string): string {
  // A lone letter in a slug labels a part (dataset-a-segmentation), not the article "a".
  const parts = (slug || "").split("-").filter(Boolean)
    .map((p, i) => (/^[a-z]$/.test(p) ? p.toUpperCase() : caseToken(p, i === 0)))
  // Keep a dash between acronyms (TCGA-BRCA, NCT-CRC-HE-100K); elsewhere a space.
  return parts.reduce((out, p, i) => (i === 0 ? p : out + (isAcronym(parts[i - 1]) && isAcronym(p) ? "-" : " ") + p), "")
}

/** A dataset's display name: as written when it has capitals, otherwise cased word by word. */
export function formatDatasetName(name?: string | null, slug?: string | null): string {
  const raw = (name || "").trim()
  if (!raw) return formatDatasetSlug(slug || "")
  if (/[A-Z]/.test(raw)) return raw
  let first = true
  return raw.replace(/[a-z0-9]+(?:'[a-z]+)?/g, (word) => {
    const cased = caseToken(word, first)
    first = false
    return cased
  })
}

// Catalog names by slug, fetched once per page load. Paged: an unranged select stopped at
// 1000 of the 1,770 datasets, so the rest fell back to formatted slugs.
let namesPromise: Promise<Record<string, string>> | null = null

export function loadDatasetNames(): Promise<Record<string, string>> {
  namesPromise ??= (async () => {
    const data = await fetchAllRows<{ slug: string; dataset_name: string | null }>(
      "final_datasets",
      "slug,dataset_name",
      { orderBy: "slug" }
    )
    return Object.fromEntries(data.map((d) => [d.slug, formatDatasetName(d.dataset_name, d.slug)]))
  })().catch(() => ({}))
  return namesPromise
}

/** Display names for slugs, and whether a slug has a page in the dataset catalog. */
export function useDatasetCatalog(): { name: (slug: string) => string; has: (slug: string) => boolean } {
  const [names, setNames] = useState<Record<string, string>>({})
  useEffect(() => {
    let live = true
    loadDatasetNames().then((n) => live && setNames(n))
    return () => {
      live = false
    }
  }, [])
  return {
    name: (slug: string) => names[slug] || formatDatasetSlug(slug),
    has: (slug: string) => Object.hasOwn(names, slug),
  }
}

/** The display name for a slug: the catalog's name when it has one, else the formatted slug. */
export function useDatasetNames(): (slug: string) => string {
  return useDatasetCatalog().name
}

/** The dataset explorer's page for a slug. */
export const datasetHref = (slug: string) => `/?dataset=${encodeURIComponent(slug)}`
