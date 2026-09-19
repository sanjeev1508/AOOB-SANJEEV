"""Extract and enrich Astree array-out-of-bounds alarms in one pass.

Usage:
    python array_oob_analyzer.py
    python array_oob_analyzer.py <message_log_or_csv> <source_c> <output_json> [cfg_json]
    python array_oob_analyzer.py --cfg-only <source_c> <cfg_json>
    python array_oob_analyzer.py --merge-paths <output_json> [message_log]

The source C file must use the same line numbering as the locations reported
in the alarm log / CSV.  The output contains the grouped alarm data plus
``variable_info`` (the alarmed array) and ``symbol_infos`` (only the relevant
symbols: the array, its struct/parent path, and index variables -- not every
identifier on the line).  Each symbol records kind (array / struct_member /
pointer / parameter / symbol), scope, size/dims, pointer-ness, and every
declaration / read / write / parameter / address-of use with line and function.

PVER layout (do not special-case a single project):
    <pver>/Full_alarms.csv                 Astree export (``Location`` + optional ``Message``)
    <pver>/messeges.txt                    Astree path traces (``call#`` stacks); optional
    <pver>/input.c                         preprocessed AP1_bc_with_context.c, same line numbers
    <pver>/array_oob_variable_info.json    alarm / symbol output (this script)
    <pver>/full_control_flow_graph.json    caller -> [callee, ...] for every function

When the first argument is a CSV, the script also reads a sibling
``messeges.txt`` / ``messages.txt`` and attaches every matching
``array_out_of_bounds`` trace to that alarm.  CSV alone has no call
stacks, so a CSV-only run would otherwise store one empty path.

4105 and 5901 in this repo are samples only.  This pipeline must stay
accurate for every PVER that uses the same Astree export + preprocessed
source pattern -- including multi-dimensional ``arr[i][j]``, ``Type const``
declarations, struct/member arrays, split-line accesses, and CSV-only
folders that have no message log.
"""

import csv
import json
import os
import re
import sys
from collections import OrderedDict
from io import StringIO


DEFAULT_LOG = r"C:\Users\URE2COB\conf\AOOB-9-9\messeges.txt"
DEFAULT_SOURCE = r"C:\Users\URE2COB\conf\AOOB-9-9\input.c"
DEFAULT_OUTPUT = r"C:\Users\URE2COB\conf\AOOB-9-9\array_oob_variable_info.json"
CFG_OUTPUT_NAME = "full_control_flow_graph.json"
DEFAULT_CFG_OUTPUT = r"C:\Users\URE2COB\conf\AOOB-9-9\full_control_flow_graph.json"

MAX_OCCURRENCES_SHOWN = 80  # cap for occurrence lists; counts stay complete
PRIMITIVE_TYPES = {
    "uint8", "uint16", "uint32", "uint64", "sint8", "sint16", "sint32", "sint64",
    "real32", "real64", "boolean", "bool", "uint", "sint", "float32", "float64",
    "char", "short", "int", "long", "float", "double", "void", "size_t",
}
POINTER_MACRO_RE = re.compile(r"\bP2(?:VAR|CONST|FUNC|MEM|P2VAR|P2CONST)\b")
STRUCT_HEAD_RE = re.compile(
    r"(?:^|[=;,({])\s*(?:typedef\s+)?(?P<kind>struct|union)\s*(?P<name>[A-Za-z_]\w*)?"
)
CLOSE_TYPE_RE = re.compile(r"^\s*\}\s*(?P<name>[A-Za-z_]\w*)")

LOCATION_RE = re.compile(r"\bat\s+([^\s\]]+(?:\.\d+)?(?:-[^\s\]]+)?)\s*\]?\s*$")
CALL_RE = re.compile(r"^call#(.+?)\s+at\s")
LOCATION_SPLIT_RE = re.compile(r"^(?P<file>.*):(?P<numeric>\d+\.\d+-(?:\d+\.)?\d+)$")
SINGLE_LOCATION_RE = re.compile(r"^(\d+)\.(\d+)-(\d+)$")
MULTI_LOCATION_RE = re.compile(r"^(\d+)\.(\d+)-(\d+)\.(\d+)$")
ARRAY_RE = re.compile(r"([A-Za-z_]\w*)\s*\[\s*$")
ASSIGN_RE = re.compile(r"(?<![=!<>+\-*/%&|^])=(?!=)")
IDENTIFIER_RE = re.compile(r"[A-Za-z_]\w*")
TYPEDEF_RE = re.compile(
    r"^\s*typedef\s+(?P<base>.+?)\s+(?P<name>[A-Za-z_]\w*)\s*(?P<dims>\[[^;]*\])?\s*;\s*$"
)
SKIP_KEYWORDS = {
    "return", "if", "else", "for", "while", "do", "switch", "case",
    "break", "continue", "goto", "sizeof", "void", "const", "static",
    "volatile", "extern", "struct", "union", "enum", "unsigned", "signed",
    "char", "short", "int", "long", "float", "double", "bool", "true",
    "false", "NULL", "typedef",
}
# Statement starters only -- type keywords (int/const/static/...) are valid
# as the last token of a declaration prefix (``static Type const name[]``).
STATEMENT_KEYWORDS = {
    "return", "if", "else", "for", "while", "do", "switch", "case",
    "break", "continue", "goto", "sizeof",
}
DEFINE_RE = re.compile(
    r"^\s*#\s*define\s+(?P<name>[A-Za-z_]\w*)\s+"
    r"\(?\s*(?P<value>[+-]?\d+)\s*[UuLl]*\s*\)?"
)
ENUM_ASSIGN_RE = re.compile(r"\b([A-Za-z_]\w*)\s*=\s*([+-]?\d+)")
DIM_PIECE_RE = re.compile(r"\[(.*?)\]")
SAFE_DIM_EXPR_RE = re.compile(r"^[0-9+\-*/() \t]+$")
MANGLED_SUFFIX_RE = re.compile(r"___[0-9A-Fa-f]{4,}$")


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


def looks_like_alarms_csv(path, text):
    if path.lower().endswith(".csv"):
        return True
    head = text.lstrip()[:120].lower()
    return head.startswith("sep=") or head.startswith("order;")


def blocks_from_csv(text):
    """Turn an Astree Full_alarms.csv into the same ``[ALARM ...]`` blocks
    the message-log parser already understands.  Works for both the 4105
    style (Message filled in) and the 5901 style (Location only)."""
    stream = StringIO(text)
    first = stream.readline()
    if not first.lower().startswith("sep="):
        stream.seek(0)
    reader = csv.DictReader(stream, delimiter=";")
    for row in reader:
        cleaned = {(key or "").strip(): (value or "").strip() for key, value in row.items()}
        location = cleaned.get("Location", "")
        if not location:
            continue
        message = cleaned.get("Message", "")
        category = cleaned.get("Category", "")
        if message and re.search(r"\bat\s+\S+$", message):
            alarm = message
        elif message:
            alarm = f"{message} at {location}"
        else:
            alarm = f"ALARM (A) array_out_of_bounds at {location}"
        if "array_out_of_bounds" not in alarm.lower():
            if "out-of-bound" in category.lower() or "array" in category.lower():
                alarm = f"ALARM (A) array_out_of_bounds: {message} at {location}".strip()
            else:
                continue
        yield f"[{alarm}]\n"


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


MESSAGE_LOG_NAMES = ("messeges.txt", "messages.txt", "message.txt")


def is_aoob_block(block):
    return "array_out_of_bounds" in (block.get("alarm") or "").lower()


def sibling_message_log(input_path):
    folder = os.path.dirname(os.path.abspath(input_path)) or "."
    source = os.path.abspath(input_path)
    for name in MESSAGE_LOG_NAMES:
        candidate = os.path.abspath(os.path.join(folder, name))
        if candidate != source and os.path.isfile(candidate):
            return candidate
    return None


def location_numeric_key(location):
    match = LOCATION_SPLIT_RE.match(location or "")
    return match.group("numeric") if match else None


def index_log_paths(blocks):
    by_location = OrderedDict()
    by_numeric = OrderedDict()
    for block in blocks:
        if not is_aoob_block(block):
            continue
        location = block.get("location") or ""
        by_location.setdefault(location, []).append(block)
        numeric = location_numeric_key(location)
        if numeric:
            by_numeric.setdefault(numeric, []).append(block)
    return by_location, by_numeric


def traces_for_location(location, by_location, by_numeric):
    if location in by_location:
        return by_location[location]
    numeric = location_numeric_key(location)
    if numeric and numeric in by_numeric:
        return by_numeric[numeric]
    return []


def unique_traces(paths):
    seen = set()
    unique = []
    for path in paths:
        key = tuple(path.get("call_stack") or [])
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def path_records(paths, enclosing=None):
    unique = unique_traces(paths)
    records = [{
        "path_id": path_id,
        "call_stack": path.get("call_stack") or [],
        "function_sequence": function_sequence(path.get("call_stack") or []) or ([enclosing] if enclosing else []),
        "alarm": path.get("alarm"),
        "source_lines": path.get("source_lines") or [],
    } for path_id, path in enumerate(unique, 1)]
    return records, len(paths), len(unique)


def load_message_log_paths(log_path):
    if not log_path or not os.path.isfile(log_path):
        return OrderedDict(), OrderedDict()
    blocks = [parse_block(block) for block in read_blocks(read_text(log_path).splitlines(True))]
    return index_log_paths(blocks)


def merge_paths_into_json(output_path, log_path):
    with open(output_path, "r", encoding="utf-8-sig") as stream:
        entries = json.load(stream)
    if not isinstance(entries, list):
        raise ValueError("variable info JSON must contain an array")
    by_location, by_numeric = load_message_log_paths(log_path)
    updated = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        traces = traces_for_location(entry.get("location") or "", by_location, by_numeric)
        if not traces:
            continue
        records, total, unique = path_records(traces, entry.get("enclosing_function"))
        entry["paths"] = records
        entry["no_of_paths"] = unique
        entry["no_of_traces"] = total
        updated += 1
    with open(output_path, "w", encoding="utf-8") as stream:
        json.dump(entries, stream, indent=2, ensure_ascii=False)
    return updated, len(entries)


def _skip_ws_left(text, index):
    while index >= 0 and text[index].isspace():
        index -= 1
    return index


def _match_opener_left(text, index, closer, opener):
    depth = 1
    index -= 1
    while index >= 0 and depth:
        char = text[index]
        if char == closer:
            depth += 1
        elif char == opener:
            depth -= 1
        index -= 1
    return index


def array_from_bracket(text, bracket_pos):
    """Walk left from the alarmed ``[`` to the array / member being indexed.

    Skips earlier dimensions so ``arr[i][j]`` with the alarm on ``[j]``
    yields ``arr`` (dimension 1), not the first index name ``i``.
    Also unwraps ``(ptr->field)[i]`` (staying *inside* the parens so a
    previous statement cannot be picked up) and ``name\\n    [i]``.
    """
    if bracket_pos < 0 or bracket_pos >= len(text) or text[bracket_pos] != "[":
        return None, 0, []
    index = _skip_ws_left(text, bracket_pos - 1)
    dimension = 0
    while index >= 0 and text[index] == "]":
        index = _match_opener_left(text, index, "]", "[")
        dimension += 1
        index = _skip_ws_left(text, index)
    floor = 0
    while index >= floor and text[index] == ")":
        close = index
        opener_before = _match_opener_left(text, index, ")", "(")
        floor = max(floor, opener_before + 2)
        index = _skip_ws_left(text, close - 1)
    parts = []
    while index >= floor:
        if text[index] == ")":
            close = index
            opener_before = _match_opener_left(text, index, ")", "(")
            floor = max(floor, opener_before + 2)
            index = _skip_ws_left(text, close - 1)
            continue
        if text[index] == "]":
            index = _match_opener_left(text, index, "]", "[")
            index = _skip_ws_left(text, index)
            continue
        if text[index] == "*":
            index = _skip_ws_left(text, index - 1)
            continue
        if text[index].isalnum() or text[index] == "_":
            end = index + 1
            while index >= floor and (text[index].isalnum() or text[index] == "_"):
                index -= 1
            parts.append(text[index + 1:end])
            index = _skip_ws_left(text, index)
            if index >= floor and text[index] == ".":
                index = _skip_ws_left(text, index - 1)
                continue
            if index >= floor + 1 and text[index] == ">" and text[index - 1] == "-":
                index = _skip_ws_left(text, index - 2)
                continue
            break
        break
    parts.reverse()
    if not parts:
        return None, dimension, []
    name = parts[-1]
    if name in SKIP_KEYWORDS:
        return None, dimension, parts
    return name, dimension, parts


def _highlighted_index(text, col1, col2):
    start = max(col1 - 1, 0)
    end = min(len(text), col2)
    snippet = text[start:end]
    return snippet[1:] if snippet.startswith("[") else snippet


def locate_array_access(lines, location):
    """Resolve the array, alarmed index, and dimension at an Astree location."""
    parsed = location_numbers(location)
    if parsed is None:
        return None
    line1, col1, line2, col2 = parsed
    if not 1 <= line1 <= len(lines) or not 1 <= line2 <= len(lines):
        return None
    lookback = 2
    start_line = max(1, line1 - lookback)
    prefix_lines = lines[start_line - 1:line1 - 1]
    prefix = "\n".join(prefix_lines)
    if prefix:
        prefix += "\n"
    first = lines[line1 - 1]
    if line1 == line2:
        col0 = col1 - 1
        if 0 <= col0 < len(first) and first[col0] == "[":
            bracket = col0
        else:
            bracket = first.rfind("[", 0, min(len(first), max(col0 + 1, 0)))
        index_text = _highlighted_index(first, col1, col2)
        combined = prefix + first
        abs_bracket = len(prefix) + bracket if bracket >= 0 else len(prefix) + max(col0, 0)
    else:
        parts = [first[col1 - 1:]]
        parts.extend(lines[number - 1] for number in range(line1 + 1, line2))
        parts.append(lines[line2 - 1][:col2])
        span = "\n".join(parts)
        index_text = span[1:] if span.startswith("[") else span
        combined = prefix + first[:col1 - 1] + span
        abs_bracket = len(prefix) + (col1 - 1)
        if abs_bracket >= len(combined) or combined[abs_bracket] != "[":
            found = combined.rfind("[", 0, min(len(combined), abs_bracket + 1))
            if found >= 0:
                abs_bracket = found
    name, dimension, path = array_from_bracket(combined, abs_bracket)
    if name is None:
        # Last resort: first access whose `[` sits at this column.
        for access_name, _ in array_accesses(combined):
            name = access_name
            break
    return {
        "index": index_text,
        "array": name,
        "parsed": (line1, col1),
        "dimension": dimension,
        "member_path": path,
        "context_tokens": [part for part in path[:-1] if part not in SKIP_KEYWORDS],
    }


def find_index_and_array(lines, location):
    access = locate_array_access(lines, location)
    if access is None:
        return None, None, None
    return access["index"], access["array"], access["parsed"]


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


def extract_function_name(signature):
    """Return the real function name from a (possibly macro-wrapped)
    signature, by matching the *last* balanced parenthesis group instead of
    a naive greedy regex.

    This matters for AUTOSAR/MISRA-style code such as::

        FUNC(void, MOCMEM_CODE) MoCMemStrtUp_Co_Hndlr(uint8 checkword)

    A regex like ``name\\s*\\([^;{}]*\\)$`` greedily backtracks and reports
    "FUNC" as the function name (since "FUNC(...)" is itself a valid
    ``identifier(...)`` match ending at the same position after
    backtracking through the outer parens) -- every macro-wrapped function
    in the file then collides on that one dict key. Scanning the *closing*
    paren backward to its match avoids this: the identifier immediately
    before the final ``(...)`` group is always the real declarator name,
    regardless of what return-type macros precede it. (On already
    preprocessed input, such as Astree's ALL_bc_with_context.c, these
    macros are typically expanded away already -- this still helps on
    non-preprocessed sources.)
    """
    text = signature.rstrip()
    if not text.endswith(")"):
        return None
    depth = 0
    i = len(text) - 1
    while i >= 0:
        if text[i] == ")":
            depth += 1
        elif text[i] == "(":
            depth -= 1
            if depth == 0:
                break
        i -= 1
    if i <= 0:
        return None
    before = text[:i].rstrip()
    match = re.search(r"([A-Za-z_]\w*)$", before)
    if not match:
        return None
    name = match.group(1)
    return None if name in SKIP_KEYWORDS else name


def find_signature_line(name, pending, current_line_no, current_line_prefix):
    """Among the lines that made up a signature, find the one that
    actually contains ``name(`` -- as opposed to an intervening
    preprocessor line marker (``# 323 "file.c"``) or a return-type-only
    continuation line -- so ``declaration_line`` points at the real
    ``ReturnType FuncName(...)`` text a person would look for, not just
    wherever the opening "{" happened to land (which may be a line or two
    below the actual signature)."""
    pattern = re.compile(r"\b" + re.escape(name) + r"\s*\(")
    for line_no, text in reversed(pending):
        if pattern.search(text):
            return line_no
    if pattern.search(current_line_prefix):
        return current_line_no
    return pending[-1][0] if pending else current_line_no


def function_ranges(lines):
    """Return (ranges, signatures).

    ranges: {func_name: (body_start_line, body_end_line)} (1-based, inclusive)
    signatures: {func_name: {"text": <signature text>, "line": <line number
        of the actual `name(...)` text>}}
    """
    ranges, signatures = {}, {}
    depth, pending = 0, []  # list of (line_no, stripped_text)
    current, start = None, None
    for index, line in enumerate(lines):
        line_no = index + 1
        for pos, char in enumerate(line):
            if char == "{":
                if depth == 0:
                    signature = (
                        " ".join(text for _, text in pending) + " " + line[:pos]
                    ).strip()
                    current = extract_function_name(signature)
                    start = line_no
                    if current:
                        sig_line = find_signature_line(current, pending, line_no, line[:pos])
                        signatures[current] = {"text": signature, "line": sig_line}
                    pending = []
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0 and current:
                    ranges[current] = (start, line_no)
                    current = None
        stripped = line.strip()
        if stripped and not stripped.startswith("//"):
            pending.append((line_no, stripped))
            if stripped.endswith(";"):
                pending = []
    return ranges, signatures


def enclosing_function(line_no, ranges):
    if line_no is None:
        return None
    return next((name for name, (start, end) in ranges.items()
                 if start <= line_no <= end), None)


def build_typedefs(lines):
    """Map typedef name -> underlying base type / array dims, so a
    variable's array-ness can be resolved even when it's hidden behind a
    typedef (e.g. ``typedef uint16 LinSM_ModeType[8];`` used as the type of
    a plain-looking local)."""
    typedefs = {}
    for number, line in enumerate(lines, 1):
        match = TYPEDEF_RE.match(line)
        if match:
            typedefs[match.group("name")] = {
                "base": match.group("base").strip(),
                "dims": match.group("dims").strip() if match.group("dims") else None,
                "declaration_line": number,
                "declaration_text": line.strip(),
            }
    return typedefs


def normalize_macro_type(prefix):
    """AUTOSAR/MISRA-style code routinely wraps the real type in a macro:
    ``FUNC(uintPtr)``, ``VAR(LinSM_ModeType, AUTOMATIC)``,
    ``P2VAR(uint8, AUTOMATIC, RTE_VAR)`` and so on -- the *real* type is
    conventionally the macro's first argument. If ``prefix`` is exactly one
    such wrapper call, return (real_type, macro_name); otherwise return
    (prefix, None) unchanged. (On already-preprocessed input these macros
    are usually expanded away, but this keeps the script correct against
    non-preprocessed sources too.)"""
    match = re.match(r"^([A-Za-z_]\w*)\s*\(([^()]*)\)\s*$", prefix)
    if not match:
        return prefix, None
    macro_name, args = match.groups()
    first_arg = args.split(",")[0].strip()
    return (first_arg, macro_name) if first_arg else (prefix, None)


def _is_declaration_prefix(prefix):
    """Accept a type prefix; reject assignments and member-access uses.

    ``static ApplicationType const name[]`` is a declaration (last token is
    the qualifier ``const``).  ``ptr->field[i] =`` and ``Y = arr[0]`` are
    not.  Type keywords such as ``int`` / ``const`` are valid prefix tails;
    only statement starters (``if``, ``return``, ...) are rejected.
    """
    text = prefix.strip()
    if not text:
        return False
    if "=" in text:
        return False
    if text.endswith("->") or text.endswith("."):
        return False
    if "->" in text or re.search(r"[A-Za-z0-9_]\s*\.\s*[A-Za-z_]", text):
        return False
    tokens = text.split()
    if tokens and tokens[-1] in STATEMENT_KEYWORDS:
        return False
    return True


def build_declaration_index(lines):
    """Single pass over the whole file collecting every declaration-shaped
    match, keyed by name. Used instead of re-scanning all N lines for every
    symbol we look up -- on a real ~600k-line preprocessed file that
    per-symbol rescan would be the dominant cost for a batch run.

    A declaration's prefix may legitimately contain balanced parens (a
    macro-wrapped type like ``FUNC(uintPtr)`` or ``VAR(Type, AUTOMATIC)``);
    only an *unbalanced* paren count (still inside an open parameter list,
    e.g. mid function-signature) disqualifies a match -- this is the same
    rule applied uniformly whether the symbol turns out to be an array, a
    scalar, or anything else; it isn't special-cased per kind.
    """
    pattern = re.compile(
        r"^\s*(?:(?P<prefix>.+?)\b)?(?P<name>[A-Za-z_]\w*)\s*"
        r"(?P<dims>\[[^;=]*\])?\s*"
        r"(?:__attribute__\s*\(\([^;]*\)\))?\s*(?:[;={])"
    )
    type_only_re = re.compile(r"^\s*([A-Za-z_]\w*)\s*$")
    index = {}
    pending_type = None
    pending_struct = None
    brace_depth = 0
    struct_stack = []
    for number, line in enumerate(lines, 1):
        head = STRUCT_HEAD_RE.search(line)
        if head and "{" not in line[head.start():head.start() + 12]:
            pending_struct = {"kind": head.group("kind"), "name": head.group("name")}
        elif pending_struct and ";" in line and "{" not in line:
            pending_struct = None
        for char in line:
            if char == "{":
                if pending_struct or (head and "{" in line):
                    kind = pending_struct["kind"] if pending_struct else (head.group("kind") if head else "struct")
                    name = pending_struct["name"] if pending_struct else (head.group("name") if head else None)
                    struct_stack.append({
                        "kind": kind,
                        "name": name,
                        "depth": brace_depth,
                        "fields": [],
                    })
                    pending_struct = None
                brace_depth += 1
            elif char == "}":
                brace_depth -= 1
                if struct_stack and struct_stack[-1]["depth"] == brace_depth:
                    closed = struct_stack.pop()
                    close = CLOSE_TYPE_RE.match(line)
                    if close:
                        closed["name"] = close.group("name")
                    if closed.get("name"):
                        for field in closed["fields"]:
                            field["enclosing_struct"] = closed["name"]
                            field["enclosing_kind"] = closed["kind"]
        match = pattern.match(line)
        if match:
            name = match.group("name")
            prefix = (match.group("prefix") or "").strip()
            dims = match.group("dims")
            if not prefix and pending_type:
                # Type on the previous line: ``T1_uint8_t\\n    name[6][6];``
                # Do not treat ``name[i] =`` as a declaration just because a
                # type-looking token happened to sit above it.
                dims_names = IDENTIFIER_RE.findall(dims or "")
                if "=" in line and dims_names:
                    prefix = ""
                else:
                    prefix = pending_type
            if (
                name not in SKIP_KEYWORDS
                and _is_declaration_prefix(prefix)
                and prefix.count("(") == prefix.count(")")
            ):
                datatype, macro_wrapper = normalize_macro_type(prefix)
                decl_text = line.strip()
                if pending_type and not (match.group("prefix") or "").strip():
                    decl_text = f"{pending_type} {decl_text}"
                owner = next(
                    (item for item in reversed(struct_stack) if item.get("name")),
                    struct_stack[-1] if struct_stack else None,
                )
                entry = {
                    "datatype": datatype,
                    "raw_prefix": prefix,
                    "macro_wrapper": macro_wrapper,
                    "array_dims": match.group("dims").strip() if match.group("dims") else None,
                    "declaration_line": number,
                    "declaration_text": decl_text,
                    "is_pointer": ("*" in prefix) or bool(POINTER_MACRO_RE.search(prefix)),
                    "pointer_depth": prefix.count("*"),
                    "is_struct_member": bool(struct_stack),
                    "enclosing_struct": (owner or {}).get("name"),
                    "enclosing_kind": (owner or {}).get("kind"),
                }
                index.setdefault(name, []).append(entry)
                if struct_stack:
                    struct_stack[-1]["fields"].append(entry)
        type_only = type_only_re.match(line)
        if type_only and type_only.group(1) not in STATEMENT_KEYWORDS:
            pending_type = type_only.group(1)
        elif line.strip():
            pending_type = None
    return index


def _ident_pieces(text):
    """Split ``rba_Reg_GTM_Tom_Channel_tst`` into {rba, reg, gtm, tom, channel, tst}."""
    pieces = set()
    for ident in IDENTIFIER_RE.findall(text or ""):
        for part in ident.split("_"):
            if part:
                pieces.add(part.lower())
    return pieces


def _decl_context_score(candidate, context_tokens):
    haystack = _ident_pieces(" ".join(
        part for part in (
            candidate.get("datatype"),
            candidate.get("raw_prefix"),
            candidate.get("declaration_text"),
        ) if part
    ))
    score = 1 if candidate.get("array_dims") else 0
    if candidate.get("is_struct_member"):
        score += 1
    owner = (candidate.get("enclosing_struct") or "").lower()
    for token in context_tokens or ():
        lowered = token.lower()
        if lowered and lowered in haystack:
            score += 5
        if owner and lowered in owner:
            score += 4
    return score


def declaration(name, decl_index, prefer_range=None, context_tokens=None,
                use_line=None):
    """Look up ``name`` in a pre-built declaration index (see
    ``build_declaration_index``). If multiple candidates exist (e.g. a
    local shadowing a global of the same name, or several hardware
    ``CH[]`` fields), prefer one inside ``prefer_range`` (the function
    enclosing the symbol's use), then the nearest declaration above the
    use, then the one whose type tokens best match the access path
    (``TOM.CH`` -> ``..._Tom_Channel_tst``, not ``..._Atom_...``).
    """
    candidates = decl_index.get(name)
    if not candidates:
        return None
    pool = candidates
    if prefer_range:
        start, end = prefer_range
        local = [item for item in candidates if start <= item["declaration_line"] <= end]
        if local:
            pool = local
    if use_line is not None:
        preceding = [item for item in pool if item["declaration_line"] <= use_line]
        if preceding:
            pool = preceding
    if context_tokens and len(pool) > 1:
        return max(pool, key=lambda item: (
            _decl_context_score(item, context_tokens),
            item["declaration_line"],
        ))
    return pool[-1]


def build_constant_index(lines):
    """Map ``#define NAME 4`` / ``enum { NAME = 4 }`` identifiers to ints
    so symbolic array dims can be resolved on any PVER, not just ones
    that already expanded sizes to ``(15L)``."""
    constants = {}
    for line in lines:
        define = DEFINE_RE.match(line)
        if define:
            constants.setdefault(define.group("name"), int(define.group("value")))
            continue
        stripped = line.lstrip()
        if "=" not in stripped or stripped.startswith("#"):
            continue
        for match in ENUM_ASSIGN_RE.finditer(line):
            constants.setdefault(match.group(1), int(match.group(2)))
    return constants


def _constant_value(name, constants):
    if name in constants:
        return constants[name]
    stripped = MANGLED_SUFFIX_RE.sub("", name)
    if stripped != name and stripped in constants:
        return constants[stripped]
    return None


def resolve_dim_sizes(dims, constants):
    """Return (sizes, all_known) for ``[(2L)][(15L)]`` or ``[NAME + 1U]``."""
    if not dims:
        return [], False
    constants = constants or {}
    sizes = []
    known = True
    for inner in DIM_PIECE_RE.findall(dims):
        # Strip C integer suffixes *before* identifier lookup so ``12U``
        # cannot be read as ``12`` + a constant named ``U``.
        stripped = re.sub(r"(\d)[UuLl]+\b", r"\1", inner)

        def replace(match, _constants=constants):
            value = _constant_value(match.group(0), _constants)
            return str(value) if value is not None else match.group(0)

        replaced = IDENTIFIER_RE.sub(replace, stripped)
        cleaned = re.sub(r"[UuLl]+", "", replaced)
        if SAFE_DIM_EXPR_RE.match(cleaned):
            try:
                sizes.append(int(eval(cleaned, {"__builtins__": {}}, {})))
            except Exception:
                sizes.append(None)
                known = False
        else:
            sizes.append(None)
            known = False
    return sizes, known and all(size is not None for size in sizes)


def function_declaration(name, signatures):
    """Fallback decl info for a symbol that is itself a function name."""
    sig = signatures.get(name)
    if not sig:
        return None
    return {
        "datatype": "function",
        "array_dims": None,
        "declaration_line": sig["line"],
        "declaration_text": sig["text"],
        "is_pointer": False,
        "is_struct_member": False,
        "enclosing_struct": None,
    }


def build_parameter_index(signatures):
    """Map parameter name -> list of owning function signatures."""
    index = {}
    for func_name, sig in signatures.items():
        text = sig["text"]
        paren = re.search(r"\(([^()]*)\)\s*$", text)
        if not paren:
            continue
        for param in paren.group(1).split(","):
            param = param.strip()
            if not param or param == "void":
                continue
            match = re.search(r"([A-Za-z_]\w*)\s*(\[[^\]]*\])?$", param)
            if not match:
                continue
            name = match.group(1)
            if name in SKIP_KEYWORDS:
                continue
            prefix = param[:match.start(1)].strip()
            index.setdefault(name, []).append({
                "datatype": prefix or None,
                "array_dims": match.group(2),
                "declaration_line": sig["line"],
                "declaration_text": f"parameter of {func_name}{text[text.find('('):]}",
                "declared_in_function": func_name,
                "is_pointer": "*" in prefix or bool(POINTER_MACRO_RE.search(prefix)),
                "pointer_depth": prefix.count("*"),
                "is_struct_member": False,
                "enclosing_struct": None,
            })
    return index


def parameter_declaration(name, param_index, prefer_func=None):
    candidates = param_index.get(name) if param_index else None
    if not candidates:
        return None
    if prefer_func:
        for item in candidates:
            if item.get("declared_in_function") == prefer_func:
                return item
    return candidates[0]


def resolve_typedef_dims(datatype, typedefs):
    """If ``datatype`` resolves (possibly through qualifiers) to a typedef
    name that itself carries array dims, return (dims, typedef_name)."""
    if not datatype:
        return None, None
    tokens = IDENTIFIER_RE.findall(datatype)
    for token in reversed(tokens):  # the base type name is usually last
        entry = typedefs.get(token)
        if entry and entry["dims"]:
            return entry["dims"], token
    return None, None


def _callee_at(line, pos):
    """Function whose argument list contains ``pos``, or None."""
    depth = 0
    index = pos - 1
    while index >= 0:
        char = line[index]
        if char == ")":
            depth += 1
        elif char == "(":
            if depth == 0:
                cursor = index - 1
                while cursor >= 0 and line[cursor].isspace():
                    cursor -= 1
                if cursor >= 0 and (line[cursor].isalnum() or line[cursor] == "_"):
                    end = cursor + 1
                    while cursor >= 0 and (line[cursor].isalnum() or line[cursor] == "_"):
                        cursor -= 1
                    name = line[cursor + 1:end]
                    return None if name in STATEMENT_KEYWORDS else name
                return None
            depth -= 1
        index -= 1
    return None


def _is_write_target(line, start, end):
    """True if this identifier is the object being assigned, not an index
    or a ``ptr->`` / ``obj.`` parent of the assigned member."""
    assignment = ASSIGN_RE.search(line)
    if assignment is None or start >= assignment.start():
        return False
    depth = 0
    for index, char in enumerate(line[:assignment.start()]):
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
        if index == start and depth > 0:
            return False
    after = line[end:assignment.start()]
    if re.match(r"\s*(->|\.)", after):
        return False
    return True


def _looks_like_decl_line(line, name):
    return bool(re.search(
        r"(?:^|[\s*])" + re.escape(name) + r"\s*(?:\[[^;=]*\])?\s*(?:__attribute__\b.*)?[;={]",
        line,
    )) and _is_declaration_prefix(line.split(name, 1)[0])


def collect_occurrences(lines, ranges, filename, wanted):
    """One pass over the source, recording only the requested symbols."""
    if not wanted:
        return {}
    line_functions = [None] * len(lines)
    for name, (start, end) in ranges.items():
        for line_no in range(start, min(end, len(lines)) + 1):
            line_functions[line_no - 1] = name
    occurrences = {name: [] for name in wanted}
    for number, line in enumerate(lines, 1):
        owner = line_functions[number - 1] or "global"
        for match in IDENTIFIER_RE.finditer(line):
            name = match.group(0)
            if name not in wanted:
                continue
            start, end = match.start(), match.end()
            callee = _callee_at(line, start)
            if _looks_like_decl_line(line, name):
                access = "declaration"
            elif re.search(r"&\s*$", line[:start]):
                access = "address"
            elif _is_write_target(line, start, end):
                access = "write"
            elif callee:
                access = "parameter"
            else:
                access = "read"
            occurrences[name].append({
                "location": f"{filename}:{number}.{start + 1}-{end}",
                "access": access,
                "line_text": line.strip(),
                "function": owner,
                "callee": callee,
                "line": number,
            })
    return occurrences


def relevant_symbols(access, typedefs):
    """Symbols that belong to this alarm -- not every identifier on the line."""
    if not access or not access.get("array"):
        return []
    ordered = []
    seen = set()

    def add(name, role, index=None, dimension=None):
        if not name or name in SKIP_KEYWORDS or name in PRIMITIVE_TYPES:
            return
        if name in typedefs and role == "index":
            return
        key = (name, role)
        if key in seen:
            return
        seen.add(key)
        ordered.append({
            "name": name,
            "role": role,
            "index": index,
            "dimension": dimension,
        })

    add(access["array"], "flagged_array", access.get("index"), access.get("dimension"))
    path = access.get("member_path") or []
    for parent in path[:-1]:
        add(parent, "parent")
    for ident in IDENTIFIER_RE.findall(access.get("index") or ""):
        add(ident, "index")
    return ordered


def _classify_symbol(kind_hint, decl, decl_kind, array_dims):
    if kind_hint == "function":
        return "function"
    is_array = bool(array_dims) or kind_hint == "array"
    is_pointer = bool(decl and decl.get("is_pointer"))
    is_member = bool(decl and decl.get("is_struct_member"))
    datatype = (decl or {}).get("datatype") or ""
    is_struct = bool(re.search(r"\b(struct|union)\b", datatype)) or bool(
        (decl or {}).get("enclosing_kind")
    )
    if kind_hint == "flagged_array" or is_array:
        return "array"
    if is_member:
        return "struct_member"
    if is_pointer:
        return "pointer"
    if decl_kind == "parameter":
        return "parameter"
    if is_struct:
        return "struct"
    return "symbol"


def variable_info(name, index, kind, decl_index, ranges, filename, cache,
                   occurrence_index, signatures, typedefs, alarm_line=None,
                   constants=None, context_tokens=None, dimension_index=None,
                   param_index=None, role=None, member_path=None):
    """Resolve full info for one relevant symbol at an alarm's location."""
    prefer_range = None
    enclosing_use = enclosing_function(alarm_line, ranges) if alarm_line else None
    if enclosing_use:
        prefer_range = ranges.get(enclosing_use)
    cache_key = (name, prefer_range, tuple(context_tokens or ()), role)
    if cache_key in cache:
        info = dict(cache[cache_key])
        info["index_expression"] = index
        info["kind"] = kind
        info["role"] = role or info.get("role")
        if dimension_index is not None:
            info["dimension_index"] = dimension_index
        if member_path:
            info["member_path"] = member_path
        return info

    decl = declaration(name, decl_index, prefer_range, context_tokens, alarm_line)
    decl_kind = "variable"
    if decl and decl.get("is_struct_member"):
        decl_kind = "struct_member"
    if decl is None and kind == "function":
        decl = function_declaration(name, signatures)
        decl_kind = "function_definition" if decl else decl_kind
    if decl is None:
        param_decl = parameter_declaration(name, param_index, enclosing_use)
        if param_decl:
            decl = param_decl
            decl_kind = "parameter"

    array_dims = decl["array_dims"] if decl else None
    array_dims_source = "declaration" if (decl and array_dims) else None
    typedef_name = None
    if decl and not array_dims:
        array_dims, typedef_name = resolve_typedef_dims(decl.get("datatype"), typedefs)
        if array_dims:
            array_dims_source = f"typedef:{typedef_name}"

    resolved_sizes, sizes_known = resolve_dim_sizes(array_dims, constants)
    if dimension_index is not None and dimension_index < len(resolved_sizes):
        alarmed_size_known = resolved_sizes[dimension_index] is not None
    else:
        alarmed_size_known = sizes_known

    enclosing = (
        decl.get("declared_in_function") if decl and decl.get("declared_in_function")
        else (enclosing_function(decl["declaration_line"], ranges) if decl and decl.get("declaration_line") else None)
    )
    occurrences_full = [dict(item) for item in occurrence_index.get(name, [])]
    if decl and decl.get("declaration_line"):
        decl_prefix = f"{filename}:{decl['declaration_line']}."
        for item in occurrences_full:
            if item["location"].startswith(decl_prefix):
                item["access"] = "declaration"
    # Locals / parameters of the same name in other functions are different
    # objects -- keep only uses inside the declaring function.
    if enclosing and not (decl and decl.get("is_struct_member")):
        occurrences_full = [
            item for item in occurrences_full
            if item.get("function") == enclosing
        ]
    elif decl and decl.get("is_struct_member") and member_path:
        parents = {part for part in member_path[:-1] if part not in SKIP_KEYWORDS}
        if parents:
            narrowed = [
                item for item in occurrences_full
                if item["access"] == "declaration"
                or any(part in item.get("line_text", "") for part in parents)
            ]
            if narrowed:
                occurrences_full = narrowed
    access_counts = {}
    for item in occurrences_full:
        access_counts[item["access"]] = access_counts.get(item["access"], 0) + 1
    passed_to = []
    seen_pass = set()
    for item in occurrences_full:
        callee = item.get("callee")
        if not callee or item["access"] not in {"parameter", "address"}:
            continue
        key = (callee, item["location"], item["access"])
        if key in seen_pass:
            continue
        seen_pass.add(key)
        passed_to.append({
            "function": callee,
            "location": item["location"],
            "mode": "address" if item["access"] == "address" else "value",
            "line_text": item["line_text"],
            "in_function": item["function"],
        })
    used_in = []
    for item in occurrences_full:
        func = item.get("function")
        if func and func not in used_in:
            used_in.append(func)
    truncated = len(occurrences_full) > MAX_OCCURRENCES_SHOWN
    occurrences_sample = (
        occurrences_full[:MAX_OCCURRENCES_SHOWN // 2] + occurrences_full[-MAX_OCCURRENCES_SHOWN // 2:]
        if truncated else list(occurrences_full)
    )

    if decl_kind == "parameter":
        scope = "parameter"
    elif decl and decl.get("is_struct_member"):
        scope = "struct_member"
    elif enclosing:
        scope = "local"
    else:
        scope = "global"

    resolved_kind = _classify_symbol(kind, decl, decl_kind, array_dims)
    is_array = resolved_kind == "array" or bool(array_dims)
    is_pointer = bool(decl and decl.get("is_pointer"))
    size_known = bool(alarmed_size_known or sizes_known) if is_array else None
    parents = [part for part in (member_path or [])[:-1] if part not in SKIP_KEYWORDS]

    info = {
        "array_name": name,
        "symbol_name": name,
        "kind": resolved_kind,
        "role": role or ("flagged_array" if kind == "array" else "related"),
        "index_expression": index,
        "dimension_index": dimension_index,
        "datatype": decl["datatype"] if decl else None,
        "macro_wrapper": decl.get("macro_wrapper") if decl else None,
        "is_array": is_array,
        "is_pointer": is_pointer,
        "is_struct": resolved_kind == "struct",
        "is_struct_member": bool(decl and decl.get("is_struct_member")),
        "member_of": (decl or {}).get("enclosing_struct"),
        "parent_symbol": parents[-1] if parents else None,
        "member_path": member_path or None,
        "pointed_type": (decl.get("datatype") if is_pointer and decl else None),
        "array_dims": array_dims,
        "array_dims_source": array_dims_source,
        "resolved_sizes": resolved_sizes or None,
        "size_known": size_known,
        "scope": scope,
        "declared_in_function": enclosing,
        "declaration_line": decl["declaration_line"] if decl else None,
        "declaration_text": decl["declaration_text"] if decl else None,
        "declaration_kind": decl_kind if decl else "unknown",
        "access_counts": access_counts,
        "passed_to": passed_to,
        "used_in_functions": used_in,
        "occurrence_count": len(occurrences_full),
        "occurrences_truncated": truncated,
        "occurrences": occurrences_sample,
    }
    cache[cache_key] = info
    return dict(info)


# Constructs, casts and attributes that look like ``name(`` but are not calls.
CFG_CALL_SKIP = SKIP_KEYWORDS | {
    "sizeof", "typeof", "alignof", "_Alignof", "_Generic",
    "__attribute__", "__asm", "__asm__", "__typeof__", "__typeof",
    "__builtin_offsetof", "offsetof", "va_arg", "va_start", "va_end",
    "defined", "catch", "static_cast", "dynamic_cast", "reinterpret_cast",
    "const_cast",
}
CFG_SUFFIX_ATTRS = {
    "__attribute__", "__asm", "__asm__", "asm",
    "noexcept", "throw", "override", "final",
}
_CFG_NON_CODE_RE = re.compile(
    r"/\*.*?\*/"
    r"|//(?:\\\r?\n|[^\n])*"
    r"|\"(?:\\.|[^\"\\\n])*\""
    r"|'(?:\\.|[^'\\\n])*'"
    r"|^[ \t]*#(?:\\\r?\n|[^\n])*",
    re.DOTALL | re.MULTILINE,
)
_CFG_CALL_RE = re.compile(r"\b([A-Za-z_]\w*)\s*\(")
_CFG_INDIRECT_CALL_RE = re.compile(r"\(\s*\*\s*([A-Za-z_]\w*)\s*\)\s*\(")


def cfg_output_path(variable_output_path):
    folder = os.path.dirname(os.path.abspath(variable_output_path)) or "."
    return os.path.join(folder, CFG_OUTPUT_NAME)


def _blank_non_code(text):
    """Replace comments, strings and ``#`` line markers with spaces."""

    def blank(match):
        return "".join("\n" if char == "\n" else " " for char in match.group(0))

    return _CFG_NON_CODE_RE.sub(blank, text)


def _match_pair(code, start, opener, closer):
    depth = 0
    for index in range(start, len(code)):
        char = code[index]
        if char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return index
    return None


def _skip_ident(code, index):
    end = index
    while end < len(code) and (code[end].isalnum() or code[end] == "_"):
        end += 1
    return code[index:end], end


def _definition_brace_after(code, index):
    """Return the ``{`` of a definition after ``name(...)``, else None.

    Only GCC/C++ trailing attributes may sit between the parameter list and
    the body.  A following identifier that starts another declarator
    (``FUNC(...) Name(...) {``) is not treated as this name's body -- that
    keeps return-type macros from stealing the real function name.
    """
    i = index
    limit = min(len(code), index + 4000)
    while i < limit:
        char = code[i]
        if char.isspace():
            i += 1
            continue
        if char == "{":
            return i
        if char == "_" or char.isalpha():
            name, i = _skip_ident(code, i)
            if name not in CFG_SUFFIX_ATTRS and name not in {"const", "volatile"}:
                return None
            while i < limit and code[i].isspace():
                i += 1
            if i < limit and code[i] == "(":
                closing = _match_pair(code, i, "(", ")")
                if closing is None:
                    return None
                i = closing + 1
            continue
        return None
    return None


def _collect_calls(body):
    """Unique callees in source order, including ``(*fp)(...)``."""
    callees = []
    seen = set()

    def add(name):
        if name in CFG_CALL_SKIP or name in seen:
            return
        seen.add(name)
        callees.append(name)

    for match in _CFG_CALL_RE.finditer(body):
        add(match.group(1))
    for match in _CFG_INDIRECT_CALL_RE.finditer(body):
        add(match.group(1))
    return callees


def build_control_flow_graph(source_text):
    """Full caller -> [callee, ...] map for every function in the source.

    Every defined function is a key (empty list if it calls nobody).  Every
    callee is also a key, so external / prototype-only functions appear
    instead of vanishing as missing nodes.  Connections are direct calls
    inside the function body; keywords, sizeof and attributes are skipped.
    """
    code = _blank_non_code(source_text)
    graph = OrderedDict()
    defined = []
    declared = []
    position = 0
    while True:
        match = _CFG_CALL_RE.search(code, position)
        if match is None:
            break
        name = match.group(1)
        open_paren = match.end() - 1
        close_paren = _match_pair(code, open_paren, "(", ")")
        if close_paren is None:
            position = match.end()
            continue
        if name in CFG_CALL_SKIP:
            position = close_paren + 1
            continue
        open_brace = _definition_brace_after(code, close_paren + 1)
        if open_brace is not None:
            close_brace = _match_pair(code, open_brace, "{", "}")
            if close_brace is None:
                position = open_brace + 1
                continue
            callees = _collect_calls(code[open_brace + 1:close_brace])
            if name in graph:
                merged = list(graph[name])
                seen = set(merged)
                for callee in callees:
                    if callee not in seen:
                        seen.add(callee)
                        merged.append(callee)
                graph[name] = merged
            else:
                graph[name] = callees
            defined.append(name)
            position = close_brace + 1
            continue
        after = close_paren + 1
        while after < len(code) and code[after].isspace():
            after += 1
        if after < len(code) and code[after] == ";":
            declared.append(name)
            position = after + 1
            continue
        position = close_paren + 1

    for name in declared:
        graph.setdefault(name, [])
    for callees in list(graph.values()):
        for callee in callees:
            graph.setdefault(callee, [])
    return graph


def write_control_flow_graph(source_text, cfg_path):
    graph = build_control_flow_graph(source_text)
    with open(cfg_path, "w", encoding="utf-8") as stream:
        json.dump(graph, stream, indent=2, ensure_ascii=False)
    return graph


def main():
    args = sys.argv[1:]
    if args and args[0] == "--cfg-only":
        source_path = args[1] if len(args) > 1 else DEFAULT_SOURCE
        cfg_path = args[2] if len(args) > 2 else DEFAULT_CFG_OUTPUT
        graph = write_control_flow_graph(read_text(source_path), cfg_path)
        edges = sum(len(callees) for callees in graph.values())
        print(f"Control-flow functions: {len(graph)}")
        print(f"Control-flow edges: {edges}")
        print(f"CFG written to: {cfg_path}")
        return
    if args and args[0] == "--merge-paths":
        output_path = args[1] if len(args) > 1 else DEFAULT_OUTPUT
        log_file = args[2] if len(args) > 2 else sibling_message_log(output_path)
        if not log_file:
            raise SystemExit("No messeges.txt / messages.txt found next to the JSON")
        updated, total = merge_paths_into_json(output_path, log_file)
        print(f"Merged message-log paths into {updated} of {total} alarms")
        print(f"Output written to: {output_path}")
        return
    log_path = args[0] if args else DEFAULT_LOG
    source_path = args[1] if len(args) > 1 else DEFAULT_SOURCE
    output_path = args[2] if len(args) > 2 else DEFAULT_OUTPUT
    cfg_path = args[3] if len(args) > 3 else cfg_output_path(output_path)
    log_text = read_text(log_path)
    source_text = read_text(source_path)
    source_lines = source_text.splitlines()
    log_by_location, log_by_numeric = OrderedDict(), OrderedDict()
    if looks_like_alarms_csv(log_path, log_text):
        raw_blocks = list(blocks_from_csv(log_text))
        sibling = sibling_message_log(log_path)
        if sibling:
            log_by_location, log_by_numeric = load_message_log_paths(sibling)
    else:
        raw_blocks = list(read_blocks(log_text.splitlines(True)))
    blocks = [parse_block(block) for block in raw_blocks]
    matched = [block for block in blocks if is_aoob_block(block)]
    groups = OrderedDict()
    for block in matched:
        groups.setdefault(block["location"], []).append(block)
    if log_by_location or log_by_numeric:
        for location in list(groups):
            traces = traces_for_location(location, log_by_location, log_by_numeric)
            if traces:
                groups[location] = traces
    ranges, signatures = function_ranges(source_lines)
    typedefs = build_typedefs(source_lines)
    constants = build_constant_index(source_lines)
    filename = source_path.replace("\\", "/").rsplit("/", 1)[-1]
    decl_index = build_declaration_index(source_lines)
    param_index = build_parameter_index(signatures)
    planned = []
    wanted = set()
    for location, paths in groups.items():
        access = locate_array_access(source_lines, location)
        if access is None or not access.get("array"):
            variable = next((variable_from_underline(path["source_lines_raw"],
                                                      path["underline_lines_raw"])
                             for path in paths
                             if variable_from_underline(path["source_lines_raw"],
                                                        path["underline_lines_raw"]) is not None), None)
            if variable and re.match(r"^[A-Za-z_]\w*$", variable):
                access = {
                    "array": variable, "index": None, "parsed": None,
                    "dimension": None, "context_tokens": None, "member_path": [variable],
                }
        symbols = relevant_symbols(access, typedefs)
        for item in symbols:
            wanted.add(item["name"])
        planned.append((location, paths, access, symbols))
    occurrence_index = collect_occurrences(source_lines, ranges, filename, wanted)
    cache, output = {}, []
    for group_id, (location, paths, access, symbols) in enumerate(planned, 1):
        index = access["index"] if access else None
        primary = access["array"] if access else None
        parsed = access["parsed"] if access else None
        dimension_index = access["dimension"] if access else None
        context_tokens = access["context_tokens"] if access else None
        alarm_line = parsed[0] if parsed else None
        enclosing = enclosing_function(alarm_line, ranges) if alarm_line else None
        path_output, trace_count, unique_count = path_records(paths, enclosing)
        variable = next((variable_from_underline(path["source_lines_raw"],
                                                  path["underline_lines_raw"])
                         for path in paths
                         if variable_from_underline(path["source_lines_raw"],
                                                    path["underline_lines_raw"]) is not None), None)
        if not variable and primary:
            variable = f"{primary}[{index}]" if index else primary
        member_path = (access or {}).get("member_path")
        symbol_infos = []
        for item in symbols:
            kind = "array" if item["role"] == "flagged_array" else "symbol"
            symbol_infos.append(variable_info(
                item["name"], item["index"], kind, decl_index, ranges, filename,
                cache, occurrence_index, signatures, typedefs, alarm_line,
                constants,
                context_tokens if item["role"] == "flagged_array" else member_path,
                item["dimension"],
                param_index, item["role"],
                member_path if item["name"] == primary else None,
            ))
        infos = [info for info in symbol_infos if info.get("role") == "flagged_array"]
        if not infos:
            infos = [info for info in symbol_infos if info["kind"] == "array"]
        if infos:
            infos = [dict(info) for info in infos]
        if symbol_infos:
            symbol_infos = [dict(info) for info in symbol_infos]
        entry = {
            "group_id": group_id,
            "location": location,
            "no_of_paths": unique_count,
            "no_of_traces": trace_count,
            "variable": variable,
            "enclosing_function": enclosing,
            "paths": path_output,
            "variable_info": infos[0] if infos else None,
            "variable_infos": infos,
            "symbol_infos": symbol_infos,
        }
        output.append(entry)
    with open(output_path, "w", encoding="utf-8") as stream:
        json.dump(output, stream, indent=2, ensure_ascii=False)
    graph = write_control_flow_graph(source_text, cfg_path)
    edges = sum(len(callees) for callees in graph.values())
    print(f"Parsed blocks: {len(blocks)}")
    print(f"Matched array_out_of_bounds blocks: {len(matched)}")
    print(f"Groups: {len(output)}")
    print(f"Groups with variable info: {sum(bool(x['variable_infos']) for x in output)}")
    print(f"Groups with full symbol info: {sum(bool(x['symbol_infos']) for x in output)}")
    unresolved_size = sum(
        1 for g in output for s in g["symbol_infos"]
        if s["kind"] == "array" and not s["size_known"]
    )
    print(f"Array symbols with unresolved size: {unresolved_size}")
    print(f"Relevant symbols (not line-noise): {sum(len(x['symbol_infos']) for x in output)}")
    print(f"Control-flow functions: {len(graph)}")
    print(f"Control-flow edges: {edges}")
    print(f"Output written to: {output_path}")
    print(f"CFG written to: {cfg_path}")


if __name__ == "__main__":
    main()