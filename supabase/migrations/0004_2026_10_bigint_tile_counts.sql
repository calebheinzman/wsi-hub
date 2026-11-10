-- Tile counts past INTEGER's 2,147,483,647: pathology foundation-model papers report training
-- on billions of tiles (Atlas 3.4 billion, Campanella et al. 3.25 billion). Run once in the
-- Supabase SQL editor before pushing a run that has them. Safe to re-run: widening a column
-- that is already BIGINT changes nothing. A database created from 0001_init.sql already has this.
ALTER TABLE papers_dct ALTER COLUMN n_tiles TYPE BIGINT;
ALTER TABLE final_datasets ALTER COLUMN n_tiles TYPE BIGINT;
