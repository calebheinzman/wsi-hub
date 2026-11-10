"use client"

import { useState, useMemo, useEffect } from "react"
import { ArrowUpDown, ChevronLeft, ChevronRight, ExternalLink, Eye, EyeOff } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { Skeleton } from "@/components/ui/skeleton"
import type { DatasetEntry } from "./paper-types"
import { cn, formatValueLabel } from "@/lib/utils"
import { getFieldDescription } from "@/lib/field-descriptions"
import { formatDatasetName } from "@/lib/dataset-names"

interface DataTableProps {
  data: DatasetEntry[]
  onRowClick?: (dataset: DatasetEntry) => void
  loading?: boolean
}

interface ColumnConfig {
  key: string
  label: string
  priority: number
  minWidth?: string
  maxWidth?: string
  defaultVisible: boolean
  useBadges?: boolean
  description?: string
}

const COLUMNS: ColumnConfig[] = [
  { key: "dataset_name", label: "Dataset Name", priority: 1, minWidth: "200px", defaultVisible: true, useBadges: false },
  { key: "num_papers", label: "Papers", priority: 2, minWidth: "80px", defaultVisible: true, useBadges: false },
  { key: "year", label: "Year", priority: 3, minWidth: "80px", defaultVisible: true, useBadges: false },
  { key: "tissue_site", label: "Tissue Site", priority: 4, minWidth: "150px", defaultVisible: true, useBadges: true },
  { key: "domain", label: "Domain", priority: 5, minWidth: "120px", defaultVisible: true, useBadges: true },
  { key: "staining_type", label: "Staining", priority: 6, minWidth: "100px", defaultVisible: true, useBadges: true },
  { key: "description", label: "Description", priority: 7, minWidth: "300px", defaultVisible: true, useBadges: false },
  { key: "download_url", label: "Download", priority: 8, minWidth: "100px", defaultVisible: true, useBadges: false },
  { key: "species", label: "Species", priority: 9, minWidth: "100px", defaultVisible: false, useBadges: true },
  { key: "wsi_or_patch", label: "Type", priority: 10, minWidth: "100px", defaultVisible: false, useBadges: true },
  { key: "task", label: "Task", priority: 11, minWidth: "120px", defaultVisible: false, useBadges: true },
  { key: "n_slides", label: "Slides", priority: 12, minWidth: "80px", defaultVisible: false, useBadges: false },
  { key: "n_patients", label: "Patients", priority: 13, minWidth: "80px", defaultVisible: false, useBadges: false },
  { key: "n_tiles", label: "Tiles", priority: 14, minWidth: "80px", defaultVisible: false, useBadges: false },
  { key: "magnification", label: "Magnification", priority: 15, minWidth: "120px", defaultVisible: false, useBadges: false },
  { key: "approx_size_gb", label: "Size (GB)", priority: 16, minWidth: "80px", defaultVisible: false, useBadges: false },
]

const DEFAULT_VISIBLE_DESKTOP = ["dataset_name", "num_papers", "year", "tissue_site", "domain", "staining_type", "description", "download_url"]
const DEFAULT_VISIBLE_MOBILE = ["dataset_name", "num_papers", "year", "tissue_site"]

export function DataTable({ data, onRowClick, loading }: DataTableProps) {
  const [page, setPage] = useState(1)
  const [pageSize] = useState(10)
  const [sortField, setSortField] = useState<string | null>("num_papers")
  const [sortDirection, setSortDirection] = useState<"asc" | "desc">("desc")
  const [visibleColumns, setVisibleColumns] = useState<string[]>(DEFAULT_VISIBLE_DESKTOP)
  const [isMobile, setIsMobile] = useState(false)

  // Detect mobile
  useEffect(() => {
    const checkMobile = () => {
      const mobile = window.innerWidth < 768
      setIsMobile(mobile)
      if (mobile && visibleColumns.length > 4) {
        setVisibleColumns(DEFAULT_VISIBLE_MOBILE)
      }
    }
    checkMobile()
    window.addEventListener("resize", checkMobile)
    return () => window.removeEventListener("resize", checkMobile)
  }, [])

  // Sort data
  const sortedData = useMemo(() => {
    if (!sortField) return data

    return [...data].sort((a, b) => {
      const aValue = a[sortField]
      const bValue = b[sortField]

      if (aValue === bValue) return 0

      // Handle nulls
      if (aValue === null || aValue === undefined || String(aValue).toLowerCase() === "nan") return 1
      if (bValue === null || bValue === undefined || String(bValue).toLowerCase() === "nan") return -1

      // Numeric sorting
      if (typeof aValue === "number" && typeof bValue === "number") {
        return sortDirection === "asc" ? aValue - bValue : bValue - aValue
      }

      // String sorting with numeric awareness
      const comparison = String(aValue).localeCompare(String(bValue), undefined, { numeric: true })
      return sortDirection === "asc" ? comparison : -comparison
    })
  }, [data, sortField, sortDirection])

  // Paginate data
  const paginatedData = useMemo(() => {
    const start = (page - 1) * pageSize
    const end = start + pageSize
    return sortedData.slice(start, end)
  }, [sortedData, page, pageSize])

  const totalPages = Math.ceil(sortedData.length / pageSize)

  const handleSort = (field: string) => {
    if (sortField === field) {
      setSortDirection(sortDirection === "asc" ? "desc" : "asc")
    } else {
      setSortField(field)
      setSortDirection("desc")
    }
    setPage(1)
  }

  const toggleColumn = (columnKey: string) => {
    setVisibleColumns((prev) =>
      prev.includes(columnKey)
        ? prev.filter((key) => key !== columnKey)
        : [...prev, columnKey]
    )
  }

  const renderCellContent = (column: ColumnConfig, value: any, row?: Record<string, any>) => {
    // Handle null/undefined/nan
    if (value === null || value === undefined || String(value).toLowerCase() === "nan") {
      return <span className="text-muted-foreground">—</span>
    }

    if (column.key === "dataset_name") {
      return formatDatasetName(String(value), row?.slug)
    }

    // Handle download links
    if (column.key === "download_url") {
      return (
        <a
          href={String(value)}
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex items-center gap-1 text-blue-600 hover:underline"
          onClick={(e) => e.stopPropagation()}
        >
          <span>Link</span>
          <ExternalLink className="h-3 w-3" />
        </a>
      )
    }

    // Handle badges for multi-value fields
    if (column.useBadges) {
      const stringValue = String(value)
      if (stringValue.includes("|")) {
        const values = stringValue.split("|").map((v) => v.trim()).filter((v) => v && v.toLowerCase() !== "nan")
        return (
          <div className="flex flex-wrap gap-1">
            {values.slice(0, 3).map((v, i) => (
              <Badge key={i} variant="secondary" className="text-xs">
                {formatValueLabel(v)}
              </Badge>
            ))}
            {values.length > 3 && (
              <Badge variant="outline" className="text-xs">
                +{values.length - 3}
              </Badge>
            )}
          </div>
        )
      }
      return <Badge variant="secondary" className="text-xs">{formatValueLabel(stringValue)}</Badge>
    }

    // Handle long descriptions with tooltip
    if (column.key === "description") {
      const stringValue = String(value)
      if (stringValue.length > 50) {
        return (
          <TooltipProvider>
            <Tooltip>
              <TooltipTrigger asChild>
                <span className="cursor-help">{stringValue.slice(0, 50)}...</span>
              </TooltipTrigger>
              <TooltipContent className="max-w-md">
                <p>{stringValue}</p>
              </TooltipContent>
            </Tooltip>
          </TooltipProvider>
        )
      }
    }

    return String(value)
  }

  if (loading) {
    return (
      <div className="space-y-3">
        {[...Array(5)].map((_, i) => (
          <Skeleton key={i} className="h-12 w-full" />
        ))}
      </div>
    )
  }

  return (
    <div className="space-y-4">
      {/* Column visibility toggle */}
      <div className="flex justify-end">
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button variant="outline" size="sm">
              <Eye className="h-4 w-4 mr-2" />
              Columns
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="w-48">
            {COLUMNS.map((column) => (
              <DropdownMenuCheckboxItem
                key={column.key}
                checked={visibleColumns.includes(column.key)}
                onCheckedChange={() => toggleColumn(column.key)}
              >
                {column.label}
              </DropdownMenuCheckboxItem>
            ))}
          </DropdownMenuContent>
        </DropdownMenu>
      </div>

      {/* Table */}
      <div className="rounded-md border">
        <Table>
          <TableHeader>
            <TableRow>
              {COLUMNS.filter((col) => visibleColumns.includes(col.key)).map((column) => {
                const description = getFieldDescription(column.key)
                return (
                  <TableHead
                    key={column.key}
                    style={{ minWidth: column.minWidth, maxWidth: column.maxWidth }}
                    className="cursor-pointer hover:bg-muted/50"
                    onClick={() => handleSort(column.key)}
                  >
                    {description ? (
                      <TooltipProvider>
                        <Tooltip delayDuration={300}>
                          <TooltipTrigger asChild>
                            <div className="flex items-center gap-2">
                              <span className="border-b border-dotted border-muted-foreground/50">{column.label}</span>
                              <ArrowUpDown className="h-4 w-4" />
                              {sortField === column.key && (
                                <span className="text-xs text-muted-foreground">
                                  {sortDirection === "asc" ? "↑" : "↓"}
                                </span>
                              )}
                            </div>
                          </TooltipTrigger>
                          <TooltipContent side="top" className="max-w-xs text-sm">
                            <p>{description}</p>
                          </TooltipContent>
                        </Tooltip>
                      </TooltipProvider>
                    ) : (
                      <div className="flex items-center gap-2">
                        <span>{column.label}</span>
                        <ArrowUpDown className="h-4 w-4" />
                        {sortField === column.key && (
                          <span className="text-xs text-muted-foreground">
                            {sortDirection === "asc" ? "↑" : "↓"}
                          </span>
                        )}
                      </div>
                    )}
                  </TableHead>
                )
              })}
            </TableRow>
          </TableHeader>
          <TableBody>
            {paginatedData.length === 0 ? (
              <TableRow>
                <TableCell colSpan={visibleColumns.length} className="text-center py-8 text-muted-foreground">
                  No datasets found
                </TableCell>
              </TableRow>
            ) : (
              paginatedData.map((row) => (
                <TableRow
                  key={row.slug}
                  className="cursor-pointer hover:bg-muted/50"
                  onClick={() => onRowClick?.(row)}
                >
                  {COLUMNS.filter((col) => visibleColumns.includes(col.key)).map((column) => (
                    <TableCell key={column.key}>
                      {renderCellContent(column, row[column.key], row)}
                    </TableCell>
                  ))}
                </TableRow>
              ))
            )}
          </TableBody>
        </Table>
      </div>

      {/* Pagination */}
      {totalPages > 1 && (
        <div className="flex items-center justify-between">
          <div className="text-sm text-muted-foreground">
            Showing {(page - 1) * pageSize + 1} to {Math.min(page * pageSize, sortedData.length)} of {sortedData.length} results
          </div>
          <div className="flex items-center gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={() => setPage((p) => Math.max(1, p - 1))}
              disabled={page === 1}
            >
              <ChevronLeft className="h-4 w-4" />
              Previous
            </Button>
            <div className="flex items-center gap-1">
              {[...Array(Math.min(totalPages, 5))].map((_, i) => {
                let pageNum
                if (totalPages <= 5) {
                  pageNum = i + 1
                } else if (page <= 3) {
                  pageNum = i + 1
                } else if (page >= totalPages - 2) {
                  pageNum = totalPages - 4 + i
                } else {
                  pageNum = page - 2 + i
                }
                return (
                  <Button
                    key={i}
                    variant={page === pageNum ? "default" : "outline"}
                    size="sm"
                    onClick={() => setPage(pageNum)}
                    className="w-8 h-8 p-0"
                  >
                    {pageNum}
                  </Button>
                )
              })}
            </div>
            <Button
              variant="outline"
              size="sm"
              onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
              disabled={page === totalPages}
            >
              Next
              <ChevronRight className="h-4 w-4" />
            </Button>
          </div>
        </div>
      )}
    </div>
  )
}
