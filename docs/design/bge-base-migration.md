# BGE-base migration

The production embedding contract is `BAAI/bge-base-en-v1.5` with 768-dimensional
vectors. This replaces the previous 1024-dimensional BGE-M3/BGE-large-compatible
storage contract.

## Data migration

Revision `e2b6f1a7c903` performs the model cutover atomically:

1. Existing pending suggestions become `expired`. Their scores and ordering belong
   to the previous model, and leaving them active would consume the per-article cap
   and prevent BGE-base replacements.
2. Existing embeddings are truncated because vectors from different models and
   dimensions are not convertible.
3. `embeddings.vector` changes from `vector(1024)` to `vector(768)`.

Approved, rejected, applying, and applied suggestions are preserved. They represent
editorial or publication decisions rather than an unreviewed model queue.

Downgrading also truncates embeddings before restoring `vector(1024)`. It does not
reactivate expired suggestions because their reason for expiry is not recorded.

## Deployment sequence

1. Stop analysis workers.
2. Apply Alembic migrations.
3. Deploy API and workers with `EMBEDDING_MODEL=BAAI/bge-base-en-v1.5`.
4. Trigger analysis per site. The existing embedding pipeline re-encodes every
   active article and then builds a fresh pending suggestion queue.

The current baseline uses exact pgvector search and has no vector ANN index, so
there is no vector index to rebuild. The ordinary `embeddings.article_id` B-tree
index is unaffected.
