import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import ReactFlow, {
  Background,
  Controls,
  Edge,
  MarkerType,
  MiniMap,
  Node,
  Position,
} from "reactflow";
import dagre from "dagre";
import "reactflow/dist/style.css";
import { AlarmDetail, CfgNeighborhood, GraphHighlight, GraphPayload, PathRecord, PverInfo, PverListResponse, VariableInfo } from "./types";
import FullGraph from "./FullGraph";
import { AccessFilter, executionRole, matchesAccessFilter, orderFunctionsByExecution } from "./cfgOrder";
import { PathFilter, filterSequencesByVariable, functionKey, pathFunctions, uniqueSequences } from "./paths";

const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/$/, "");

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "The request failed.";
}

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, init);
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail ?? `Request failed (${response.status})`);
  }
  return response.json() as Promise<T>;
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => {
    window.setTimeout(resolve, ms);
  });
}

function indexSymbols(expression?: string): string[] {
  if (!expression) return [];
  const symbols = expression.match(/[A-Za-z_]\w*/g) ?? [];
  return [...new Set(symbols)];
}

function alarmTitle(detail: AlarmDetail): string {
  if (detail.variable) return detail.variable;
  const info = detail.variable_info;
  const name = info?.symbol_name ?? info?.array_name;
  if (!name) return "Indexed variable alarm";
  return info?.index_expression ? `${name}[${info.index_expression}]` : name;
}

function styleFunctionNode(highlighted: boolean, kind: "caller" | "center" | "callee" | "path" | "leaf") {
  if (highlighted) {
    return { background: "#6b4f12", color: "#fff6d8", borderColor: "#ffc93c" };
  }
  if (kind === "center") return { background: "#1d3b3b", color: "#d6fff2", borderColor: "#37d7a5" };
  if (kind === "callee" || kind === "leaf") return { background: "#1d3b3b", color: "#d6fff2", borderColor: "#37d7a5" };
  if (kind === "caller") return { background: "#182c39", color: "#d6fff2", borderColor: "#2f8caf" };
  return { background: "#182c39", color: "#d6fff2", borderColor: "#2f8caf" };
}

function CallGraph({
  paths,
  functionSnippets,
  highlightedFunctions,
  neighborhood,
  highlight,
  pathFilter,
}: {
  paths: PathRecord[];
  functionSnippets?: AlarmDetail["function_snippets"];
  highlightedFunctions: Set<string>;
  neighborhood?: CfgNeighborhood | null;
  highlight?: GraphHighlight | null;
  pathFilter: PathFilter;
}) {
  const [selectedNode, setSelectedNode] = useState<string | null>(null);
  const visiblePaths = useMemo(() => {
    const sequences = uniqueSequences(paths);
    const allowed = pathFilter === "var_present"
      ? new Set(filterSequencesByVariable(sequences, highlightedFunctions).map((item) => item.join(">")))
      : null;
    if (!allowed) return paths;
    return paths.filter((path) => allowed.has(pathFunctions(path).join(">")));
  }, [highlightedFunctions, pathFilter, paths]);
  const hasAstreePaths = visiblePaths.some((path) => pathFunctions(path).length > 0);
  const sequence = highlight?.function_sequence?.length
    ? highlight.function_sequence
    : highlight?.cf_nodes ?? [];
  const { nodes, edges } = useMemo(() => {
    const graph = new dagre.graphlib.Graph();
    graph.setDefaultEdgeLabel(() => ({}));
    const nodeMap = new Map<string, Node>();
    const edgeMap = new Map<string, Edge>();
    const addEdge = (source: string, target: string) => {
      const edgeId = `${source}->${target}`;
      if (!edgeMap.has(edgeId)) {
        edgeMap.set(edgeId, {
          id: edgeId,
          source,
          target,
          markerEnd: { type: MarkerType.ArrowClosed, color: "#6574cd" },
          style: { stroke: "#6574cd" },
        });
      }
    };
    const addNode = (id: string, label: string, kind: "caller" | "center" | "callee" | "path" | "leaf" | "root") => {
      nodeMap.set(id, {
        id,
        data: { label },
        position: { x: 0, y: 0 },
        sourcePosition: Position.Bottom,
        targetPosition: Position.Top,
        style: kind === "root"
          ? { background: "#19213a", color: "#e6ebff", borderColor: "#6978ff" }
          : styleFunctionNode(false, kind),
      });
    };

    if (hasAstreePaths) {
      const root = "root";
      addNode(root, "alarm entry", "root");
      visiblePaths.forEach((path) => {
        const functions = pathFunctions(path);
        let parent = root;
        functions.forEach((fn, depth) => {
          const id = `${parent}/${fn}`;
          const existing = nodeMap.get(id);
          const callSite = path.call_stack?.find((entry) => entry.startsWith(`call#${fn} `));
          if (existing) {
            existing.data = {
              ...existing.data,
              count: (existing.data.count ?? 1) + 1,
              sources: [...(existing.data.sources ?? []), callSite].filter(Boolean),
            };
          } else {
            nodeMap.set(id, {
              id,
              data: { label: fn, count: 1, sources: callSite ? [callSite] : [] },
              position: { x: 0, y: 0 },
              sourcePosition: Position.Bottom,
              targetPosition: Position.Top,
              style: {
                background: depth === functions.length - 1 ? "#1d3b3b" : "#182c39",
                color: "#d6fff2",
                borderColor: depth === functions.length - 1 ? "#37d7a5" : "#2f8caf",
              },
            });
          }
          addEdge(parent, id);
          parent = id;
        });
      });
    } else if (sequence.length) {
      sequence.forEach((name, index) => {
        const kind = index === sequence.length - 1 ? "center" : index === 0 ? "caller" : "path";
        addNode(name, name, kind);
        if (index > 0) addEdge(sequence[index - 1], name);
      });
    } else if (neighborhood?.function) {
      const center = neighborhood.function;
      addNode(center, center, "center");
      neighborhood.callers.forEach((name) => {
        addNode(`caller:${name}`, name, "caller");
        addEdge(`caller:${name}`, center);
      });
      neighborhood.callees.forEach((name) => {
        addNode(`callee:${name}`, name, "callee");
        addEdge(center, `callee:${name}`);
      });
    }

    graph.setGraph({ rankdir: "TB", nodesep: 28, ranksep: 62, marginx: 20, marginy: 20 });
    nodeMap.forEach((node) => graph.setNode(node.id, { width: 190, height: 54 }));
    edgeMap.forEach((edge) => graph.setEdge(edge.source, edge.target));
    dagre.layout(graph);
    nodeMap.forEach((node) => {
      const point = graph.node(node.id);
      node.position = { x: point.x - 95, y: point.y - 27 };
      if (node.id === "root") return;
      const label = functionKey(String(node.data.label));
      const highlighted = highlightedFunctions.has(label) || Boolean(neighborhood && label === neighborhood.function && !hasAstreePaths);
      node.className = highlighted ? "highlighted-function-node" : undefined;
      node.style = {
        ...node.style,
        background: highlighted ? "#6b4f12" : node.style?.background,
        color: highlighted ? "#fff6d8" : node.style?.color,
        borderColor: highlighted ? "#ffc93c" : node.style?.borderColor,
      };
    });
    return { nodes: Array.from(nodeMap.values()), edges: Array.from(edgeMap.values()) };
  }, [hasAstreePaths, highlight, highlightedFunctions, neighborhood, sequence, visiblePaths]);

  if (!sequence.length && !hasAstreePaths && !neighborhood?.function) return <div className="empty-panel">No call paths are available.</div>;
  const selected = nodes.find((node) => node.id === selectedNode);
  const highlightCount = nodes.filter((node) => node.className === "highlighted-function-node").length;
  return (
    <>
      {highlightCount > 0 && (
        <div className="highlight-note">
          {`${highlightCount} node(s) matched by the selected symbol's functions`}
        </div>
      )}
      <div className="flow-shell">
      <ReactFlow nodes={nodes} edges={edges} fitView nodesConnectable={false} zoomOnDoubleClick={false}
        onNodeClick={(_, node) => setSelectedNode(node.id)}>
        <Background color="#26304b" gap={22} />
        <Controls showInteractive={false} />
        <MiniMap nodeColor={(node) => {
          if (node.id === "root") return "#6978ff";
          return node.className === "highlighted-function-node" ? "#ffc93c" : "#37d7a5";
        }} />
      </ReactFlow>
      </div>
      {selected && selected.id !== "root" && (() => {
        const baseFunctionName = selected.data.label.split("|", 1)[0];
        const snippet = functionSnippets?.[baseFunctionName];
        return (
          <div className="node-inspector">
            <strong>{selected.data.label}</strong>
            <span>
              {snippet
                ? `input.c:${snippet.start_line}-${snippet.end_line}`
                : "No source definition found in input.c"}
            </span>
            {snippet && (
              <pre className="source-snippet">{snippet.text}</pre>
            )}
          </div>
        );
      })()}
    </>
  );
}

function VariableExplorer({
  detail,
  onFunctionsChange,
}: {
  detail: AlarmDetail;
  onFunctionsChange: (names: string[]) => void;
}) {
  const variables = useMemo(() => {
    const all = (detail.symbol_infos?.length
      ? detail.symbol_infos
      : [detail.variable_info, ...detail.variable_infos]).filter(
      (item): item is VariableInfo => Boolean(item),
    );
    const seen = new Set<string>();
    return all.filter((item) => {
      if (item.kind === "function") return false;
      const key = `${item.symbol_name ?? item.array_name ?? "unknown"}|${item.index_expression ?? ""}`;
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  }, [detail]);
  const [selectedVariable, setSelectedVariable] = useState(0);
  const [expandedFunctions, setExpandedFunctions] = useState<Set<string>>(new Set());
  const [accessFilter, setAccessFilter] = useState<AccessFilter>("all");
  const selected = variables[selectedVariable] ?? variables[0];
  const occurrences = selected?.occurrences ?? [];
  const groupedOccurrences = useMemo(() => {
    const groups = new Map<string, typeof occurrences>();
    occurrences.forEach((occurrence) => {
      const name = occurrence.function || "global";
      groups.set(name, [...(groups.get(name) ?? []), occurrence]);
    });
    const path = detail.graph_highlight?.function_sequence?.length
      ? detail.graph_highlight.function_sequence
      : detail.graph_highlight?.cf_nodes ?? [];
    const ordered = orderFunctionsByExecution([...groups.keys()], {
      preferred: selected?.used_in_functions_ordered ?? selected?.used_in_functions,
      path,
      leaf: detail.graph_highlight?.center ?? detail.enclosing_function,
    });
    return ordered
      .filter((name) => groups.has(name))
      .map((name) => [name, groups.get(name) ?? []] as const)
      .filter(([, items]) => matchesAccessFilter(items.map((item) => item.access), accessFilter));
  }, [accessFilter, detail.enclosing_function, detail.graph_highlight, occurrences, selected?.used_in_functions, selected?.used_in_functions_ordered]);

  const listedFunctions = useMemo(
    () => groupedOccurrences.map(([name]) => name).filter((name) => name !== "global"),
    [groupedOccurrences],
  );

  useEffect(() => {
    onFunctionsChange(listedFunctions);
  }, [listedFunctions, onFunctionsChange]);

  useEffect(() => {
    setSelectedVariable(0);
    setExpandedFunctions(new Set());
    setAccessFilter("all");
  }, [detail.order_id]);

  return (
    <section className="panel variable-panel">
      <div className="panel-heading">
        <div>
          <span className="eyebrow">data flow</span>
          <h2>Variables & occurrences</h2>
        </div>
        <span className="count-pill">{occurrences.length} occurrences</span>
      </div>
      <div className="variable-layout">
        <div className="variable-list">
          <label htmlFor="variable-filter">Indexed variables</label>
          {variables.length ? (
            variables.map((item, index) => (
              <button
                className={`variable-item ${selectedVariable === index ? "active" : ""}`}
                key={`${item.array_name}-${index}`}
                onClick={() => {
                  setSelectedVariable(index);
                  setExpandedFunctions(new Set());
                  setAccessFilter("all");
                }}
              >
                <strong>
                  {item.symbol_name ?? item.array_name ?? "unknown symbol"}
                  {(item.role === "flagged_array" || item.array_name === detail.variable_info?.array_name) && <span className="flag-badge">flagged</span>}
                </strong>
                <code>{item.kind ?? "symbol"}{item.role ? ` · ${item.role}` : ""}{item.index_expression ? ` · ${item.index_expression}` : ""}</code>
                <span className="index-symbols">
                  {indexSymbols(item.index_expression).map((symbol) => `## ${symbol}`).join("  ")}
                </span>
                <span className="variable-summary">
                  {item.datatype ?? "type unavailable"}
                  {item.is_pointer ? " · pointer" : ""}
                  {item.is_struct_member && item.member_of ? ` · member of ${item.member_of}` : ""}
                  {" · "}{String(item.array_dims ?? item.resolved_sizes ?? "dimensions unavailable")}
                  {" · "}{item.scope ?? "scope unavailable"}
                </span>
                <span className="variable-summary">{item.occurrence_count ?? 0} occurrences</span>
              </button>
            ))
          ) : (
            <div className="muted">No variable metadata in this export.</div>
          )}
        </div>
        <div className="occurrence-panel">
          {selected ? (
            <>
              <div className="variable-meta">
                <div><label>symbol</label><code>{selected.symbol_name ?? selected.array_name ?? "unknown symbol"}</code></div>
                <div><label>kind</label><span>{selected.kind ?? "symbol"}{selected.role ? ` (${selected.role})` : ""}</span></div>
                <div><label>type</label><span>{selected.datatype ?? "type unavailable"}{selected.is_pointer ? " *" : ""}</span></div>
                <div><label>size / dims</label><span>{selected.resolved_sizes ? `[${selected.resolved_sizes.join("][")}]` : String(selected.array_dims ?? "unavailable")}</span></div>
                <div><label>scope</label><span>{selected.scope ?? "unavailable"}{selected.declared_in_function ? ` in ${selected.declared_in_function}` : ""}</span></div>
                <div><label>declared line</label><span>{selected.declaration_line ?? "unavailable"}</span></div>
                {selected.declaration_text && (
                  <div><label>declaration</label><code>{selected.declaration_text}</code></div>
                )}
                {selected.is_struct_member && (
                  <div><label>struct member</label><span>{selected.member_of ?? "unknown struct"}{selected.parent_symbol ? ` via ${selected.parent_symbol}` : ""}</span></div>
                )}
                {selected.member_path && selected.member_path.length > 1 && (
                  <div><label>member path</label><code>{selected.member_path.join(".")}</code></div>
                )}
                {selected.access_counts && (
                  <div><label>accesses</label><span>{Object.entries(selected.access_counts).map(([key, value]) => `${key} ${value}`).join(" · ") || "none"}</span></div>
                )}
                {(selected.used_in_functions_ordered ?? selected.used_in_functions)?.length ? (
                  <div><label>used in</label><span>{(selected.used_in_functions_ordered ?? selected.used_in_functions ?? []).join(" → ")}</span></div>
                ) : null}
                {selected.passed_to && selected.passed_to.length > 0 && (
                  <div><label>passed to</label><span>{selected.passed_to.slice(0, 8).map((item) => `${item.function} (${item.mode})`).join(", ")}{selected.passed_to.length > 8 ? ` +${selected.passed_to.length - 8}` : ""}</span></div>
                )}
              </div>
              {selected.occurrences_truncated && (
                <div className="metadata-note">
                  Showing a bounded sample of {selected.occurrence_count} total occurrences.
                </div>
              )}
              <button className="back-button" onClick={() => setSelectedVariable(0)}>
                ← back to variable list
              </button>
              <div className="access-filter">
                <button className={accessFilter === "all" ? "active" : ""} onClick={() => setAccessFilter("all")}>all</button>
                <button className={accessFilter === "with_read" ? "active" : ""} onClick={() => setAccessFilter("with_read")}>with read access</button>
                <button className={accessFilter === "without_read" ? "active" : ""} onClick={() => setAccessFilter("without_read")}>without read access</button>
              </div>
              <div className="function-tree">
                {groupedOccurrences.length ? groupedOccurrences.map(([name, items], index) => {
                  const expanded = expandedFunctions.has(name);
                  const role = executionRole(
                    name,
                    groupedOccurrences.map(([item]) => item),
                    detail.graph_highlight?.center ?? detail.enclosing_function,
                  );
                  return (
                    <div className="function-group" key={name}>
                      <button
                        className="function-toggle"
                        onClick={() => setExpandedFunctions((current) => {
                          const next = new Set(current);
                          if (next.has(name)) next.delete(name); else next.add(name);
                          return next;
                        })}
                      >
                        <span>{expanded ? "▾" : "▸"} <span className={`role-badge ${role}`}>{role}</span>{index + 1}. {name}</span>
                        <span className="count-pill">{items.length}</span>
                      </button>
                      {expanded && <div className="occurrence-list">
                        {items.map((occurrence, index) => (
                          <article className="occurrence" key={`${occurrence.location}-${index}`}>
                            <div className="occurrence-topline">
                              <span className={`access ${occurrence.access ?? "unknown"}`}>{occurrence.access ?? "unknown"}</span>
                              <code>{occurrence.location ?? "location unavailable"}</code>
                            </div>
                            {occurrence.line_text && <pre>{occurrence.line_text}</pre>}
                          </article>
                        ))}
                      </div>}
                    </div>
                  );
                }) : <div className="muted">No occurrences recorded for this variable.</div>}
              </div>
            </>
          ) : <div className="empty-panel">Select a variable to inspect its occurrences.</div>}
        </div>
      </div>
    </section>
  );
}

function AgentChat({ pver, mode, alarm }: { pver: string; mode: "alarm" | "pver"; alarm?: string }) {
  const [messages, setMessages] = useState<Array<{ role: "system" | "user" | "assistant"; text: string }>>([
    {
      role: "system",
      text: "Agent workspace. Search an alarm order in the top bar, or talk through this PVER here. Analysis tools will plug into this thread next.",
    },
  ]);
  const [draft, setDraft] = useState("");

  const send = (event: FormEvent) => {
    event.preventDefault();
    const text = draft.trim();
    if (!text) return;
    setMessages((current) => [
      ...current,
      { role: "user", text },
      { role: "assistant", text: "Saved. This chat will drive the next analysis steps." },
    ]);
    setDraft("");
  };

  return (
    <aside className="agent-chat">
      <div className="sidebar-heading">
        <div>
          <span className="eyebrow">agent</span>
          <h1>{pver || "workspace"}</h1>
        </div>
        <span className="count-pill">{mode}{alarm ? ` · ${alarm}` : ""}</span>
      </div>
      <div className="chat-log">
        {messages.map((item, index) => (
          <article className={`chat-bubble ${item.role}`} key={`${item.role}-${index}`}>
            <span>{item.role}</span>
            <p>{item.text}</p>
          </article>
        ))}
      </div>
      <form className="chat-input" onSubmit={send}>
        <input
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          placeholder="ask about this PVER or alarm…"
          disabled={!pver}
        />
        <button type="submit" disabled={!pver || !draft.trim()}>Send</button>
      </form>
    </aside>
  );
}

export default function App() {
  const [pvers, setPvers] = useState<PverInfo[]>([]);
  const [activePver, setActivePver] = useState<string>("");
  const [pverStatus, setPverStatus] = useState<PverInfo | null>(null);
  const [mode, setMode] = useState<"alarm" | "pver">("pver");
  const [graph, setGraph] = useState<GraphPayload | null>(null);
  const [detail, setDetail] = useState<AlarmDetail | null>(null);
  const [highlight, setHighlight] = useState<GraphHighlight | null>(null);
  const [highlightedFunctions, setHighlightedFunctions] = useState<Set<string>>(new Set());
  const [pathFilter, setPathFilter] = useState<PathFilter>("all_unique");
  const [lookup, setLookup] = useState("");
  const [opening, setOpening] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState("");

  const refreshPvers = useCallback(async () => {
    const payload = await api<PverListResponse>("/api/pvers");
    setPvers(payload.items);
    return payload;
  }, []);

  useEffect(() => {
    void refreshPvers().catch((requestError) => setError(errorMessage(requestError)));
  }, [refreshPvers]);

  const openPver = async (pverId: string) => {
    if (!pverId) return;
    setOpening(true);
    setError("");
    setDetail(null);
    setGraph(null);
    setHighlight(null);
    setActivePver(pverId);
    try {
      let info = await api<PverInfo>(`/api/pvers/${encodeURIComponent(pverId)}/open`, { method: "POST" });
      setPverStatus(info);
      while (info.status === "building") {
        await sleep(2000);
        info = await api<PverInfo>(`/api/pvers/${encodeURIComponent(pverId)}/status`);
        setPverStatus(info);
      }
      if (info.status === "error") {
        throw new Error(info.message || `Failed to analyze PVER ${pverId}`);
      }
      if (info.status === "ready" && !info.active) {
        info = await api<PverInfo>(`/api/pvers/${encodeURIComponent(pverId)}/open`, { method: "POST" });
        setPverStatus(info);
      }
      setGraph(await api<GraphPayload>("/api/graph"));
      await refreshPvers();
    } catch (requestError) {
      setError(errorMessage(requestError));
    } finally {
      setOpening(false);
    }
  };

  const loadAlarm = async (order: string) => {
    setDetailLoading(true);
    setError("");
    try {
      const result = await api<AlarmDetail>(`/api/alarm?order=${encodeURIComponent(order)}`);
      setDetail(result);
      setHighlight(result.graph_highlight ?? null);
      setHighlightedFunctions(new Set());
      setLookup(result.order_id);
    } catch (requestError) {
      setDetail(null);
      setHighlight(null);
      setError(errorMessage(requestError));
    } finally {
      setDetailLoading(false);
    }
  };

  const uniqueFunctionPaths = detail
    ? new Set(detail.paths.map((path) => pathFunctions(path).join(" → "))).size
    : 0;

  const onLookup = (event: FormEvent) => {
    event.preventDefault();
    const value = lookup.trim();
    if (value) void loadAlarm(value);
  };

  const applyFunctionHighlights = useCallback((names: string[]) => {
    setHighlightedFunctions((current) => {
      const next = new Set(names.map(functionKey));
      if (next.size === current.size && [...next].every((name) => current.has(name))) {
        return current;
      }
      return next;
    });
  }, []);

  return (
    <main className="app-shell">
      <header className="topbar">
        <label className="pver-picker">
          <span>PVER</span>
          <select
            aria-label="PVER folder"
            value={activePver}
            disabled={opening}
            onChange={(event) => void openPver(event.target.value)}
          >
            <option value="">choose PVER…</option>
            {pvers.map((item) => (
              <option key={item.id} value={item.id}>
                {item.name}{item.has_cfg ? "" : " · build"} · {item.alarm_count} alarms
              </option>
            ))}
          </select>
        </label>
        <div className="view-toggle">
          <button className={mode === "alarm" ? "active" : ""} onClick={() => setMode("alarm")} disabled={!activePver || opening}>single alarm</button>
          <button className={mode === "pver" ? "active" : ""} onClick={() => setMode("pver")} disabled={!activePver || opening}>single PVER</button>
        </div>
        <form className="lookup" onSubmit={onLookup}>
          <span className="prompt">/</span>
          <input
            aria-label="Alarm order search"
            value={lookup}
            onChange={(event) => setLookup(event.target.value)}
            placeholder="search alarm order (e.g. 2,989)"
            disabled={!activePver || opening}
          />
          <button type="submit" disabled={!activePver || opening}>Search</button>
        </form>
        <div className="status">
          <span className={`status-dot ${opening ? "busy" : activePver ? "" : "idle"}`} />
          {opening
            ? `building ${activePver}…`
            : activePver
              ? `${activePver} · ${mode}`
              : "no PVER selected"}
        </div>
      </header>

      <div className="workspace">
        <AgentChat pver={activePver} mode={mode} alarm={detail?.order_id} />
        <section className={`content ${mode === "pver" ? "graph-content" : ""}`}>
          {error && <div className="error-banner"><strong>Could not load</strong><span>{error}</span></div>}
          {opening ? (
            <div className="center-state">
              <div className="spinner" />
              <h2>Building {activePver}</h2>
              <p>{pverStatus?.message || "Reading folder files and writing both analysis JSONs into this PVER folder."}</p>
            </div>
          ) : mode === "pver" ? (
            graph ? (
              <FullGraph graph={graph} highlight={highlight} detail={detail} pathFilter={pathFilter} onPathFilterChange={setPathFilter} />
            ) : (
              <div className="center-state empty-main">
                <span className="empty-icon">⌁</span>
                <h2>{activePver ? "Full graph is loading" : "Choose a PVER"}</h2>
                <p>Single PVER mode draws this folder’s full control-flow graph. Search an alarm order to highlight the path and list the functions that touch the variable.</p>
              </div>
            )
          ) : detailLoading ? <div className="center-state"><div className="spinner" />Loading alarm detail…</div> :
            detail ? (
              <>
                <section className="panel alarm-header">
                  <div>
                    <div className="header-kicker"><span className="alarm-type">{detail.type || "Alarm"}</span><span>PVER {activePver} · order {detail.order_id}</span></div>
                    <h2>{alarmTitle(detail)}</h2>
                    <p className="location">{detail.location}</p>
                  </div>
                  <div className="header-stats">
                    <div><strong>{detail.paths.length}</strong><span>call paths</span></div>
                    <div><strong>{uniqueFunctionPaths}</strong><span>unique function paths</span></div>
                    <div><strong>{detail.variable_info?.occurrence_count ?? 0}</strong><span>occurrences</span></div>
                  </div>
                  <div className="alarm-message">{detail.message || "No diagnostic message supplied."}</div>
                </section>
                <div className="analysis-grid">
                  <section className="panel graph-panel">
                    <div className="panel-heading">
                      <div><span className="eyebrow">control flow</span><h2>{detail.paths.some((path) => pathFunctions(path).length > 0) ? "Merged call graph" : detail.graph_highlight?.function_sequence?.length ? "Control-flow path" : "Function control flow"}</h2></div>
                      <span className="hint">{detail.paths.some((path) => pathFunctions(path).length > 0) ? `${uniqueFunctionPaths} unique paths · shared prefixes are merged` : detail.graph_highlight?.function_sequence?.length ? "same caller→callee walk as the PVER graph" : "callers and callees from the full PVER graph"}</span>
                    </div>
                    <div className="access-filter">
                      <button className={pathFilter === "all_unique" ? "active" : ""} onClick={() => setPathFilter("all_unique")}>show all unique paths</button>
                      <button className={pathFilter === "var_present" ? "active" : ""} onClick={() => setPathFilter("var_present")}>paths with selected variable</button>
                    </div>
                    <CallGraph
                      paths={detail.paths}
                      functionSnippets={detail.function_snippets}
                      highlightedFunctions={highlightedFunctions}
                      neighborhood={detail.cfg_neighborhood}
                      highlight={detail.graph_highlight}
                      pathFilter={pathFilter}
                    />
                  </section>
                  <VariableExplorer
                    detail={detail}
                    onFunctionsChange={applyFunctionHighlights}
                  />
                </div>
              </>
            ) : (
              <div className="center-state empty-main">
                <span className="empty-icon">⌁</span>
                <h2>{activePver ? "Search an alarm order" : "Choose a PVER"}</h2>
                <p>
                  {activePver
                    ? "Use the top search bar to open one alarm in this mode."
                    : "Available folders under PVERs/ are listed in the top bar."}
                </p>
              </div>
            )}
        </section>
      </div>
    </main>
  );
}
