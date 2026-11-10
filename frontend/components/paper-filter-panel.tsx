"use client"

import { useState, useMemo } from "react"
import { Search, X } from "lucide-react"
import { Input } from "@/components/ui/input"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Badge } from "@/components/ui/badge"
import { Separator } from "@/components/ui/separator"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Label } from "@/components/ui/label"
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion"
import type { PaperEntry, PaperFilterState } from "./paper-types"
import { formatValueLabel } from "@/lib/utils"

// Define numeric fields that should use range inputs
const NUMERIC_FIELDS = new Set([
  "year",
  "citations",
  "mpp",
  "tile_size_px",
  "n_patients",
  "n_slides",
  "n_tiles",
  "n_centers",
  "n_annotators",
])

interface PaperFilterPanelProps {
  data: PaperEntry[]
  filters: PaperFilterState
  onFilterChange: (field: string, values: string[]) => void
  onClearFilters: () => void
}

const EXCLUDED_FIELDS = ["url", "title", "authors", "abstract", "results", "citations"]

const CATEGORICAL_FIELDS = [
  "journal",
  "datasets_used",
  "preprocessing_methods",
  "augmentation_methods",
  "models_used",
  "model_family",
  "wsi_or_patch",
  "tissue_site",
  "domain",
  "biological_domain",
  "modality",
  "species",
  "staining_type",
  "task",
  "annotation_types",
  "annotation_level",
  "labels",
  "split_level",
  "split_method",
  "outcome_endpoint",
  "dataset_access",
  "license",
  "annotator_expertise",
  "annotator_consensus",
  "tiling_strategy",
]

export function PaperFilterPanel({
  data,
  filters,
  onFilterChange,
  onClearFilters,
}: PaperFilterPanelProps) {
  const [searchQuery, setSearchQuery] = useState("")
  const [numericRanges, setNumericRanges] = useState<Record<string, { min: string; max: string }>>({})

  // Extract unique values and ranges for each filterable field
  const { filterableFields, numericFieldRanges } = useMemo(() => {
    const values: Record<string, Set<string>> = {}
    const ranges: Record<string, { min: number; max: number }> = {}

    data.forEach((item) => {
      // Handle numeric fields
      NUMERIC_FIELDS.forEach((field) => {
        const value = item[field]
        if (value !== null && value !== undefined && String(value).toLowerCase() !== "nan") {
          const numValue = Number(value)
          if (!isNaN(numValue)) {
            if (!ranges[field]) {
              ranges[field] = { min: numValue, max: numValue }
            } else {
              ranges[field].min = Math.min(ranges[field].min, numValue)
              ranges[field].max = Math.max(ranges[field].max, numValue)
            }
          }
        }
      })

      // Handle categorical fields
      CATEGORICAL_FIELDS.forEach((field) => {
        if (EXCLUDED_FIELDS.includes(field)) return
        if (!values[field]) values[field] = new Set()

        const value = item[field]
        if (value !== null && value !== undefined) {
          const stringValue = String(value)
          // Handle pipe-separated values
          if (stringValue.includes("|")) {
            stringValue.split("|").forEach((v) => {
              const trimmed = v.trim()
              if (trimmed && trimmed.toLowerCase() !== "nan") {
                values[field].add(trimmed)
              }
            })
          }
          // Handle comma-separated values (for some fields)
          else if (stringValue.includes(",") && field !== "authors") {
            stringValue.split(",").forEach((v) => {
              const trimmed = v.trim()
              if (trimmed && trimmed.toLowerCase() !== "nan") {
                values[field].add(trimmed)
              }
            })
          } else if (stringValue.toLowerCase() !== "nan") {
            values[field].add(stringValue)
          }
        }
      })
    })

    // Convert sets to sorted arrays
    const result: Record<string, string[]> = {}
    Object.entries(values).forEach(([field, valueSet]) => {
      if (valueSet.size > 0 && valueSet.size <= 100) {
        result[field] = Array.from(valueSet).sort((a, b) =>
          a.localeCompare(b, undefined, { numeric: true })
        )
      }
    })

    return { filterableFields: result, numericFieldRanges: ranges }
  }, [data])

  // Combine and filter fields based on search query
  const filteredFields = useMemo(() => {
    const allFields = [...Object.keys(filterableFields), ...Object.keys(numericFieldRanges)]
    const uniqueFields = Array.from(new Set(allFields))
    
    if (!searchQuery) return uniqueFields
    const query = searchQuery.toLowerCase()
    return uniqueFields.filter((field) =>
      field.toLowerCase().includes(query)
    )
  }, [filterableFields, numericFieldRanges, searchQuery])

  // Count active filters
  const activeFilterCount = useMemo(() => {
    const categoricalCount = Object.values(filters).reduce((sum, values) => sum + values.length, 0)
    const numericCount = Object.keys(numericRanges).length
    return categoricalCount + numericCount
  }, [filters, numericRanges])

  const handleCheckboxChange = (field: string, value: string, checked: boolean) => {
    const currentValues = filters[field] || []
    const newValues = checked
      ? [...currentValues, value]
      : currentValues.filter((v) => v !== value)
    onFilterChange(field, newValues)
  }

  const isValueChecked = (field: string, value: string) => {
    return (filters[field] || []).includes(value)
  }

  const getActiveCountForField = (field: string) => {
    if (NUMERIC_FIELDS.has(field)) {
      return numericRanges[field] ? 1 : 0
    }
    return (filters[field] || []).length
  }

  const handleRangeChange = (field: string, type: "min" | "max", value: string) => {
    setNumericRanges((prev) => ({
      ...prev,
      [field]: {
        min: type === "min" ? value : prev[field]?.min || "",
        max: type === "max" ? value : prev[field]?.max || "",
      },
    }))
  }

  const applyNumericFilter = (field: string) => {
    const range = numericRanges[field]
    if (!range || (!range.min && !range.max)) {
      const newRanges = { ...numericRanges }
      delete newRanges[field]
      setNumericRanges(newRanges)
      onFilterChange(field, [])
      return
    }
    onFilterChange(field, [`${range.min || "*"}-${range.max || "*"}`])
  }

  const clearNumericFilter = (field: string) => {
    const newRanges = { ...numericRanges }
    delete newRanges[field]
    setNumericRanges(newRanges)
    onFilterChange(field, [])
  }

  const formatFieldName = (field: string) => {
    const acronyms = new Set([
      "wsi",
      "cnn",
      "rnn",
      "mlp",
      "ai",
      "roi",
      "id",
      "ssl",
      "ssl_method",
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

  return (
    <div className="space-y-4">
      {/* Search filters */}
      <div className="relative">
        <Search className="absolute left-2 top-2.5 h-4 w-4 text-muted-foreground" />
        <Input
          placeholder="Search filters..."
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
          className="pl-8"
        />
      </div>

      {/* Active filters count and clear button */}
      <div className="flex items-center justify-between">
        <span className="text-sm text-muted-foreground">
          {activeFilterCount} filter{activeFilterCount !== 1 ? "s" : ""} active
        </span>
        {activeFilterCount > 0 && (
          <Button
            variant="ghost"
            size="sm"
            onClick={onClearFilters}
            className="h-8 px-2 text-xs"
          >
            <X className="h-3 w-3 mr-1" />
            Clear all
          </Button>
        )}
      </div>

      <Separator />

      {/* Filter accordion */}
      <ScrollArea className="h-[calc(100vh-16rem)]">
        <Accordion type="multiple" className="w-full">
          {filteredFields.map((field) => {
            const activeCount = getActiveCountForField(field)
            const isNumeric = NUMERIC_FIELDS.has(field)
            const values = filterableFields[field] || []
            const range = numericFieldRanges[field]

            return (
              <AccordionItem key={field} value={field}>
                <AccordionTrigger className="text-sm hover:no-underline">
                  <div className="flex items-center justify-between w-full pr-2">
                    <span>{formatFieldName(field)}</span>
                    {activeCount > 0 && (
                      <Badge variant="secondary" className="ml-2">
                        {activeCount}
                      </Badge>
                    )}
                  </div>
                </AccordionTrigger>
                <AccordionContent>
                  {isNumeric && range ? (
                    <div className="space-y-3 pt-2">
                      <div className="text-xs text-muted-foreground">
                        Range: {range.min} - {range.max}
                      </div>
                      <div className="grid grid-cols-2 gap-2">
                        <div className="space-y-1">
                          <Label htmlFor={`${field}-min`} className="text-xs">
                            Min
                          </Label>
                          <Input
                            id={`${field}-min`}
                            type="number"
                            placeholder={String(range.min)}
                            value={numericRanges[field]?.min || ""}
                            onChange={(e) => handleRangeChange(field, "min", e.target.value)}
                            className="h-8"
                          />
                        </div>
                        <div className="space-y-1">
                          <Label htmlFor={`${field}-max`} className="text-xs">
                            Max
                          </Label>
                          <Input
                            id={`${field}-max`}
                            type="number"
                            placeholder={String(range.max)}
                            value={numericRanges[field]?.max || ""}
                            onChange={(e) => handleRangeChange(field, "max", e.target.value)}
                            className="h-8"
                          />
                        </div>
                      </div>
                      <div className="flex gap-2">
                        <Button
                          size="sm"
                          variant="default"
                          onClick={() => applyNumericFilter(field)}
                          className="flex-1 h-8 text-xs"
                        >
                          Apply
                        </Button>
                        {numericRanges[field] && (
                          <Button
                            size="sm"
                            variant="outline"
                            onClick={() => clearNumericFilter(field)}
                            className="h-8 px-2"
                          >
                            <X className="h-3 w-3" />
                          </Button>
                        )}
                      </div>
                    </div>
                  ) : (
                    <div className="space-y-2 pt-2 max-h-64 overflow-y-auto">
                      {values.map((value) => (
                        <div key={value} className="flex items-center space-x-2">
                          <Checkbox
                            id={`${field}-${value}`}
                            checked={isValueChecked(field, value)}
                            onCheckedChange={(checked) =>
                              handleCheckboxChange(field, value, checked as boolean)
                            }
                          />
                          <label
                            htmlFor={`${field}-${value}`}
                            className="text-sm font-normal leading-none peer-disabled:cursor-not-allowed peer-disabled:opacity-70 cursor-pointer flex-1"
                          >
                            {formatValueLabel(value)}
                          </label>
                        </div>
                      ))}
                    </div>
                  )}
                </AccordionContent>
              </AccordionItem>
            )
          })}
        </Accordion>
      </ScrollArea>
    </div>
  )
}
