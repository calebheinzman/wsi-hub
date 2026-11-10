"use client"

import { useState, useEffect, useMemo, useCallback } from "react"
import { useRouter, useSearchParams } from "next/navigation"
import { Search, Filter, Download } from "lucide-react"
import { Input } from "@/components/ui/input"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle, SheetTrigger } from "@/components/ui/sheet"
import { Skeleton } from "@/components/ui/skeleton"
import { PaperDataTable } from "./paper-data-table"
import { PaperFilterPanel } from "./paper-filter-panel"
import { PaperDetailView } from "./paper-detail-view"
import { supabase, fetchAllRows } from "@/lib/supabase"
import { downloadCsv } from "@/lib/utils"
import type { PaperEntry, PaperFilterState } from "./paper-types"

// Multi-value fields that need special handling (pipe- or comma-delimited)
const MULTI_VALUE_FIELDS = [
  "datasets_used",
  "preprocessing_methods",
  "augmentation_methods",
  "models_used",
  "model_family",
  "task",
  "tissue_site",
  "staining_type",
  "annotation_types",
  "annotation_level",
  "labels",
  "modality",
  "species",
] as const

export function PaperExplorer() {
  const router = useRouter()
  const searchParams = useSearchParams()
  const selectedUrl = searchParams.get("url")
  const decodedSelectedUrl = (() => {
    if (!selectedUrl) return null

    // Deep links frequently end up double-encoded (encodeURIComponent called more than once).
    // Try decoding up to twice, falling back to the original string if decoding fails.
    const tryDecode = (value: string) => {
      try {
        return decodeURIComponent(value)
      } catch {
        return value
      }
    }

    const once = tryDecode(selectedUrl)
    const twice = tryDecode(once)
    return twice.trim()
  })()
  const [allPapers, setAllPapers] = useState<PaperEntry[]>([])
  const [filteredPapers, setFilteredPapers] = useState<PaperEntry[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [searchQuery, setSearchQuery] = useState("")
  const [filters, setFilters] = useState<PaperFilterState>({})
  const [activeFiltersCount, setActiveFiltersCount] = useState(0)
  const [selectedPaper, setSelectedPaper] = useState<PaperEntry | null>(null)
  const [loadingInitialPaper, setLoadingInitialPaper] = useState(false)
  const [currentPage, setCurrentPage] = useState(1)

  // abstract and methods are ~60% of the payload, so the list loads without them and
  // LIST_TEXT_SELECT fills them in afterwards (search and CSV export use them).
  const LIST_PAPER_SELECT =
    "url,title,authors,citations,year,journal,datasets_used,preprocessing_methods,augmentation_methods,model_family,domain,biological_domain,modality,wsi_or_patch,tissue_site,species,staining_type,task,annotation_types,labels,mpp,scanner,tile_size_px,tiling_strategy,n_patients,n_slides,n_tiles,n_centers,class_counts,split_level,split_method,external_validation,n_annotators,annotator_expertise,annotator_consensus,outcome_endpoint,model_type,backbone,pretraining_strategy,foundation_model,uses_radiology,other_modalities,dataset_access,license,resource_urls,paper_doi,paper_venue"

  const LIST_TEXT_SELECT = "url,abstract,methods"

  const DETAIL_PAPER_SELECT =
    "url,title,authors,citations,year,journal,abstract,methods,datasets_used,preprocessing_methods,augmentation_methods,model_family,results,processing_pipeline,text_source,uses_private_data,private_datasets,domain,biological_domain,modality,wsi_or_patch,tissue_site,species,staining_type,task,annotation_types,labels,mpp,scanner,tile_size_px,tiling_strategy,n_patients,n_slides,n_tiles,n_centers,class_counts,split_level,split_method,external_validation,n_annotators,annotator_expertise,annotator_consensus,outcome_endpoint,model_type,backbone,pretraining_strategy,foundation_model,uses_radiology,other_modalities,dataset_access,license,resource_urls,paper_doi,paper_venue"

  const ensurePaperDetails = useCallback(
    async (paperUrl: string) => {
      const { data: paperData, error } = await supabase
        .from("papers_dct")
        .select(DETAIL_PAPER_SELECT)
        .eq("url", paperUrl)
        .maybeSingle()

      if (error) {
        console.error("Supabase error (paper details):", error)
        console.error("Supabase error (paper details) context:", {
          paperUrl,
          message: (error as any)?.message,
          code: (error as any)?.code,
          details: (error as any)?.details,
          hint: (error as any)?.hint,
        })
        throw error
      }

      return (paperData as PaperEntry | null) ?? null
    },
    [DETAIL_PAPER_SELECT]
  )

  // Keep currentPage in sync with `page` query param
  useEffect(() => {
    const pageParam = searchParams.get("page")
    const parsed = pageParam ? parseInt(pageParam, 10) : 1
    const normalized = Number.isFinite(parsed) && parsed > 0 ? parsed : 1
    setCurrentPage(normalized)
  }, [searchParams])

  // Fetch all papers
  useEffect(() => {
    const fetchPapers = async () => {
      try {
        setLoading(true)
        // Paged: an unranged select silently returns only the first 1000 of 14,051 rows.
        const papersData = await fetchAllRows<PaperEntry>("papers_dct", LIST_PAPER_SELECT, { orderBy: "url" })

        setAllPapers(papersData)
        setFilteredPapers(papersData)
        setLoading(false)

        // Background pass for the long text columns, one page at a time (see fetchAllRows).
        // If it fails the list still works; search just can't match abstracts or methods.
        fetchAllRows<Pick<PaperEntry, "url" | "abstract" | "methods">>("papers_dct", LIST_TEXT_SELECT, {
          orderBy: "url",
          concurrency: 1,
        })
          .then((textRows) => {
            const byUrl = new Map(textRows.map((row) => [row.url, row]))
            setAllPapers((papers) =>
              papers.map((paper) => {
                const text = byUrl.get(paper.url)
                return text ? { ...paper, abstract: text.abstract, methods: text.methods } : paper
              })
            )
          })
          .catch((err) => console.error("Error fetching paper abstracts/methods:", err))
      } catch (err) {
        const e = err as any
        console.error("Error fetching papers:", {
          message: e?.message ?? e,
          code: e?.code,
          details: e?.details,
          hint: e?.hint,
        })
        setError(`Failed to load papers${e?.message ? `: ${e.message}` : ". Please try again later."}`)
      } finally {
        setLoading(false)
      }
    }

    fetchPapers()
  }, [LIST_PAPER_SELECT, LIST_TEXT_SELECT])

  // Load or clear selected paper when URL param changes (deep linking)
  useEffect(() => {
    const loadInitialPaper = async () => {
      // No URL -> ensure we clear any selected paper
      if (!decodedSelectedUrl) {
        setSelectedPaper(null)
        return
      }

      // If we already have this paper selected, do nothing. Compare against every decoding
      // of the param: a URL that itself contains an escape (".../Dissertation%20Silimon.pdf")
      // decodes past its stored form, never matched, and reloaded the paper every render.
      const decodeOnce = (value: string) => {
        try {
          return decodeURIComponent(value)
        } catch {
          return value
        }
      }
      if (selectedPaper && selectedUrl &&
          [selectedUrl, decodeOnce(selectedUrl), decodedSelectedUrl].includes(selectedPaper.url)) {
        return
      }

      if (!loadingInitialPaper) {
        setLoadingInitialPaper(true)

        try {
          const buildCandidateUrls = () => {
            const tryDecode = (value: string) => {
              try {
                return decodeURIComponent(value)
              } catch {
                return value
              }
            }

            const candidates = new Set<string>()
            const add = (v: string | null | undefined) => {
              if (!v) return
              const trimmed = v.trim()
              if (!trimmed) return
              candidates.add(trimmed)
              if (trimmed.endsWith("/")) candidates.add(trimmed.replace(/\/+$/, ""))
              else candidates.add(`${trimmed}/`)
            }

            add(decodedSelectedUrl)
            add(selectedUrl)
            add(selectedUrl ? tryDecode(selectedUrl) : null)
            add(selectedUrl ? tryDecode(tryDecode(selectedUrl)) : null)

            return Array.from(candidates)
          }

          // Fast path: if we already fetched all papers, try selecting from local list first.
          // This avoids an extra round trip and also handles cases where Supabase lookup fails
          // due to subtle encoding mismatches.
          const candidates = buildCandidateUrls()
          const localMatch = allPapers.find((p) => candidates.includes(p.url))
          if (localMatch) {
            const detailed = await ensurePaperDetails(localMatch.url)
            setSelectedPaper(detailed ?? localMatch)
            return
          }

          let found: PaperEntry | null = null
          for (const candidate of candidates) {
            const { data: paperData, error } = await supabase
              .from("papers_dct")
              .select(DETAIL_PAPER_SELECT)
              .eq("url", candidate)
              .maybeSingle()

            if (error) {
              // Log the raw error object too; PostgrestError sometimes doesn't serialize cleanly.
              console.error("Supabase error (paper deep link):", error)
              console.error("Supabase error (paper deep link) context:", {
                selectedUrl,
                decodedSelectedUrl,
                candidateUrl: candidate,
                candidates,
                message: (error as any)?.message,
                code: (error as any)?.code,
                details: (error as any)?.details,
                hint: (error as any)?.hint,
              })
              throw error
            }

            if (paperData) {
              found = paperData as PaperEntry
              break
            }
          }

          if (found) {
            setSelectedPaper(found)
          } else {
            console.warn("Paper deep link: no match found for url param", {
              selectedUrl,
              decodedSelectedUrl,
              candidates,
            })
          }
        } catch (err) {
          const e = err as any
          console.error("Error loading initial paper:", err)
          console.error("Error loading initial paper context:", {
            selectedUrl,
            decodedSelectedUrl,
            message: e?.message ?? e,
            code: e?.code,
            details: e?.details,
            hint: e?.hint,
          })
        } finally {
          setLoadingInitialPaper(false)
        }
      }
    }

    loadInitialPaper()
  }, [selectedUrl, decodedSelectedUrl, selectedPaper, loadingInitialPaper, allPapers, ensurePaperDetails, DETAIL_PAPER_SELECT])

  // Apply filters and search (skip if we're showing detail view to avoid expensive computation)
  const papersMemo = useMemo(() => {
    // Skip filtering if we're showing a paper detail or loading one
    if (selectedPaper || loadingInitialPaper) {
      return allPapers
    }

    let result = [...allPapers]

    // Apply field filters
    Object.entries(filters).forEach(([field, selectedValues]) => {
      if (selectedValues.length > 0) {
        result = result.filter((paper) => {
          const paperValue = paper[field]
          
          return selectedValues.some((filterVal) => {
            // Handle numeric range filter format: "min-max" or "*-max" or "min-*"
            if (typeof filterVal === "string" && filterVal.includes("-") && (filterVal.startsWith("*") || filterVal.endsWith("*") || !isNaN(Number(filterVal.split("-")[0])))) {
              const [minStr, maxStr] = filterVal.split("-")
              const numValue = Number(paperValue)
              if (isNaN(numValue)) return false
              
              const min = minStr === "*" ? -Infinity : Number(minStr)
              const max = maxStr === "*" ? Infinity : Number(maxStr)
              
              return numValue >= min && numValue <= max
            }
            
            // Handle multi-value fields
            const paperValueStr = String(paperValue || "").toLowerCase()
            if (MULTI_VALUE_FIELDS.includes(field as (typeof MULTI_VALUE_FIELDS)[number]) && (paperValueStr.includes("|") || paperValueStr.includes(","))) {
              const delimiter = paperValueStr.includes("|") ? "|" : ","
              const itemValues = paperValueStr.split(delimiter).map((v) => v.trim().toLowerCase())
              return itemValues.includes(filterVal.toLowerCase())
            }

            // Single value matching
            return paperValueStr.includes(filterVal.toLowerCase())
          })
        })
      }
    })

    // Apply search query
    if (searchQuery) {
      const query = searchQuery.toLowerCase()
      result = result.filter((paper) =>
        Object.values(paper).some((value) => String(value).toLowerCase().includes(query))
      )
    }

    return result
  }, [allPapers, filters, searchQuery, selectedPaper, loadingInitialPaper])

  // Update filtered papers when memo changes
  useEffect(() => {
    setFilteredPapers(papersMemo)
  }, [papersMemo])

  // Update active filters count
  useEffect(() => {
    const count = Object.values(filters).reduce((sum, values) => sum + values.length, 0)
    setActiveFiltersCount(count)
  }, [filters])

  const selectedPosition = useMemo(() => {
    if (!selectedPaper) return -1
    const index = filteredPapers.findIndex((paper) => paper.url === selectedPaper.url)
    return index >= 0 ? index + 1 : -1
  }, [selectedPaper, filteredPapers])

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
    downloadCsv(filteredPapers as Record<string, unknown>[], `wsi_hub_papers_${new Date().toISOString().split("T")[0]}.csv`)
  }, [filteredPapers])

  // Handle row click - update URL so each paper has its own link
  const handleRowClick = useCallback(
    (paper: PaperEntry) => {
      ;(async () => {
        try {
          setLoadingInitialPaper(true)

          const detailed = await ensurePaperDetails(paper.url)
          setSelectedPaper(detailed ?? paper)

          // URLSearchParams encodes the value; encoding it first as well double-encoded it.
          const params = new URLSearchParams(searchParams.toString())
          params.set("url", paper.url)
          params.set("page", String(currentPage))
          router.push(`/papers?${params.toString()}`)
        } catch {
          setSelectedPaper(paper)
        } finally {
          setLoadingInitialPaper(false)
        }
      })()
    },
    [router, searchParams, currentPage, ensurePaperDetails]
  )

  // Handle close detail view - return to main papers explorer
  const handleCloseDetail = useCallback(() => {
    setSelectedPaper(null)
    const params = new URLSearchParams(searchParams.toString())
    params.delete("url")
    if (!params.get("page")) {
      params.set("page", String(currentPage))
    }
    router.push(`/papers?${params.toString()}`)
  }, [router, searchParams, currentPage])

  const handlePageChange = useCallback((page: number) => {
    setCurrentPage(page)
    const params = new URLSearchParams(searchParams.toString())
    params.set("page", String(page))
    if (selectedUrl) {
      params.set("url", selectedUrl)
    }
    router.push(`/papers?${params.toString()}`)
  }, [router, searchParams, selectedUrl])

  const handleFilterByField = useCallback((field: string, value: string) => {
    // Clear current paper selection and URL param so we return to list view
    setSelectedPaper(null)
    setCurrentPage(1)
    const params = new URLSearchParams()
    params.set("page", "1")
    router.push(`/papers?${params.toString()}`)

    const currentValues = filters[field] || []
    if (!currentValues.includes(value)) {
      handleFilterChange(field, [...currentValues, value])
    }
  }, [filters, handleFilterChange, router])

  const handleNavigateSelectedPaper = useCallback(
    (direction: "next" | "previous") => {
      if (!selectedPaper) return
      const currentIndex = filteredPapers.findIndex((paper) => paper.url === selectedPaper.url)
      if (currentIndex === -1) return
      const nextIndex = direction === "next" ? currentIndex + 1 : currentIndex - 1
      if (nextIndex < 0 || nextIndex >= filteredPapers.length) return
      const nextPaper = filteredPapers[nextIndex]
      setSelectedPaper(nextPaper)
      const params = new URLSearchParams(searchParams.toString())
      params.set("url", nextPaper.url)
      if (!params.get("page")) {
        params.set("page", String(currentPage))
      }
      router.push(`/papers?${params.toString()}`)
    },
    [selectedPaper, filteredPapers, searchParams, currentPage, router]
  )

  // Show loading state for initial paper (deep link)
  if (loadingInitialPaper) {
    return (
      <div className="space-y-6">
        <Skeleton className="h-12 w-full" />
        <Skeleton className="h-64 w-full" />
      </div>
    )
  }

  // Show detail view if paper is selected (skip expensive list view rendering)
  if (selectedPaper) {
    return (
      <PaperDetailView
        paper={selectedPaper}
        onClose={handleCloseDetail}
        onFilterByField={handleFilterByField}
        position={selectedPosition > 0 ? selectedPosition : undefined}
        totalItems={filteredPapers.length || undefined}
        onGoPrevious={
          selectedPosition > 1
            ? () => handleNavigateSelectedPaper("previous")
            : undefined
        }
        onGoNext={
          selectedPosition > 0 && selectedPosition < filteredPapers.length
            ? () => handleNavigateSelectedPaper("next")
            : undefined
        }
      />
    )
  }

  // Show loading state while fetching papers list
  if (loading) {
    return (
      <div className="space-y-6">
        <Skeleton className="h-12 w-full" />
        <Skeleton className="h-24 w-full" />
        <Skeleton className="h-96 w-full" />
      </div>
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
        <h2 className="text-3xl font-bold tracking-tight">Paper Explorer</h2>
        <p className="text-muted-foreground">
          Browse and explore whole-slide imaging research papers
        </p>
      </div>

      {/* Stats */}
      <div className="grid gap-4 md:grid-cols-3">
        <div className="bg-white rounded-lg border p-4">
          <div className="text-2xl font-bold">{filteredPapers.length}</div>
          <p className="text-xs text-muted-foreground">
            of {allPapers.length} papers
          </p>
        </div>
        <div className="bg-white rounded-lg border p-4">
          <div className="text-2xl font-bold">
            {allPapers.reduce((sum, p) => sum + (p.citations || 0), 0).toLocaleString()}
          </div>
          <p className="text-xs text-muted-foreground">total citations</p>
        </div>
        <div className="bg-white rounded-lg border p-4">
          <div className="text-2xl font-bold">
            {new Set(allPapers.map((p) => p.year).filter((y) => y)).size}
          </div>
          <p className="text-xs text-muted-foreground">publication years</p>
        </div>
      </div>

      {/* Search and filters */}
      <div className="flex flex-col sm:flex-row gap-4">
        <div className="flex-1 relative">
          <Search className="absolute left-2 top-2.5 h-4 w-4 text-muted-foreground" />
          <Input
            placeholder="Search papers..."
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
                <SheetTitle>Filter Papers</SheetTitle>
                <SheetDescription>
                  Select filters to narrow down the paper list
                </SheetDescription>
              </SheetHeader>
              <div className="mt-6">
                <PaperFilterPanel
                  data={allPapers}
                  filters={filters}
                  onFilterChange={handleFilterChange}
                  onClearFilters={handleClearFilters}
                />
              </div>
            </SheetContent>
          </Sheet>

          <Button variant="outline" onClick={handleExport} disabled={filteredPapers.length === 0}>
            <Download className="h-4 w-4 mr-2" />
            Export CSV
          </Button>
        </div>
      </div>

      {/* Table */}
      <PaperDataTable
        data={filteredPapers}
        onRowClick={handleRowClick}
        loading={loading}
        page={currentPage}
        onPageChange={handlePageChange}
      />
    </div>
  )
}
