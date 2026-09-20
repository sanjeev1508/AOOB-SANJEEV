export type PathRecord = {
  path_id?: number;
  call_stack?: string[];
  function_sequence?: string[];
  alarm?: string;
  source_lines?: string[];
};

export type Occurrence = {
  location?: string;
  access?: string;
  line_text?: string;
  function?: string;
  callee?: string | null;
  line?: number;
};

export type PassedTo = {
  function?: string;
  location?: string;
  mode?: string;
  line_text?: string;
  in_function?: string;
};

export type VariableInfo = {
  array_name?: string;
  symbol_name?: string;
  kind?: "array" | "function" | "symbol" | "struct" | "struct_member" | "pointer" | "parameter";
  role?: string;
  index_expression?: string;
  dimension_index?: number | null;
  datatype?: string | null;
  is_array?: boolean;
  is_pointer?: boolean;
  is_struct?: boolean;
  is_struct_member?: boolean;
  member_of?: string | null;
  parent_symbol?: string | null;
  member_path?: string[] | null;
  pointed_type?: string | null;
  array_dims?: unknown;
  resolved_sizes?: Array<number | null> | null;
  size_known?: boolean | null;
  scope?: string;
  declared_in_function?: string | null;
  declaration_line?: number | string | null;
  declaration_text?: string | null;
  declaration_kind?: string;
  access_counts?: Record<string, number>;
  passed_to?: PassedTo[];
  used_in_functions?: string[];
  used_in_functions_ordered?: string[];
  occurrence_count?: number;
  occurrences_truncated?: boolean;
  occurrences?: Occurrence[];
};

export type AlarmSummary = {
  order_id: string;
  type: string;
  category: string;
  location: string;
  classification: string;
  comment: string;
  message: string;
  group_id?: number;
  variable?: string;
  path_count: number;
  function_names: string[];
};

export type CfgNeighborhood = {
  function: string;
  callers: string[];
  callees: string[];
  caller_count?: number;
  callee_count?: number;
};

export type CfgHub = {
  name: string;
  in: number;
  out: number;
};

export type CfgSummary = {
  pver?: string | null;
  file: string;
  path?: string;
  function_count: number;
  edge_count: number;
  hubs: CfgHub[];
};

export type AlarmDetail = AlarmSummary & {
  order_key: string;
  variable_info?: VariableInfo;
  variable_infos: VariableInfo[];
  symbol_infos?: VariableInfo[];
  paths: PathRecord[];
  enclosing_function?: string | null;
  cfg_neighborhood?: CfgNeighborhood;
  graph_highlight?: GraphHighlight;
  function_snippets?: Record<string, { start_line: number; end_line: number; text: string }>;
};

export type AlarmListResponse = {
  items: AlarmSummary[];
  total: number;
  pver?: string | null;
};

export type PverInfo = {
  id: string;
  name: string;
  path: string;
  alarms_file?: string | null;
  source_file?: string | null;
  log_file?: string | null;
  output_file?: string | null;
  cfg_file?: string | null;
  has_alarms: boolean;
  has_source: boolean;
  has_output: boolean;
  has_cfg?: boolean;
  alarm_count: number;
  status: "ready" | "needs_build" | "building" | "error" | "incomplete";
  active: boolean;
  message?: string;
  total?: number;
  graph_file?: string;
};

export type PverListResponse = {
  items: PverInfo[];
  total: number;
  active?: string | null;
};

export type GraphHighlight = {
  center?: string | null;
  cf_nodes: string[];
  cf_edges: string[][];
  symbol_nodes: string[];
  function_sequence?: string[];
  function_sequences?: string[][];
  path_count?: number;
  function_roles?: Record<string, string>;
  highlight_nodes?: string[];
  highlight_edge_ids?: string[];
  segments?: Array<{ from: string; to: string; kind: string; nodes: string[]; edges: string[] }>;
  first_access?: { function?: string } | null;
};

export type GraphNode = {
  id: string;
  x: number;
  y: number;
  s: number;
};

export type GraphEdge = {
  id: string;
  s: string;
  t: string;
  w?: number;
};

export type GraphPayload = {
  pver?: string | null;
  file?: string;
  path?: string;
  version?: number;
  renderer?: string;
  nodes: GraphNode[];
  edges: GraphEdge[];
  stats?: { nodes: number; edges: number };
  function_count: number;
  cf_edge_count: number;
  df_edge_count: number;
};

export type CfgResponse = {
  pver?: string | null;
  file: string;
  graph: Record<string, string[]>;
  callers: Record<string, string[]>;
  function_count: number;
  edge_count: number;
};

export type AgentCursor = {
  current?: string | null;
  prev?: string | null;
  index?: number;
  total?: number;
  sequence?: string[];
  symbols?: string[];
  current_symbol?: string | null;
  done?: boolean;
};

export type AgentFocus = {
  agent?: string;
  stage?: string;
  activity?: string;
  functions?: string[];
  edges?: Array<{ s: string; t: string }>;
  updated_at?: number;
  cursors?: Record<string, AgentCursor>;
  call_path?: {
    sequence?: string[];
    edges?: Array<{ s: string; t: string }>;
  };
  var_paths?: Array<{
    symbol?: string;
    role?: string;
    functions?: string[];
    edges?: Array<{ s: string; t: string }>;
  }>;
};

export type PipelineEvent = {
  ts?: number;
  kind: string;
  agent?: string | null;
  stage?: string | null;
  title?: string;
  detail?: string | null;
  input_preview?: string | null;
  output_preview?: string | null;
  tool?: string | null;
  functions?: string[];
  edges?: Array<{ s: string; t: string }>;
  activity?: string | null;
};
