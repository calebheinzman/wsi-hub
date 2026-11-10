"use client"

import * as React from "react"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { getFieldDescription } from "@/lib/field-descriptions"

interface FieldLabelProps {
  fieldName: string
  label?: string
  className?: string
}

/**
 * A field label component with hover tooltip showing field description.
 * Hover directly over the label text to see the description.
 */
export function FieldLabel({ fieldName, label, className }: FieldLabelProps) {
  const description = getFieldDescription(fieldName)
  const displayLabel = label || formatFieldName(fieldName)

  if (!description) {
    return <span className={className}>{displayLabel}</span>
  }

  return (
    <Tooltip delayDuration={200}>
      <TooltipTrigger asChild>
        <span 
          className={`cursor-help border-b border-dotted border-muted-foreground/40 hover:border-muted-foreground ${className || ""}`}
        >
          {displayLabel}
        </span>
      </TooltipTrigger>
      <TooltipContent side="top" className="max-w-xs text-sm">
        <p>{description}</p>
      </TooltipContent>
    </Tooltip>
  )
}

/**
 * Format a field name for display (snake_case to Title Case)
 */
function formatFieldName(field: string): string {
  const acronyms = new Set([
    "wsi", "cnn", "rnn", "mlp", "ai", "roi", "id", "ssl", "mpp", "gb", "px",
    "doi", "pmid", "ihc", "dab", "if", "fish", "srh", "oct", "mpm", "tma"
  ])
  const smallWords = new Set(["and", "or", "of", "the", "in", "on", "for", "to", "with", "by"])

  const parts = field.split("_")
  return parts
    .map((raw, idx) => {
      const word = raw.trim()
      if (!word) return ""
      const lower = word.toLowerCase()
      if (acronyms.has(lower)) return word.toUpperCase()
      if (idx !== 0 && smallWords.has(lower)) return lower
      return lower.charAt(0).toUpperCase() + lower.slice(1)
    })
    .join(" ")
}

interface FieldSectionLabelProps {
  fieldName: string
  label?: string
  className?: string
}

/**
 * A smaller field section label used within content sections.
 * Shows as muted text with tooltip on hover (no icon).
 */
export function FieldSectionLabel({ fieldName, label, className }: FieldSectionLabelProps) {
  const description = getFieldDescription(fieldName)
  const displayLabel = label || formatFieldName(fieldName)

  if (!description) {
    return <span className={`text-xs text-muted-foreground block mb-1 ${className || ""}`}>{displayLabel}</span>
  }

  return (
    <Tooltip delayDuration={200}>
      <TooltipTrigger asChild>
        <span 
          className={`text-xs text-muted-foreground mb-1 cursor-help border-b border-dotted border-muted-foreground/40 hover:border-muted-foreground inline-block ${className || ""}`}
        >
          {displayLabel}
        </span>
      </TooltipTrigger>
      <TooltipContent side="top" className="max-w-xs text-sm">
        <p>{description}</p>
      </TooltipContent>
    </Tooltip>
  )
}
