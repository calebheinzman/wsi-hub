"use client"

import * as React from "react"
import { useState } from "react"
import {
  ChevronDown,
  ChevronUp,
  Lock,
  Unlock,
  User,
  MousePointer2,
  Image,
  Layers,
  FileImage,
  type LucideIcon,
} from "lucide-react"
import { cn, formatValueLabel } from "@/lib/utils"

// =============================================================================
// BIG STAT CARD - Dashboard-style metric display
// =============================================================================

interface BigStatProps {
  value: number | string | null | undefined
  label: string
  suffix?: string
  icon?: LucideIcon
  size?: "sm" | "md" | "lg"
  trend?: "up" | "down" | "neutral"
  className?: string
}

export function BigStat({ value, label, suffix, icon: Icon, size = "md", className }: BigStatProps) {
  if (value === null || value === undefined) return null

  const numValue = typeof value === "number" ? value : parseFloat(String(value))
  const displayValue = !isNaN(numValue)
    ? numValue >= 1000000
      ? `${(numValue / 1000000).toFixed(1)}M`
      : numValue >= 1000
      ? `${(numValue / 1000).toFixed(1)}K`
      : numValue.toLocaleString()
    : String(value)

  const sizeClasses = {
    sm: "text-2xl",
    md: "text-3xl md:text-4xl",
    lg: "text-4xl md:text-5xl",
  }

  return (
    <div className={cn("flex flex-col items-center justify-center p-4 bg-white rounded-xl border", className)}>
      {Icon && <Icon className="h-5 w-5 text-muted-foreground mb-2" />}
      <div className={cn("font-bold text-foreground tracking-tight", sizeClasses[size])}>
        {displayValue}
        {suffix && <span className="text-lg font-normal text-muted-foreground ml-1">{suffix}</span>}
      </div>
      <div className="text-xs font-medium text-muted-foreground uppercase tracking-wide mt-1">{label}</div>
    </div>
  )
}

// =============================================================================
// MAGNIFICATION SCALE - Visual indicator for resolution
// =============================================================================

interface MagnificationScaleProps {
  mpp?: number | null
  magnification?: string | number | null
  className?: string
}

export function MagnificationScale({ mpp, magnification, className }: MagnificationScaleProps) {
  // Convert MPP to approximate magnification if not provided
  let mag: number | null = null

  if (magnification) {
    const magStr = String(magnification).replace(/[x×]/gi, "").trim()
    mag = parseFloat(magStr)
  } else if (mpp && typeof mpp === "number" && mpp > 0) {
    // Approximate: 40x ≈ 0.25 MPP, 20x ≈ 0.5 MPP, 10x ≈ 1.0 MPP, 5x ≈ 2.0 MPP
    mag = 10 / mpp
  }

  if (!mag || isNaN(mag)) return null

  // Clamp to reasonable range
  const clampedMag = Math.max(5, Math.min(40, mag))
  const magDisplay = Math.round(clampedMag)

  return (
    <div className={cn("flex items-baseline gap-2", className)}>
      <span className="inline-flex items-center px-2 py-0.5 rounded-full bg-gray-100 text-gray-800 text-xs font-medium">
        {magDisplay}×
      </span>
      {mpp && (
        <span className="text-xs text-muted-foreground">
          {typeof mpp === "number" ? mpp.toFixed(2) : mpp} µm/px
        </span>
      )}
    </div>
  )
}

// =============================================================================
// ICON ATTRIBUTE - Binary/standard attributes with icons
// =============================================================================

type AttributeType = "access" | "species" | "imageType" | "validation" | "custom"

interface IconAttributeProps {
  type: AttributeType
  value: string | boolean | null | undefined
  label?: string
  icon?: LucideIcon
  className?: string
}

const attributeIcons: Record<string, { true: LucideIcon; false: LucideIcon; label: { true: string; false: string } }> = {
  access: {
    true: Unlock,
    false: Lock,
    label: { true: "Open Access", false: "Restricted" },
  },
}

const speciesIcons: Record<string, LucideIcon> = {
  human: User,
  mouse: MousePointer2,
  default: User,
}

const imageTypeIcons: Record<string, LucideIcon> = {
  wsi: FileImage,
  patch: Image,
  tile: Layers,
  default: Image,
}

export function IconAttribute({ type, value, label, icon: CustomIcon, className }: IconAttributeProps) {
  if (value === null || value === undefined) return null

  let Icon: LucideIcon
  let displayLabel: string

  if (type === "access") {
    const isOpen = value === true || String(value).toLowerCase().includes("public") || String(value).toLowerCase().includes("open")
    Icon = isOpen ? Unlock : Lock
    displayLabel = label || (isOpen ? "Open Access" : "Restricted")
    const colorClass = isOpen ? "text-emerald-600 bg-emerald-50 border-emerald-200" : "text-amber-600 bg-amber-50 border-amber-200"
    return (
      <div className={cn("inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full border text-xs font-medium", colorClass, className)}>
        <Icon className="h-3.5 w-3.5" />
        {displayLabel}
      </div>
    )
  }

  if (type === "species") {
    const raw = String(value)
    const parts = raw
      .split("|")
      .map((p) => p.trim())
      .filter((p) => p.length > 0)

    const speciesKey = (parts[0] || raw).toLowerCase()
    Icon = speciesIcons[speciesKey] || speciesIcons.default

    if (parts.length > 1) {
      displayLabel = parts.map((p) => formatValueLabel(p)).join(", ")
    } else {
      displayLabel = label || formatValueLabel(raw)
    }
    return (
      <div className={cn("inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full border border-emerald-200 bg-emerald-50 text-emerald-700 text-xs font-medium", className)}>
        <Icon className="h-3.5 w-3.5" />
        {displayLabel}
      </div>
    )
  }

  if (type === "imageType") {
    const typeKey = String(value).toLowerCase()
    Icon = imageTypeIcons[typeKey] || imageTypeIcons.default
    displayLabel = label || String(value).toUpperCase()
    return (
      <div className={cn("inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full border border-purple-200 bg-purple-50 text-purple-700 text-xs font-medium", className)}>
        <Icon className="h-3.5 w-3.5" />
        {displayLabel}
      </div>
    )
  }

  if (type === "validation") {
    const hasValidation = value === true || String(value).toLowerCase() === "yes"
    Icon = hasValidation ? Unlock : Lock
    displayLabel = label || (hasValidation ? "External Validation" : "No External Validation")
    const colorClass = hasValidation ? "text-emerald-600 bg-emerald-50 border-emerald-200" : "text-orange-600 bg-orange-50 border-orange-200"
    return (
      <div className={cn("inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full border text-xs font-medium", colorClass, className)}>
        <Icon className="h-3.5 w-3.5" />
        {displayLabel}
      </div>
    )
  }

  // Custom
  Icon = CustomIcon || User
  displayLabel = label || String(value)
  return (
    <div className={cn("inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full border border-gray-200 bg-gray-50 text-gray-700 text-xs font-medium", className)}>
      <Icon className="h-3.5 w-3.5" />
      {displayLabel}
    </div>
  )
}

// =============================================================================
// COLLAPSIBLE SECTION - For long content
// =============================================================================

interface CollapsibleSectionProps {
  title?: string
  children: React.ReactNode
  defaultOpen?: boolean
  maxHeight?: number
  className?: string
}

export function CollapsibleSection({
  title,
  children,
  defaultOpen = false,
  maxHeight = 200,
  className,
}: CollapsibleSectionProps) {
  const [isOpen, setIsOpen] = useState(defaultOpen)
  const contentRef = React.useRef<HTMLDivElement>(null)
  const [needsCollapse, setNeedsCollapse] = useState(false)

  React.useEffect(() => {
    if (contentRef.current) {
      setNeedsCollapse(contentRef.current.scrollHeight > maxHeight)
    }
  }, [children, maxHeight])

  if (!needsCollapse) {
    return <div className={className}>{children}</div>
  }

  return (
    <div className={className}>
      <div
        ref={contentRef}
        className={cn("overflow-hidden transition-all duration-300", !isOpen && "relative")}
        style={{ maxHeight: isOpen ? "none" : maxHeight }}
      >
        {children}
        {!isOpen && (
          <div className="absolute bottom-0 left-0 right-0 h-12 bg-gradient-to-t from-white to-transparent pointer-events-none" />
        )}
      </div>
      <button
        onClick={() => setIsOpen(!isOpen)}
        className="flex items-center gap-1 text-xs font-medium text-blue-600 hover:text-blue-700 mt-2"
      >
        {isOpen ? (
          <>
            <ChevronUp className="h-3.5 w-3.5" />
            Show Less
          </>
        ) : (
          <>
            <ChevronDown className="h-3.5 w-3.5" />
            Show More
          </>
        )}
      </button>
    </div>
  )
}

// =============================================================================
// METADATA RAIL ITEM - For sticky sidebar
// =============================================================================

interface MetadataRailItemProps {
  icon?: LucideIcon
  label: React.ReactNode
  children: React.ReactNode
  className?: string
}

export function MetadataRailItem({ icon: Icon, label, children, className }: MetadataRailItemProps) {
  return (
    <div className={cn("py-2", className)}>
      <div className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground uppercase tracking-wide mb-1.5">
        {Icon && <Icon className="h-3.5 w-3.5" />}
        {label}
      </div>
      <div className="text-sm">{children}</div>
    </div>
  )
}

// =============================================================================
// HERO HEADER - Full-width page header
// =============================================================================

interface HeroHeaderProps {
  title: string
  subtitle?: string
  year?: number | string | null
  venue?: string | null
  citations?: number | null
  authors?: string[]
  primaryAction?: {
    label: string
    href: string
    icon?: LucideIcon
  }
  secondaryActions?: {
    label: string
    href: string
    icon?: LucideIcon
  }[]
  onBack?: () => void
  className?: string
}

export function HeroHeader({
  title,
  subtitle,
  year,
  venue,
  citations,
  authors,
  primaryAction,
  secondaryActions,
  onBack,
  className,
}: HeroHeaderProps) {
  return (
    <div className={cn("bg-white border-b", className)}>
      <div className="max-w-7xl mx-auto px-4 py-6">
        {/* Back button */}
        {onBack && (
          <button
            onClick={onBack}
            className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground mb-4 -ml-1"
          >
            <ChevronUp className="h-4 w-4 rotate-[-90deg]" />
            Back
          </button>
        )}

        {/* Title row */}
        <div className="flex flex-col lg:flex-row lg:items-start lg:justify-between gap-4">
          <div className="flex-1 min-w-0">
            <h1 className="text-2xl md:text-3xl font-bold text-foreground leading-tight">{title}</h1>
            {subtitle && <p className="text-muted-foreground mt-1 line-clamp-2">{subtitle}</p>}

            {/* Meta line */}
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1 mt-3 text-sm text-muted-foreground">
              {year && <span className="font-medium text-foreground">{year}</span>}
              {venue && (
                <>
                  <span className="text-gray-300">•</span>
                  <span>{venue}</span>
                </>
              )}
              {citations !== null && citations !== undefined && (
                <>
                  <span className="text-gray-300">•</span>
                  <span>{citations.toLocaleString()} citations</span>
                </>
              )}
            </div>

            {/* Authors */}
            {authors && authors.length > 0 && (
              <div className="flex flex-wrap gap-1.5 mt-3">
                {authors.slice(0, 6).map((author, idx) => (
                  <span
                    key={idx}
                    className="inline-flex items-center px-2 py-0.5 rounded-full bg-gray-100 text-xs text-gray-700"
                  >
                    {author}
                  </span>
                ))}
                {authors.length > 6 && (
                  <span className="inline-flex items-center px-2 py-0.5 rounded-full bg-gray-100 text-xs text-gray-500">
                    +{authors.length - 6} more
                  </span>
                )}
              </div>
            )}
          </div>

          {/* Actions */}
          <div className="flex flex-wrap items-center gap-2 flex-shrink-0">
            {secondaryActions?.map((action, idx) => (
              <a
                key={idx}
                href={action.href}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1.5 px-3 py-2 rounded-lg border bg-white text-sm font-medium text-gray-700 hover:bg-gray-50"
              >
                {action.icon && <action.icon className="h-4 w-4" />}
                {action.label}
              </a>
            ))}
            {primaryAction && (
              <a
                href={primaryAction.href}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1.5 px-4 py-2 rounded-lg bg-blue-600 text-sm font-medium text-white hover:bg-blue-700"
              >
                {primaryAction.icon && <primaryAction.icon className="h-4 w-4" />}
                {primaryAction.label}
              </a>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

// =============================================================================
// PROCESSING FLOW - Visual flow chart for preprocessing
// =============================================================================

interface ProcessingFlowProps {
  steps: string[]
  className?: string
}

export function ProcessingFlow({ steps, className }: ProcessingFlowProps) {
  if (!steps || steps.length === 0) return null

  return (
    <div
      className={cn(
        "flex flex-wrap items-center gap-1",
        className,
      )}
    >
      {steps.map((step, idx) => (
        <React.Fragment key={idx}>
          <div className="inline-flex items-center px-2 py-0.5 rounded-md bg-blue-50 border border-blue-200 text-[11px] leading-snug font-medium text-blue-700">
            {step}
          </div>
          {idx < steps.length - 1 && (
            <svg className="h-3 w-3 text-gray-400 mx-0.5 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
            </svg>
          )}
        </React.Fragment>
      ))}
    </div>
  )
}
