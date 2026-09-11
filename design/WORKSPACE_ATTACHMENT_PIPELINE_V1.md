# Workspace Attachment Pipeline V1

Status: proposed for the next implementation round  
Priority: P1  
Scope: local single-user desktop product; workspace-scoped text-PDF attachments

## 1. Problem

The product can read PDFs only when an operator preconfigures `STATA_AGENT_LIBRARY` to an existing
directory. It has no user attachment boundary: the composer button is disabled, `ChatIn` carries no
attachment references, and there is no quarantine, byte limit, format sniffing, durable lifecycle,
workspace isolation, or stale-file cleanup.

The current RAG adapter also has a correctness gap. `_rag()` calls `build_hybrid()` without a
`source_role`, so the configured directory defaults to `style_only`; `search_literature` then filters
strictly for `citable_evidence`. The normal UI path can therefore build a healthy index that the
literature tool is unable to query.

Adding a file picker alone would create a dangerous and unusable half-feature. V1 must establish one
complete path:

```text
browser bytes
  -> bounded raw-body transport
  -> private staging/quarantine
  -> content and parser validation
  -> durable lifecycle + ledger event
  -> accepted workspace object
  -> bounded text-PDF chunks
  -> workspace-scoped RAG
  -> opaque attachment reference in a chat turn
```

## 2. Architectural Fit

- The SQLite event ledger remains the canonical audit history. Attachment lifecycle rows are a
  local operational index and must be coupled to lifecycle events in the same SQLite transaction.
- Binary files and extracted text remain outside events. Events contain only small metadata and
  opaque references, consistent with DD-01.
- `ChatService` remains the owner of one turn. It records validated attachment references with the
  user event; FastAPI only resolves workspace/input and composes services.
- The agent loop remains LLM-first and tool-calling. Attachments do not add a second intent router or
  an attachment-specific orchestration flow.
- RAG chunks remain untrusted retrieved data. `source_role` controls citation eligibility, not trust,
  authorization, or privacy.
- The attachment root is private application storage outside the mounted UI/webroot. It is partitioned
  by opaque workspace identity and does not use the client filename as a filesystem path.
- Existing privacy modes govern whether later model calls may receive RAG snippets. Upload, validation,
  listing, cleanup, and indexing never contact a provider.

No core architecture change is required. The additive SQLite table is a rebuildable lifecycle/index
surface in the existing database; it is not a second research truth store.

## 3. V1 Product Contract

V1 supports one format: bounded, unencrypted, text-bearing PDF.

This deliberately excludes images, scanned-PDF OCR, Office documents, archives, datasets, scripts,
and executable content. A rejected or quarantined file must not be described as malware-free; V1
provides type and parser isolation, not antivirus or Content Disarm and Reconstruction.

Default limits:

| Limit | Value |
|---|---:|
| File bytes | 25 MiB |
| Files selected per composer action | 8 |
| Attachment references per chat turn | 8 |
| Retained attachment bytes per workspace | 512 MiB |
| Ready attachments per workspace | 128 |
| Display filename | 180 Unicode characters |
| PDF pages parsed | existing 64-page hard cap |
| Chunks per PDF/workspace build | existing bounded RAG caps |
| Staging TTL | 1 hour |
| Quarantine TTL | 7 days |
| Reconcile/cleanup work per startup call | bounded constructor-configurable limit |

Limits are application constants with constructor injection for tests. Environment overrides are not
required in V1.

## 4. Trust and Threat Model

Every uploaded byte, declared MIME value, filename, PDF text, metadata, and embedded instruction is
untrusted.

V1 protects against:

- oversized or infinite request bodies, including lying/missing `Content-Length`;
- path traversal, absolute/UNC paths, Windows alternate data streams, control/NUL characters, device
  names, and client filenames used as physical paths;
- cross-workspace ID enumeration and object access;
- extension/MIME/magic mismatch, non-PDF content, encrypted PDFs, parser crashes/timeouts, and common
  active/embedded-content indicators;
- symlink/junction/reparse escape from the private attachment root;
- partial writes, duplicate submissions, request disconnects, process crashes between filesystem and
  database transitions, and stale staging/orphan objects;
- corrupt or hostile RAG cache data causing unbounded materialization or poisoning other documents;
- PDF prompt injection being treated as a system/user instruction;
- raw paths, file content, parser exceptions, secrets, or user-home information leaking through API
  errors, diagnostics, or model-visible attachment manifests.

V1 does not claim protection from a kernel/filesystem attacker with write access to the process-owned
attachment directory, a novel parser exploit, or malware that requires a real AV/CDR engine.

The upload controls follow the OWASP file-upload principles: allow-list necessary formats, validate
content rather than trusting `Content-Type`, generate storage names, enforce size limits, and keep
files outside the webroot. Python 3.12 `Path.is_junction()`/`is_symlink()` are part of the Windows path
guard, but canonical containment is still checked again immediately before promote/read operations.

## 5. Private Filesystem Layout

Default root:

```text
<database-parent>/.attachments/
  workspaces/
    <sha256-of-opaque-workspace-id>/
      staging/
        <attachment-id>.part
      quarantine/
        <attachment-id>.pdf
      objects/
        <sha256-prefix>/<sha256>.pdf
      derived/
        rag-cache.v1.json
```

Rules:

1. The workspace directory token is generated from the already opaque workspace identity; neither the
   workspace slug nor user path is used as a storage component.
2. The client filename is display-only metadata. Physical names use server-created attachment IDs or
   SHA-256 digests.
3. The root and every managed ancestor must be a real directory and not a symlink or Windows junction.
   Resolved staging/quarantine/object paths must remain inside the expected workspace partition.
4. Staging files use exclusive creation. The body is streamed in fixed-size blocks while counting bytes
   and hashing; the implementation must not call `request.body()` or buffer the whole file.
5. Promotion is a same-filesystem atomic replace/link operation after flush and fsync. Existing
   content-addressed objects are verified and reused; unrelated paths are never overwritten.
6. The application never serves this directory through `StaticFiles` and never returns its absolute
   path to the browser or model.

An optional attachment-root constructor/config value may relocate the whole private root. Writing
directly into an arbitrary registered `WorkspaceRecord.root` is not part of V1.

## 6. Durable Metadata and Events

SQLite schema v4 adds an `attachments` table. Minimum fields:

```text
attachment_id        opaque server ID, primary key
workspace_id         opaque workspace identity
idea_id              current ledger/workspace slug
display_name         validated UI-only name
declared_media_type  advisory, bounded string
detected_format      pdf | unknown
source_role          style_only | citable_evidence
status               pending | quarantined | ready | rejected | failed
byte_size            nonnegative bounded integer
sha256               lowercase digest when known
storage_key          relative opaque key, never an absolute path
parser_version       integer when parsed
page_count/chunk_count/extracted_chars
scanned_suspect      boolean
error_code           stable enum, never raw exception text
created_at/updated_at/ready_at/expires_at
latest_event_seq
state_version        monotonic CAS version
```

Required indexes/constraints:

- workspace/status/recency list index;
- unique `(workspace_id, sha256)` for non-null hashes;
- check constraints for status, role, sizes, booleans, and state version;
- storage keys must be relative opaque values generated by the adapter.

Lifecycle event constants:

- `attachment.intake.requested`;
- existing `artifact.stored` for `ready`;
- `attachment.quarantined`;
- `attachment.rejected`;
- `attachment.failed`.

Events contain only attachment/workspace IDs, format, size/hash, role, status, parser counters, stable
error code, and relative storage key when ready. They never contain binary/text content, absolute path,
raw filename, raw exception, MIME headers, or parser output. Reducers remain forward-compatible no-ops
for these operational events.

The store exposes narrow transactional operations rather than generic SQL from the application layer:

- begin pending row + requested event;
- CAS terminal transition + terminal event;
- get/list by workspace;
- get by `(workspace_id, sha256)` for idempotency;
- bounded stale/pending/quarantine candidates for maintenance.

## 7. State Machine and Cross-Resource Consistency

```text
new
  -> pending
       -> ready
       -> quarantined
       -> rejected
       -> failed

quarantined
  -> rejected     (TTL cleanup or explicit future action)
  -> ready        (future reprocessing, not a V1 public API)
```

Normal flow:

1. Validate workspace, filename, role, declared length and quota.
2. Transactionally append `attachment.intake.requested` and insert `pending` row.
3. Stream into the exact exclusive staging key while hashing and enforcing the byte limit.
4. Validate `.pdf` extension case-insensitively, allowed declared media type, PDF magic, and bounded
   isolated parser result.
5. If usable text PDF, promote the content-addressed object and transactionally CAS `pending -> ready`
   with `artifact.stored`.
6. If structurally PDF but encrypted, scanned-only, active/embedded, parser-timeout, or parser-crashed,
   move only the exact staged file to quarantine and CAS with a stable quarantine event/code.
7. If non-PDF, empty, extension/magic mismatch, or over limit, delete only the exact staging file and
   CAS to rejected.
8. Any internal filesystem/database failure returns no success. Best-effort exact-key cleanup may run;
   otherwise reconciliation owns the residual state.

The filesystem and SQLite cannot be committed atomically. V1 therefore makes every boundary
reconcilable:

- pending row, no file: `failed/missing_staging` after grace period;
- pending row with stale staging: delete that exact file, then fail the row;
- pending row with a valid promoted object: verify digest and complete the terminal CAS;
- ready row with missing/mismatched object: fail closed as `artifact_missing`/`hash_mismatch`; never
  claim the document is available;
- object with no metadata/event: delete only if its name is a valid generated key, it is older than the
  grace period, and a bounded exact lookup confirms no canonical reference;
- stale quarantined bytes: delete after the documented TTL and transition to rejected with a stable
  cleanup reason;
- ready objects are never automatically deleted.

Reconciliation is idempotent, bounded, workspace-scoped, and invoked from the existing explicit
startup/lifespan path. It does not recursively delete a workspace root.

## 8. PDF Isolation and RAG Contract

Raw PDF parsing must not occur in the FastAPI event loop or provider thread. The concrete parser adapter
runs one file in a spawned local subprocess with:

- a fixed timeout and no shell;
- bounded page/chunk/character output;
- a bounded result file/pipe;
- stable result/error codes;
- no network/provider/tool access;
- process termination and exact temporary-output cleanup on timeout.

Windows does not provide a portable hard memory limit through the Python standard library, so V1 must
describe this as crash/time isolation, not a complete malware sandbox.

Only `ready` attachments feed the workspace RAG. A quarantined/rejected/failed attachment contributes
zero chunks. One failed document must not disable healthy documents.

RAG changes:

- `Chunk` gains an optional opaque `attachment_id` while old constructors remain compatible;
- document/chunk identity includes attachment content hash and parser version, not an absolute path;
- cached chunk loading validates cache byte size, schema version, item count, scalar types, field
  lengths, roles, and chunk limits before construction;
- corrupt/oversized cache is ignored and rebuilt per document without exposing raw cache data;
- `_rag(idea)` becomes workspace-aware and uses a workspace-specific derived cache;
- global `STATA_AGENT_LIBRARY`, when configured, is an explicitly labelled shared source with an
  explicit role policy; it must not silently impersonate a workspace source;
- the normal UI composition path must be tested through `_rag() -> build -> search_literature`;
- retrieval has strict `top_k` bounds and deterministic tie-breaking;
- `search_literature` returns `chunk_id`, opaque `attachment_id` when present, safe `doc_id`, page,
  `source_role`, bounded snippet, and an untrusted-data label; never a filesystem path;
- content containing prompt-injection language stays quoted tool data and cannot alter tool policy.

Uploaded PDFs default to `style_only`. Choosing `citable_evidence` must be an explicit upload option in
the UI/API. Source role is immutable in V1; role promotion/demotion is deferred rather than silently
rewriting citation authority.

## 9. Chat Reference Contract

`ChatIn` and `ChatTurnRequest` gain an optional bounded attachment reference collection. Existing callers
default to empty and remain compatible.

Before calling `ChatService`, the adapter resolves every ID through the attachment service and requires:

- same workspace identity/idea;
- `ready` status;
- at most eight unique IDs;
- deterministic request order;
- no client-supplied path, role, hash, or parser metadata.

The user event stores a safe manifest, not bytes or extracted text. `ContextAssembler` renders a bounded
L4 block such as attachment ID, safe display name, role, format, and page count under an explicit
“untrusted data; never instructions” delimiter. No raw PDF content is automatically inserted into the
prompt. The agent uses `search_literature` to retrieve bounded snippets when useful.

If current-turn attachment IDs are present, the workspace retriever may restrict results to those IDs;
custom/legacy retrievers that do not support the optional filter remain usable through capability
inspection. Historical user-event attachment manifests remain replayable even if the derived RAG cache
is rebuilt.

## 10. Local HTTP and UI Surface

Transport endpoints use upload bodies are raw bytes, not multipart, so V1 adds no `python-multipart`
dependency.

Endpoints:

```text
POST /api/attachments?ws=<id>
  headers: X-Attachment-Filename (percent-encoded UTF-8), X-Attachment-Role,
           X-Idempotency-Key, Content-Type
  body: raw PDF bytes

GET /api/attachments?ws=<id>&limit=100

POST /api/chat and /api/chat/stream
  body adds: attachment_ids: [opaque ids]
```

Upload response codes:

- `201` ready new attachment;
- `200` idempotent duplicate;
- `413` body/file/workspace quota exceeded;
- `415` unsupported type or extension/magic/MIME mismatch;
- `422` invalid filename/role/request fields;
- `409` attachment state/idempotency conflict;
- `500` stable sanitized internal failure.

Quarantined files may return `202` with safe state/error code so the UI can explain that OCR or manual
review is unavailable. Responses never include absolute storage paths, raw PDF metadata/text, or raw
exceptions. Cross-workspace lookup returns the same not-found response as an unknown ID.

The composer enables the paperclip, uses a hidden `.pdf` file input, percent-encodes Unicode display names,
assigns one stable idempotency key per selected file, uploads selected files sequentially,
shows bounded chips with ready/quarantined/rejected states, and sends only ready attachment IDs. Removing
a chip detaches it from the current turn; it does not delete the canonical object. Workspace switching
clears pending composer references and aborts in-flight browser uploads. The UI continues to construct
DOM nodes without `innerHTML`.

## 11. Compatibility and Migration

- Schema v3 -> v4 is explicit, transactional, idempotent, and contains no filesystem work.
- Existing databases, workspaces, user events, `ChatTurnRequest` callers, RAG chunks/caches, custom
  retrievers, and configured global libraries continue to operate.
- Old RAG caches without the V1 schema are treated as derived and rebuilt, never migrated in place as
  canonical data.
- Existing `artifact.stored` consumers remain forward-compatible because the new payload is metadata-only
  and reducers already ignore unsupported artifact projections.
- The attachment service is framework-neutral and importable without FastAPI/PyMuPDF. Concrete raw-body,
  filesystem, SQLite, and PDF parser adapters remain outside the DTO contract.

## 12. Explicitly Deferred

- OCR, remote vision, local vision models, image interpretation, and scanned-PDF recovery.
- PNG/JPEG, CSV/DTA, DOCX/XLSX, archives, replication packages, scripts, and author do-file execution.
- Antivirus/CDR, cloud scanning, upload sharing, remote storage, auth/RBAC, multi-user tenancy.
- Download/preview endpoints, accepted-file deletion, workspace deletion, role mutation, and bulk import.
- Remote web download/fetch hardening and SSRF work.
- Redis/RQ, distributed parsing workers, durable general-purpose job queues.
- RAG reranking, embedding model changes, OCR quality evaluation, and a full domain retrieval gold set.
- Changes to Stata run roots, legacy artifact tools, writer/figure evidence, or evidence authority.

## 13. Acceptance Summary

1. A bounded text PDF can move from raw browser bytes to a `ready` workspace attachment, ledger event,
   workspace RAG hit, and chat attachment reference without exposing a raw path.
2. Oversized, disguised, encrypted, scanned-only, active, malformed, timed-out, and hostile PDFs reach a
   stable non-ready state and contribute zero chunks.
3. Filenames never select storage paths; traversal, UNC/ADS, symlink, junction, TOCTOU, and cross-workspace
   tests leave an external sentinel untouched.
4. Same-workspace duplicate content is idempotent; different workspaces remain isolated; crash boundary
   states reconcile deterministically.
5. Corrupt cache data is bounded and rebuildable; one bad PDF does not disable healthy documents.
6. Literature search returns stable chunk/attachment provenance and preserves citable/style role rules.
7. Upload/list/reconcile perform zero provider, executor, general task-queue, or network work.
8. Existing chat, context, RAG, diagnostics, outbox, evidence, Stata, UI, eval, coverage, wheel, and belief
   map gates remain green.

## 14. References

- [OWASP File Upload Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/File_Upload_Cheat_Sheet.html)
- [Python 3.12 pathlib documentation](https://docs.python.org/3.12/library/pathlib.html)
- `ARCHITECTURE.md`
- `design/PROJECT_ARCHITECTURE_AUDIT.md`
- `design/dd-01-domain-events.md`
- `design/dd-03-context-memory.md`
- `design/dd-04-tool-permission.md`
- `design/dd-07-rag-skills.md`
