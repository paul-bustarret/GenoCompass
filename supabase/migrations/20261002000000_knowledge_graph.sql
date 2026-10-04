-- Rare-disease knowledge graph: nodes, edges, similarity, coverage, clusters.
-- Already applied to project ihoiwfiexgbilhtwopic; recorded here so the repo
-- carries the schema. Guarded so it is safe to re-run.

create table if not exists public.nodes (
  id        text primary key,          -- e.g. MONDO:0009655, NCBIGene:6448, HP:0002360
  type      text not null,             -- gene, disease, phenotype, pathway, variant, trial, ...
  name      text,
  synonyms  text,                      -- pipe-separated
  attrs     jsonb not null default '{}'::jsonb
);

create table if not exists public.edges (
  edge_id      text primary key,
  src          text not null references public.nodes(id) on delete cascade,
  dst          text not null references public.nodes(id) on delete cascade,
  relation     text not null,          -- caused_by, has_phenotype, in_pathway, studies, funds, ...
  source       text,                   -- hpo, reactome, clinvar, ctgov, reporter, pubmed
  source_url   text,
  retrieved_at date,
  confidence   real,
  evidence     text,                   -- observed | inferred
  polarity     text,
  effect       real,
  quote        text,
  frequency    text
);

create table if not exists public.similarity (
  a              text not null references public.nodes(id) on delete cascade,
  b              text not null references public.nodes(id) on delete cascade,
  therapeutic    real,
  phenotype_view real,
  gene           real,
  pathway        real,
  phenotype      real,
  lookalike      boolean,
  top_witnesses  text,
  primary key (a, b)
);

create table if not exists public.coverage (
  disease_id   text not null references public.nodes(id) on delete cascade,
  source       text not null,
  query        text not null,
  n_results    integer,
  n_kept       integer,
  retrieved_at date,
  primary key (disease_id, source, query)
);

create table if not exists public.clusters (
  cluster     integer not null,
  disease_id  text not null references public.nodes(id) on delete cascade,
  primary key (cluster, disease_id)
);

create index if not exists edges_src_idx      on public.edges (src);
create index if not exists edges_dst_idx      on public.edges (dst);
create index if not exists edges_relation_idx on public.edges (relation);
create index if not exists nodes_type_idx     on public.nodes (type);

-- RLS: on, read-only for the API. Writes only via dashboard / service role.
alter table public.nodes      enable row level security;
alter table public.edges      enable row level security;
alter table public.similarity enable row level security;
alter table public.coverage   enable row level security;
alter table public.clusters   enable row level security;

drop policy if exists "read nodes"      on public.nodes;
drop policy if exists "read edges"      on public.edges;
drop policy if exists "read similarity" on public.similarity;
drop policy if exists "read coverage"   on public.coverage;
drop policy if exists "read clusters"   on public.clusters;

create policy "read nodes"      on public.nodes      for select to anon, authenticated using (true);
create policy "read edges"      on public.edges      for select to anon, authenticated using (true);
create policy "read similarity" on public.similarity for select to anon, authenticated using (true);
create policy "read coverage"   on public.coverage   for select to anon, authenticated using (true);
create policy "read clusters"   on public.clusters   for select to anon, authenticated using (true);

-- OPTIONAL, after CSV import: turn "A|B|C" synonyms into a real array.
-- alter table public.nodes
--   alter column synonyms type text[]
--   using case when synonyms is null or synonyms = '' then null
--              else string_to_array(synonyms, '|') end;
