# AOOB-9-9

Astree array-out-of-bounds (AOOB) enrichment, graph exploration, and
multi-agent triage for automotive preprocessed C projects.

Each analysis unit is a **PVER** folder with a fixed file layout. The
**analyzer** enriches alarms with symbol facts and a caller–callee CFG. The
**UI** browses one alarm or the whole PVER graph. The **agent pipeline**
(`aoob_pipeline/`) classifies alarms as TP / FP / uncertain under a
never-miss-TP policy.

`PVERs/4105` and `PVERs/5901` are samples only. Any folder that follows the
same contract must work the same way.

---

## High-level architecture

```text
┌─────────────┐     ┌──────────────────┐     ┌────────────────────────────┐
│ Astree CSV  │     │ array_oob_       │     │ UI (React + FastAPI)       │
│ messeges.txt│────▶│ analyzer.py      │────▶│  single alarm / PVER graph │
│ input.c     │     │                  │     │  Classify → live stream    │
└─────────────┘     └────────┬─────────┘     └─────────────▲──────────────┘
                             │                             │
                             ▼                             │
                    PVERs/<id>/                            │
                      array_oob_variable_info.json         │
                      full_control_flow_graph.json         │
                      input.c                              │
                             │                             │
                             ▼                             │
                    ┌──────────────────┐                   │
                    │ aoob_pipeline/   │───────────────────┘
                    │ prep → explore → │  EventBus + agent_runs/
                    │ merge → prove →  │
                    │ validate → final │
                    └──────────────────┘
```

Two products share one PVER:

| Layer | Purpose | Required? |
|-------|---------|-----------|
| Analyzer | Symbols, paths, CFG JSON | Yes for UI + agents |
| Explorer UI | Browse alarms / graphs | Optional |
| Agent triage | Classify one alarm (TP/FP/uncertain) | Optional |

---

## Repository layout

```text
AOOB-9-9/
├── array_oob_analyzer.py          # CLI analyzer (symbols + CFG)
├── aoob_pipeline/                 # LangGraph multi-agent triage
│   ├── orchestrator.py            # prep → explore → prove → final
│   ├── prep.py                    # deterministic skeletons + size recovery
│   ├── explore_graph.py           # CALL_PATH + VAR_VALUE explorers
│   ├── explore_support.py         # sequences, seeds, body mining
│   ├── tools.py / session.py      # move/get visit cursor + tools
│   ├── merge.py / validate.py     # packs + quote scrubbing
│   ├── prove_graph.py             # TP_PROVE + FP_PROVE (no tools)
│   ├── final_graph.py             # multi-run adjudication
│   ├── policy.py                  # ironclad FP gates
│   ├── llm.py / config.py         # per-agent backends from .env
│   ├── events.py / artifacts.py   # live stream + run folders
│   ├── schemas.py / prompts.py
│   └── tests/
├── PVERs/
│   └── <id>/                      # one folder per PVER
│       ├── Full_alarms.csv
│       ├── input.c
│       ├── messeges.txt           # optional but preferred
│       ├── array_oob_variable_info.json
│       ├── full_control_flow_graph.json
│       └── agent_runs/<order>/<timestamp>/   # triage artifacts
├── UI/
│   ├── src/                       # Vite React frontend
│   └── backend/                   # FastAPI (explorer + classify APIs)
├── requirements-agent.txt
├── .env                           # per-agent LLM backends (gitignored)
└── README.md
```

---

## PVER folder contract

| File | Role |
|------|------|
| `Full_alarms.csv` | Astree export (`Order`, `Location`, optional `Message`) |
| `input.c` | Preprocessed source; **same line numbers as Astree** |
| `messeges.txt` | Optional. Full call stacks per AOOB alarm (many paths per location) |
| `array_oob_variable_info.json` | Analyzer output: alarms, symbols, occurrences, paths |
| `full_control_flow_graph.json` | Analyzer CFG: `{ caller: [callee, ...] }` for every function |

Notes:

- CSV alone has **one row per alarm** and **no call stacks**. Path multiplicity
  comes from `messeges.txt` (merged by location when the analyzer runs on CSV).
- The **full PVER graph** in the UI is built **only** from that folder’s
  `full_control_flow_graph.json` — never from another PVER’s files.
- UI / pipeline alarm lookup uses Astree **Order** (e.g. `2,475`). Internally
  the JSON may use `group_id`; resolution joins via **Location** when needed.

---

## Analyzer (`array_oob_analyzer.py`)

```powershell
# Full enrich + CFG into the paths you pass (usually inside PVERs/<id>/)
python array_oob_analyzer.py <alarms.csv|messeges.txt> <input.c> <array_oob_variable_info.json> [cfg.json]

# CFG only
python array_oob_analyzer.py --cfg-only <input.c> <full_control_flow_graph.json>

# Attach / refresh Astree stacks into an existing variable-info JSON
python array_oob_analyzer.py --merge-paths <array_oob_variable_info.json> [messeges.txt]
```

What it produces per alarm group:

- Flagged array / index / parent symbols (not every identifier on the line)
- Kind, scope, dims/size when known, declaration, access counts
- Occurrences with line, function, access mode
- `paths[]` from the message log when available (`no_of_paths`, `no_of_traces`)
- Sibling CFG JSON: closed caller-to-callee map (defined functions, prototypes, callees)

---

## Explorer UI

### Modes

| Mode | Content |
|------|---------|
| **Single alarm** | Alarm header; **control flow** (merged unique Astree paths); **data flow** (variables and occurrences, CFG-ordered functions, read-access filter) |
| **Single PVER** | Full graph from CFG JSON; path highlight from the searched alarm; right-hand variable / function list |

Shared chrome: PVER picker, mode toggle, top alarm-order search, left **agent chat**
(live triage stream), orange **agent focus** overlay on the graph while Classify runs.

### Control-flow / graph filters

- **Show all unique paths** — every distinct Astree function sequence
- **Paths with selected variable** — only paths that touch a function where the
  current data-flow variable is present

PVER graph also has: show all node labels, show all edges, click node to
expand/collapse callees.

### Run locally

Backend (`UI/backend`):

```powershell
python -m pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Frontend (`UI`):

```powershell
npm install
npm run dev
```

Vite proxies `/api` to `http://127.0.0.1:8000`.

### Main API surface

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/pvers` | List PVER folders and readiness |
| POST | `/api/pvers/{id}/open` | Activate folder (build if CFG missing) |
| GET | `/api/pvers/{id}/status` | Poll analyzer job |
| GET | `/api/alarms` | Alarm list for active PVER |
| GET | `/api/alarm?order=` / `/api/alarm/{order}` | Alarm detail + `graph_highlight` |
| GET | `/api/graph` | Layout payload from that PVER’s CFG JSON |
| GET | `/api/cfg` / `summary` / `neighborhood` / `search` | CFG helpers |
| POST | `/api/pvers/{id}/alarms/{order}/classify` | Start agent triage (async) |
| GET | `/api/pvers/{id}/alarms/{order}/classify` | Poll status + streamed events |
| GET | `/api/health` | Liveness |

### Data flow ordering (UI)

Function lists in both modes are ordered using the control-flow graph:

1. **declaration** (global)
2. **intermediate** callers before callees
3. **leaf** — alarm / enclosing function last

Optional filters: **all** / **with read access** / **without read access**.

---

## Agent triage pipeline

LangGraph multi-agent classification lives in `aoob_pipeline/`. It is **not**
required for the explorer UI alone. From the UI: open an alarm → **Classify alarm**.
From the CLI:

```powershell
python -m pip install -r requirements-agent.txt
python -m aoob_pipeline --pver 4105 --order 2475
# or Order as shown in Astree / UI:
python -m aoob_pipeline --pver 4105 --order "2,475"
# lower VRAM (explore/prove sequential):
python -m aoob_pipeline --pver 4105 --order 2475 --sequential
```

### End-to-end flow

```text
  load alarm (Order → Location → group)
           │
           ▼
  ┌──────────────────┐
  │ 1. PREP          │  deterministic (no LLM)
  │  path-class      │  collapse Astree paths, local guard scan,
  │  collapse        │  symbol skeletons, array size / initializer recovery
  └────────┬─────────┘
           │
     ┌─────┴─────┐   (parallel unless --sequential)
     ▼           ▼
  CALL_PATH   VAR_VALUE     explorers (LangGraph + tools or code_driven)
  EXPLORE     EXPLORE
     │           │
     └─────┬─────┘
           ▼
  ┌──────────────────┐
  │ 2. MERGE         │  attach facts to path classes; coverage full|partial
  └────────┬─────────┘
           │
     ┌─────┴─────┐
     ▼           ▼
  TP_PROVE    FP_PROVE      provers (LLM only, no tools)
     │           │
     └─────┬─────┘
           ▼
  ┌──────────────────┐
  │ 3. VALIDATE      │  drop uncited quotes / soft claims
  └────────┬─────────┘
           ▼
  ┌──────────────────┐
  │ 4. FINAL × N     │  multi-run vote (default N=3)
  │  + policy clamp  │  ironclad FP gate; prefer uncertain/TP
  └────────┬─────────┘
           │
           ▼  (optional, max AOOB_MAX_REEXPLORE)
        re-explore focus → merge → prove → final again
           │
           ▼
     verdict + result.json
```

Live events (`stage`, `agent_input`, `agent_thinking`, `agent_output`, `done`)
are written to `events.jsonl` and streamed into the UI chat / FullGraph overlay.

### Five agents

| Agent | Role | Tools? |
|-------|------|--------|
| **CALL_PATH_EXPLORE** | Walk unique call-path function sequences; quote path/declaration/alarm facts | Yes (`move_func`, `get_current`, …) or code-driven |
| **VAR_VALUE_EXPLORE** | Gather declaration / write / guard / size / value-shaping facts for index & array | Same |
| **TP_PROVE** | Argue a credible OOB case from the merged pack | No |
| **FP_PROVE** | Argue the access is safe for all path classes | No |
| **FINAL_CLASSIFICATION** | Adjudicate TP vs FP into `TP` / `FP` / `uncertain` | No |

Each agent has its own backend/model in `.env`
(`CALL_PATH_EXPLORE_AGENT`, `*_MODEL`, etc.). Backends: `local` | `network` | `bosch`.

### Explore modes (`AOOB_EXPLORE_MODE`)

| Mode | Behavior | When to use |
|------|----------|-------------|
| **`code_driven`** (default) | Python walks every required function, opens full bodies, mines quotes; LLM extracts structured facts only | Small models (e.g. 7B) that fail tool-calling |
| **`tool_agent`** | LLM must call `get_current` / `move_func` / `submit_explore_pack` itself | Strong tool-calling models |

In both modes:

- A **visit cursor** requires every function in the sequence to be opened.
- Deterministic **seed facts** always include array/index declarations and the
  alarm site line (verified against `input.c`).
- If the LLM never submits, **force-open + fallback mining** still produces a pack.
- Prep recovers **array size from initializer element count** when the
  declaration is `T name[] = { ... }` with no explicit dimension.

### Visit tools (tool_agent mode)

| Tool | Purpose |
|------|---------|
| `visit_status` | Current index, opened set, remaining |
| `move_func` | `next` / `prev` / absolute step (no source) |
| `get_current` | Full body of current sequence function; marks visited |
| `get_func` | Named function body; marks visited if in sequence |
| `get_lines` | Raw line range (capped) |
| `submit_explore_pack` | Accept pack only if visit gate passes and facts non-empty |

### Classification policy (never miss TP)

Wrong **FP** is treated as the worst error (hides a real bug). Defaults:

- Prefer **uncertain** or **TP** when evidence is thin.
- **FP** only when **ironclad** (`AOOB_FP_PRECISION_MODE=strict`):
  - FP agent claims `fp` with `coverage=full`
  - Every path class addressed
  - `array_size=known` and `index_range` not `unknown`
  - No TP witness; no leftover `missing_evidence`
  - Bound-related facts present in the merged pack (guard / clamp / size hint / …)
- Final votes that say `FP` without ironclad evidence are **clamped to uncertain**.
- Validated TP witness forbids an FP label.

`AOOB_TP_POLICY`:

- `type_legal` — any feasible C witness counts for TP
- `operating_range` — only realistic operating values

### Pipeline knobs (`.env`)

| Variable | Default | Meaning |
|----------|---------|---------|
| `AOOB_PATH_CLASS_CAP` | `15` | Max path classes after collapse (`partial` if capped) |
| `AOOB_FINAL_RUNS` | `3` | Independent final votes |
| `AOOB_MAX_TOOL_ROUNDS` | `12` | Explorer LangGraph tool-loop budget |
| `AOOB_MAX_REEXPLORE` | `1` | Bounded re-explore if final requests it |
| `AOOB_EXPLORE_MODE` | `code_driven` | See above |
| `AOOB_FP_PRECISION_MODE` | `strict` | Ironclad FP gate |
| `AOOB_TP_POLICY` | `type_legal` | Witness policy |
| `AOOB_OLLAMA_RUNTIME_FALLBACK` | `1` | Fallback if an Ollama agent fails |
| `AOOB_MAX_VISIBLE_TOOLS` | `10` | UI/tool listing clamp |

Example agent wiring (illustrative — set models you actually serve):

```env
CALL_PATH_EXPLORE_AGENT=local
CALL_PATH_EXPLORE_MODEL=qwen2.5-coder:7b
VAR_VALUE_EXPLORE_AGENT=local
VAR_VALUE_EXPLORE_MODEL=qwen2.5-coder:7b
TP_PROVE_AGENT=network
TP_PROVE_MODEL=qwen3:14b
FP_PROVE_AGENT=network
FP_PROVE_MODEL=qwen3:14b
FINAL_CLASSIFICATION_AGENT=network
FINAL_CLASSIFICATION_MODEL=gemma4:26b
AOOB_EXPLORE_MODE=code_driven
```

Do **not** commit real API keys. Prefer local `.env` overrides that stay out of git.

### Run artifacts

Each classify writes under:

```text
PVERs/<id>/agent_runs/<order>/<UTC-timestamp>/
  01_prep.json
  02_call_path_explore.json
  02_var_value_explore.json
  03_merged.json
  04_tp_prove.json
  04_fp_prove.json
  05_validated.json
  06_final_vote.json
  result.json
  events.jsonl
  (+ 07…11_* if re-explore runs)
```

### Package map

| Module | Responsibility |
|--------|----------------|
| `orchestrator.py` | Stage wiring, parallel explore/prove, re-explore |
| `prep.py` | Alarm load, path-class collapse, local guard, size recovery |
| `source_index.py` | Line/function spans over `input.c` + quote verify |
| `explore_graph.py` | Explorer LangGraphs + code_driven path |
| `explore_support.py` | Sequences, seeds, body mining |
| `merge.py` | Combined pack + coverage |
| `prove_graph.py` | TP/FP LangGraphs |
| `validate.py` | Drop uncited findings |
| `final_graph.py` | Multi-run final + policy clamp |
| `policy.py` | Ironclad FP checks |
| `events.py` | EventBus for UI stream |
| `llm.py` / `config.py` | Resolve backends from `.env` |

---

## Configuration summary

| Need | Config |
|------|--------|
| Analyzer / UI browse only | No LLM `.env` required |
| Agent classify | Root `.env` with per-agent `*_AGENT` / `*_MODEL` + Ollama or Bosch URLs |
| Secrets | `MODEL_FARM_API_KEY` etc. — never commit |

---

## Tests

```powershell
# Explorer API
cd UI\backend
python -m pytest tests/test_api.py -q

# Frontend typecheck
cd UI
npx tsc --noEmit -p tsconfig.app.json

# Agent pipeline (no live LLM required for unit tests)
cd <repo-root>
python -m pytest aoob_pipeline/tests -q
```

---

## Typical workflow

1. Drop Astree CSV + `input.c` (+ `messeges.txt`) into `PVERs/<id>/`.
2. Run the analyzer to produce `array_oob_variable_info.json` and CFG JSON
   (or open the PVER in the UI and let it build).
3. Browse in **Single alarm** or **Single PVER**.
4. Install agent deps, configure `.env`, restart the backend.
5. Open an alarm → **Classify** → watch prep / explorers / prove / final in chat.
6. Inspect `agent_runs/.../result.json` for the verdict and evidence trail.
