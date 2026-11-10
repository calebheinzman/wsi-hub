"use client"

import { useState, useMemo, useEffect } from "react"
import { ArrowUpDown, ChevronLeft, ChevronRight, Eye } from "lucide-react"
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
import type { PaperEntry } from "./paper-types"
import { formatValueLabel } from "@/lib/utils"
import { datasetHref, useDatasetCatalog } from "@/lib/dataset-names"
import { SemanticBadge, parseBadgeValues } from "./semantic-badge"

interface PaperDataTableProps {
  data: PaperEntry[]
  onRowClick?: (paper: PaperEntry) => void
  loading?: boolean
  page?: number
  onPageChange?: (page: number) => void
}

interface ColumnConfig {
  key: string
  label: string
  priority: number
  minWidth?: string
  maxWidth?: string
  defaultVisible: boolean
  useBadges?: boolean
}

const parseAuthors = (raw: string): string[] => {
  const trimmed = (raw ?? "").trim()
  if (!trimmed) return []

  // Try JSON array first: '["A", "B"]'
  if (trimmed.startsWith("[") && trimmed.endsWith("]")) {
    try {
      const parsed = JSON.parse(trimmed)
      if (Array.isArray(parsed)) {
        return parsed.map((v) => String(v))
      }
    } catch {
      // fall through
    }
  }

  // Fallback to delimiter-based splitting
  if (trimmed.includes("|")) {
    return trimmed.split("|")
  }

  if (trimmed.includes(",")) {
    return trimmed.split(",")
  }

  return [trimmed]
}

const COLUMNS: ColumnConfig[] = [
  { key: "title", label: "Title", priority: 1, minWidth: "300px", defaultVisible: true, useBadges: false },
  { key: "authors", label: "Authors", priority: 2, minWidth: "200px", defaultVisible: true, useBadges: true },
  { key: "year", label: "Year", priority: 3, minWidth: "80px", defaultVisible: true, useBadges: false },
  { key: "citations", label: "Citations", priority: 4, minWidth: "100px", defaultVisible: true, useBadges: false },
  { key: "journal", label: "Journal", priority: 5, minWidth: "150px", defaultVisible: false, useBadges: false },
  { key: "methods", label: "Methods", priority: 6, minWidth: "200px", defaultVisible: false, useBadges: false },
  { key: "datasets_used", label: "Datasets", priority: 7, minWidth: "150px", defaultVisible: false, useBadges: true },
  { key: "models_used", label: "Models", priority: 8, minWidth: "150px", defaultVisible: false, useBadges: true },
  { key: "task", label: "Task", priority: 9, minWidth: "120px", defaultVisible: false, useBadges: true },
  { key: "tissue_site", label: "Tissue Site", priority: 10, minWidth: "120px", defaultVisible: false, useBadges: true },
  { key: "staining_type", label: "Staining", priority: 11, minWidth: "100px", defaultVisible: false, useBadges: true },
  { key: "preprocessing_methods", label: "Preprocessing", priority: 12, minWidth: "150px", defaultVisible: false, useBadges: true },
  { key: "augmentation_methods", label: "Augmentation", priority: 13, minWidth: "150px", defaultVisible: false, useBadges: true },
]

const DEFAULT_VISIBLE_DESKTOP = ["title", "authors", "year", "citations"]
const DEFAULT_VISIBLE_MOBILE = ["title", "year", "citations"]

export function PaperDataTable({ data, onRowClick, loading, page: controlledPage, onPageChange }: PaperDataTableProps) {
  const [uncontrolledPage, setUncontrolledPage] = useState(1)
  const [pageSize] = useState(10)
  const datasetCatalog = useDatasetCatalog()
  const [sortField, setSortField] = useState<string | null>("citations")
  const [sortDirection, setSortDirection] = useState<"asc" | "desc">("desc")
  const [visibleColumns, setVisibleColumns] = useState<string[]>(DEFAULT_VISIBLE_DESKTOP)
  const [isMobile, setIsMobile] = useState(false)

  const page = controlledPage ?? uncontrolledPage

  // Detect mobile
  useEffect(() => {
    const checkMobile = () => {
      const mobile = window.innerWidth < 768
      setIsMobile(mobile)
      if (mobile && visibleColumns.length > 3) {
        setVisibleColumns(DEFAULT_VISIBLE_MOBILE)
      }
    }
    checkMobile()
    window.addEventListener("resize", checkMobile)
    return () => window.removeEventListener("resize", checkMobile)
  }, [])

  const setPage = (newPage: number) => {
    if (onPageChange) {
      onPageChange(newPage)
    } else {
      setUncontrolledPage(newPage)
    }
  }

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

  const renderCellContent = (column: ColumnConfig, value: any) => {
    // Handle null/undefined/nan
    if (value === null || value === undefined || String(value).toLowerCase() === "nan") {
      return <span className="text-muted-foreground">—</span>
    }

    const stringValue = String(value)

    // Handle authors (comma-separated)
    if (column.key === "authors") {
      const authors = parseAuthors(stringValue)
        .map((a) => a.trim().replace(/^"|"$/g, ""))
        .filter((a) => a)
      if (authors.length > 3) {
        return (
          <div className="flex flex-wrap gap-1">
            {authors.slice(0, 3).map((author, i) => (
              <Badge key={i} variant="secondary" className="text-xs">
                {author}
              </Badge>
            ))}
            <Badge variant="outline" className="text-xs">
              +{authors.length - 3}
            </Badge>
          </div>
        )
      }
      return (
        <div className="flex flex-wrap gap-1">
          {authors.map((author, i) => (
            <Badge key={i} variant="secondary" className="text-xs">
              {author}
            </Badge>
          ))}
        </div>
      )
    }

    // Datasets: catalog names, linked to the dataset's page when the catalog has it
    if (column.key === "datasets_used") {
      const slugs = parseBadgeValues(stringValue).map((v) => v.trim()).filter((v) => v && v.toLowerCase() !== "nan")
      return (
        <div className="flex flex-wrap gap-1">
          {slugs.slice(0, 3).map((slug, i) => (
            <SemanticBadge
              key={i}
              value={slug}
              label={datasetCatalog.name(slug)}
              fieldName="datasets_used"
              className="text-xs"
              href={datasetCatalog.has(slug) ? datasetHref(slug) : undefined}
            />
          ))}
          {slugs.length > 3 && (
            <Badge variant="outline" className="text-xs">
              +{slugs.length - 3}
            </Badge>
          )}
        </div>
      )
    }

    // Handle badges for multi-value fields
    if (column.useBadges && (stringValue.includes("|") || stringValue.includes(","))) {
      const delimiter = stringValue.includes("|") ? "|" : ","
      const values = stringValue.split(delimiter).map((v) => v.trim()).filter((v) => v && v.toLowerCase() !== "nan")
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

    if (column.useBadges) {
      return <Badge variant="secondary" className="text-xs">{formatValueLabel(stringValue)}</Badge>
    }

    // Handle long titles with tooltip
    if (column.key === "title" && stringValue.length > 80) {
      return (
        <TooltipProvider>
          <Tooltip>
            <TooltipTrigger asChild>
              <span className="cursor-help line-clamp-2">{stringValue}</span>
            </TooltipTrigger>
            <TooltipContent className="max-w-md">
              <p>{stringValue}</p>
            </TooltipContent>
          </Tooltip>
        </TooltipProvider>
      )
    }

    // Handle long methods text with tooltip
    if (column.key === "methods" && stringValue.length > 60) {
      return (
        <TooltipProvider>
          <Tooltip>
            <TooltipTrigger asChild>
              <span className="cursor-help line-clamp-2 text-xs text-muted-foreground">{stringValue}</span>
            </TooltipTrigger>
            <TooltipContent className="max-w-lg">
              <p className="whitespace-pre-wrap">{stringValue}</p>
            </TooltipContent>
          </Tooltip>
        </TooltipProvider>
      )
    }

    return stringValue
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
              {COLUMNS.filter((col) => visibleColumns.includes(col.key)).map((column) => (
                <TableHead
                  key={column.key}
                  style={{ minWidth: column.minWidth, maxWidth: column.maxWidth }}
                  className="cursor-pointer hover:bg-muted/50"
                  onClick={() => handleSort(column.key)}
                >
                  <div className="flex items-center gap-2">
                    <span>{column.label}</span>
                    <ArrowUpDown className="h-4 w-4" />
                    {sortField === column.key && (
                      <span className="text-xs text-muted-foreground">
                        {sortDirection === "asc" ? "↑" : "↓"}
                      </span>
                    )}
                  </div>
                </TableHead>
              ))}
            </TableRow>
          </TableHeader>
          <TableBody>
            {paginatedData.length === 0 ? (
              <TableRow>
                <TableCell colSpan={visibleColumns.length} className="text-center py-8 text-muted-foreground">
                  No papers found
                </TableCell>
              </TableRow>
            ) : (
              paginatedData.map((row) => (
                <TableRow
                  key={row.url}
                  className="cursor-pointer hover:bg-muted/50"
                  onClick={() => onRowClick?.(row)}
                >
                  {COLUMNS.filter((col) => visibleColumns.includes(col.key)).map((column) => (
                    <TableCell key={column.key}>
                      {renderCellContent(column, row[column.key])}
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
              onClick={() => setPage(Math.max(1, page - 1))}
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
              onClick={() => setPage(Math.min(totalPages, page + 1))}
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
