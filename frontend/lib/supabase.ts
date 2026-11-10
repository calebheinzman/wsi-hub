import { createClient } from "@supabase/supabase-js"

const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL
const supabaseAnonKey = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY

if (!supabaseUrl) {
  throw new Error("Supabase URL is missing")
}
if (!supabaseAnonKey) {
  throw new Error("Supabase Anon Key is missing")
}

export const supabase = createClient(supabaseUrl, supabaseAnonKey)

/**
 * PostgREST caps an unranged select at `db-max-rows` (1000 by default), and it does
 * so silently -- you get 1000 rows and no error. papers_dct alone holds 3,306, so a
 * plain `.select(cols)` was dropping roughly two thirds of the table on the floor.
 *
 * Both explorers filter, search and paginate client-side, so they do need the whole
 * table; the fix is to page through it rather than to push filtering server-side.
 *
 * The first page also returns the row count, and the remaining pages are then fetched
 * `concurrency` at a time. Pages are ordered by `orderBy` (the primary key) so that
 * concurrent ranges can neither overlap nor skip rows. Keep `concurrency` at 1 for
 * heavy columns: 15 parallel pages of papers_dct.abstract hit statement timeouts.
 */
export async function fetchAllRows<T>(
  table: string,
  columns: string,
  { orderBy, pageSize = 1000, concurrency = 4 }: { orderBy: string; pageSize?: number; concurrency?: number }
): Promise<T[]> {
  const fetchPage = async (from: number, count?: "exact") => {
    const { data, error, count: total } = await supabase
      .from(table)
      .select(columns, count ? { count } : undefined)
      .order(orderBy)
      .range(from, from + pageSize - 1)

    if (error) throw error
    return { rows: (data ?? []) as T[], total }
  }

  const first = await fetchPage(0, "exact")
  const total = first.total ?? first.rows.length
  const starts: number[] = []
  for (let from = pageSize; from < total; from += pageSize) starts.push(from)

  const pages: T[][] = new Array(starts.length)
  let next = 0
  const worker = async () => {
    while (next < starts.length) {
      const i = next++
      pages[i] = (await fetchPage(starts[i])).rows
    }
  }
  await Promise.all(Array.from({ length: Math.min(concurrency, starts.length) }, worker))

  return first.rows.concat(...pages)
}
