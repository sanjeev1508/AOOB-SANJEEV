import { PathRecord } from "./types";

export type PathFilter = "all_unique" | "var_present";

export function functionKey(name: string): string {
  return name.split("|", 1)[0].trim();
}

export function pathFunctions(path: PathRecord): string[] {
  const raw = path.function_sequence?.length
    ? [...path.function_sequence]
    : (path.call_stack ?? [])
      .map((entry) => entry.match(/^call#(.+?)(?:\||\s+at\s)/)?.[1])
      .filter((name): name is string => Boolean(name));
  const cleaned = raw.map(functionKey).filter(Boolean);
  return cleaned.filter((name, index) => index === 0 || cleaned[index - 1] !== name);
}

export function uniqueSequences(paths: PathRecord[]): string[][] {
  const seen = new Set<string>();
  const sequences: string[][] = [];
  for (const path of paths) {
    const sequence = pathFunctions(path);
    if (!sequence.length) continue;
    const key = sequence.join(">");
    if (seen.has(key)) continue;
    seen.add(key);
    sequences.push(sequence);
  }
  return sequences;
}

export function filterSequencesByVariable(sequences: string[][], varFunctions: Iterable<string>): string[][] {
  const present = new Set(
    [...varFunctions].map(functionKey).filter((name) => name && name !== "global"),
  );
  if (!present.size) return [];
  return sequences.filter((sequence) => sequence.some((name) => present.has(functionKey(name))));
}

export function sequenceNodes(sequences: string[][]): string[] {
  const names: string[] = [];
  const seen = new Set<string>();
  for (const sequence of sequences) {
    for (const name of sequence) {
      const key = functionKey(name);
      if (!key || seen.has(key)) continue;
      seen.add(key);
      names.push(key);
    }
  }
  return names;
}

export function sequenceEdges(sequences: string[][]): Array<[string, string]> {
  const edges: Array<[string, string]> = [];
  const seen = new Set<string>();
  for (const sequence of sequences) {
    for (let index = 0; index < sequence.length - 1; index += 1) {
      const source = functionKey(sequence[index]);
      const target = functionKey(sequence[index + 1]);
      const id = `${source}->${target}`;
      if (!source || !target || source === target || seen.has(id)) continue;
      seen.add(id);
      edges.push([source, target]);
    }
  }
  return edges;
}
