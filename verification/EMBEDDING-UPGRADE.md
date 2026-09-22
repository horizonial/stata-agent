# Embedding/index upgrade protocol

Dense indexes are immutable `knowledge_embedding_index_revisions`; rebuilding never overwrites an
old vector set, and citations continue to identify canonical Knowledge Nodes rather than a vector
position. An embedding or index change follows this gate:

1. Build the new profile as a candidate revision alongside the current revision.
2. Run the versioned retrieval evaluation set against both exact revisions.
3. Record recall@k, per-case regressions, dataset fingerprint, profile identity, and both revision
   IDs with `assess_dense_index_upgrade`.
4. A candidate is eligible only if it meets the absolute recall threshold, stays within the allowed
   aggregate regression, and introduces no per-case regression.
5. Change the selected embedding profile/configuration only after an eligible report is reviewed.
   A build or migration must never move that selection automatically.
6. Retain the old revision for rollback and for reproducing historical retrieval traces. Existing
   Evidence and citations are not rewritten during an index cutover.

The evaluation-set hash makes a comparison non-transferable to a silently changed question set.
