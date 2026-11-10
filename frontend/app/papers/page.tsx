"use client"

import { useEffect, useState, Suspense } from "react"
import { PaperExplorer } from "@/components/paper-explorer"
import { NavigationHeader } from "@/components/navigation-header"
import { supabase } from "@/lib/supabase"
import { Skeleton } from "@/components/ui/skeleton"

function PapersPageContent() {
  const [datasetCount, setDatasetCount] = useState<number>()
  const [paperCount, setPaperCount] = useState<number>()

  useEffect(() => {
    const fetchCounts = async () => {
      try {
        const { count: datasets } = await supabase
          .from("final_datasets")
          .select("*", { count: "exact", head: true })
        const { count: papers } = await supabase
          .from("papers_dct")
          .select("*", { count: "exact", head: true })
        if (datasets !== null) setDatasetCount(datasets)
        if (papers !== null) setPaperCount(papers)
      } catch (error) {
        console.error("Error fetching counts:", error)
      }
    }
    fetchCounts()
  }, [])

  return (
    <div className="min-h-screen bg-gray-50">
      <NavigationHeader datasetCount={datasetCount} paperCount={paperCount} />
      <main className="container mx-auto p-4 sm:p-6 lg:p-8">
        <PaperExplorer />
      </main>
    </div>
  )
}

export default function PapersPage() {
  return (
    <Suspense fallback={<PageSkeleton />}>
      <PapersPageContent />
    </Suspense>
  )
}

function PageSkeleton() {
  return (
    <div className="min-h-screen bg-gray-50">
      <Skeleton className="h-16 w-full" />
      <main className="container mx-auto p-4 sm:p-6 lg:p-8">
        <div className="space-y-6">
          <Skeleton className="h-24 w-full" />
          <Skeleton className="h-96 w-full" />
        </div>
      </main>
    </div>
  )
}
