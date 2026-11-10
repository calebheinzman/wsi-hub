"use client"

import { useState, useEffect, useCallback } from "react"
import ReactMarkdown from "react-markdown"
import { useRouter } from "next/navigation"
import {
  ExternalLink,
  Users,
  Microscope,
  FileText,
  Database,
  Layers,
  Tag,
  FlaskConical,
  Cpu,
  Ruler,
  Target,
  Shield,
  Award,
  Dna,
  BookOpen,
  Hash,
  Download,
  Brain,
  Activity,
  Workflow,
  Crosshair,
  ChevronLeft,
  ChevronRight,
  type LucideIcon,
} from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { formatValueLabel } from "@/lib/utils"
import { supabase } from "@/lib/supabase"
import { reportedMetrics, textSourceNote } from "@/lib/metrics"
import {
  SemanticBadge,
  SemanticBadgeList,
  parseBadgeValues,
} from "@/components/semantic-badge"
import { FieldLabel, FieldSectionLabel } from "@/components/field-label"
import {
  HeroHeader,
  BigStat,
  MagnificationScale,
  IconAttribute,
  CollapsibleSection,
  MetadataRailItem,
  ProcessingFlow,
} from "@/components/ui-enhanced"
import type { PaperEntry } from "./paper-types"
import { datasetHref, useDatasetCatalog } from "@/lib/dataset-names"

interface PaperDetailViewProps {
  paper: PaperEntry
  onClose: () => void
  onFilterByField?: (field: string, value: string) => void
  position?: number
  totalItems?: number
  onGoPrevious?: () => void
  onGoNext?: () => void
}

interface MetricRecord {
  task: string
  dataset_slug: string | null
  // Which evaluation the row is: one row per dataset, split, level and subset.
  dataset_reported?: string | null
  split?: string | null
  eval_level?: string | null
  subset?: string | null
  is_primary?: boolean | null
  text_source?: string | null
  auc_roc?: number | null
  auprc?: number | null
  macro_f1?: number | null
  micro_f1?: number | null
  weighted_f1?: number | null
  f1_score?: number | null
  balanced_accuracy?: number | null
  accuracy?: number | null
  precision?: number | null
  recall?: number | null
  specificity?: number | null
  npv?: number | null
  mcc?: number | null
  kappa?: number | null
  dice_coefficient?: number | null
  iou?: number | null
  pixel_accuracy?: number | null
  hd95?: number | null
  aji?: number | null
  map?: number | null
  ap50?: number | null
  ap75?: number | null
  froc?: number | null
  sensitivity_at_1_fp_per_image?: number | null
  sensitivity_at_2_fp_per_image?: number | null
  mae?: number | null
  rmse?: number | null
  r2?: number | null
  spearman_r?: number | null
  pearson_r?: number | null
  c_index?: number | null
  hazard_ratio?: number | null
  time_dependent_auc?: string | null
  ibs?: number | null
}

// Check if a value is meaningful
const isMeaningful = (value: any): boolean => {
  if (value === null || value === undefined) return false
  const raw = String(value).trim()
  if (!raw) return false
  const lower = raw.toLowerCase()
  if (["nan", "none", "null", "unknown", "n/a", "na"].includes(lower)) return false
  const num = Number(raw)
  if (!isNaN(num) && num === 0) return false
  return true
}

// Normalize processing_pipeline JSON (2D array of strings) from Supabase
const parseProcessingPipeline = (raw: any): string[][] => {
  if (!raw) return []

  let value: any = raw
  if (typeof value === "string") {
    try {
      value = JSON.parse(value)
    } catch {
      return []
    }
  }

  if (!Array.isArray(value)) return []

  const pipelines: string[][] = []
  for (const p of value) {
    if (Array.isArray(p)) {
      const steps = p
        .map((s) => String(s ?? "").trim())
        .filter((s) => s.length > 0)
      if (steps.length > 0) {
        pipelines.push(steps)
      }
    }
  }

  return pipelines
}

// Normalize methods/results text from various legacy formats into readable markdown
const formatMethodsText = (methods: unknown): string => {
  if (!methods) return ""

  // Handle pre-parsed object/array (from JSON column)
  if (typeof methods === "object") {
    // Special case: array with a single string value (common for methods/results fields).
    // In this case, run the string through the same formatter logic so inline
    // dash-separated labels become proper bullets.
    if (Array.isArray(methods) && methods.length === 1 && typeof methods[0] === "string") {
      return formatMethodsText(methods[0])
    }

    return formatParsedValue(methods)
  }

  const raw = String(methods).trim()

  // Check for [object Object] which means a serialization issue
  if (raw === "[object Object]" || raw.includes("[object Object]")) {
    return ""
  }

  // If it's already markdown-style prose with multiple bullet lines, return as-is
  // (we only check for newline-separated bullets to avoid missing inline patterns
  // like "- **Data**: ... - **Preprocessing**: ..." which we still want to
  // normalize into separate bullets.)
  if (/\n-\s/.test(raw)) {
    return raw
  }

  // Try parsing as JSON (array or object)
  if ((raw.startsWith("[") && raw.endsWith("]")) || (raw.startsWith("{") && raw.endsWith("}"))) {
    try {
      const parsed = JSON.parse(raw)
      return formatParsedValue(parsed)
    } catch {
      // Not valid JSON, return as-is
      return raw
    }
  }

  // Handle inline label patterns like
  // "Data: ... - Preprocessing: ... - Model: ... - Training: ... - Evaluation: ..."
  // or results variants like "4-Class Accuracy:", "2-Class Accuracy:", etc. These
  // sometimes come in as a single long line or with stray newlines, so we
  // normalize whitespace first.
  const normalized = raw.replace(/\s+/g, " ").trim()

  // First, try splitting on known label tokens so each label + description becomes
  // its own bullet.
  const labelRegex = /(Data:|Preprocessing:|Model:|Training:|Evaluation:|4-Class Accuracy:|2-Class Accuracy:|Carcinoma Sensitivity:|Key Finding:)/g
  const labelMatches = Array.from(normalized.matchAll(labelRegex))

  if (labelMatches.length >= 2 || (labelMatches.length === 1 && labelMatches[0].index === 0)) {
    const segments: string[] = []
    for (let i = 0; i < labelMatches.length; i++) {
      const start = labelMatches[i].index ?? 0
      const end = i + 1 < labelMatches.length ? (labelMatches[i + 1].index ?? normalized.length) : normalized.length
      const segment = normalized.slice(start, end).trim()
      if (segment) segments.push(segment)
    }
    if (segments.length > 0) {
      return segments
        .map(seg => `- ${seg}`)
        .join("\n")
    }
  }

  // Fallback: split on dash/en-dash separators between clauses.
  if (/[\s][-–][\s]/.test(normalized)) {
    const parts = normalized
      .split(/\s[-–]\s+/)
      .map(part => part.trim())
      .filter(Boolean)

    if (parts.length > 1) {
      return parts
        .map(part => {
          // Strip any leading dash/en-dash the part might still have
          const cleaned = part.replace(/^[\-–]\s+/, "").trim()
          return cleaned ? `- ${cleaned}` : ""
        })
        .filter(Boolean)
        .join("\n")
    }
  }

  return raw
}

// Convert parsed JSON value (object or array) into markdown
const formatParsedValue = (value: unknown): string => {
  if (!value) return ""

  // Handle array of strings
  if (Array.isArray(value)) {
    const lines: string[] = []
    for (const item of value) {
      if (item == null) continue

      if (typeof item === "string") {
        // Simple string item
        let s = item.trim()
        s = s.replace(/^"/, "").replace(/"$/, "")
        s = s.replace(/^[-•]\s*/, "")
        if (s) lines.push(`- ${s}`)
      } else if (typeof item === "object") {
        // Object item like { name: "...", details: "..." }
        const formatted = formatObjectToBullet(item as Record<string, unknown>)
        if (formatted) lines.push(formatted)
      }
    }
    return lines.join("\n")
  }

  // Handle plain object with key-value pairs
  if (typeof value === "object" && value !== null) {
    return formatObjectToBullets(value as Record<string, unknown>)
  }

  return String(value)
}

// Convert an object to markdown bullets
const formatObjectToBullets = (obj: Record<string, unknown>): string => {
  const lines: string[] = []

  for (const [key, val] of Object.entries(obj)) {
    if (val == null || val === "") continue

    const label = formatLabel(key)

    if (typeof val === "string") {
      lines.push(`- **${label}**: ${val}`)
    } else if (Array.isArray(val)) {
      // Array value - join as inline list or nested bullets
      const items = val.filter(v => v != null && v !== "").map(v => {
        if (typeof v === "object") {
          return formatObjectToBullet(v as Record<string, unknown>)
        }
        return String(v)
      })
      if (items.length === 1) {
        lines.push(`- **${label}**: ${items[0]}`)
      } else if (items.length > 1) {
        lines.push(`- **${label}**:`)
        for (const item of items) {
          lines.push(`  - ${item}`)
        }
      }
    } else if (typeof val === "object") {
      // Nested object
      const nested = formatObjectToBullets(val as Record<string, unknown>)
      if (nested) {
        lines.push(`- **${label}**:`)
        for (const line of nested.split("\n")) {
          lines.push(`  ${line}`)
        }
      }
    } else {
      lines.push(`- **${label}**: ${String(val)}`)
    }
  }

  return lines.join("\n")
}

// Convert a single object to one bullet line
const formatObjectToBullet = (obj: Record<string, unknown>): string => {
  // Common patterns: { name: "...", details: "..." }, { method: "...", result: "..." }
  const nameKeys = ["name", "method", "model_type", "type", "approach"]
  const detailKeys = ["details", "result", "description", "purpose", "value"]

  let name = ""
  let details = ""

  for (const k of nameKeys) {
    if (obj[k] && typeof obj[k] === "string") {
      name = obj[k] as string
      break
    }
  }

  for (const k of detailKeys) {
    if (obj[k] && typeof obj[k] === "string") {
      details = obj[k] as string
      break
    }
  }

  if (name && details) {
    return `- **${name}**: ${details}`
  }

  // Fallback: serialize all key-values
  const parts = Object.entries(obj)
    .filter(([, v]) => v != null && v !== "")
    .map(([k, v]) => `**${formatLabel(k)}**: ${typeof v === "object" ? JSON.stringify(v) : v}`)
    .join("; ")

  return parts ? `- ${parts}` : ""
}

// Convert snake_case or camelCase to Title Case
const formatLabel = (key: string): string => {
  return key
    .replace(/_/g, " ")
    .replace(/([a-z])([A-Z])/g, "$1 $2")
    .replace(/\b\w/g, c => c.toUpperCase())
}


// Section component for main content
const ContentSection = ({
  icon: Icon,
  title,
  children,
  className,
}: {
  icon?: LucideIcon
  title: string
  children: React.ReactNode
  className?: string
}) => (
  <div className={className}>
    <h3 className="flex items-center gap-2 text-sm font-semibold uppercase tracking-wide text-muted-foreground mb-3">
      {Icon && <Icon className="h-4 w-4" />}
      {title}
    </h3>
    {children}
  </div>
)

// Metric card for performance metrics
const MetricCard = ({
  label,
  value,
}: {
  label: string
  value: number | string | null | undefined
}) => {
  if (value === null || value === undefined) return null
  const numValue = typeof value === "number" ? value : Number(String(value).trim())
  // Not a plain number (a time-dependent AUC carries its horizon, "AUC@1yr=0.74"): show it as given.
  const shown = isNaN(numValue) ? String(value) : numValue.toFixed(3)

  return (
    <div className="bg-white border rounded-lg p-3 text-center">
      <div className="text-xl font-bold text-foreground">{shown}</div>
      <div className="text-xs text-muted-foreground uppercase">{label}</div>
    </div>
  )
}

// Class distribution pie
const ClassDistributionPie = ({ entries }: { entries: [string, number][] }) => {
  const total = entries.reduce((sum, [, value]) => sum + (typeof value === "number" ? value : 0), 0)
  if (!total) return null

  const radius = 16
  const circumference = 2 * Math.PI * radius
  let offset = 0
  const colors = ["#2563eb", "#16a34a", "#f97316", "#ec4899", "#a855f7", "#facc15"]

  return (
    <svg viewBox="0 0 48 48" className="w-16 h-16">
      {entries.map(([label, value], index) => {
        const amount = typeof value === "number" ? value : 0
        if (!amount) return null
        const fraction = amount / total
        const strokeDasharray = `${fraction * circumference} ${circumference}`
        const strokeDashoffset = -offset
        offset += fraction * circumference

        return (
          <circle
            key={label}
            r={radius}
            cx={24}
            cy={24}
            fill="transparent"
            stroke={colors[index % colors.length]}
            strokeWidth={8}
            strokeDasharray={strokeDasharray}
            strokeDashoffset={strokeDashoffset}
            transform="rotate(-90 24 24)"
          />
        )
      })}
    </svg>
  )
}

export function PaperDetailView({ paper, onClose, onFilterByField, position, totalItems, onGoPrevious, onGoNext }: PaperDetailViewProps) {
  const router = useRouter()
  const [metrics, setMetrics] = useState<MetricRecord[]>([])
  const [metricsLoading, setMetricsLoading] = useState(true)
  const [showAllMetrics, setShowAllMetrics] = useState(false)
  // Dataset display names: the catalog's when it has the dataset, else the formatted slug.
  // Datasets the catalog has link to their page.
  const datasetCatalog = useDatasetCatalog()
  const datasetName = datasetCatalog.name
  // Headline results first; per-split and per-class rows only on request.
  const primaryMetrics = metrics.filter((m) => m.is_primary !== false)
  const shownMetrics = showAllMetrics ? metrics : primaryMetrics

  const handleBadgeClick = useCallback((fieldName: string, value: string) => {
    if (onFilterByField) {
      onFilterByField(fieldName, value)
    }
  }, [onFilterByField])

  const handleDatasetClick = useCallback(
    (slug: string) => {
      if (!slug) return
      const encodedSlug = encodeURIComponent(slug)
      router.push(`/?dataset=${encodedSlug}`)
    },
    [router]
  )

  useEffect(() => {
    const fetchMetrics = async () => {
      try {
        setMetricsLoading(true)
        const { data, error } = await supabase.from("filtered_metrics").select("*").eq("paper_url", paper.url)
        if (!error) setMetrics(data || [])
      } catch (err) {
        console.error("Exception fetching metrics:", err)
      } finally {
        setMetricsLoading(false)
      }
    }
    fetchMetrics()
  }, [paper.url])

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null
      if (target) {
        const tag = target.tagName
        if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") {
          return
        }
        if (target.isContentEditable) {
          return
        }
      }

      if (event.key === "ArrowRight" && onGoNext) {
        event.preventDefault()
        onGoNext()
      } else if (event.key === "ArrowLeft" && onGoPrevious) {
        event.preventDefault()
        onGoPrevious()
      }
    }

    window.addEventListener("keydown", handleKeyDown)
    return () => {
      window.removeEventListener("keydown", handleKeyDown)
    }
  }, [onGoNext, onGoPrevious])

  // Parse authors
  const authors = parseBadgeValues(String(paper.authors || ""))
    .map((a) => a.trim().replace(/^"|"$/g, ""))
    .filter(Boolean)

  // Parse class counts
  const classCountEntries = (() => {
    const rawCounts = paper.class_counts
    if (!rawCounts) return null
    let entries: [string, any][] | null = null

    if (typeof rawCounts === "string") {
      const trimmed = rawCounts.trim()
      if (trimmed.startsWith("{") && trimmed.endsWith("}")) {
        try {
          const parsed = JSON.parse(trimmed)
          if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
            entries = Object.entries(parsed)
          }
        } catch {}
      }
    } else if (typeof rawCounts === "object" && !Array.isArray(rawCounts)) {
      entries = Object.entries(rawCounts)
    }

    if (!entries || entries.length === 0) return null

    return entries
      .map(([label, count]): [string, number] => {
        const numeric = typeof count === "number" ? count : Number(count)
        return [label, Number.isFinite(numeric) ? numeric : 0]
      })
      .filter(([, value]) => value > 0)
  })()

  const hasArchitectureContent =
    isMeaningful(paper.task) ||
    isMeaningful(paper.model_family) ||
    paper.uses_radiology === true

  const hasLabelsContent =
    isMeaningful(paper.labels) ||
    isMeaningful(paper.annotation_types) ||
    (classCountEntries && classCountEntries.length > 0)

  // Resource URLs
  const resourceUrls = parseBadgeValues(String(paper.resource_urls ?? ""))
    .map((v) => v.trim())
    .filter((v) => v && isMeaningful(v) && (v.startsWith("http://") || v.startsWith("https://")))

  // Build actions for hero header
  const primaryAction = paper.url
    ? { label: "View Paper", href: paper.url, icon: ExternalLink }
    : undefined

  const secondaryActions = []
  if (paper.paper_doi) {
    secondaryActions.push({
      label: "DOI",
      href: `https://doi.org/${paper.paper_doi}`,
      icon: Hash,
    })
  }

  // Parse preprocessing steps for flow visualization
  // Order: Preprocessing methods first, then Augmentation methods
  const preprocessingSteps = (() => {
    const steps: string[] = []
    const seen = new Set<string>()
    
    // Helper to add step if not already present
    const addStep = (step: string) => {
      const normalized = step.trim().toLowerCase()
      if (normalized && !seen.has(normalized)) {
        seen.add(normalized)
        steps.push(step.trim())
      }
    }
    
    // Add preprocessing methods first
    if (isMeaningful(paper.preprocessing_methods)) {
      const methods = parseBadgeValues(String(paper.preprocessing_methods))
      methods.forEach((m) => addStep(m))
    }
    
    // Add augmentation methods (these are distinct from preprocessing)
    if (isMeaningful(paper.augmentation_methods)) {
      const methods = parseBadgeValues(String(paper.augmentation_methods))
      methods.forEach((m) => addStep(m))
    }
    
    return steps.slice(0, 5)
  })()

  // High-level processing pipeline(s) extracted via schema (2D array)
  const processingPipelines = parseProcessingPipeline(paper.processing_pipeline)

  const hasMpp = isMeaningful(paper.mpp)
  let magDisplay: number | null = null
  let mppDisplay: string | null = null

  if (hasMpp) {
    const rawMpp = typeof paper.mpp === "number" ? paper.mpp : Number(paper.mpp)
    if (!Number.isNaN(rawMpp) && rawMpp > 0) {
      const approxMag = 10 / rawMpp
      const clampedMag = Math.max(5, Math.min(40, approxMag))
      magDisplay = Math.round(clampedMag)
      mppDisplay = rawMpp.toFixed(2)
    }
  }

  return (
    <div className="min-h-screen bg-gray-50">
      {/* Hero Header */}
      <HeroHeader
        title={paper.title || "Untitled Paper"}
        year={paper.year}
        venue={paper.journal || paper.paper_venue}
        citations={paper.citations}
        authors={authors}
        primaryAction={primaryAction}
        secondaryActions={secondaryActions}
        onBack={onClose}
      />

      {/* Main Content */}
      <div className="max-w-7xl mx-auto px-4 py-6">
        {/* Everything below was extracted from this text: say so when it wasn't the full paper. */}
        {textSourceNote(paper.text_source) && (
          <div className="mb-4 rounded-lg border border-amber-200 bg-amber-50 px-4 py-2 text-sm text-amber-900">
            {textSourceNote(paper.text_source)}: the full paper could not be retrieved, so its
            fields and results may be incomplete.
          </div>
        )}
        {totalItems && position && totalItems > 1 && (
          <div className="flex items-center justify-between mb-4 gap-4">
            <p className="text-sm text-muted-foreground">
              Paper {position} of {totalItems}
            </p>
            <div className="flex items-center gap-2">
              <Button
                variant="outline"
                size="sm"
                disabled={!onGoPrevious}
                onClick={onGoPrevious}
              >
                <ChevronLeft className="h-4 w-4 mr-1" />
                Previous
              </Button>
              <Button
                variant="outline"
                size="sm"
                disabled={!onGoNext}
                onClick={onGoNext}
              >
                Next
                <ChevronRight className="h-4 w-4 ml-1" />
              </Button>
            </div>
          </div>
        )}

        <div className="flex flex-col lg:flex-row gap-6">
          {/* Left Rail - Sticky Metadata */}
          <aside className="lg:w-64 flex-shrink-0">
            <div className="lg:sticky lg:top-6 space-y-1">
              {/* Data Profile */}
              <div className="bg-white rounded-xl p-4 border">
                <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground mb-3 flex items-center gap-1.5">
                  <Dna className="h-3.5 w-3.5" />
                  Data Profile
                </h3>

                <div className="space-y-3">
                  {isMeaningful(paper.tissue_site) && (
                    <MetadataRailItem
                      icon={Microscope}
                      label={<FieldLabel fieldName="tissue_site" label="Tissue" />}
                    >
                      <SemanticBadgeList value={paper.tissue_site} fieldName="tissue_site" onBadgeClick={handleBadgeClick} />
                    </MetadataRailItem>
                  )}

                  {isMeaningful(paper.species) && (
                    <MetadataRailItem icon={Users} label={<FieldLabel fieldName="species" label="Species" />}>
                      <IconAttribute type="species" value={paper.species} label={formatValueLabel(String(paper.species))} />
                    </MetadataRailItem>
                  )}

                  {isMeaningful(paper.staining_type) && (
                    <MetadataRailItem icon={FlaskConical} label={<FieldLabel fieldName="staining_type" label="Staining" />}>
                      <SemanticBadgeList value={paper.staining_type} fieldName="staining_type" onBadgeClick={handleBadgeClick} />
                    </MetadataRailItem>
                  )}

                  {isMeaningful(paper.domain) && (
                    <MetadataRailItem
                      icon={BookOpen}
                      label={<FieldLabel fieldName="domain" label="Domain" />}
                    >
                      <SemanticBadgeList value={paper.domain} fieldName="domain" onBadgeClick={handleBadgeClick} />
                    </MetadataRailItem>
                  )}

                  {isMeaningful(paper.wsi_or_patch) && (
                    <MetadataRailItem icon={Layers} label={<FieldLabel fieldName="wsi_or_patch" label="Image Type" />}>
                      <IconAttribute type="imageType" value={paper.wsi_or_patch} />
                    </MetadataRailItem>
                  )}

                  {isMeaningful(paper.modality) && (
                    <MetadataRailItem
                      icon={Microscope}
                      label={<FieldLabel fieldName="modality" label="Modality" />}
                    >
                      <SemanticBadgeList value={paper.modality} fieldName="modality" onBadgeClick={handleBadgeClick} />
                    </MetadataRailItem>
                  )}

                  {isMeaningful(paper.biological_domain) && (
                    <MetadataRailItem
                      icon={Activity}
                      label={<FieldLabel fieldName="biological_domain" label="Bio Domain" />}
                    >
                      <SemanticBadgeList value={paper.biological_domain} fieldName="biological_domain" onBadgeClick={handleBadgeClick} />
                    </MetadataRailItem>
                  )}

                  {isMeaningful(paper.other_modalities) && (
                    <MetadataRailItem
                      icon={Workflow}
                      label={<FieldLabel fieldName="other_modalities" label="Other Modalities" />}
                    >
                      <SemanticBadgeList value={paper.other_modalities} fieldName="other_modalities" onBadgeClick={handleBadgeClick} />
                    </MetadataRailItem>
                  )}
                </div>
              </div>

              {/* Model Details */}
              {(isMeaningful(paper.backbone) || isMeaningful(paper.pretraining_strategy) || isMeaningful(paper.foundation_model) || isMeaningful(paper.model_type)) && (
                <div className="bg-white rounded-xl p-4 border mt-4">
                  <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground mb-3 flex items-center gap-1.5">
                    <Brain className="h-3.5 w-3.5" />
                    Model
                  </h3>
                  <div className="space-y-3">
                    {isMeaningful(paper.backbone) && (
                      <MetadataRailItem icon={Cpu} label={<FieldLabel fieldName="backbone" label="Backbone" />}>
                        <SemanticBadgeList value={paper.backbone} fieldName="backbone" onBadgeClick={handleBadgeClick} />
                      </MetadataRailItem>
                    )}
                    {isMeaningful(paper.pretraining_strategy) && (
                      <MetadataRailItem icon={Brain} label={<FieldLabel fieldName="pretraining_strategy" label="Pretraining" />}>
                        <SemanticBadgeList value={paper.pretraining_strategy} fieldName="pretraining_strategy" onBadgeClick={handleBadgeClick} />
                      </MetadataRailItem>
                    )}
                    {isMeaningful(paper.foundation_model) && (
                      <MetadataRailItem icon={Cpu} label={<FieldLabel fieldName="foundation_model" label="Foundation Model" />}>
                        <SemanticBadgeList value={paper.foundation_model} fieldName="foundation_model" onBadgeClick={handleBadgeClick} />
                      </MetadataRailItem>
                    )}
                    {isMeaningful(paper.model_type) && (
                      <MetadataRailItem icon={Layers} label={<FieldLabel fieldName="model_type" label="Model Type" />}>
                        <SemanticBadgeList value={paper.model_type} fieldName="model_type" onBadgeClick={handleBadgeClick} />
                      </MetadataRailItem>
                    )}
                  </div>
                </div>
              )}

              {(isMeaningful(paper.mpp) || isMeaningful(paper.scanner) || isMeaningful(paper.tile_size_px) || isMeaningful(paper.tiling_strategy)) && (
                <div className="bg-white rounded-xl p-4 border mt-4">
                  <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground mb-3 flex items-center gap-1.5">
                    <Ruler className="h-3.5 w-3.5" />
                    Imaging
                  </h3>
                  {(magDisplay !== null || mppDisplay !== null || isMeaningful(paper.tiling_strategy)) && (
                    <div className="flex flex-col gap-2 pl-0.5">
                      {(magDisplay !== null || mppDisplay !== null) && (
                        <div className="flex items-center gap-3">
                          {magDisplay !== null && (
                            <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-slate-100 text-slate-700 border border-slate-200">
                              {magDisplay}
                              ×
                            </span>
                          )}
                          {mppDisplay !== null && (
                            <span className="text-xs text-gray-600 font-mono tabular-nums">
                              {mppDisplay} µm/px
                            </span>
                          )}
                        </div>
                      )}
                      {isMeaningful(paper.tiling_strategy) && (
                        <div className="flex items-center justify-between pr-1">
                          <span className="text-xs text-gray-400 font-medium">Tiling Strategy</span>
                          <span className="text-xs text-gray-900 font-semibold truncate max-w-[140px] text-right">
                            {paper.tiling_strategy}
                          </span>
                        </div>
                      )}
                    </div>
                  )}

                  {(isMeaningful(paper.scanner) || isMeaningful(paper.tile_size_px)) && (
                    <div className="mt-3 space-y-1.5 text-xs">
                      {isMeaningful(paper.scanner) && (
                        <div className="flex items-center justify-between">
                          <span className="text-muted-foreground font-medium">Scanner</span>
                          <span className="text-foreground font-normal max-w-[130px] truncate text-right">{paper.scanner}</span>
                        </div>
                      )}
                      {isMeaningful(paper.tile_size_px) && (
                        <div className="flex items-center justify-between">
                          <span className="text-muted-foreground font-medium">Tile Size</span>
                          <span className="text-foreground font-normal">{paper.tile_size_px}px</span>
                        </div>
                      )}
                    </div>
                  )}
                </div>
              )}

              {/* Access & License */}
              {(isMeaningful(paper.dataset_access) || isMeaningful(paper.license)) && (
                <div className="bg-white rounded-xl p-4 border mt-4">
                  <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground mb-3 flex items-center gap-1.5">
                    <Shield className="h-3.5 w-3.5" />
                    Access
                  </h3>

                  <div className="space-y-3">
                    {isMeaningful(paper.dataset_access) && (
                      <>
                        <IconAttribute type="access" value={paper.dataset_access} />
                        <div className="text-sm">
                          <FieldSectionLabel fieldName="dataset_access" label="Access Level" />
                          <SemanticBadgeList
                            value={paper.dataset_access}
                            fieldName="dataset_access"
                            onBadgeClick={handleBadgeClick}
                          />
                        </div>
                      </>
                    )}

                    {isMeaningful(paper.license) && (
                      <div className="text-sm">
                        <FieldSectionLabel fieldName="license" label="License" />
                        <SemanticBadgeList value={paper.license} fieldName="license" onBadgeClick={handleBadgeClick} />
                      </div>
                    )}
                  </div>
                </div>
              )}

              {/* Quick Links */}
              {resourceUrls.length > 0 && (
                <div className="bg-white rounded-xl p-4 border mt-4">
                  <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground mb-3">
                    Resources
                  </h3>
                  <div className="space-y-2">
                    {resourceUrls.slice(0, 5).map((url, idx) => (
                      <a
                        key={idx}
                        href={url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="flex items-center gap-2 text-sm text-blue-600 hover:underline"
                      >
                        <ExternalLink className="h-3.5 w-3.5" />
                        <span className="truncate">{new URL(url).hostname.replace("www.", "")}</span>
                      </a>
                    ))}
                  </div>
                </div>
              )}
            </div>
          </aside>

          {/* Main Content Area */}
          <main className="flex-1 min-w-0 space-y-6">
            {/* Row 1: Big Stats */}
            {(isMeaningful(paper.n_patients) ||
              isMeaningful(paper.n_slides) ||
              isMeaningful(paper.n_tiles) ||
              isMeaningful(paper.n_centers)) && (
              <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                <BigStat value={paper.n_patients} label="Patients" icon={Users} />
                <BigStat value={paper.n_slides} label="Slides" icon={Layers} />
                <BigStat value={paper.n_tiles} label="Tiles" icon={Database} />
                <BigStat value={paper.n_centers} label="Centers" icon={Target} />
              </div>
            )}

            {/* Row 2: Abstract */}
            {paper.abstract && (
              <div className="bg-white rounded-xl p-6 border">
                <ContentSection icon={FileText} title="Abstract">
                  <p className="text-gray-700 leading-relaxed text-[15px]">{paper.abstract}</p>
                </ContentSection>
              </div>
            )}

            {/* Row 3: Architecture & Experimental Design */}
            {(hasArchitectureContent ||
              isMeaningful(paper.external_validation) ||
              isMeaningful(paper.split_level) ||
              isMeaningful(paper.split_method) ||
              isMeaningful(paper.outcome_endpoint) ||
              isMeaningful(paper.n_annotators) ||
              isMeaningful(paper.annotator_expertise) ||
              isMeaningful(paper.annotator_consensus)) && (
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                {/* Architecture */}
                {hasArchitectureContent && (
                  <div className="bg-white rounded-xl p-5 border">
                    <ContentSection icon={Cpu} title="Architecture & Strategy">
                      <div className="space-y-3">
                        {isMeaningful(paper.task) && (
                          <div>
                            <FieldSectionLabel fieldName="task" label="Task" />
                            <SemanticBadgeList value={paper.task} fieldName="task" onBadgeClick={handleBadgeClick} />
                          </div>
                        )}
                        {isMeaningful(paper.model_family) && (
                          <div>
                            <FieldSectionLabel fieldName="model_family" label="Model Family" />
                            <SemanticBadgeList value={paper.model_family} fieldName="model_family" onBadgeClick={handleBadgeClick} />
                          </div>
                        )}
                        {/* Capabilities */}
                        {paper.uses_radiology === true && (
                          <div className="flex flex-wrap gap-1.5 pt-2 border-t">
                            <Badge variant="data">Radiology</Badge>
                          </div>
                        )}
                      </div>
                    </ContentSection>
                  </div>
                )}

                {/* Experimental Design */}
                {(isMeaningful(paper.external_validation) ||
                  isMeaningful(paper.split_level) ||
                  isMeaningful(paper.split_method) ||
                  isMeaningful(paper.outcome_endpoint) ||
                  isMeaningful(paper.n_annotators) ||
                  isMeaningful(paper.annotator_expertise) ||
                  isMeaningful(paper.annotator_consensus)) && (
                  <div className="bg-white rounded-xl p-5 border">
                    <ContentSection icon={Shield} title="Experimental Design">
                      <div className="space-y-3">
                        {/* Validation Status - Prominent */}
                        <div className="pb-3 border-b">
                          <IconAttribute
                            type="validation"
                            value={paper.external_validation}
                            className="text-sm"
                          />
                        </div>

                        {(isMeaningful(paper.split_level) || isMeaningful(paper.split_method)) && (
                          <div>
                            <FieldSectionLabel fieldName="split_level" label="Split Strategy" />
                            <div className="flex flex-wrap gap-1.5">
                              {isMeaningful(paper.split_level) && (
                                <SemanticBadge value={String(paper.split_level)} fieldName="split_level" onClick={handleBadgeClick} />
                              )}
                              {isMeaningful(paper.split_method) && (
                                <SemanticBadge value={String(paper.split_method)} fieldName="split_method" onClick={handleBadgeClick} />
                              )}
                            </div>
                          </div>
                        )}

                        {isMeaningful(paper.outcome_endpoint) && (
                          <div>
                            <FieldSectionLabel fieldName="outcome_endpoint" label="Outcome" />
                            <SemanticBadgeList value={paper.outcome_endpoint} fieldName="outcome_endpoint" onBadgeClick={handleBadgeClick} />
                          </div>
                        )}

                        {(isMeaningful(paper.n_annotators) || isMeaningful(paper.annotator_expertise) || isMeaningful(paper.annotator_consensus)) && (
                          <div className="pt-2 border-t">
                            <FieldSectionLabel fieldName="n_annotators" label="Annotation Quality" />
                            <div className="flex flex-wrap gap-2 text-sm">
                              {isMeaningful(paper.n_annotators) && (
                                <span className="text-muted-foreground">
                                  {paper.n_annotators} annotators
                                </span>
                              )}
                              {isMeaningful(paper.annotator_expertise) && (
                                <Badge variant="secondary">{formatValueLabel(String(paper.annotator_expertise))}</Badge>
                              )}
                              {isMeaningful(paper.annotator_consensus) && (
                                <Badge variant="outline">{formatValueLabel(String(paper.annotator_consensus))}</Badge>
                              )}
                            </div>
                          </div>
                        )}
                      </div>
                    </ContentSection>
                  </div>
                )}
              </div>
            )}

            {/* Row 4: Methods & Preprocessing */}
            {(paper.methods || preprocessingSteps.length > 0 || processingPipelines.length > 0) && (
              <div className="bg-white rounded-xl p-5 border">
                <ContentSection icon={FlaskConical} title="Methods & Preprocessing">
                  <div className="space-y-4">
                    {processingPipelines.length > 0 && (
                      <div className="space-y-2">
                        <FieldSectionLabel fieldName="processing_pipeline" label="Processing Pipeline" />
                        {processingPipelines.map((steps, idx) => (
                          <div key={idx} className="flex items-center gap-2">
                            {processingPipelines.length > 1 && (
                              <span className="w-6 pr-1 text-[10px] font-semibold text-muted-foreground mt-0.5 text-right shrink-0">
                                {`P${idx + 1}`}
                              </span>
                            )}
                            <ProcessingFlow steps={steps} className="flex-1 min-w-0" />
                          </div>
                        ))}
                      </div>
                    )}

                    {preprocessingSteps.length > 0 && (
                      <div>
                        <FieldSectionLabel fieldName="preprocessing_methods" label="Preprocessing & Augmentation" />
                        <div className="flex flex-wrap gap-2">
                          {preprocessingSteps.map((step, idx) => (
                            <Badge key={`${step}-${idx}`} variant="outline">
                              {step}
                            </Badge>
                          ))}
                        </div>
                      </div>
                    )}

                    {isMeaningful(paper.augmentation_methods) && (
                      <div>
                        <FieldSectionLabel fieldName="augmentation_methods" label="Augmentation Methods" />
                        <div className="flex flex-wrap gap-2">
                          <SemanticBadgeList
                            value={paper.augmentation_methods}
                            fieldName="augmentation_methods"
                            onBadgeClick={handleBadgeClick}
                          />
                        </div>
                      </div>
                    )}

                    {paper.methods && (
                      <div>
                        <FieldSectionLabel fieldName="methods" label="Methodology" />
                        <CollapsibleSection maxHeight={150}>
                          <div className="prose prose-sm max-w-none text-gray-700 leading-relaxed">
                            <ReactMarkdown>{formatMethodsText(paper.methods)}</ReactMarkdown>
                          </div>
                        </CollapsibleSection>
                      </div>
                    )}
                  </div>
                </ContentSection>
              </div>
            )}

            {/* Row 5: Results */}
            {isMeaningful(paper.results) && (
              <div className="bg-white rounded-xl p-5 border">
                <ContentSection icon={Target} title="Results">
                  <CollapsibleSection maxHeight={200}>
                    <div className="prose prose-sm max-w-none text-gray-700 leading-relaxed">
                      <ReactMarkdown>{formatMethodsText(paper.results)}</ReactMarkdown>
                    </div>
                  </CollapsibleSection>
                </ContentSection>
              </div>
            )}

            {/* Row 6: Labels & Class Distribution */}
            {hasLabelsContent && (
              <div className="bg-white rounded-xl p-5 border">
                <ContentSection icon={Tag} title="Labels & Annotations">
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                    <div className="space-y-3">
                      {isMeaningful(paper.annotation_types) && (
                        <div>
                          <FieldSectionLabel fieldName="annotation_types" label="Annotation Types" />
                          <CollapsibleSection maxHeight={80}>
                            <SemanticBadgeList value={paper.annotation_types} fieldName="annotation_types" onBadgeClick={handleBadgeClick} />
                          </CollapsibleSection>
                        </div>
                      )}
                      {isMeaningful(paper.labels) && (
                        <div>
                          <FieldSectionLabel fieldName="labels" label="Labels" />
                          <CollapsibleSection maxHeight={80}>
                            <SemanticBadgeList value={paper.labels} fieldName="labels" onBadgeClick={handleBadgeClick} />
                          </CollapsibleSection>
                        </div>
                      )}
                    </div>

                    {classCountEntries && classCountEntries.length > 0 && (
                      <div>
                        <FieldSectionLabel fieldName="class_counts" label="Class Distribution" />
                        <div className="flex items-center gap-4">
                          <ClassDistributionPie entries={classCountEntries} />
                          <div className="flex-1 space-y-1">
                            {classCountEntries.slice(0, 5).map(([label, count]) => (
                              <div key={label} className="flex justify-between text-sm">
                                <span className="text-muted-foreground truncate max-w-[100px]">
                                  {formatValueLabel(label)}
                                </span>
                                <span className="font-medium">{count.toLocaleString()}</span>
                              </div>
                            ))}
                          </div>
                        </div>
                      </div>
                    )}
                  </div>
                </ContentSection>
              </div>
            )}

            {/* Row 7: Performance Metrics */}
            {!metricsLoading && metrics.length > 0 && (
              <div className="bg-white rounded-xl p-5 border">
                <ContentSection icon={Award} title="Performance Metrics">
                  <div className="space-y-4">
                    {shownMetrics.map((metric, idx) => {
                      const taskType = metric.task?.toLowerCase() || "unknown"
                      const context = [
                        !metric.dataset_slug ? metric.dataset_reported : null,
                        metric.split ? formatValueLabel(metric.split.replace(/_/g, " ")) : null,
                        metric.eval_level ? `${metric.eval_level} level` : null,
                        metric.subset,
                        textSourceNote(metric.text_source),
                      ].filter(Boolean).join(" · ")

                      return (
                        <div key={idx} className="border rounded-lg p-4 bg-gray-50">
                          <div className="flex items-center justify-between mb-3 gap-3">
                            <div className="flex items-center gap-2 min-w-0">
                              <Badge variant="method" className="text-sm">
                                {formatValueLabel(taskType)}
                              </Badge>
                              {context && <span className="text-xs text-muted-foreground truncate">{context}</span>}
                            </div>
                            {metric.dataset_slug && metric.dataset_slug !== "paper-level" && (
                              <div className="flex items-center justify-end gap-2 text-xs max-w-xs">
                                {(() => {
                                  const slug = metric.dataset_slug as string
                                  const label = datasetName(slug)
                                  return (
                                    <button
                                      type="button"
                                      onClick={() => handleDatasetClick(slug)}
                                      className="inline-flex items-center gap-1 rounded-full border border-gray-200 bg-white/80 px-2 py-0.5 text-[11px] font-medium text-gray-700 shadow-sm hover:bg-gray-50 hover:border-gray-300 max-w-[220px]"
                                      title={label}
                                    >
                                      <Database className="h-3 w-3 text-gray-500" />
                                      <span className="truncate">{label}</span>
                                      <ExternalLink className="h-3 w-3 text-gray-400 flex-shrink-0" />
                                    </button>
                                  )
                                })()}
                                <span className="text-[10px] uppercase tracking-wide text-muted-foreground">
                                  Dataset
                                </span>
                              </div>
                            )}
                          </div>

                          <div className="grid grid-cols-3 md:grid-cols-5 gap-2">
                            {/* Every metric the row reports, the task's headline metrics first
                                (this used to show a fixed few per task, and nothing for regression). */}
                            {reportedMetrics(metric as Record<string, any>, taskType).map(([key, label, value]) => (
                              <MetricCard key={key} label={label} value={value} />
                            ))}
                          </div>
                        </div>
                      )
                    })}
                    {metrics.length > primaryMetrics.length && (
                      <button
                        type="button"
                        onClick={() => setShowAllMetrics((v) => !v)}
                        className="text-xs text-muted-foreground underline"
                      >
                        {showAllMetrics
                          ? "Show headline results only"
                          : `Show all ${metrics.length} results (other splits and per-class)`}
                      </button>
                    )}
                  </div>
                </ContentSection>
              </div>
            )}

            {/* Datasets Used */}
            {(isMeaningful(paper.datasets_used) || paper.uses_private_data) && (
              <div className="bg-white rounded-xl p-5 border">
                <ContentSection icon={Database} title="Datasets Used">
                  {isMeaningful(paper.datasets_used) && (
                    <CollapsibleSection maxHeight={100}>
                      <SemanticBadgeList
                        value={paper.datasets_used}
                        fieldName="datasets_used"
                        formatLabel={datasetName}
                        hrefFor={(slug) => (datasetCatalog.has(slug) ? datasetHref(slug) : undefined)}
                      />
                    </CollapsibleSection>
                  )}
                  {paper.uses_private_data && (
                    <p className="mt-3 text-sm text-muted-foreground">
                      Also uses non-public data
                      {isMeaningful(paper.private_datasets)
                        ? `: ${String(paper.private_datasets).split("|").map((d) => d.trim()).filter(Boolean).join(", ")}`
                        : ""}
                    </p>
                  )}
                </ContentSection>
              </div>
            )}
          </main>
        </div>
      </div>
    </div>
  )
}
