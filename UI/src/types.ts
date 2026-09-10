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
};

export type VariableInfo = {
  array_name?: string;
  symbol_name?: string;
  kind?: "array" | "function" | "symbol";
  index_expression?: string;
  datatype?: string | null;
  array_dims?: unknown;
  scope?: string;
  declared_in_function?: string | null;
  declaration_line?: string | null;
  declaration_text?: string | null;
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

export type AlarmDetail = AlarmSummary & {
  order_key: string;
  variable_info?: VariableInfo;
  variable_infos: VariableInfo[];
  symbol_infos?: VariableInfo[];
  paths: PathRecord[];
  function_snippets?: Record<string, { start_line: number; end_line: number; text: string }>;
};

export type AlarmListResponse = {
  items: AlarmSummary[];
  total: number;
};
