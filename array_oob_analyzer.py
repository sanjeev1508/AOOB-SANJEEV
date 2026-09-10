"""Extract and enrich Astree array-out-of-bounds alarms in one pass.

Usage:
    python array_oob_analyzer.py
    python array_oob_analyzer.py <message_log> <source_c> <output_json>

The source C file must use the same line numbering as the locations reported
in the alarm log.  The output contains the grouped alarm data plus
``variable_info`` (the pointed array) and ``variable_infos`` (all indexed
arrays in the alarm source span).
"""

import json
import re
import sys
from collections import OrderedDict


DEFAULT_LOG = r"C:\Users\URE2COB\conf\AOOB-9-9\messeges.txt"
DEFAULT_SOURCE = r"C:\Users\URE2COB\conf\AOOB-9-9\input.c"
DEFAULT_OUTPUT = r"C:\Users\URE2COB\conf\AOOB-9-9\array_oob_variable_info.json"

LOCATION_RE = re.compile(r"\bat\s+([^\s\]]+(?:\.\d+)?(?:-[^\s\]]+)?)\s*\]?\s*$")
CALL_RE = re.compile(r"^call#(.+?)\s+at\s")
LOCATION_SPLIT_RE = re.compile(r"^(?P<file>.*):(?P<numeric>\d+\.\d+-(?:\d+\.)?\d+)$")
SINGLE_LOCATION_RE = re.compile(r"^(\d+)\.(\d+)-(\d+)$")
MULTI_LOCATION_RE = re.compile(r"^(\d+)\.(\d+)-(\d+)\.(\d+)$")
ARRAY_RE = re.compile(r"([A-Za-z_]\w*)\s*\[\s*$")
ASSIGN_RE = re.compile(r"(?<![=!<>+\-*/%&|^])=(?!=)")
FUNC_SIG_RE = re.compile(r"([A-Za-z_]\w*)\s*\([^;{}]*\)\s*$")
IDENTIFIER_RE = re.compile(r"[A-Za-z_]\w*")
MAX_NON_ARRAY_OCCURRENCES = 300
SKIP_KEYWORDS = {
    "return", "if", "else", "for", "while", "do", "switch", "case",
    "break", "continue", "goto", "sizeof", "void", "const", "static",
    "volatile", "extern", "struct", "union", "enum", "unsigned", "signed",
    "char", "short", "int", "long", "float", "double", "bool", "true",
    "false", "NULL",
}


def read_text(path):
    with open(path, "rb") as stream:
        raw = stream.read()
    if raw.startswith(b"\xff\xfe"):
        return raw.decode("utf-16-le")
    if raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16-be")
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def read_blocks(lines):
    i = 0
    while i < len(lines):
        if not lines[i].strip().startswith("["):
            i += 1
            continue
        block = [lines[i]]
        i += 1
        if not block[0].rstrip().endswith("]"):
            while i < len(lines) and not lines[i].rstrip().endswith("]"):
                block.append(lines[i])
                i += 1
            if i < len(lines):
                block.append(lines[i])
                i += 1
        while i < len(lines) and (lines[i].startswith(">") or not lines[i].strip()):
            block.append(lines[i])
            i += 1
            if not lines[i - 1].strip():
                break
        yield "".join(block)


def parse_block(block):
    call_stack, source, underline = [], [], []
    source_raw, underline_raw = [], []
    alarm = None
    for raw in block.splitlines():
        cleaned = raw.strip()
        if cleaned.startswith("["):
            cleaned = cleaned[1:].strip()
        if cleaned.endswith("]"):
            cleaned = cleaned[:-1].strip()
        if not cleaned:
            continue
        if cleaned.startswith(">"):
            content = cleaned[1:].strip()
            marker = raw.find(">")
            raw_content = raw[marker + 1:] if marker >= 0 else raw
            if content and set(content) <= {"~", " "}:
                underline.append(content)
                underline_raw.append(raw_content)
            else:
                source.append(content)
                source_raw.append(raw_content)
        elif cleaned.startswith("ALARM") or cleaned.startswith("ERROR"):
            alarm = cleaned
        elif cleaned.startswith("call#") or cleaned.startswith("loop"):
            call_stack.append(cleaned)
    match = LOCATION_RE.search(alarm or "")
    location = match.group(1) if match else "UNKNOWN_LOCATION::" + (alarm or block[:60])
    return {
        "call_stack": call_stack,
        "alarm": alarm,
        "source_lines": source,
        "source_lines_raw": source_raw,
        "underline_lines_raw": underline_raw,
        "location": location,
    }


def variable_from_underline(source_raw, underline_raw):
    for source, underline in zip(source_raw, underline_raw):
        start = underline.find("~")
        if start >= 0:
            return source[start:underline.rfind("~") + 1]
    return None


def location_numbers(location):
    match = LOCATION_SPLIT_RE.match(location)
    numeric = match.group("numeric") if match else location
    match = MULTI_LOCATION_RE.match(numeric)
    if match:
        return tuple(map(int, match.groups()))
    match = SINGLE_LOCATION_RE.match(numeric)
    if match:
        line, start, end = map(int, match.groups())
        return line, start, line, end
    return None


def function_sequence(call_stack):
    return [match.group(1) for entry in call_stack
            if (match := CALL_RE.match(entry.strip()))]


def find_index_and_array(lines, location):
    parsed = location_numbers(location)
    if parsed is None:
        return None, None, None
    line1, col1, line2, col2 = parsed
    if not 1 <= line1 <= len(lines) or not 1 <= line2 <= len(lines):
        return None, None, None
    first = lines[line1 - 1]
    starts_at_bracket = col1 <= len(first) and first[col1 - 1] == "["
    offset = col1 if starts_at_bracket else col1 - 1
    if line1 == line2:
        expression = first[offset:col2]
        prefix = first[:offset]
    else:
        parts = [first[offset:]]
        parts.extend(lines[n - 1] for n in range(line1 + 1, line2))
        parts.append(lines[line2 - 1][:col2])
        expression = "\n".join(parts)
        prefix = first[:offset]
        if not ARRAY_RE.search(prefix) and line1 > 1:
            prefix = lines[line1 - 2] + prefix
    match = ARRAY_RE.search(prefix)
    if match is None:
        match = re.search(r"([A-Za-z_]\w*)\s*[\)\]]\s*\[\s*$", prefix)
    array = match.group(1) if match else None
    if array is None:
        names = [name for name, _ in array_accesses("\n".join(lines[line1 - 1:line2]))]
        if not names and line1 > 1:
            names = [name for name, _ in array_accesses(lines[line1 - 2])]
        array = names[0] if names else None
    return expression, array, (line1, col1)


def array_accesses(text):
    result, seen = [], set()
    for match in re.finditer(r"([A-Za-z_]\w*)\s*\)?\s*\[", text):
        name = match.group(1)
        if name in seen:
            continue
        opening = text.find("[", match.start(), match.end())
        depth = 0
        closing = None
        for pos in range(opening, len(text)):
            if text[pos] == "[":
                depth += 1
            elif text[pos] == "]":
                depth -= 1
                if depth == 0:
                    closing = pos
                    break
        result.append((name, text[opening + 1:closing] if closing is not None else None))
        seen.add(name)
    return result


def function_ranges(lines):
    ranges, depth, pending = {}, 0, []
    current, start = None, None
    for index, line in enumerate(lines):
        for pos, char in enumerate(line):
            if char == "{":
                if depth == 0:
                    signature = (" ".join(pending) + " " + line[:pos]).strip()
                    match = FUNC_SIG_RE.search(signature)
                    current = match.group(1) if match and match.group(1) not in SKIP_KEYWORDS else None
                    start = index + 1
                    pending = []
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0 and current:
                    ranges[current] = (start, index + 1)
                    current = None
        stripped = line.strip()
        if stripped and not stripped.startswith("//"):
            pending.append(stripped)
            if stripped.endswith(";"):
                pending = []
    return ranges


def enclosing_function(line_no, ranges):
    return next((name for name, (start, end) in ranges.items()
                 if start <= line_no <= end), None)


def declaration(name, lines):
    pattern = re.compile(
        r"^\s*(?P<prefix>.+?)\b" + re.escape(name) +
        r"\b\s*(?P<dims>\[[^;=]*\])?\s*(?:[;={])"
    )
    for number, line in enumerate(lines, 1):
        match = pattern.match(line)
        if match:
            prefix = match.group("prefix").strip()
            if not prefix or prefix.split()[-1] in SKIP_KEYWORDS or "(" in prefix:
                continue
            return {
                "datatype": prefix,
                "array_dims": match.group("dims").strip() if match.group("dims") else None,
                "declaration_line": number,
                "declaration_text": line.strip(),
            }
    return None


def build_occurrence_index(lines, ranges, filename):
    line_functions = [None] * len(lines)
    for name, (start, end) in ranges.items():
        for line_no in range(start, min(end, len(lines)) + 1):
            line_functions[line_no - 1] = name
    occurrences = {}
    for number, line in enumerate(lines, 1):
        assignment = ASSIGN_RE.search(line)
        for match in IDENTIFIER_RE.finditer(line):
            name = match.group(0)
            access = (
                "write" if assignment and match.start() < assignment.start()
                else "read"
            )
            occurrences.setdefault(name, []).append({
                "location": f"{filename}:{number}.{match.start() + 1}-{match.end()}",
                "access": access,
                "line_text": line.strip(),
                "function": line_functions[number - 1] or "global",
            })
    return occurrences


def build_declaration_index(lines):
    declarations = {}
    function_pattern = re.compile(
        r"^\s*(?P<prefix>.*?)\b(?P<name>[A-Za-z_]\w*)\s*"
        r"\([^;{}]*\)\s*\{"
    )
    split_function_pattern = re.compile(
        r"^\s*(?P<prefix>.*?)\b(?P<name>[A-Za-z_]\w*)\s*"
        r"\([^;{}]*\)\s*$"
    )
    pattern = re.compile(
        r"^\s*(?P<prefix>[A-Za-z_][\w\s*]*?)\b"
        r"(?P<name>[A-Za-z_]\w*)\b\s*"
        r"(?P<dims>\[[^;=]*\])?\s*(?:[;={])"
    )
    for number, line in enumerate(lines, 1):
        function_match = function_pattern.match(line)
        if not function_match and number < len(lines):
            split_match = split_function_pattern.match(line)
            if split_match and lines[number].strip().startswith("{"):
                function_match = split_match
        if function_match and function_match.group("prefix").strip():
            declarations.setdefault(function_match.group("name"), {
                "datatype": function_match.group("prefix").strip(),
                "array_dims": None,
                "declaration_line": number,
                "declaration_text": line.strip(),
            })
        match = pattern.match(line)
        if not match or "(" in match.group("prefix"):
            continue
        name = match.group("name")
        declarations.setdefault(name, {
            "datatype": match.group("prefix").strip(),
            "array_dims": match.group("dims").strip() if match.group("dims") else None,
            "declaration_line": number,
            "declaration_text": line.strip(),
        })
    return declarations


def variable_info(name, index, kind, ranges, filename, cache, occurrence_index,
                  declaration_index):
    if name in cache:
        info = dict(cache[name])
        info["index_expression"] = index
        info["kind"] = kind
        return info
    decl = declaration_index.get(name)
    occurrences = [dict(item) for item in occurrence_index.get(name, [])]
    if decl:
        for item in occurrences:
            if item["location"].startswith(f"{filename}:{decl['declaration_line']}."):
                item["access"] = "declaration"
    enclosing = enclosing_function(decl["declaration_line"], ranges) if decl else None
    occurrence_count = len(occurrences)
    occurrences_truncated = False
    if kind != "array" and occurrence_count > MAX_NON_ARRAY_OCCURRENCES:
        half = MAX_NON_ARRAY_OCCURRENCES // 2
        occurrences = occurrences[:half] + occurrences[-half:]
        occurrences_truncated = True
    info = {
        "array_name": name,
        "symbol_name": name,
        "kind": kind,
        "index_expression": index,
        "datatype": decl["datatype"] if decl else None,
        "array_dims": decl["array_dims"] if decl else None,
        "scope": "local" if enclosing else "global",
        "declared_in_function": enclosing,
        "declaration_line": decl["declaration_line"] if decl else None,
        "declaration_text": decl["declaration_text"] if decl else None,
        "occurrence_count": occurrence_count,
        "occurrences_truncated": occurrences_truncated,
        "occurrences": occurrences,
    }
    cache[name] = info
    return dict(info)


def symbols_in_source_span(text):
    type_or_literal = re.compile(
        r"^(?:u?int(?:8|16|32|64)?|sint(?:8|16|32|64)?|"
        r"uint|boolean|size_t|ptrdiff_t|U|UL|L|F)$",
        re.IGNORECASE,
    )
    return list(dict.fromkeys(
        name for name in IDENTIFIER_RE.findall(text)
        if name not in SKIP_KEYWORDS and not type_or_literal.match(name)
    ))


def main():
    log_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_LOG
    source_path = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_SOURCE
    output_path = sys.argv[3] if len(sys.argv) > 3 else DEFAULT_OUTPUT
    log_text = read_text(log_path)
    source_lines = read_text(source_path).splitlines()
    blocks = [parse_block(block) for block in read_blocks(log_text.splitlines(True))]
    matched = [block for block in blocks if "array_out_of_bounds" in json.dumps(block).lower()]
    groups = OrderedDict()
    for block in matched:
        groups.setdefault(block["location"], []).append(block)
    ranges = function_ranges(source_lines)
    filename = source_path.replace("\\", "/").rsplit("/", 1)[-1]
    occurrence_index = build_occurrence_index(source_lines, ranges, filename)
    declaration_index = build_declaration_index(source_lines)
    cache, output = {}, []
    for group_id, (location, paths) in enumerate(groups.items(), 1):
        path_output = [{
            "path_id": path_id,
            "call_stack": path["call_stack"],
            "function_sequence": function_sequence(path["call_stack"]),
            "alarm": path["alarm"],
            "source_lines": path["source_lines"],
        } for path_id, path in enumerate(paths, 1)]
        variable = next((variable_from_underline(path["source_lines_raw"],
                                                  path["underline_lines_raw"])
                         for path in paths
                         if variable_from_underline(path["source_lines_raw"],
                                                    path["underline_lines_raw"]) is not None), None)
        index, primary, parsed = find_index_and_array(source_lines, location)
        if primary is None and variable and re.match(r"^[A-Za-z_]\w*$", variable):
            primary, index = variable, None
        infos = []
        symbol_infos = []
        if parsed:
            line1, _, line2, _ = location_numbers(location)
            source_span = "\n".join(source_lines[line1 - 1:line2])
            accesses = array_accesses(source_span)
            ordered = ([(primary, index)] if primary else []) + [
                item for item in accesses if item[0] != primary
            ]
            array_names = {name for name, _ in accesses}
            all_names = symbols_in_source_span(source_span)
            ordered_symbols = ([primary] if primary else []) + [
                name for name in all_names if name != primary
            ]
            for name in ordered_symbols:
                matching_index = next(
                    (item_index for item_name, item_index in ordered
                     if item_name == name),
                    None,
                )
                kind = "array" if name in array_names else (
                    "function" if re.search(r"\b" + re.escape(name) + r"\s*\(", source_span)
                    else "symbol"
                )
                symbol_infos.append(variable_info(
                    name, matching_index, kind, ranges, filename, cache,
                    occurrence_index, declaration_index
                ))
            infos = [info for info in symbol_infos if info["kind"] == "array"]
        elif primary:
            symbol_infos.append(variable_info(
                primary, index, "symbol", ranges, filename, cache,
                occurrence_index, declaration_index
            ))
            infos = [symbol_infos[0]]
        if infos:
            infos = [dict(info) for info in infos]
        if symbol_infos:
            symbol_infos = [dict(info) for info in symbol_infos]
        entry = {
            "group_id": group_id,
            "location": location,
            "no_of_paths": len(paths),
            "variable": variable,
            "paths": path_output,
            "variable_info": infos[0] if infos else None,
            "variable_infos": infos,
            "symbol_infos": symbol_infos,
        }
        output.append(entry)
    with open(output_path, "w", encoding="utf-8") as stream:
        json.dump(output, stream, indent=2, ensure_ascii=False)
    print(f"Parsed blocks: {len(blocks)}")
    print(f"Matched array_out_of_bounds blocks: {len(matched)}")
    print(f"Groups: {len(output)}")
    print(f"Groups with variable info: {sum(bool(x['variable_infos']) for x in output)}")
    print(f"Output written to: {output_path}")


if __name__ == "__main__":
    main()
