export type CfgOrderEdge = { s: string; t: string };

function comparePriority(left: [number, number, string], right: [number, number, string]): number {
  if (left[0] !== right[0]) return left[0] - right[0];
  if (left[1] !== right[1]) return left[1] - right[1];
  return left[2].localeCompare(right[2]);
}

export function orderFunctionsByExecution(
  names: string[],
  options: {
    preferred?: string[];
    path?: string[];
    leaf?: string | null;
    edges?: CfgOrderEdge[];
  } = {},
): string[] {
  const unique: string[] = [];
  for (const name of names) {
    if (name && !unique.includes(name)) unique.push(name);
  }
  const declaration = unique.filter((name) => name === "global");
  const funcs = unique.filter((name) => name !== "global");
  if (!funcs.length) return declaration;

  if (options.preferred?.length) {
    const preferred = options.preferred.filter((name) => name !== "global" && funcs.includes(name));
    const missing = funcs.filter((name) => !preferred.includes(name));
    const leaf = options.leaf && funcs.includes(options.leaf) ? options.leaf : null;
    const body = leaf
      ? [...preferred, ...missing].filter((name) => name !== leaf).concat(leaf)
      : [...preferred, ...missing];
    return [...declaration, ...body];
  }

  const pathPos = new Map((options.path ?? []).map((name, index) => [name, index]));
  const funcSet = new Set(funcs);
  const incoming = new Map(funcs.map((name) => [name, 0]));
  const outgoing = new Map(funcs.map((name) => [name, [] as string[]]));
  for (const edge of options.edges ?? []) {
    if (funcSet.has(edge.s) && funcSet.has(edge.t) && edge.s !== edge.t) {
      outgoing.get(edge.s)?.push(edge.t);
      incoming.set(edge.t, (incoming.get(edge.t) ?? 0) + 1);
    }
  }

  const dist = new Map<string, number>();
  const leaf = options.leaf && funcSet.has(options.leaf) ? options.leaf : null;
  if (leaf) {
    const preds = new Map<string, string[]>();
    outgoing.forEach((callees, caller) => {
      callees.forEach((callee) => {
        const list = preds.get(callee) ?? [];
        list.push(caller);
        preds.set(callee, list);
      });
    });
    const queue = [leaf];
    dist.set(leaf, 0);
    for (const node of queue) {
      for (const pred of preds.get(node) ?? []) {
        if (!dist.has(pred)) {
          dist.set(pred, (dist.get(node) ?? 0) + 1);
          queue.push(pred);
        }
      }
    }
  }

  const priority = (name: string): [number, number, string] => {
    if (pathPos.has(name)) return [0, pathPos.get(name) ?? 0, name];
    if (dist.has(name)) return [1, -(dist.get(name) ?? 0), name];
    return [2, 0, name];
  };

  const remaining = new Map(incoming);
  const ready = funcs.filter((name) => remaining.get(name) === 0)
    .sort((left, right) => comparePriority(priority(left), priority(right)));
  const ordered: string[] = [];
  while (ready.length) {
    const node = ready.shift();
    if (!node) break;
    ordered.push(node);
    for (const next of outgoing.get(node) ?? []) {
      remaining.set(next, (remaining.get(next) ?? 1) - 1);
      if (remaining.get(next) === 0) {
        ready.push(next);
        ready.sort((left, right) => comparePriority(priority(left), priority(right)));
      }
    }
  }
  const leftover = funcs.filter((name) => !ordered.includes(name))
    .sort((left, right) => comparePriority(priority(left), priority(right)));
  let result = [...ordered, ...leftover];
  if (leaf && result.includes(leaf)) {
    result = result.filter((name) => name !== leaf).concat(leaf);
  }
  return [...declaration, ...result];
}

export type AccessFilter = "all" | "with_read" | "without_read";

export function hasReadAccess(accesses: Array<string | undefined>): boolean {
  return accesses.some((access) => access === "read");
}

export function matchesAccessFilter(accesses: Array<string | undefined>, filter: AccessFilter): boolean {
  if (filter === "all") return true;
  const reads = hasReadAccess(accesses);
  return filter === "with_read" ? reads : !reads;
}

export function executionRole(name: string, ordered: string[], leaf?: string | null): "declaration" | "leaf" | "intermediate" {
  if (name === "global") return "declaration";
  if (leaf && name === leaf) return "leaf";
  if (ordered.length && ordered[ordered.length - 1] === name && name !== "global") return "leaf";
  return "intermediate";
}
