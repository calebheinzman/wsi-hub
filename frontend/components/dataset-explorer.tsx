"use client"

import { useState, useEffect, useMemo, useCallback } from "react"
import { useRouter, useSearchParams } from "next/navigation"
import { Search, Filter, Download } from "lucide-react"
import { Input } from "@/components/ui/input"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle, SheetTrigger } from "@/components/ui/sheet"
import { DataTable } from "./data-table"
import { FilterPanel } from "./filter-panel"
import { DatasetStats } from "./dataset-stats"
import { DatasetDetailView } from "./dataset-detail-view"
import { fetchAllRows } from "@/lib/supabase"
import { downloadCsv } from "@/lib/utils"
import type { DatasetEntry, FilterState } from "./paper-types"

export function DatasetExplorer() {
  const router = useRouter()
  const searchParams = useSearchParams()

  const [data, setData] = useState<DatasetEntry[]>([])
  const [filteredData, setFilteredData] = useState<DatasetEntry[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [searchQuery, setSearchQuery] = useState("")
  const [filters, setFilters] = useState<FilterState>({})
  const [activeFiltersCount, setActiveFiltersCount] = useState(0)
  const [selectedDataset, setSelectedDataset] = useState<DatasetEntry | null>(null)

  // Fetch data
  useEffect(() => {
    const fetchData = async () => {
      try {
        setLoading(true)
        
        // Paged: an unranged select silently caps at 1000 rows (final_datasets is 1,770).
        // Papers per dataset come from the dataset_paper_counts view (migration 0005),
        // not from paging all ~90k filtered_metrics rows into the browser to count them.
        const [datasetsData, countsData] = await Promise.all([
          fetchAllRows<DatasetEntry>("final_datasets", "*", { orderBy: "slug" }),
          fetchAllRows<{ dataset_slug: string; num_papers: number }>(
            "dataset_paper_counts",
            "dataset_slug, num_papers",
            { orderBy: "dataset_slug" }
          ),
        ])

        const paperCounts: Record<string, number> = {}
        countsData.forEach((row) => {
          paperCounts[row.dataset_slug] = row.num_papers
        })

        // Transform and enrich data
        const transformedData: DatasetEntry[] = (datasetsData || []).map((item) => {
          const dataset: DatasetEntry = {
            ...item,
            num_papers: paperCounts[item.slug] || 0,
            magnification:
              item.mag_min && item.mag_max
                ? item.mag_min === item.mag_max
                  ? `${item.mag_min}×`
                  : `${item.mag_min}× - ${item.mag_max}×`
                : undefined,
            tile_dimensions:
              item.tile_width_px && item.tile_height_px
                ? `${item.tile_width_px} × ${item.tile_height_px}`
                : undefined,
          }
          return dataset
        })

        // Remove duplicates by slug
        const uniqueData = transformedData.reduce((acc, item) => {
          if (!acc.find((d) => d.slug === item.slug)) {
            acc.push(item)
          }
          return acc
        }, [] as DatasetEntry[])

        setData(uniqueData)
        setFilteredData(uniqueData)
      } catch (err) {
        const e = err as any
        console.error("Error fetching data:", {
          message: e?.message ?? e,
          code: e?.code,
          details: e?.details,
          hint: e?.hint,
        })
        setError(`Failed to load datasets${e?.message ? `: ${e.message}` : ". Please try again later."}`)
      } finally {
        setLoading(false)
      }
    }

    fetchData()
  }, [])

  // Apply filters and search
  const filteredDataMemo = useMemo(() => {
    let result = [...data]

    // Apply field filters (AND between fields, OR within field)
    Object.entries(filters).forEach(([field, values]) => {
      if (values.length > 0) {
        result = result.filter((item) => {
          const itemValue = item[field]
          return values.some((value) => {
            // Handle numeric range filter format: "min-max" or "*-max" or "min-*"
            if (typeof value === "string" && value.includes("-") && (value.startsWith("*") || value.endsWith("*") || !isNaN(Number(value.split("-")[0])))) {
              const [minStr, maxStr] = value.split("-")
              const numValue = Number(itemValue)
              if (isNaN(numValue)) return false
              
              const min = minStr === "*" ? -Infinity : Number(minStr)
              const max = maxStr === "*" ? Infinity : Number(maxStr)
              
              return numValue >= min && numValue <= max
            }
            
            // Handle categorical/string filters
            const itemValueStr = String(itemValue || "").toLowerCase()
            const lowerValue = value.toLowerCase()
            
            // Check if field contains pipe-separated values
            if (itemValueStr.includes("|")) {
              return itemValueStr.split("|").some((v) => v.trim().toLowerCase() === lowerValue)
            }
            return itemValueStr === lowerValue || itemValueStr.includes(lowerValue)
          })
        })
      }
    })

    // Apply search query
    if (searchQuery) {
      const query = searchQuery.toLowerCase()
      result = result.filter((item) =>
        Object.values(item).some((value) =>
          String(value).toLowerCase().includes(query)
        )
      )
    }

    return result
  }, [data, filters, searchQuery])

  // Update filtered data when memo changes
  useEffect(() => {
    setFilteredData(filteredDataMemo)
  }, [filteredDataMemo])

  // Update active filters count
  useEffect(() => {
    const count = Object.values(filters).reduce((sum, values) => sum + values.length, 0)
    setActiveFiltersCount(count)
  }, [filters])

  const selectedPosition = useMemo(() => {
    if (!selectedDataset) return -1
    const index = filteredData.findIndex((item) => item.slug === selectedDataset.slug)
    return index >= 0 ? index + 1 : -1
  }, [selectedDataset, filteredData])

  // Deep-link support: open dataset detail when `?dataset=` is present in URL
  useEffect(() => {
    const slug = searchParams.get("dataset")

    if (!slug) {
      if (selectedDataset) {
        setSelectedDataset(null)
      }
      return
    }

    if (!data.length) return

    if (selectedDataset && selectedDataset.slug === slug) return

    const match = data.find((item) => item.slug === slug)
    if (match) {
      setSelectedDataset(match)
    }
  }, [searchParams, data, selectedDataset])

  // Handle filter change
  const handleFilterChange = useCallback((field: string, values: string[]) => {
    setFilters((prev) => ({ ...prev, [field]: values }))
  }, [])

  // Clear all filters
  const handleClearFilters = useCallback(() => {
    setFilters({})
    setSearchQuery("")
  }, [])

  // Export to CSV
  const handleExport = useCallback(() => {
    downloadCsv(filteredData as Record<string, unknown>[], `wsi_hub_datasets_${new Date().toISOString().split("T")[0]}.csv`)
  }, [filteredData])

  // Handle row click
  const handleRowClick = useCallback((dataset: DatasetEntry) => {
    setSelectedDataset(dataset)
    const encodedSlug = encodeURIComponent(dataset.slug)
    router.push(`/?dataset=${encodedSlug}`)
  }, [router])

  // Handle close detail view
  const handleCloseDetail = useCallback(() => {
    setSelectedDataset(null)
    router.push("/")
  }, [router])

  // Handle filter by field from detail view badge click
  const handleFilterByField = useCallback((field: string, value: string) => {
    // Close detail view and apply filter
    setSelectedDataset(null)
    router.push("/")
    // Add the filter value to existing filters for this field
    const currentValues = filters[field] || []
    if (!currentValues.includes(value)) {
      handleFilterChange(field, [...currentValues, value])
    }
  }, [filters, handleFilterChange, router])

  const handleNavigateSelectedDataset = useCallback(
    (direction: "next" | "previous") => {
      if (!selectedDataset) return
      const currentIndex = filteredData.findIndex((item) => item.slug === selectedDataset.slug)
      if (currentIndex === -1) return
      const nextIndex = direction === "next" ? currentIndex + 1 : currentIndex - 1
      if (nextIndex < 0 || nextIndex >= filteredData.length) return
      const nextDataset = filteredData[nextIndex]
      setSelectedDataset(nextDataset)
      const encodedSlug = encodeURIComponent(nextDataset.slug)
      router.push(`/?dataset=${encodedSlug}`)
    },
    [selectedDataset, filteredData, router]
  )

  // Show detail view if dataset is selected
  if (selectedDataset) {
    return (
      <DatasetDetailView 
        dataset={selectedDataset} 
        onClose={handleCloseDetail}
        onFilterByField={handleFilterByField}
        position={selectedPosition > 0 ? selectedPosition : undefined}
        totalItems={filteredData.length || undefined}
        onGoPrevious={
          selectedPosition > 1
            ? () => handleNavigateSelectedDataset("previous")
            : undefined
        }
        onGoNext={
          selectedPosition > 0 && selectedPosition < filteredData.length
            ? () => handleNavigateSelectedDataset("next")
            : undefined
        }
      />
    )
  }

  // Show error state
  if (error) {
    return (
      <Alert variant="destructive">
        <AlertDescription>{error}</AlertDescription>
      </Alert>
    )
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div>
        <h2 className="text-3xl font-bold tracking-tight">Dataset Explorer</h2>
        <p className="text-muted-foreground">
          Browse and explore whole-slide imaging datasets with comprehensive metadata
        </p>
      </div>

      {/* Stats */}
      <DatasetStats data={filteredData} totalCount={data.length} />

      {/* Search and filters */}
      <div className="flex flex-col sm:flex-row gap-4">
        <div className="flex-1 relative">
          <Search className="absolute left-2 top-2.5 h-4 w-4 text-muted-foreground" />
          <Input
            placeholder="Search datasets..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            className="pl-8"
          />
        </div>

        <div className="flex gap-2">
          <Sheet>
            <SheetTrigger asChild>
              <Button variant="outline">
                <Filter className="h-4 w-4 mr-2" />
                Filters
                {activeFiltersCount > 0 && (
                  <Badge variant="secondary" className="ml-2">
                    {activeFiltersCount}
                  </Badge>
                )}
              </Button>
            </SheetTrigger>
            <SheetContent side="left" className="w-[400px] sm:w-[540px]">
              <SheetHeader>
                <SheetTitle>Filter Datasets</SheetTitle>
                <SheetDescription>
                  Select filters to narrow down the dataset list
                </SheetDescription>
              </SheetHeader>
              <div className="mt-6">
                <FilterPanel
                  data={data}
                  filters={filters}
                  onFilterChange={handleFilterChange}
                  onClearFilters={handleClearFilters}
                />
              </div>
            </SheetContent>
          </Sheet>

          <Button variant="outline" onClick={handleExport} disabled={filteredData.length === 0}>
            <Download className="h-4 w-4 mr-2" />
            Export CSV
          </Button>
        </div>
      </div>

      {/* Table */}
      <DataTable data={filteredData} onRowClick={handleRowClick} loading={loading} />
    </div>
  )
}
