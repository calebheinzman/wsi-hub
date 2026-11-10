import { clsx, type ClassValue } from 'clsx'
import { twMerge } from 'tailwind-merge'

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

export function formatValueLabel(input: string): string {
  const s = (input ?? '').trim()
  if (!s) return s
  const lower = s.toLowerCase()
  const specials: Record<string, string> = {
    'wsi': 'WSI',
    'ihc': 'IHC',
    'h&e': 'H&E',
    'he': 'H&E',
    'ffpe': 'FFPE',
    'cnn': 'CNN',
    'rnn': 'RNN',
    'mlp': 'MLP',
    'ai': 'AI',
    'roi': 'ROI',
    'auc': 'AUC',
    'auprc': 'AUPRC',
    'mil': 'MIL',
    'vit': 'ViT',
    'gnn': 'GNN',
    'gan': 'GAN',
    'svm': 'SVM',
    'llm': 'LLM',
    'vlm': 'VLM',
    'ssl': 'SSL',
    'tma': 'TMA',
    'dab': 'DAB',
    'ihc-dab': 'IHC-DAB',
  }
  if (specials[lower]) return specials[lower]

  const small = new Set(['and', 'or', 'of', 'the', 'in', 'on', 'for', 'to', 'with', 'by'])

  const titleToken = (token: string) => token.split('-').map(p => p ? p[0].toUpperCase() + p.slice(1) : p).join('-')

  const words = s.split(/\s+/)
  const cased = words.map((w, i) => {
    const lw = w.toLowerCase()
    if (specials[lw]) return specials[lw]
    if (i !== 0 && small.has(lw)) return lw
    return titleToken(lw)
  })
  return cased.join(' ')
}

/**
 * Download rows as a CSV file. A Blob, not a data: URL: encodeURI leaves "#" alone, so the
 * first "#" in any value started a URL fragment and cut the file off there (the paper export
 * stopped after 63 of 370 rows). Empty values are blank and objects are JSON, rather than
 * "null" and "[object Object]". Columns are the union of every row's keys.
 */
export function downloadCsv(rows: Record<string, unknown>[], filename: string): void {
  if (rows.length === 0) return
  const columns = Array.from(new Set(rows.flatMap((row) => Object.keys(row))))
  const cell = (value: unknown) => {
    if (value === null || value === undefined) return ""
    const text = typeof value === "object" ? JSON.stringify(value) : String(value)
    return `"${text.replace(/"/g, '""')}"`
  }
  const csv = [columns.join(","), ...rows.map((row) => columns.map((c) => cell(row[c])).join(","))].join("\n")
  const url = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }))
  const link = document.createElement("a")
  link.href = url
  link.download = filename
  document.body.appendChild(link)
  link.click()
  document.body.removeChild(link)
  URL.revokeObjectURL(url)
}
