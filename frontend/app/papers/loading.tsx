import { Skeleton } from "@/components/ui/skeleton"

export default function Loading() {
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
