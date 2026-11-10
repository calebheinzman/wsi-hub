"use client"

import { useState, useEffect, useCallback } from "react"
import {
  ExternalLink,
  Microscope,
  FileText,
  Download,
  Layers,
  Tag,
  Database,
  Ruler,
  HardDrive,
  Globe,
  Shield,
  Cpu,
  Dna,
  FlaskConical,
  BookOpen,
  Users,
  Target,
  TrendingUp,
  Activity,
  Crosshair,
  Key,
  ChevronLeft,
  ChevronRight,
  type LucideIcon,
} from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Skeleton } from "@/components/ui/skeleton"
import { MetricTable } from "./metric-table"
import { formatValueLabel } from "@/lib/utils"
import { supabase } from "@/lib/supabase"
import { SemanticBadge, SemanticBadgeList, parseBadgeValues } from "@/components/semantic-badge"
import { FieldLabel, FieldSectionLabel } from "@/components/field-label"
import {
  HeroHeader,
  BigStat,
  MagnificationScale,
  IconAttribute,
  CollapsibleSection,
  MetadataRailItem,
} from "@/components/ui-enhanced"
import type { DatasetEntry, FilterState } from "./paper-types"
import { formatDatasetName } from "@/lib/dataset-names"

interface DatasetDetailViewProps {
  dataset: DatasetEntry
  onClose: () => void
  onFilterByField?: (field: string, value: string) => void
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

export function DatasetDetailView({ dataset, onClose, onFilterByField, position, totalItems, onGoPrevious, onGoNext }: DatasetDetailViewProps & { position?: number; totalItems?: number; onGoPrevious?: () => void; onGoNext?: () => void }) {
  const [classificationMetrics, setClassificationMetrics] = useState<Record<string, any> | null>(null)
  const [segmentationMetrics, setSegmentationMetrics] = useState<Record<string, any> | null>(null)
  const [detectionMetrics, setDetectionMetrics] = useState<Record<string, any> | null>(null)
  const [regressionMetrics, setRegressionMetrics] = useState<Record<string, any> | null>(null)
  const [survivalMetrics, setSurvivalMetrics] = useState<Record<string, any> | null>(null)
  const [loadingMetrics, setLoadingMetrics] = useState(true)
  const [metricsError, setMetricsError] = useState<string | null>(null)

  // Handle badge click to filter by field value
  const handleBadgeClick = useCallback((fieldName: string, value: string) => {
    if (onFilterByField) {
      onFilterByField(fieldName, value)
    }
  }, [onFilterByField])

  useEffect(() => {
    // This view is reused when stepping between datasets: drop a response that arrives
    // after the user has moved on, or it would show the previous dataset's metrics.
    let live = true

    const fetchMetrics = async () => {
      try {
        setLoadingMetrics(true)
        const { data: metricsData, error } = await supabase
          .from("filtered_metrics")
          .select("*")
          .eq("dataset_slug", dataset.slug)

        if (error) throw error
        if (!live) return

        const classMetrics: Record<string, any> = {}
        const segMetrics: Record<string, any> = {}
        const detMetrics: Record<string, any> = {}
        const regMetrics: Record<string, any> = {}
        const survMetrics: Record<string, any> = {}

        // One headline row per paper: skip validation-split and per-class rows.
        metricsData?.filter((row) => row.is_primary !== false).forEach((row) => {
          if (row.task === "classification") classMetrics[row.paper_url] = row
          else if (row.task === "segmentation") segMetrics[row.paper_url] = row
          else if (row.task === "detection") detMetrics[row.paper_url] = row
          else if (row.task === "regression") regMetrics[row.paper_url] = row
          else if (row.task === "survival") survMetrics[row.paper_url] = row
        })

        setClassificationMetrics(Object.keys(classMetrics).length > 0 ? classMetrics : null)
        setSegmentationMetrics(Object.keys(segMetrics).length > 0 ? segMetrics : null)
        setDetectionMetrics(Object.keys(detMetrics).length > 0 ? detMetrics : null)
        setRegressionMetrics(Object.keys(regMetrics).length > 0 ? regMetrics : null)
        setSurvivalMetrics(Object.keys(survMetrics).length > 0 ? survMetrics : null)
      } catch (error) {
        console.error("Error fetching metrics:", error)
        if (live) setMetricsError("Failed to load metrics")
      } finally {
        if (live) setLoadingMetrics(false)
      }
    }

    fetchMetrics()
    return () => {
      live = false
    }
  }, [dataset.slug])

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

  // Resource URLs
  const resourceUrls = parseBadgeValues(String(dataset.resource_urls ?? ""))
    .map((v) => v.trim())
    .filter((v) => Boolean(v) && isMeaningful(v))

  const hasAnyMetrics =
    classificationMetrics || segmentationMetrics || detectionMetrics || regressionMetrics || survivalMetrics

  // Build actions for hero header
  const primaryAction = dataset.download_url
    ? { label: "Download", href: dataset.download_url, icon: Download }
    : undefined

  const secondaryActions = []
  if (dataset.home_page_url) {
    secondaryActions.push({
      label: "Home Page",
      href: dataset.home_page_url,
      icon: Globe,
    })
  }
  if (dataset.code_url) {
    secondaryActions.push({
      label: "Code",
      href: dataset.code_url,
      icon: FileText,
    })
  }

  // Determine dataset size category
  const getSizeCategory = () => {
    const slides = dataset.n_slides || 0
    if (slides >= 10000) return { label: "Large", color: "text-emerald-600" }
    if (slides >= 1000) return { label: "Medium", color: "text-amber-600" }
    if (slides >= 100) return { label: "Small", color: "text-gray-600" }
    return { label: "Tiny", color: "text-gray-400" }
  }

  const sizeCategory = getSizeCategory()

  return (
    <div className="min-h-screen bg-gray-50">
      {/* Hero Header */}
      <HeroHeader
        title={formatDatasetName(dataset.dataset_name, dataset.slug)}
        subtitle={dataset.description || undefined}
        year={dataset.year}
        primaryAction={primaryAction}
        secondaryActions={secondaryActions}
        onBack={onClose}
      />

      {/* Main Content */}
      <div className="max-w-7xl mx-auto px-4 py-6">
        {dataset.parent_slug && (
          <p className="mb-4 text-sm text-muted-foreground">
            Part of{" "}
            <a href={`/?dataset=${encodeURIComponent(dataset.parent_slug)}`} className="text-blue-600 hover:underline">
              {dataset.parent_slug.toUpperCase()}
            </a>
          </p>
        )}
        {totalItems && position && totalItems > 1 && (
          <div className="flex items-center justify-between mb-4 gap-4">
            <p className="text-sm text-muted-foreground">
              Dataset {position} of {totalItems}
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
            <div className="lg:sticky lg:top-6 space-y-4">
              {/* Data Profile */}
              <div className="bg-white rounded-xl p-4 border">
                <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground mb-3 flex items-center gap-1.5">
                  <Dna className="h-3.5 w-3.5" />
                  Data Profile
                </h3>

                <div className="space-y-3">
                  {isMeaningful(dataset.tissue_site) && (
                    <MetadataRailItem
                      icon={Microscope}
                      label={<FieldLabel fieldName="tissue_site" label="Tissue" />}
                    >
                      <SemanticBadgeList value={dataset.tissue_site} fieldName="tissue_site" onBadgeClick={handleBadgeClick} />
                    </MetadataRailItem>
                  )}

                  {isMeaningful(dataset.species) && (
                    <MetadataRailItem icon={Users} label={<FieldLabel fieldName="species" label="Species" />}>
                      <IconAttribute type="species" value={dataset.species} label={formatValueLabel(String(dataset.species))} />
                    </MetadataRailItem>
                  )}

                  {isMeaningful(dataset.staining_type) && (
                    <MetadataRailItem icon={FlaskConical} label={<FieldLabel fieldName="staining_type" label="Staining" />}>
                      <SemanticBadgeList value={dataset.staining_type} fieldName="staining_type" onBadgeClick={handleBadgeClick} />
                    </MetadataRailItem>
                  )}

                  {isMeaningful(dataset.domain) && (
                    <MetadataRailItem
                      icon={BookOpen}
                      label={<FieldLabel fieldName="domain" label="Domain" />}
                    >
                      <SemanticBadgeList value={dataset.domain} fieldName="domain" onBadgeClick={handleBadgeClick} />
                    </MetadataRailItem>
                  )}

                  {isMeaningful(dataset.wsi_or_patch) && (
                    <MetadataRailItem icon={Layers} label={<FieldLabel fieldName="wsi_or_patch" label="Image Type" />}>
                      <IconAttribute type="imageType" value={dataset.wsi_or_patch} />
                    </MetadataRailItem>
                  )}

                  {isMeaningful(dataset.modality) && (
                    <MetadataRailItem
                      icon={Microscope}
                      label={<FieldLabel fieldName="modality" label="Modality" />}
                    >
                      <SemanticBadgeList value={dataset.modality} fieldName="modality" onBadgeClick={handleBadgeClick} />
                    </MetadataRailItem>
                  )}
                </div>
              </div>

              {/* Imaging Setup */}
              {(isMeaningful(dataset.mpp) || isMeaningful(dataset.scanner) || isMeaningful(dataset.magnification) || isMeaningful(dataset.mag_min)) && (
                <div className="bg-white rounded-xl p-4 border">
                  <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground mb-3 flex items-center gap-1.5">
                    <Ruler className="h-3.5 w-3.5" />
                    Imaging
                  </h3>

                  {isMeaningful(dataset.mpp) && (
                    <div className="mb-4">
                      <MagnificationScale mpp={dataset.mpp} magnification={dataset.magnification} />
                    </div>
                  )}

                  <div className="space-y-2 text-sm">
                    {isMeaningful(dataset.scanner) && (
                      <div className="flex justify-between">
                        <FieldLabel fieldName="scanner" label="Scanner" className="text-muted-foreground" />
                        <span className="font-medium text-right max-w-[120px] truncate">{dataset.scanner}</span>
                      </div>
                    )}
                    {isMeaningful(dataset.tile_size_px) && (
                      <div className="flex justify-between">
                        <FieldLabel fieldName="tile_size_px" label="Tile Size" className="text-muted-foreground" />
                        <span className="font-medium">{dataset.tile_size_px}px</span>
                      </div>
                    )}
                    {isMeaningful(dataset.image_format) && (
                      <div className="flex justify-between">
                        <FieldLabel fieldName="image_format" label="Format" className="text-muted-foreground" />
                        <span className="font-medium">{dataset.image_format}</span>
                      </div>
                    )}
                  </div>
                </div>
              )}

              {/* Access & License */}
              <div className="bg-white rounded-xl p-4 border">
                <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground mb-3 flex items-center gap-1.5">
                  <Shield className="h-3.5 w-3.5" />
                  Access
                </h3>

                <div className="space-y-3">
                  <IconAttribute type="access" value={dataset.is_public} />

                  {isMeaningful(dataset.dataset_access) && (
                    <div className="text-sm">
                      <FieldSectionLabel fieldName="dataset_access" label="Access Level" />
                      <SemanticBadgeList value={dataset.dataset_access} fieldName="dataset_access" onBadgeClick={handleBadgeClick} />
                    </div>
                  )}

                  {isMeaningful(dataset.license) && (
                    <div className="text-sm">
                      <FieldSectionLabel fieldName="license" label="License" />
                      <SemanticBadgeList value={dataset.license} fieldName="license" onBadgeClick={handleBadgeClick} />
                    </div>
                  )}

                  {isMeaningful(dataset.institutions) && (
                    <MetadataRailItem label={<FieldLabel fieldName="institutions" label="Institutions" />}>
                      <div className="space-y-1 text-xs text-foreground">
                        {parseBadgeValues(String(dataset.institutions)).map((inst, idx) => {
                          const cleaned = inst.trim().replace(/^"|"$/g, "")
                          if (!cleaned) return null
                          return (
                            <div
                              key={idx}
                              className="rounded-md bg-gray-100 border border-gray-200 px-2 py-1 leading-snug break-words"
                            >
                              {formatValueLabel(cleaned)}
                            </div>
                          )
                        })}
                      </div>
                    </MetadataRailItem>
                  )}
                </div>
              </div>

              {/* Quick Links */}
              {resourceUrls.length > 0 && (
                <div className="bg-white rounded-xl p-4 border">
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
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
              <BigStat value={dataset.n_slides} label="Slides" icon={Layers} />
              <BigStat value={dataset.n_patients} label="Patients" icon={Users} />
              <BigStat value={dataset.n_tiles} label="Tiles" icon={Database} />
              <BigStat value={dataset.approx_size_gb} label="Size" suffix="GB" icon={HardDrive} />
            </div>

            {/* Dataset Size Context */}
            {isMeaningful(dataset.n_slides) && (
              <div className="bg-white rounded-xl p-4 border flex items-center justify-between">
                <span className="text-sm text-muted-foreground">Dataset Size Category</span>
                <span className={`text-sm font-semibold ${sizeCategory.color}`}>{sizeCategory.label}</span>
              </div>
            )}

            {/* Row 2: Task & Annotations */}
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              {/* Task */}
              <div className="bg-white rounded-xl p-5 border">
                <ContentSection icon={Target} title="Task & Annotations">
                  <div className="space-y-3">
                    {isMeaningful(dataset.task) && (
                      <div>
                        <FieldSectionLabel fieldName="task" label="Task" />
                        <SemanticBadgeList value={dataset.task} fieldName="task" onBadgeClick={handleBadgeClick} />
                      </div>
                    )}
                    {isMeaningful(dataset.annotation_level) && (
                      <div>
                        <FieldSectionLabel fieldName="annotation_level" label="Annotation Level" />
                        <SemanticBadgeList value={dataset.annotation_level} fieldName="annotation_level" onBadgeClick={handleBadgeClick} />
                      </div>
                    )}
                    {isMeaningful(dataset.annotation_types) && (
                      <div>
                        <FieldSectionLabel fieldName="annotation_types" label="Annotation Types" />
                        <CollapsibleSection maxHeight={60}>
                          <SemanticBadgeList value={dataset.annotation_types} fieldName="annotation_types" onBadgeClick={handleBadgeClick} />
                        </CollapsibleSection>
                      </div>
                    )}
                    {isMeaningful(dataset.annotation_format) && (
                      <div className="space-y-1">
                        <FieldSectionLabel fieldName="annotation_format" label="Format" />
                        <SemanticBadgeList value={dataset.annotation_format} fieldName="annotation_format" onBadgeClick={handleBadgeClick} />
                      </div>
                    )}
                  </div>
                </ContentSection>
              </div>

              {/* Study Design */}
              <div className="bg-white rounded-xl p-5 border">
                <ContentSection icon={Shield} title="Study Design">
                  <div className="space-y-3">
                    {/* Validation Status - Prominent */}
                    <div className="pb-3 border-b">
                      <IconAttribute
                        type="validation"
                        value={dataset.external_validation}
                        className="text-sm"
                      />
                    </div>

                    {(isMeaningful(dataset.split_level) || isMeaningful(dataset.split_method)) && (
                      <div>
                        <FieldSectionLabel fieldName="split_level" label="Split Strategy" />
                        <div className="flex flex-wrap gap-1.5">
                          {isMeaningful(dataset.split_level) && (
                            <Badge variant="validation" className="text-xs">
                              {formatValueLabel(String(dataset.split_level))} Split
                            </Badge>
                          )}
                          {isMeaningful(dataset.split_method) && (
                            <Badge variant="validation" className="text-xs">
                              {formatValueLabel(String(dataset.split_method))}
                            </Badge>
                          )}
                        </div>
                      </div>
                    )}

                    {isMeaningful(dataset.n_centers) && (
                      <div className="space-y-1">
                        <FieldSectionLabel fieldName="n_centers" label="Multi-center" className="block" />
                        <span className="text-sm font-medium text-gray-900 block">
                          {formatValueLabel(String(dataset.n_centers))} center{Number(dataset.n_centers) === 1 ? "" : "s"}
                        </span>
                      </div>
                    )}
                  </div>
                </ContentSection>
              </div>
            </div>

            {/* Row 3: Labels */}
            {isMeaningful(dataset.labels) && (
              <div className="bg-white rounded-xl p-5 border">
                <ContentSection icon={Tag} title="Labels">
                  <CollapsibleSection maxHeight={120}>
                    <SemanticBadgeList value={dataset.labels} fieldName="labels" onBadgeClick={handleBadgeClick} />
                  </CollapsibleSection>
                </ContentSection>
              </div>
            )}

            {/* Row 4: Preprocessing & Augmentation */}
            {(isMeaningful(dataset.sample_prep) ||
              isMeaningful(dataset.tiling_strategy)) && (
              <div className="bg-white rounded-xl p-5 border">
                <ContentSection icon={FlaskConical} title="Preprocessing & Augmentation">
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                    {isMeaningful(dataset.sample_prep) && (
                      <div>
                        <FieldSectionLabel fieldName="sample_prep" label="Sample Preparation" />
                        <SemanticBadgeList value={dataset.sample_prep} fieldName="sample_prep" onBadgeClick={handleBadgeClick} />
                      </div>
                    )}
                    {isMeaningful(dataset.tiling_strategy) && (
                      <div>
                        <FieldSectionLabel fieldName="tiling_strategy" label="Tiling Strategy" />
                        <SemanticBadgeList value={dataset.tiling_strategy} fieldName="tiling_strategy" onBadgeClick={handleBadgeClick} />
                      </div>
                    )}
                  </div>
                </ContentSection>
              </div>
            )}

            {/* Row 6: Associated Paper */}
            {(isMeaningful(dataset.paper_doi) || isMeaningful(dataset.paper_venue)) && (
              <div className="bg-white rounded-xl p-5 border">
                <ContentSection icon={FileText} title="Associated Paper">
                  <div className="flex flex-wrap gap-4">
                    {isMeaningful(dataset.paper_doi) && (
                      <a
                        href={`https://doi.org/${dataset.paper_doi}`}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="inline-flex items-center gap-1.5 text-sm text-blue-600 hover:underline"
                      >
                        DOI: {dataset.paper_doi}
                        <ExternalLink className="h-3.5 w-3.5" />
                      </a>
                    )}
                    {isMeaningful(dataset.paper_venue) && (
                      <span className="text-sm text-muted-foreground">
                        Venue: <span className="font-medium text-foreground">{dataset.paper_venue}</span>
                      </span>
                    )}
                  </div>
                </ContentSection>
              </div>
            )}

            {/* Row 6: Performance Metrics */}
            {loadingMetrics ? (
              <div className="space-y-3">
                <Skeleton className="h-8 w-48" />
                <Skeleton className="h-32 w-full" />
              </div>
            ) : metricsError ? (
              <Alert variant="destructive">
                <AlertDescription>{metricsError}</AlertDescription>
              </Alert>
            ) : hasAnyMetrics ? (
              <div className="space-y-4">
                {classificationMetrics && (
                  <div className="bg-white rounded-xl p-5 border">
                    <ContentSection icon={Cpu} title="Classification Metrics">
                      <MetricTable title="Classification" data={classificationMetrics} />
                    </ContentSection>
                  </div>
                )}

                {segmentationMetrics && (
                  <div className="bg-white rounded-xl p-5 border">
                    <ContentSection icon={Layers} title="Segmentation Metrics">
                      <MetricTable title="Segmentation" data={segmentationMetrics} />
                    </ContentSection>
                  </div>
                )}

                {detectionMetrics && (
                  <div className="bg-white rounded-xl p-5 border">
                    <ContentSection icon={Target} title="Detection Metrics">
                      <MetricTable title="Detection" data={detectionMetrics} />
                    </ContentSection>
                  </div>
                )}

                {regressionMetrics && (
                  <div className="bg-white rounded-xl p-5 border">
                    <ContentSection icon={TrendingUp} title="Regression Metrics">
                      <MetricTable title="Regression" data={regressionMetrics} />
                    </ContentSection>
                  </div>
                )}

                {survivalMetrics && (
                  <div className="bg-white rounded-xl p-5 border">
                    <ContentSection icon={Activity} title="Survival Analysis Metrics">
                      <MetricTable title="Survival" data={survivalMetrics} />
                    </ContentSection>
                  </div>
                )}
              </div>
            ) : (
              <div className="bg-white rounded-xl p-8 border text-center text-muted-foreground">
                No performance metrics available for this dataset
              </div>
            )}
          </main>
        </div>
      </div>
    </div>
  )
}
