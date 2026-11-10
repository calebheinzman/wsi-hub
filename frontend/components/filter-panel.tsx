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
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import type { DatasetEntry, FilterState } from "./paper-types"
import { formatValueLabel } from "@/lib/utils"
import { getFieldDescription } from "@/lib/field-descriptions"

// Define numeric fields that should use range inputs
const NUMERIC_FIELDS = new Set([
  "year",
  "mag_min",
  "mag_max",
  "mpp",
  "tile_size_px",
  "approx_size_gb",
  "n_slides",
  "n_patients",
  "n_tiles",
  "n_centers",
])

interface FilterPanelProps {
  data: DatasetEntry[]
  filters: FilterState
  onFilterChange: (field: string, values: string[]) => void
  onClearFilters: () => void
}

export function FilterPanel({
  data,
  filters,
  onFilterChange,
  onClearFilters,
}: FilterPanelProps) {
  const [searchQuery, setSearchQuery] = useState("")
  const [numericRanges, setNumericRanges] = useState<Record<string, { min: string; max: string }>>({})

  // Extract unique values and ranges for each filterable field
  const { filterableFields, numericFieldRanges } = useMemo(() => {
    const values: Record<string, Set<string>> = {}
    const ranges: Record<string, { min: number; max: number }> = {}
    const excludedFields = ["description", "slug", "download_url", "code_url", "home_page_url", "resource_urls", "num_papers", "magnification", "tile_dimensions", "paper_doi", "paper_venue"]

    data.forEach((item) => {
      Object.keys(item).forEach((field) => {
        if (excludedFields.includes(field)) return
        
        const value = item[field]
        if (value !== null && value !== undefined && String(value).toLowerCase() !== "nan") {
          // Handle numeric fields
          if (NUMERIC_FIELDS.has(field)) {
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
          // Handle categorical fields
          else {
            if (!values[field]) values[field] = new Set()
            if (typeof value === "string" && value.includes("|")) {
              value.split("|").forEach((v) => {
                const trimmed = v.trim()
                if (trimmed) values[field].add(trimmed)
              })
            } else {
              values[field].add(String(value))
            }
          }
        }
      })
    })

    // Convert sets to sorted arrays
    const result: Record<string, string[]> = {}
    Object.entries(values).forEach(([field, valueSet]) => {
      if (valueSet.size > 0 && valueSet.size <= 100) { // Limit to reasonable number of options
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

  // Handle numeric range changes
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
      // Clear filter if both are empty
      const newRanges = { ...numericRanges }
      delete newRanges[field]
      setNumericRanges(newRanges)
      onFilterChange(field, [])
      return
    }
    
    // Create a synthetic filter value for numeric range
    onFilterChange(field, [`${range.min || "*"}-${range.max || "*"}`])
  }

  const clearNumericFilter = (field: string) => {
    const newRanges = { ...numericRanges }
    delete newRanges[field]
    setNumericRanges(newRanges)
    onFilterChange(field, [])
  }

  // Format field name for display
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
                    {getFieldDescription(field) ? (
                      <Tooltip delayDuration={200}>
                        <TooltipTrigger asChild>
                          <span className="cursor-help border-b border-dotted border-muted-foreground/40 hover:border-muted-foreground">
                            {formatFieldName(field)}
                          </span>
                        </TooltipTrigger>
                        <TooltipContent side="right" className="max-w-xs text-sm">
                          <p>{getFieldDescription(field)}</p>
                        </TooltipContent>
                      </Tooltip>
                    ) : (
                      <span>{formatFieldName(field)}</span>
                    )}
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
