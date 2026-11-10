"use client"

import { useEffect, useState, Suspense } from "react"
import { DatasetExplorer } from "@/components/dataset-explorer"
import { NavigationHeader } from "@/components/navigation-header"
import { supabase } from "@/lib/supabase"

function HomeContent() {
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
        <DatasetExplorer />
      </main>
    </div>
  )
}

export default function Home() {
  return (
    <Suspense fallback={<div>Loading...</div>}>
      <HomeContent />
    </Suspense>
  )
}
