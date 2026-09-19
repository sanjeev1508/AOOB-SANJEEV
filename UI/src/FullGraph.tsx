import { useEffect, useMemo, useRef, useState } from "react";
import Graph from "graphology";
import Sigma from "sigma";
import { AlarmDetail, AgentFocus, GraphHighlight, GraphPayload, VariableInfo } from "./types";
import { AccessFilter, executionRole, matchesAccessFilter, orderFunctionsByExecution } from "./cfgOrder";
import { PathFilter, filterSequencesByVariable, functionKey, sequenceEdges, sequenceNodes, uniqueSequences } from "./paths";

const C = {
  base: "#2f8caf",
  dim: "#1a2740",
  hop: "#8cb4ff",
  alarm: "#37d7a5",
  origin: "#ffc93c",
  expand: "#49d9ad",
  callee: "#c9a0ff",
  edge: "#243656",
  pathEdge: "#65e2b9",
  expandEdge: "#9ca8ff",
  label: "#dce3f6",
  agent: "#ff7a59",
  agentEdge: "#ffb08a",
};

const MAX_EXPAND = 40;

type Props = {
  graph: GraphPayload;
  highlight?: GraphHighlight | null;
  detail?: AlarmDetail | null;
  pathFilter: PathFilter;
  onPathFilterChange: (filter: PathFilter) => void;
  agentFocus?: AgentFocus | null;
};

function roleColor(role?: string): string {
  if (role === "alarm" || role === "origin_and_alarm") return C.alarm;
  if (role === "origin") return C.origin;
  return C.hop;
}

function alarmVariables(detail?: AlarmDetail | null): VariableInfo[] {
  const all = (detail?.symbol_infos?.length
    ? detail.symbol_infos
    : [detail?.variable_info, ...(detail?.variable_infos ?? [])]).filter(
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
}

function functionRows(
  info: VariableInfo | undefined,
  path: string[],
  leaf?: string | null,
  edges?: Array<{ s: string; t: string }>,
) {
  const onPath = new Set(path);
  const groups = new Map<string, { name: string; count: number; accesses: string[] }>();
  const add = (name: string, access?: string) => {
    const key = name || "global";
    const current = groups.get(key) ?? { name: key, count: 0, accesses: [] };
    current.count += 1;
    if (access && !current.accesses.includes(access)) current.accesses.push(access);
    groups.set(key, current);
  };
  for (const occurrence of info?.occurrences ?? []) {
    add(occurrence.function || "global", occurrence.access);
  }
  for (const name of info?.used_in_functions_ordered ?? info?.used_in_functions ?? []) {
    if (name && !groups.has(name)) add(name);
  }
  const ordered = orderFunctionsByExecution([...groups.keys()], {
    preferred: info?.used_in_functions_ordered,
    path,
    leaf,
    edges,
  });
  return ordered.filter((name) => groups.has(name)).map((name) => ({
    ...groups.get(name)!,
    onPath: onPath.has(name),
    role: executionRole(name, ordered, leaf),
  }));
}

function resolveNode(graph: Graph, name: string): string | null {
  if (graph.hasNode(name)) return name;
  const key = functionKey(name);
  return graph.hasNode(key) ? key : null;
}

function selectedFunctions(info: VariableInfo | undefined): string[] {
  const names = [
    ...(info?.used_in_functions_ordered ?? info?.used_in_functions ?? []),
    ...(info?.occurrences ?? []).map((item) => item.function || ""),
  ];
  return names.map(functionKey).filter((name) => name && name !== "global");
}

export default function FullGraph({
  graph,
  highlight,
  detail,
  pathFilter,
  onPathFilterChange,
  agentFocus,
}: Props) {
  const hostRef = useRef<HTMLDivElement>(null);
  const graphRef = useRef<Graph | null>(null);
  const sigmaRef = useRef<Sigma | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [showAllLabels, setShowAllLabels] = useState(false);
  const [showEdges, setShowEdges] = useState(false);
  const [selectedVariable, setSelectedVariable] = useState<number | null>(null);
  const [accessFilter, setAccessFilter] = useState<AccessFilter>("all");

  const variables = useMemo(() => alarmVariables(detail), [detail]);
  const sequences = useMemo(() => {
    const fromDetail = uniqueSequences(detail?.paths ?? []);
    const raw = fromDetail.length
      ? fromDetail
      : highlight?.function_sequences?.length
        ? highlight.function_sequences
        : highlight?.function_sequence?.length
          ? [highlight.function_sequence]
          : highlight?.cf_nodes?.length
            ? [highlight.cf_nodes]
            : [];
    const varNames = [
      ...(selectedFunctions(selectedVariable == null ? undefined : alarmVariables(detail)[selectedVariable])),
    ];
    return pathFilter === "var_present" ? filterSequencesByVariable(raw, varNames) : raw;
  }, [detail, highlight, pathFilter, selectedVariable]);
  const sequence = sequenceNodes(sequences);
  const selected = selectedVariable == null ? undefined : variables[selectedVariable];
  const rows = useMemo(
    () => functionRows(
      selected,
      sequence,
      highlight?.center ?? detail?.enclosing_function,
      graph.edges,
    ).filter((row) => matchesAccessFilter(row.accesses, accessFilter)),
    [accessFilter, detail?.enclosing_function, graph.edges, highlight?.center, selected, sequence],
  );

  useEffect(() => {
    setSelectedVariable(null);
    setExpanded(new Set());
    setAccessFilter("all");
  }, [detail?.order_id, graph]);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const g = new Graph({ type: "directed", multi: false, allowSelfLoops: true });
    for (const node of graph.nodes) {
      if (g.hasNode(node.id)) continue;
      g.addNode(node.id, {
        x: node.x,
        y: node.y,
        size: node.s || 2,
        color: C.base,
        label: node.id,
        originalSize: node.s || 2,
        forceLabel: false,
        zIndex: 0,
      });
    }
    for (const edge of graph.edges) {
      if (g.hasEdge(edge.id) || !g.hasNode(edge.s) || !g.hasNode(edge.t)) continue;
      const size = Math.min(0.45 + Math.log2((edge.w || 1) + 1) * 0.3, 1.8);
      g.addEdgeWithKey(edge.id, edge.s, edge.t, {
        size,
        color: C.edge,
        hidden: true,
        originalSize: size,
        zIndex: 0,
      });
    }
    const renderer = new Sigma(g, host, {
      allowInvalidContainer: true,
      renderLabels: true,
      labelFont: "JetBrains Mono, SFMono-Regular, Consolas, monospace",
      labelColor: { color: C.label },
      labelSize: 10,
      labelWeight: "500",
      labelRenderedSizeThreshold: 6,
      defaultEdgeType: "arrow",
      stagePadding: 40,
      zIndex: true,
    });
    renderer.getCamera().animatedReset({ duration: 0 });
    const onClick = ({ node }: { node: string }) => {
      setExpanded((current) => {
        const next = new Set(current);
        if (next.has(node)) next.delete(node);
        else next.add(node);
        return next;
      });
    };
    renderer.on("clickNode", onClick);
    graphRef.current = g;
    sigmaRef.current = renderer;
    return () => {
      renderer.off("clickNode", onClick);
      renderer.kill();
      graphRef.current = null;
      sigmaRef.current = null;
    };
  }, [graph]);

  useEffect(() => {
    const g = graphRef.current;
    const renderer = sigmaRef.current;
    if (!g || !renderer) return;

    const resolved = sequence.map((name) => resolveNode(g, name)).filter((name): name is string => Boolean(name));
    const seqSet = new Set(resolved);
    const roles = highlight?.function_roles ?? {};
    const pathEdges = new Set<string>();
    highlight?.highlight_edge_ids?.forEach((id) => pathEdges.add(id));
    highlight?.segments?.forEach((segment) => {
      segment.edges.forEach((id) => pathEdges.add(id));
    });
    const stale: string[] = [];
    g.forEachEdge((id) => {
      if (id.startsWith("hl:")) stale.push(id);
    });
    stale.forEach((id) => g.dropEdge(id));
    sequenceEdges(sequences).forEach(([source, target]) => {
      const from = resolveNode(g, source);
      const to = resolveNode(g, target);
      if (!from || !to) return;
      const edgeId = `${from}->${to}`;
      pathEdges.add(edgeId);
      if (!g.hasEdge(from, to)) {
        const overlay = `hl:${edgeId}`;
        g.addEdgeWithKey(overlay, from, to, {
          size: 1.8,
          color: C.pathEdge,
          hidden: false,
          originalSize: 1.8,
          zIndex: 2,
        });
        pathEdges.add(overlay);
      }
    });
    const active = Boolean(highlight?.center || resolved.length);
    const calleeOf = new Map<string, string>();
    const expandEdges = new Set<string>();
    expanded.forEach((name) => {
      if (!g.hasNode(name)) return;
      const callees = g.outNeighbors(name).slice(0, MAX_EXPAND);
      callees.forEach((callee) => {
        if (!calleeOf.has(callee)) calleeOf.set(callee, name);
        expandEdges.add(`${name}->${callee}`);
      });
    });

    const agentNodes = new Set(
      (agentFocus?.functions ?? [])
        .map((name) => resolveNode(g, name))
        .filter((name): name is string => Boolean(name)),
    );
    const agentEdgeKeys = new Set(
      (agentFocus?.edges ?? [])
        .map((edge) => {
          const from = resolveNode(g, edge.s);
          const to = resolveNode(g, edge.t);
          return from && to ? `${from}->${to}` : "";
        })
        .filter(Boolean),
    );

    g.forEachNode((id, attrs) => {
      const base = Number(attrs.originalSize || 2);
      const onPath = seqSet.has(id);
      const isExpanded = expanded.has(id);
      const isCallee = calleeOf.has(id);
      const isAgent = agentNodes.has(id);
      if (isAgent) {
        g.setNodeAttribute(id, "color", C.agent);
        g.setNodeAttribute(id, "size", Math.max(10, base * 3.2));
        g.setNodeAttribute(id, "label", id);
        g.setNodeAttribute(id, "forceLabel", true);
        g.setNodeAttribute(id, "zIndex", 4);
      } else if (onPath) {
        const role = roles[id] || (id === highlight?.center ? "alarm" : "hop");
        g.setNodeAttribute(id, "color", isExpanded ? C.expand : roleColor(role));
        g.setNodeAttribute(id, "size", Math.max(role === "alarm" || isExpanded ? 9 : 7, base * 2.8));
        g.setNodeAttribute(id, "label", id);
        g.setNodeAttribute(id, "forceLabel", true);
        g.setNodeAttribute(id, "zIndex", 3);
      } else if (isExpanded) {
        g.setNodeAttribute(id, "color", C.expand);
        g.setNodeAttribute(id, "size", Math.max(7, base * 2.4));
        g.setNodeAttribute(id, "label", id);
        g.setNodeAttribute(id, "forceLabel", true);
        g.setNodeAttribute(id, "zIndex", 3);
      } else if (isCallee) {
        g.setNodeAttribute(id, "color", C.callee);
        g.setNodeAttribute(id, "size", Math.max(5.5, base * 2));
        g.setNodeAttribute(id, "label", id);
        g.setNodeAttribute(id, "forceLabel", true);
        g.setNodeAttribute(id, "zIndex", 2);
      } else {
        g.setNodeAttribute(id, "color", active || agentNodes.size ? C.dim : C.base);
        g.setNodeAttribute(id, "size", active || agentNodes.size ? Math.max(0.8, base * 0.35) : base);
        g.setNodeAttribute(id, "label", showAllLabels || !active ? id : "");
        g.setNodeAttribute(id, "forceLabel", showAllLabels);
        g.setNodeAttribute(id, "zIndex", 0);
      }
    });

    g.forEachEdge((id, attrs, source, target) => {
      const onPath = pathEdges.has(id) || pathEdges.has(`${source}->${target}`);
      const isExpand = expandEdges.has(id) || expandEdges.has(`${source}->${target}`);
      const isAgent = agentEdgeKeys.has(`${source}->${target}`) || agentEdgeKeys.has(id);
      const visible = showEdges || onPath || isExpand || isAgent;
      if (isAgent) {
        g.setEdgeAttribute(id, "hidden", false);
        g.setEdgeAttribute(id, "color", C.agentEdge);
        g.setEdgeAttribute(id, "size", (Number(attrs.originalSize) || 1) * 3.6);
        g.setEdgeAttribute(id, "zIndex", 3);
      } else if (onPath) {
        g.setEdgeAttribute(id, "hidden", false);
        g.setEdgeAttribute(id, "color", C.pathEdge);
        g.setEdgeAttribute(id, "size", (Number(attrs.originalSize) || 1) * 3.2);
        g.setEdgeAttribute(id, "zIndex", 2);
      } else if (isExpand) {
        g.setEdgeAttribute(id, "hidden", false);
        g.setEdgeAttribute(id, "color", C.expandEdge);
        g.setEdgeAttribute(id, "size", (Number(attrs.originalSize) || 1) * 2.4);
        g.setEdgeAttribute(id, "zIndex", 1);
      } else {
        g.setEdgeAttribute(id, "hidden", !visible);
        g.setEdgeAttribute(id, "color", active || agentNodes.size ? "#152033" : C.edge);
        g.setEdgeAttribute(id, "size", active || agentNodes.size ? Math.max(0.2, Number(attrs.originalSize) * 0.35) : attrs.originalSize);
        g.setEdgeAttribute(id, "zIndex", 0);
      }
    });

    renderer.setSetting("labelRenderedSizeThreshold", showAllLabels || active || expanded.size || agentNodes.size ? 0 : 6);
    renderer.refresh();
  }, [agentFocus, expanded, graph, highlight, sequence, sequences, showAllLabels, showEdges]);

  useEffect(() => {
    const renderer = sigmaRef.current;
    const g = graphRef.current;
    if (!renderer || !g) return;
    const agentNames = (agentFocus?.functions ?? [])
      .map((name) => resolveNode(g, name))
      .filter((name): name is string => Boolean(name));
    const callees = [...expanded].flatMap((name) => (g.hasNode(name) ? g.outNeighbors(name).slice(0, MAX_EXPAND) : []));
    const focus = (agentNames.length ? agentNames : expanded.size ? [...expanded, ...callees] : sequence)
      .filter((id, index, all) => all.indexOf(id) === index && g.hasNode(id));
    if (!focus.length) {
      if (!highlight?.center && !agentFocus?.functions?.length) renderer.getCamera().animatedReset({ duration: 400 });
      return;
    }
    const points = focus
      .map((id) => renderer.getNodeDisplayData(id))
      .filter((point): point is NonNullable<typeof point> => Boolean(point));
    if (!points.length) return;
    const xs = points.map((point) => point.x);
    const ys = points.map((point) => point.y);
    renderer.getCamera().animate(
      {
        ...renderer.getCamera().getState(),
        angle: 0,
        x: (Math.min(...xs) + Math.max(...xs)) / 2,
        y: (Math.min(...ys) + Math.max(...ys)) / 2,
        ratio: focus.length === 1 ? 0.08 : agentNames.length ? 0.18 : expanded.size ? 0.28 : 0.22,
      },
      { duration: 280 },
    );
  }, [agentFocus, expanded, graph, highlight, sequence]);

  const fit = () => {
    sigmaRef.current?.getCamera().animatedReset({ duration: 400 });
  };

  const zoomPath = () => {
    const renderer = sigmaRef.current;
    const g = graphRef.current;
    if (!renderer || !g || !sequence.length) return;
    const points = sequence
      .filter((id) => g.hasNode(id))
      .map((id) => renderer.getNodeDisplayData(id))
      .filter((point): point is NonNullable<typeof point> => Boolean(point));
    if (!points.length) return;
    const xs = points.map((point) => point.x);
    const ys = points.map((point) => point.y);
    const cam = renderer.getCamera();
    cam.animate(
      {
        ...cam.getState(),
        angle: 0,
        x: (Math.min(...xs) + Math.max(...xs)) / 2,
        y: (Math.min(...ys) + Math.max(...ys)) / 2,
        ratio: 0.18,
      },
      { duration: 420 },
    );
  };

  const zoomFunction = (name: string) => {
    const renderer = sigmaRef.current;
    const g = graphRef.current;
    if (!renderer || !g || !g.hasNode(name)) return;
    const point = renderer.getNodeDisplayData(name);
    if (!point) return;
    renderer.getCamera().animate(
      { ...renderer.getCamera().getState(), angle: 0, x: point.x, y: point.y, ratio: 0.08 },
      { duration: 420 },
    );
  };

  return (
    <div className="pver-graph-layout">
      <div className="full-graph">
        <div ref={hostRef} className="sigma-host" />
        {agentFocus?.agent || agentFocus?.activity ? (
          <div className="agent-live-banner">
            <strong>{agentFocus.agent || agentFocus.stage || "pipeline"}</strong>
            <span>{agentFocus.activity || "working"}</span>
            {agentFocus.functions?.length ? (
              <em>{agentFocus.functions.slice(0, 4).join(" · ")}{agentFocus.functions.length > 4 ? "…" : ""}</em>
            ) : null}
          </div>
        ) : null}
        <div className="graph-filters">
          <label>
            <input type="checkbox" checked={showAllLabels} onChange={(event) => setShowAllLabels(event.target.checked)} />
            show function names of all nodes
          </label>
          <label>
            <input type="checkbox" checked={showEdges} onChange={(event) => setShowEdges(event.target.checked)} />
            show the edges
          </label>
          <div className="access-filter">
            <button className={pathFilter === "all_unique" ? "active" : ""} onClick={() => onPathFilterChange("all_unique")}>show all unique paths</button>
            <button className={pathFilter === "var_present" ? "active" : ""} onClick={() => onPathFilterChange("var_present")}>paths with selected variable</button>
          </div>
        </div>
        <div className="graph-hud">
          <button type="button" onClick={fit}>Fit</button>
          <button type="button" onClick={zoomPath} disabled={!highlight?.center}>Zoom to path</button>
        </div>
        <div className="graph-legend">
          <span><i className="swatch origin" /> origin</span>
          <span><i className="swatch hop" /> hop</span>
          <span><i className="swatch alarm" /> alarm</span>
          <span><i className="swatch expand" /> expanded</span>
          <span><i className="swatch callee" /> callee</span>
          <span><i className="swatch agent" /> agent focus</span>
          <span>{graph.file || "full_control_flow_graph.json"} · {graph.stats?.nodes ?? graph.function_count} nodes · {graph.stats?.edges ?? graph.cf_edge_count} edges</span>
          {sequences.length ? (
            <span>{sequences.length} unique paths · {sequence.length} nodes</span>
          ) : (
            <span>search an alarm order to highlight a path</span>
          )}
          <span>click a node to expand callees · click again to collapse</span>
        </div>
      </div>
      <aside className="var-fn-panel">
        <div className="panel-heading">
          <div>
            <span className="eyebrow">data flow</span>
            <h2>{selected ? "Present in functions" : "Indexed variables"}</h2>
          </div>
          <span className="count-pill">{selected ? rows.length : variables.length}</span>
        </div>
        {!detail ? (
          <p className="location">Search an alarm order to list variables and the functions that use them.</p>
        ) : selected ? (
          <>
            <p className="location">{selected.symbol_name ?? selected.array_name ?? "symbol"}{selected.index_expression ? `[${selected.index_expression}]` : ""}</p>
            <button className="back-button" onClick={() => setSelectedVariable(null)}>← back to variable list</button>
            <div className="access-filter">
              <button className={accessFilter === "all" ? "active" : ""} onClick={() => setAccessFilter("all")}>all</button>
              <button className={accessFilter === "with_read" ? "active" : ""} onClick={() => setAccessFilter("with_read")}>with read access</button>
              <button className={accessFilter === "without_read" ? "active" : ""} onClick={() => setAccessFilter("without_read")}>without read access</button>
            </div>
            <div className="variable-list">
              {rows.length ? rows.map((row) => (
                <button
                  key={row.name}
                  className="variable-item"
                  disabled={row.name === "global"}
                  onClick={() => zoomFunction(row.name)}
                >
                  <strong>
                    <span className={`role-badge ${row.role}`}>{row.role}</span>
                    {row.name}
                    {row.onPath && <span className="flag-badge">on path</span>}
                  </strong>
                  <code>{row.accesses.join(" · ") || "present"} · {row.count} occurrence{row.count === 1 ? "" : "s"}</code>
                </button>
              )) : (
                <div className="muted">This variable has no recorded function uses.</div>
              )}
            </div>
          </>
        ) : (
          <>
            <p className="location">Choose a variable to list the functions it is present in.</p>
            <div className="variable-list">
              {variables.length ? variables.map((item, index) => (
                <button
                  key={`${item.symbol_name ?? item.array_name}-${index}`}
                  className="variable-item"
                  onClick={() => {
                    setSelectedVariable(index);
                    setAccessFilter("all");
                  }}
                >
                  <strong>
                    {item.symbol_name ?? item.array_name ?? "unknown symbol"}
                    {(item.role === "flagged_array") && <span className="flag-badge">flagged</span>}
                  </strong>
                  <code>{item.kind ?? "symbol"}{item.role ? ` · ${item.role}` : ""}{item.index_expression ? ` · ${item.index_expression}` : ""}</code>
                  <span className="variable-summary">{item.occurrence_count ?? item.occurrences?.length ?? 0} occurrences · {(item.used_in_functions ?? []).filter((name) => name && name !== "global").length} functions</span>
                </button>
              )) : (
                <div className="muted">No variable metadata for this alarm.</div>
              )}
            </div>
          </>
        )}
      </aside>
    </div>
  );
}
