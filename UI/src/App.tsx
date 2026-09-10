import { FormEvent, useEffect, useMemo, useState } from "react";
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
import { AlarmDetail, PathRecord, VariableInfo } from "./types";

const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/$/, "");

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "The request failed.";
}

async function api<T>(path: string): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`);
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail ?? `Request failed (${response.status})`);
  }
  return response.json() as Promise<T>;
}

function pathFunctions(path: PathRecord): string[] {
  const raw = path.function_sequence?.length ? [...path.function_sequence] : (path.call_stack ?? [])
    .map((entry) => entry.match(/^call#(.+?)(?:\||\s+at\s)/)?.[1])
    .filter((name): name is string => Boolean(name));
  return raw.filter((name, index) => index === 0 || raw[index - 1] !== name);
}

function indexSymbols(expression?: string): string[] {
  if (!expression) return [];
  const symbols = expression.match(/[A-Za-z_]\w*/g) ?? [];
  return [...new Set(symbols)];
}

function CallGraph({
  paths,
  functionSnippets,
}: {
  paths: PathRecord[];
  functionSnippets?: AlarmDetail["function_snippets"];
}) {
  const [selectedNode, setSelectedNode] = useState<string | null>(null);
  const { nodes, edges } = useMemo(() => {
    const graph = new dagre.graphlib.Graph();
    graph.setDefaultEdgeLabel(() => ({}));
    const nodeMap = new Map<string, Node>();
    const edgeMap = new Map<string, Edge>();
    const root = "root";
    nodeMap.set(root, {
      id: root,
      data: { label: "alarm entry" },
      position: { x: 0, y: 0 },
      sourcePosition: Position.Bottom,
      targetPosition: Position.Top,
      style: { background: "#19213a", color: "#e6ebff", borderColor: "#6978ff" },
    });

    paths.forEach((path, pathIndex) => {
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
        const edgeId = `${parent}->${id}`;
        if (!edgeMap.has(edgeId)) {
          edgeMap.set(edgeId, {
            id: edgeId,
            source: parent,
            target: id,
            label: pathIndex ? undefined : undefined,
            markerEnd: { type: MarkerType.ArrowClosed, color: "#6574cd" },
            style: { stroke: "#6574cd" },
          });
        }
        parent = id;
      });
    });

    graph.setGraph({ rankdir: "TB", nodesep: 28, ranksep: 62, marginx: 20, marginy: 20 });
    nodeMap.forEach((node) => graph.setNode(node.id, { width: 190, height: 54 }));
    edgeMap.forEach((edge) => graph.setEdge(edge.source, edge.target));
    dagre.layout(graph);
    nodeMap.forEach((node) => {
      const point = graph.node(node.id);
      node.position = { x: point.x - 95, y: point.y - 27 };
    });
    return { nodes: Array.from(nodeMap.values()), edges: Array.from(edgeMap.values()) };
  }, [paths]);

  if (paths.length === 0) return <div className="empty-panel">No call paths are available.</div>;
  const selected = nodes.find((node) => node.id === selectedNode);
  return (
    <>
      <div className="flow-shell">
      <ReactFlow nodes={nodes} edges={edges} fitView nodesConnectable={false} zoomOnDoubleClick={false}
        onNodeClick={(_, node) => setSelectedNode(node.id)}>
        <Background color="#26304b" gap={22} />
        <Controls showInteractive={false} />
        <MiniMap nodeColor={(node) => (node.id === "root" ? "#6978ff" : "#37d7a5")} />
      </ReactFlow>
      </div>
      {selected && selected.id !== "root" && (
        <div className="node-inspector">
          <strong>{selected.data.label}</strong>
          <span>
            {functionSnippets?.[selected.data.label]
              ? `input.c:${functionSnippets[selected.data.label].start_line}-${functionSnippets[selected.data.label].end_line}`
              : "No source definition found in input.c"}
          </span>
          {functionSnippets?.[selected.data.label] && (
            <pre className="source-snippet">{functionSnippets[selected.data.label].text}</pre>
          )}
        </div>
      )}
    </>
  );
}

function VariableExplorer({
  detail,
}: {
  detail: AlarmDetail;
}) {
  const variables = useMemo(() => {
    const all = (detail.symbol_infos?.length
      ? detail.symbol_infos
      : [detail.variable_info, ...detail.variable_infos]).filter(
      (item): item is VariableInfo => Boolean(item),
    );
    const seen = new Set<string>();
    return all.filter((item) => {
      const key = `${item.symbol_name ?? item.array_name ?? "unknown"}|${item.index_expression ?? ""}`;
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  }, [detail]);
  const [selectedVariable, setSelectedVariable] = useState(0);
  const [expandedFunctions, setExpandedFunctions] = useState<Set<string>>(new Set());
  const selected = variables[selectedVariable] ?? variables[0];
  const occurrences = selected?.occurrences ?? [];
  const groupedOccurrences = useMemo(() => {
    const groups = new Map<string, typeof occurrences>();
    occurrences.forEach((occurrence) => {
      const name = occurrence.function || "global";
      groups.set(name, [...(groups.get(name) ?? []), occurrence]);
    });
    return [...groups.entries()];
  }, [occurrences]);

  useEffect(() => {
    setSelectedVariable(0);
    setExpandedFunctions(new Set());
  }, [detail.order_id]);

  return (
    <section className="panel variable-panel">
      <div className="panel-heading">
        <div>
          <span className="eyebrow">data surface</span>
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
                onClick={() => setSelectedVariable(index)}
              >
                <strong>
                  {item.symbol_name ?? item.array_name ?? "unknown symbol"}
                  {item.array_name === detail.variable_info?.array_name && <span className="flag-badge">flagged</span>}
                </strong>
                <code>{item.kind ?? "symbol"}{item.index_expression ? ` · ${item.index_expression}` : ""}</code>
                <span className="index-symbols">
                  {indexSymbols(item.index_expression).map((symbol) => `## ${symbol}`).join("  ")}
                </span>
                <span className="variable-summary">
                  {item.datatype ?? "type unavailable"} · {String(item.array_dims ?? "dimensions unavailable")} · {item.scope ?? "scope unavailable"}
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
                <div><label>type</label><span>{selected.datatype ?? "type unavailable"}</span></div>
                <div><label>size</label><span>{String(selected.array_dims ?? "size unavailable")}</span></div>
                <div><label>scope</label><span>{selected.scope ?? "scope unavailable"}</span></div>
                <div><label>declared line</label><span>{selected.declaration_line ?? "unavailable"}</span></div>
              </div>
              {selected.occurrences_truncated && (
                <div className="metadata-note">
                  Showing a bounded sample of {selected.occurrence_count} total occurrences.
                </div>
              )}
              <button className="back-button" onClick={() => setSelectedVariable(0)}>
                ← back to variable list
              </button>
              <div className="function-tree">
                {groupedOccurrences.length ? groupedOccurrences.map(([name, items]) => {
                  const expanded = expandedFunctions.has(name);
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
                        <span>{expanded ? "▾" : "▸"} {name}</span>
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

export default function App() {
  const [detail, setDetail] = useState<AlarmDetail | null>(null);
  const [lookup, setLookup] = useState("");
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState("");

  const loadAlarm = async (order: string) => {
    setDetailLoading(true);
    setError("");
    try {
      const result = await api<AlarmDetail>(`/api/alarm/${encodeURIComponent(order)}`);
      setDetail(result);
      setLookup(result.order_id);
    } catch (requestError) {
      setDetail(null);
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

  return (
    <main className="app-shell">
      <header className="topbar">
        <form className="lookup" onSubmit={onLookup}>
          <span className="prompt">/</span>
          <input
            aria-label="Alarm order lookup"
            value={lookup}
            onChange={(event) => setLookup(event.target.value)}
            placeholder="look up alarm order (e.g. 1,002)"
          />
          <button type="submit">Inspect</button>
        </form>
      </header>

      <div className="workspace">
        <section className="content">
          {error && <div className="error-banner"><strong>Could not load alarm</strong><span>{error}</span></div>}
          {detailLoading ? <div className="center-state"><div className="spinner" />Loading alarm detail…</div> :
            detail ? (
              <>
                <section className="panel alarm-header">
                  <div>
                    <div className="header-kicker"><span className="alarm-type">{detail.type || "Alarm"}</span><span>order {detail.order_id}</span></div>
                    <h2>{detail.variable || "Indexed variable alarm"}</h2>
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
                      <div><span className="eyebrow">control flow</span><h2>Merged call graph</h2></div>
                      <span className="hint">shared prefixes are merged</span>
                    </div>
                    <CallGraph
                      paths={detail.paths}
                      functionSnippets={detail.function_snippets}
                    />
                  </section>
                  <VariableExplorer detail={detail} />
                </div>
              </>
            ) : (
              <div className="center-state empty-main">
                <span className="empty-icon">⌁</span>
                <h2>Select an alarm to begin</h2>
                <p>Choose a result from the index or enter an order in the lookup bar.</p>
              </div>
            )}
        </section>
      </div>
    </main>
  );
}
