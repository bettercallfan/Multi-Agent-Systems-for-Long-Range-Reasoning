# Runtime memory design

The runtime memory layer is a framework-owned adaptation of established agent
memory research; it is intentionally not a second autonomous runtime.

* **CoALA** provides the taxonomy: working, episodic, semantic and procedural
  memory. `MemoryRecord.kind` follows these categories; artifact records bind
  facts to real files.
* **MemGPT** motivates a small always-visible core and paged recall/archival
  storage. `ContextCapsule` is the bounded main-context view for one DAG node.
* **Generative Agents** motivates retrieval using relevance, recency and
  importance. This implementation adds DAG proximity and evidence quality so
  stale or unverified facts do not outrank authorized evidence.
* **LLMLingua-2** is reused through the existing communication compressor only
  after deterministic selection. Core goals, constraints, paths, numbers and
  evidence references are never passed through lossy compression.

`RuntimeMemoryManager` is called by `GraphScheduler` before and after every
node. Executors receive a read-only `memory_capsule`; only the Python manager
can write `runtime_memory.sqlite3`. The existing `workflow_memory.py` remains
the cross-run procedural skill store and is deliberately separate from
run-scoped state.

The first implementation uses SQLite and deterministic token/keyword scoring.
Embedding retrieval, A-MEM-style linked notes and HippoRAG-style graph
retrieval are optional later backends, not hidden requirements of this MVP.
