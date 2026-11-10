"use client"

import { useMemo } from "react"
import { Database, Layers, MapPin } from "lucide-react"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import type { DatasetEntry } from "./paper-types"

interface DatasetStatsProps {
  data: DatasetEntry[]
  totalCount: number
}

export function DatasetStats({ data, totalCount }: DatasetStatsProps) {
  const stats = useMemo(() => {
    const totalSlides = data.reduce((sum, item) => {
      const numSlides = Number(item.n_slides)
      return sum + (isNaN(numSlides) ? 0 : numSlides)
    }, 0)

    const uniqueTissueSites = new Set<string>()
    data.forEach((item) => {
      if (item.tissue_site && String(item.tissue_site) !== "nan") {
        const tissueSite = String(item.tissue_site)
        if (tissueSite.includes("|")) {
          tissueSite.split("|").forEach((site) => uniqueTissueSites.add(site.trim()))
        } else {
          uniqueTissueSites.add(tissueSite)
        }
      }
    })

    return {
      totalDatasets: data.length,
      totalSlides,
      uniqueTissueSites: uniqueTissueSites.size,
    }
  }, [data])

  return (
    <div className="grid gap-4 md:grid-cols-3">
      <Card>
        <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
          <CardTitle className="text-sm font-medium">Total Datasets</CardTitle>
          <Database className="h-4 w-4 text-muted-foreground" />
        </CardHeader>
        <CardContent>
          <div className="text-2xl font-bold">{stats.totalDatasets}</div>
          <p className="text-xs text-muted-foreground">
            of {totalCount} total
          </p>
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
          <CardTitle className="text-sm font-medium">Total Slides</CardTitle>
          <Layers className="h-4 w-4 text-muted-foreground" />
        </CardHeader>
        <CardContent>
          <div className="text-2xl font-bold">{stats.totalSlides.toLocaleString()}</div>
          <p className="text-xs text-muted-foreground">
            across filtered datasets
          </p>
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
          <CardTitle className="text-sm font-medium">Tissue Sites</CardTitle>
          <MapPin className="h-4 w-4 text-muted-foreground" />
        </CardHeader>
        <CardContent>
          <div className="text-2xl font-bold">{stats.uniqueTissueSites}</div>
          <p className="text-xs text-muted-foreground">
            unique tissue types
          </p>
        </CardContent>
      </Card>
    </div>
  )
}
