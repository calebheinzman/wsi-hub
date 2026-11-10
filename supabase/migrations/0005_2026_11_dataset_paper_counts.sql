-- Papers per dataset, counted in the database. The dataset explorer used to page all of
-- filtered_metrics (89,626 rows, ~90 requests) into the browser just to count distinct
-- paper_url per dataset_slug; this returns one row per dataset instead.
-- Written as DISTINCT-then-count so Postgres hash-aggregates a seq scan (~70 ms); the
-- equivalent count(DISTINCT paper_url) walks idx_metrics_dataset_slug and takes ~8 s.
-- security_invoker keeps filtered_metrics' RLS policy in force. Safe to re-run.
CREATE OR REPLACE VIEW dataset_paper_counts WITH (security_invoker = on) AS
SELECT dataset_slug, count(*)::int AS num_papers
FROM (SELECT DISTINCT dataset_slug, paper_url FROM filtered_metrics) AS pairs
GROUP BY dataset_slug;

GRANT SELECT ON dataset_paper_counts TO anon, authenticated;
