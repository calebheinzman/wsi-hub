"use client"

import * as React from "react"
import Link from "next/link"
import { Badge } from "@/components/ui/badge"
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { formatValueLabel } from "@/lib/utils"
import { getFieldDescription } from "@/lib/field-descriptions"
import {
  Brain,
  Cpu,
  Target,
  Layers,
  Microscope,
  FlaskConical,
  User,
  Activity,
  Shield,
  Ruler,
  Image,
  Database,
  type LucideIcon,
} from "lucide-react"

// Semantic category definitions for WSI domain
type SemanticCategory = "method" | "biology" | "data" | "validation" | "secondary"

interface FieldCategoryConfig {
  category: SemanticCategory
  icon?: LucideIcon
}

// Map field names to their semantic categories
const fieldCategoryMap: Record<string, FieldCategoryConfig> = {
  // Method category (blue) - Architecture, ML approach, processing
  model_family: { category: "method", icon: Cpu },
  models_used: { category: "method", icon: Cpu },
  task: { category: "method", icon: Target },
  pretraining_strategy: { category: "method", icon: Brain },
  backbone: { category: "method", icon: Cpu },
  foundation_model: { category: "method", icon: Brain },
  model_type: { category: "method", icon: Cpu },
  tiling_strategy: { category: "method", icon: Layers },
  preprocessing_methods: { category: "method", icon: FlaskConical },
  augmentation_methods: { category: "method", icon: FlaskConical },

  // Biology category (green) - Tissue, species, staining, domain
  tissue_site: { category: "biology", icon: Microscope },
  species: { category: "biology", icon: User },
  staining_type: { category: "biology", icon: FlaskConical },
  domain: { category: "biology", icon: Activity },
  biological_domain: { category: "biology", icon: Activity },
  modality: { category: "biology", icon: Microscope },
  sample_prep: { category: "biology", icon: FlaskConical },
  other_modalities: { category: "biology", icon: Microscope },

  // Data category (purple) - Resolution, format, structure
  wsi_or_patch: { category: "data", icon: Image },
  mpp: { category: "data", icon: Ruler },
  tile_size_px: { category: "data", icon: Ruler },
  magnification: { category: "data", icon: Microscope },
  image_format: { category: "data", icon: Image },
  scanner: { category: "data", icon: Microscope },
  annotation_types: { category: "data", icon: Database },
  annotation_level: { category: "data", icon: Database },
  labels: { category: "data", icon: Database },
  results: { category: "data", icon: Database },

  // Validation category (amber/orange) - Study rigor, validation
  split_level: { category: "validation", icon: Shield },
  split_method: { category: "validation", icon: Shield },
  external_validation: { category: "validation", icon: Shield },
  outcome_endpoint: { category: "validation", icon: Target },
  dataset_access: { category: "validation", icon: Shield },
  license: { category: "validation", icon: Shield },
}

// Value-based category overrides (for specific values regardless of field)
const valueCategoryOverrides: Record<string, SemanticCategory> = {
  // Method values
  cnn: "method",
  resnet: "method",
  vit: "method",
  transformer: "method",
  "attention-based": "method",
  mil: "method",
  classification: "method",
  segmentation: "method",
  detection: "method",
  regression: "method",
  survival: "method",
  "patch-based": "method",
  "wsi-level": "method",

  // Biology values
  lung: "biology",
  breast: "biology",
  colon: "biology",
  liver: "biology",
  kidney: "biology",
  brain: "biology",
  skin: "biology",
  prostate: "biology",
  stomach: "biology",
  pancreas: "biology",
  thyroid: "biology",
  ovary: "biology",
  bladder: "biology",
  "h&e": "biology",
  he: "biology",
  ihc: "biology",
  human: "biology",
  mouse: "biology",
  oncology: "biology",
  pathology: "biology",

  // Data values
  wsi: "data",
  patch: "data",
  svs: "data",
  ndpi: "data",
  tiff: "data",

  // Validation values
  holdout: "validation",
  "cross-validation": "validation",
  "k-fold": "validation",
  "patient-level": "validation",
  "slide-level": "validation",
  external: "validation",
  internal: "validation",
}

function getCategoryForValue(value: string, fieldName?: string): SemanticCategory {
  const normalizedValue = value.toLowerCase().trim()

  // Check value-based overrides first
  if (valueCategoryOverrides[normalizedValue]) {
    return valueCategoryOverrides[normalizedValue]
  }

  // Check field-based category
  if (fieldName && fieldCategoryMap[fieldName]) {
    return fieldCategoryMap[fieldName].category
  }

  return "secondary"
}

function getIconForField(fieldName?: string): LucideIcon | undefined {
  if (fieldName && fieldCategoryMap[fieldName]) {
    return fieldCategoryMap[fieldName].icon
  }
  return undefined
}

interface SemanticBadgeProps {
  value: string
  label?: string  // shown instead of the formatted value (e.g. a dataset's display name)
  fieldName?: string
  showIcon?: boolean
  className?: string
  onClick?: (fieldName: string, value: string) => void
  href?: string  // makes the badge a link (e.g. to a dataset's page); takes precedence over onClick
}

export function SemanticBadge({ value, label, fieldName, showIcon = false, className, onClick, href }: SemanticBadgeProps) {
  const category = getCategoryForValue(value, fieldName)
  const Icon = showIcon ? getIconForField(fieldName) : undefined
  const displayValue = label ?? formatValueLabel(value.trim().replace(/^"|"$/g, ""))
  const isClickable = !href && onClick && fieldName

  if (href) {
    return (
      <Link href={href} className="max-w-full" onClick={(e) => e.stopPropagation()}>
        <Badge
          variant={category}
          className={`${className || ""} max-w-full break-words whitespace-normal cursor-pointer underline-offset-2 hover:underline hover:ring-2 hover:ring-offset-1 hover:ring-blue-400 transition-all`}
        >
          {Icon && <Icon className="h-3 w-3 mr-1" />}
          {displayValue}
        </Badge>
      </Link>
    )
  }

  const badge = (
    <Badge 
      variant={category} 
      className={`${className || ""} max-w-full break-words whitespace-normal ${isClickable ? "cursor-pointer hover:ring-2 hover:ring-offset-1 hover:ring-blue-400 transition-all" : ""}`}
      onClick={isClickable ? () => onClick(fieldName, value.trim()) : undefined}
    >
      {Icon && <Icon className="h-3 w-3 mr-1" />}
      {displayValue}
    </Badge>
  )

  return badge
}

// Parse badge values from various formats
export function parseBadgeValues(raw: string): string[] {
  const trimmed = (raw ?? "").trim()
  if (!trimmed) return []

  if (trimmed.startsWith("[") && trimmed.endsWith("]")) {
    try {
      const parsed = JSON.parse(trimmed)
      if (Array.isArray(parsed)) {
        return parsed.map((v) => String(v))
      }
    } catch {
      const inner = trimmed.slice(1, -1)
      if (inner.includes("'")) {
        const parts = inner
          .split(",")
          .map((part) => part.trim())
          .filter(Boolean)

        if (parts.length > 0) {
          return parts.map((part) => part.replace(/^["']|["']$/g, ""))
        }
      }
    }
  }

  if (trimmed.includes("|")) {
    return trimmed.split("|")
  }

  if (trimmed.includes(",")) {
    return trimmed.split(",")
  }

  return [trimmed]
}

interface SemanticBadgeListProps {
  value: string | number | boolean | null | undefined
  fieldName?: string
  showIcon?: boolean
  className?: string
  onBadgeClick?: (fieldName: string, value: string) => void
  formatLabel?: (value: string) => string
  hrefFor?: (value: string) => string | undefined  // a link for this value, if it has one
}

export function SemanticBadgeList({ value, fieldName, showIcon = false, className, onBadgeClick, formatLabel, hrefFor }: SemanticBadgeListProps) {
  if (!value) return null

  const values = parseBadgeValues(String(value))
  if (values.length === 0) return null

  return (
    <div className={`flex flex-wrap gap-1 ${className || ""}`}>
      {values.map((v, i) => {
        const cleaned = v.trim().replace(/^"|"$/g, "")
        if (!cleaned) return null
        return (
          <SemanticBadge
            key={i}
            value={cleaned}
            label={formatLabel?.(cleaned)}
            fieldName={fieldName}
            showIcon={showIcon}
            onClick={onBadgeClick}
            href={hrefFor?.(cleaned)}
          />
        )
      })}
    </div>
  )
}

// MPP Resolution indicator component
interface MppIndicatorProps {
  mpp: number | string | null | undefined
  className?: string
}

export function MppIndicator({ mpp, className }: MppIndicatorProps) {
  if (mpp === null || mpp === undefined) return null

  const numMpp = typeof mpp === "number" ? mpp : parseFloat(String(mpp))
  if (isNaN(numMpp) || numMpp <= 0) return null

  // Determine resolution level based on MPP
  // Lower MPP = higher resolution
  // ~0.25 MPP ≈ 40x, ~0.5 MPP ≈ 20x, ~1.0 MPP ≈ 10x, ~2.0 MPP ≈ 5x
  let level: "high" | "medium" | "low"
  let label: string
  let magnification: string

  if (numMpp <= 0.3) {
    level = "high"
    label = "High Res"
    magnification = "~40x"
  } else if (numMpp <= 0.6) {
    level = "medium"
    label = "Med Res"
    magnification = "~20x"
  } else {
    level = "low"
    label = "Low Res"
    magnification = numMpp <= 1.2 ? "~10x" : "~5x"
  }

  const colorClasses = {
    high: "bg-emerald-100 text-emerald-800 border-emerald-200",
    medium: "bg-amber-100 text-amber-800 border-amber-200",
    low: "bg-gray-100 text-gray-700 border-gray-200",
  }

  const barWidths = {
    high: "w-full",
    medium: "w-2/3",
    low: "w-1/3",
  }

  return (
    <div className={`flex items-center gap-2 ${className || ""}`}>
      <div className="flex flex-col gap-1">
        <div className="flex items-center gap-2">
          <span className={`text-xs font-medium px-2 py-0.5 rounded border ${colorClasses[level]}`}>
            {label}
          </span>
          <span className="text-xs text-muted-foreground">{magnification}</span>
        </div>
        <div className="flex items-center gap-1.5">
          <span className="text-xs text-muted-foreground">{numMpp.toFixed(3)} MPP</span>
          <div className="w-16 h-1.5 bg-gray-200 rounded-full overflow-hidden">
            <div className={`h-full ${barWidths[level]} ${level === "high" ? "bg-emerald-500" : level === "medium" ? "bg-amber-500" : "bg-gray-400"} rounded-full`} />
          </div>
        </div>
      </div>
    </div>
  )
}

// Capabilities/Features indicator for boolean fields
interface CapabilitiesIndicatorProps {
  capabilities: {
    label: string
    value: boolean | null | undefined
    icon?: LucideIcon
  }[]
  showInactive?: boolean
  className?: string
}

export function CapabilitiesIndicator({ capabilities, showInactive = false, className }: CapabilitiesIndicatorProps) {
  const activeCapabilities = capabilities.filter((c) => c.value === true)
  const inactiveCapabilities = capabilities.filter((c) => c.value === false)

  if (activeCapabilities.length === 0 && !showInactive) return null

  return (
    <div className={`flex flex-wrap gap-1.5 ${className || ""}`}>
      {activeCapabilities.map((cap, i) => (
        <span
          key={i}
          className="inline-flex items-center gap-1 px-2 py-0.5 text-xs font-medium rounded-full bg-green-100 text-green-800 border border-green-200"
        >
          {cap.icon && <cap.icon className="h-3 w-3" />}
          {cap.label}
        </span>
      ))}
      {showInactive &&
        inactiveCapabilities.map((cap, i) => (
          <span
            key={`inactive-${i}`}
            className="inline-flex items-center gap-1 px-2 py-0.5 text-xs font-medium rounded-full bg-gray-100 text-gray-400 border border-gray-200 line-through"
          >
            {cap.icon && <cap.icon className="h-3 w-3" />}
            {cap.label}
          </span>
        ))}
    </div>
  )
}

// Stat bar component for the header area
interface StatBarProps {
  stats: {
    label: string
    value: string | number | null | undefined
    icon?: LucideIcon
  }[]
  className?: string
}

export function StatBar({ stats, className }: StatBarProps) {
  const validStats = stats.filter(
    (s) => s.value !== null && s.value !== undefined && String(s.value).trim() !== ""
  )

  if (validStats.length === 0) return null

  return (
    <div className={`flex flex-wrap items-center gap-x-4 gap-y-1 text-sm text-muted-foreground ${className || ""}`}>
      {validStats.map((stat, i) => (
        <React.Fragment key={i}>
          {i > 0 && <span className="text-gray-300">•</span>}
          <span className="inline-flex items-center gap-1">
            {stat.icon && <stat.icon className="h-3.5 w-3.5" />}
            <span>{stat.label}:</span>
            <span className="font-medium text-foreground">{stat.value}</span>
          </span>
        </React.Fragment>
      ))}
    </div>
  )
}
