"use client"

import Link from "next/link"
import { usePathname } from "next/navigation"
import { Microscope, Database, FileText } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { cn } from "@/lib/utils"

interface NavigationHeaderProps {
  datasetCount?: number
  paperCount?: number
}

export function NavigationHeader({ datasetCount, paperCount }: NavigationHeaderProps) {
  const pathname = usePathname()
  const isDatasets = pathname === "/"
  const isPapers = pathname === "/papers"

  return (
    <header className="sticky top-0 z-50 w-full border-b bg-white shadow-sm">
      <div className="container mx-auto px-4 sm:px-6 lg:px-8">
        <div className="flex h-16 items-center">
          {/* Logo and Title */}
          <Link href="/" className="flex items-center gap-2 hover:opacity-80 transition-opacity mr-8">
            <Microscope className="h-6 w-6 text-blue-600" />
            <h1 className="text-xl font-bold">
              <span className="hidden sm:inline">WSI </span>
              <span>Hub</span>
            </h1>
          </Link>

          {/* Navigation Tabs - Centered */}
          <nav className="flex items-center gap-2 flex-1 justify-center">
            <Link
              href="/"
              className={cn(
                "flex items-center gap-2 px-5 py-2.5 text-sm font-medium rounded-lg transition-all",
                isDatasets
                  ? "bg-blue-600 text-white shadow-md"
                  : "bg-gray-100 hover:bg-gray-200 text-gray-700"
              )}
            >
              <Database className="h-4 w-4" />
              <span>Datasets</span>
              {datasetCount !== undefined && (
                <Badge 
                  variant={isDatasets ? "secondary" : "outline"} 
                  className={cn("ml-1", isDatasets ? "bg-white/20 text-white border-white/30" : "")}
                >
                  {datasetCount}
                </Badge>
              )}
            </Link>

            <Link
              href="/papers"
              className={cn(
                "flex items-center gap-2 px-5 py-2.5 text-sm font-medium rounded-lg transition-all",
                isPapers
                  ? "bg-blue-600 text-white shadow-md"
                  : "bg-gray-100 hover:bg-gray-200 text-gray-700"
              )}
            >
              <FileText className="h-4 w-4" />
              <span>Papers</span>
              {paperCount !== undefined && (
                <Badge 
                  variant={isPapers ? "secondary" : "outline"}
                  className={cn("ml-1", isPapers ? "bg-white/20 text-white border-white/30" : "")}
                >
                  {paperCount}
                </Badge>
              )}
            </Link>
          </nav>

          {/* Quick Stats (Desktop Only) */}
          <div className="hidden lg:flex items-center gap-4 text-sm text-gray-600">
            {datasetCount !== undefined && (
              <div className="flex items-center gap-1.5">
                <Database className="h-4 w-4 text-gray-500" />
                <span className="font-medium">{datasetCount}</span>
              </div>
            )}
            {paperCount !== undefined && (
              <div className="flex items-center gap-1.5">
                <FileText className="h-4 w-4 text-gray-500" />
                <span className="font-medium">{paperCount}</span>
              </div>
            )}
          </div>
        </div>
      </div>
    </header>
  )
}
