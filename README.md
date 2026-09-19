# AOOB-9-9

Astree array-out-of-bounds (AOOB) enrichment and exploration for automotive
preprocessed C projects. Each analysis unit is a **PVER** folder with a fixed
file layout. The analyzer enriches alarms with symbol facts and a full
caller-to-callee graph; the UI lets you browse one alarm or the whole PVER graph.

PVERs/4105 and PVERs/5901 are **samples only**. Any new folder that follows
the same pattern must work the same way.

---

## Repository layout

`	ext
AOOB-9-9/
|-- array_oob_analyzer.py          # CLI analyzer (symbols + CFG)
|-- PVERs/
|   +-- <id>/                      # one folder per PVER
|       |-- Full_alarms.csv        # Astree alarm export (required for enrich)
|       |-- input.c                # preprocessed source, line-aligned
|       |-- messeges.txt           # Astree path traces (optional but preferred)
|       |-- array_oob_variable_info.json   # analyzer output
|       +-- full_control_flow_graph.json   # analyzer / CFG output
|-- UI/                            # React + FastAPI explorer
|   |-- src/                       # Vite React frontend
|   +-- backend/                   # FastAPI API
+-- README.md
`

---

## PVER folder contract

| File | Role |
|------|------|
| Full_alarms.csv | Astree export (Order, Location, optional Message) |
| input.c | Preprocessed AP1_bc_with_context.c (same line numbers as Astree) |
| messeges.txt | Optional. Full call stacks per AOOB alarm (many paths per location) |
| rray_oob_variable_info.json | Written by the analyzer: alarms, symbols, occurrences, paths |
| ull_control_flow_graph.json | Written by the analyzer: { caller: [callee, ...] } for every function |

Notes:

- CSV alone has **one row per alarm** and **no call stacks**. Path multiplicity
  comes from messeges.txt (merged by location when the analyzer runs on CSV).
- The **full PVER graph** in the UI is built **only** from that folder's
  ull_control_flow_graph.json — never from another PVER's files.

---

## Analyzer (rray_oob_analyzer.py)

`powershell
# Full enrich + CFG into the paths you pass (usually inside PVERs/<id>/)
python array_oob_analyzer.py <alarms.csv|messeges.txt> <input.c> <array_oob_variable_info.json> [cfg.json]

# CFG only
python array_oob_analyzer.py --cfg-only <input.c> <full_control_flow_graph.json>

# Attach / refresh Astree stacks into an existing variable-info JSON
python array_oob_analyzer.py --merge-paths <array_oob_variable_info.json> [messeges.txt]
`

What it produces per alarm group:

- Flagged array / index / parent symbols (not every identifier on the line)
- Kind, scope, dims/size when known, declaration, access counts
- Occurrences with line, function, access mode
- paths[] from the message log when available (
o_of_paths, 
o_of_traces)
- Sibling CFG JSON: closed caller-to-callee map (defined functions, prototypes, callees)

---

## UI

### Modes

| Mode | Content |
|------|---------|
| **Single alarm** | Alarm header; **control flow** (merged unique Astree paths); **data flow** (variables and occurrences, CFG-ordered functions, read-access filter) |
| **Single PVER** | Full graph from ull_control_flow_graph.json; path highlight from the searched alarm; right-hand variable / function list |

Shared chrome: PVER picker, mode toggle, top alarm-order search, left agent chat placeholder.

### Control-flow / graph filters

- **Show all unique paths** — every distinct Astree function sequence
- **Paths with selected variable** — only paths that touch a function where the current data-flow variable is present

PVER graph also has: show all node labels, show all edges, click node to expand/collapse callees.

### Run locally

Backend (UI/backend):

`powershell
python -m pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
`

Frontend (UI):

`powershell
npm install
npm run dev
`

Vite proxies /api to http://127.0.0.1:8000.

### Main API surface

| Method | Path | Purpose |
|--------|------|---------|
| GET | /api/pvers | List PVER folders and readiness |
| POST | /api/pvers/{id}/open | Activate folder (build if CFG missing) |
| GET | /api/pvers/{id}/status | Poll analyzer job |
| GET | /api/alarm?order= | Alarm detail + graph_highlight |
| GET | /api/graph | Layout payload from that PVER's CFG JSON |
| GET | /api/cfg / summary / neighborhood / search | CFG helpers |
| GET | /api/health | Liveness |

---

## Data flow (UI)

Function lists in both modes are ordered using the control-flow graph:

1. **declaration** (global)
2. **intermediate** callers before callees
3. **leaf** — alarm / enclosing function last

Optional filters: **all** / **with read access** / **without read access**.

---

## Configuration

Root .env holds per-agent LLM backend settings for future work. It is not
required to run the analyzer or the explorer UI today.

Do not commit real API keys. Prefer local overrides that stay out of git.

---

## Tests

`powershell
cd UI\backend
python -m pytest tests/test_api.py -q
`

`powershell
cd UI
npx tsc --noEmit -p tsconfig.app.json
`

---

## Agent triage pipeline

LangGraph multi-agent classification lives in `aoob_pipeline/` (not required for the explorer UI alone).

`powershell
python -m pip install -r requirements-agent.txt
python -m aoob_pipeline --pver 4105 --order 1
# lower VRAM:
python -m aoob_pipeline --pver 4105 --order 1 --sequential
`

Stages: deterministic prep → CALL_PATH + VAR_VALUE explorers (tools) → merge/validate → TP/FP prove → final (multi-run). Artifacts write to `PVERs/<id>/agent_runs/<order>/<timestamp>/`. Configure each agent backend/model in `.env`. From the UI, open an alarm and use **Classify alarm**.
