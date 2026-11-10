"use client"

import { useState, useEffect, useMemo } from "react"
import Link from "next/link"
import { ArrowUpDown, ExternalLink } from "lucide-react"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { Skeleton } from "@/components/ui/skeleton"
import { supabase } from "@/lib/supabase"
import { isMetricKey, METRIC_LABELS, SPLIT_LABELS, textSourceNote } from "@/lib/metrics"

interface MetricTableProps {
  title: string
  data: Record<string, Record<string, any>> | undefined
}

export function MetricTable({ title, data }: MetricTableProps) {
  const [paperDisplayInfo, setPaperDisplayInfo] = useState<Record<string, { title: string }>>({})
  const [loadingPaperInfo, setLoadingPaperInfo] = useState(true)
  const [sortKey, setSortKey] = useState<string | null>(null)
  const [sortDirection, setSortDirection] = useState<"asc" | "desc">("desc")

  // Fetch paper titles. The detail view is reused when stepping between datasets, so a
  // slow lookup for the previous dataset could land last and replace this one's titles,
  // leaving every row on "Loading..." except papers the two datasets share.
  useEffect(() => {
    let live = true

    const fetchPaperDisplayInfo = async () => {
      if (!data) return

      const paperUrls = Object.keys(data)
      setPaperDisplayInfo({})
      setLoadingPaperInfo(paperUrls.length > 0)
      if (paperUrls.length === 0) return

      try {
        const { data: papersData, error } = await supabase
          .from("papers_dct")
          .select("url, title")
          .in("url", paperUrls)

        if (error) throw error
        if (!live) return

        const paperInfo: Record<string, { title: string }> = {}
        papersData?.forEach((paper) => {
          paperInfo[paper.url] = { title: paper.title || "Untitled Paper" }
        })
        setPaperDisplayInfo(paperInfo)
      } catch (error) {
        console.error("Error fetching paper info:", error)
      } finally {
        if (live) setLoadingPaperInfo(false)
      }
    }

    fetchPaperDisplayInfo()
    return () => {
      live = false
    }
  }, [data])

  // A row whose title could not be found shows the link's site rather than "Loading..." forever.
  const fallbackTitle = (url: string) => {
    try {
      return new URL(url).hostname.replace(/^www\./, "")
    } catch {
      return url
    }
  }

  const isMeaningfulMetricValue = (value: any) => {
    if (value === null || value === undefined) return false

    const str = String(value).trim()
    if (!str) return false

    const lower = str.toLowerCase()
    if (lower === "nan" || lower === "none" || lower === "null" || lower === "unknown" || lower === "n/a" || lower === "na") {
      return false
    }

    const num = Number(str)
    if (!isNaN(num) && num === 0) return false

    return true
  }

  // Get metric columns, but only keep columns that have at least one meaningful value
  const metricColumns = useMemo(() => {
    if (!data) return []
    const allMetrics = new Set<string>()
    Object.values(data).forEach((metrics) => {
      // Only score columns: the rest of a row (split, subset, metric_key, text_source...)
      // describes the evaluation and used to show up as columns of its own.
      Object.keys(metrics).forEach((key) => {
        if (isMetricKey(key)) {
          allMetrics.add(key)
        }
      })
    })

    const columns = Array.from(allMetrics)
    const filtered = columns.filter((key) => {
      return Object.values(data).some((metrics) => isMeaningfulMetricValue((metrics as any)[key]))
    })

    return filtered.sort()
  }, [data])

  // Set default sort key
  useEffect(() => {
    if (!sortKey && metricColumns.length > 0) {
      if (title.toLowerCase().includes("classification")) {
        setSortKey("accuracy")
      } else if (title.toLowerCase().includes("segmentation")) {
        setSortKey("dice_coefficient")
      } else {
        setSortKey(metricColumns[0])
      }
    }
  }, [metricColumns, sortKey, title])

  // Sort data
  const sortedPapers = useMemo(() => {
    if (!data) return []

    const papers = Object.entries(data).map(([url, metrics]) => ({
      url,
      metrics,
    }))

    if (!sortKey) return papers

    return papers.sort((a, b) => {
      const aValue = a.metrics[sortKey]
      const bValue = b.metrics[sortKey]

      // Handle nulls
      if (aValue === null || aValue === undefined) return 1
      if (bValue === null || bValue === undefined) return -1

      const aNum = Number(aValue)
      const bNum = Number(bValue)

      if (isNaN(aNum) || isNaN(bNum)) return 0

      return sortDirection === "asc" ? aNum - bNum : bNum - aNum
    })
  }, [data, sortKey, sortDirection])

  const handleSort = (key: string) => {
    if (sortKey === key) {
      setSortDirection(sortDirection === "asc" ? "desc" : "asc")
    } else {
      setSortKey(key)
      setSortDirection("desc")
    }
  }

  const formatMetricName = (name: string) => {
    if (METRIC_LABELS[name]) return METRIC_LABELS[name]
    return name
      .split("_")
      .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
      .join(" ")
  }

  const formatMetricValue = (value: any) => {
    if (!isMeaningfulMetricValue(value)) return ""

    const str = String(value).trim()
    const num = Number(str)
    if (isNaN(num)) return str
    return num.toFixed(3)
  }

  if (!data || Object.keys(data).length === 0) {
    return (
      <div className="text-center py-8 text-muted-foreground">
        No {title.toLowerCase()} metrics available
      </div>
    )
  }

  if (loadingPaperInfo) {
    return (
      <div className="space-y-3">
        {[...Array(3)].map((_, i) => (
          <Skeleton key={i} className="h-12 w-full" />
        ))}
      </div>
    )
  }

  return (
    <div className="rounded-md border overflow-x-auto">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead className="sticky left-0 bg-background z-10 min-w-[300px]">
              Paper
            </TableHead>
            {metricColumns.map((column) => (
              <TableHead
                key={column}
                className="cursor-pointer hover:bg-muted/50 min-w-[120px]"
                onClick={() => handleSort(column)}
              >
                <div className="flex items-center gap-2">
                  <span>{formatMetricName(column)}</span>
                  <ArrowUpDown className="h-4 w-4" />
                  {sortKey === column && (
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
          {sortedPapers.map(({ url, metrics }) => (
            <TableRow key={url}>
              <TableCell className="sticky left-0 bg-background z-10 font-medium">
                <Link
                  href={`/papers?url=${encodeURIComponent(url)}`}
                  className="inline-flex items-center gap-1 text-blue-600 hover:underline"
                  onClick={(e) => e.stopPropagation()}
                >
                  <span className="line-clamp-2">
                    {paperDisplayInfo[url]?.title || (loadingPaperInfo ? "Loading..." : fallbackTitle(url))}
                  </span>
                  <ExternalLink className="h-3 w-3 flex-shrink-0" />
                </Link>
                {(metrics.split || textSourceNote(metrics.text_source)) && (
                  <div className="mt-1 text-xs font-normal text-muted-foreground">
                    {[metrics.split ? SPLIT_LABELS[metrics.split] || metrics.split : null,
                      textSourceNote(metrics.text_source)].filter(Boolean).join(" · ")}
                  </div>
                )}
              </TableCell>
              {metricColumns.map((column) => (
                <TableCell key={column}>
                  {formatMetricValue(metrics[column])}
                </TableCell>
              ))}
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  )
}
