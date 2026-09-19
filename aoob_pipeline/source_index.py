"""Lightweight line/function index over preprocessed ``input.c``."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_NON_CODE_RE = re.compile(
    r"/\*.*?\*/"
    r"|//(?:\\\r?\n|[^\n])*"
    r"|\"(?:\\.|[^\"\\\n])*\""
    r"|'(?:\\.|[^'\\\n])*'"
    r"|^[ \t]*#(?:\\\r?\n|[^\n])*",
    re.DOTALL | re.MULTILINE,
)
_CANDIDATE_RE = re.compile(r"\b([A-Za-z_]\w*)\s*\(")
_PAREN_RE = re.compile(r"[()]")
_BRACE_RE = re.compile(r"[{}]")
_C_KEYWORDS = frozenset(
    {
        "if", "else", "for", "while", "do", "switch", "case", "default",
        "return", "goto", "break", "continue", "sizeof", "typedef", "struct",
        "union", "enum", "static", "const", "volatile", "inline", "extern",
        "register", "auto", "signed", "unsigned", "void", "char", "short",
        "int", "long", "float", "double", "_Bool", "defined",
    }
)


def _blank_non_code(text: str) -> str:
    def blank(match: re.Match[str]) -> str:
        return "".join("\n" if char == "\n" else " " for char in match.group(0))

    return _NON_CODE_RE.sub(blank, text)


def _match_bracket(code: str, start: int, pattern: re.Pattern[str], opener: str) -> int | None:
    depth = 0
    for match in pattern.finditer(code, start):
        if match.group() == opener:
            depth += 1
        else:
            depth -= 1
            if depth == 0:
                return match.start()
    return None


def _definition_brace(code: str, index: int) -> int | None:
    limit = min(len(code), index + 2000)
    i = index
    while i < limit:
        ch = code[i]
        if ch in " \t\r\n":
            i += 1
            continue
        if ch == "(":
            end = _match_bracket(code, i, _PAREN_RE, "(")
            if end is None:
                return None
            i = end + 1
            continue
        if ch == "{":
            return i
        if ch == ";":
            return None
        # attribute / qualifier tokens
        if ch.isalpha() or ch == "_":
            while i < limit and (code[i].isalnum() or code[i] in "_"):
                i += 1
            continue
        return None
    return None


def collapse_ws(text: str) -> str:
    return " ".join((text or "").split())


@dataclass(frozen=True)
class FuncSpan:
    name: str
    start_line: int
    end_line: int


class SourceIndex:
    """Map function names and line ranges onto ``input.c``."""

    def __init__(self, source_path: Path):
        self.path = source_path
        text = source_path.read_text(encoding="utf-8", errors="replace")
        self.lines = text.splitlines()
        self._spans: dict[str, list[FuncSpan]] = {}
        self._line_to_func: list[str | None] = [None] * (len(self.lines) + 1)
        self._index_functions(text)

    def _index_functions(self, text: str) -> None:
        code = _blank_non_code(text)
        line_starts = [0]
        for i, ch in enumerate(text):
            if ch == "\n":
                line_starts.append(i + 1)

        def offset_to_line(offset: int) -> int:
            lo, hi = 0, len(line_starts) - 1
            while lo <= hi:
                mid = (lo + hi) // 2
                if line_starts[mid] <= offset:
                    lo = mid + 1
                else:
                    hi = mid - 1
            return hi + 1

        for match in _CANDIDATE_RE.finditer(code):
            name = match.group(1)
            if name in _C_KEYWORDS:
                continue
            paren = match.end() - 1
            close = _match_bracket(code, paren, _PAREN_RE, "(")
            if close is None:
                continue
            brace = _definition_brace(code, close + 1)
            if brace is None:
                continue
            end = _match_bracket(code, brace, _BRACE_RE, "{")
            if end is None:
                continue
            start_line = offset_to_line(match.start())
            end_line = offset_to_line(end)
            span = FuncSpan(name=name, start_line=start_line, end_line=end_line)
            self._spans.setdefault(name, []).append(span)
            for line in range(start_line, min(end_line, len(self.lines)) + 1):
                if 1 <= line < len(self._line_to_func):
                    self._line_to_func[line] = name

    def function_span(self, name: str, *, near_line: int | None = None) -> FuncSpan | None:
        spans = self._spans.get(name) or []
        if not spans:
            return None
        if near_line is None:
            return spans[0]
        best = min(spans, key=lambda s: abs(((s.start_line + s.end_line) // 2) - near_line))
        return best

    def enclosing_function(self, line: int) -> str | None:
        if 1 <= line < len(self._line_to_func):
            return self._line_to_func[line]
        return None

    def get_lines(self, start: int, end: int) -> list[tuple[int, str]]:
        start = max(1, start)
        end = min(len(self.lines), end)
        if end < start:
            return []
        return [(i, self.lines[i - 1]) for i in range(start, end + 1)]

    def get_func(self, name: str, *, window: int | None = None, near_line: int | None = None) -> dict:
        span = self.function_span(name, near_line=near_line)
        if span is None:
            return {"error": f"function not found: {name}"}
        start, end = span.start_line, span.end_line
        if window is not None and window > 0 and near_line is not None:
            start = max(span.start_line, near_line - window)
            end = min(span.end_line, near_line + window)
        rows = self.get_lines(start, end)
        snippet = "\n".join(f"{ln}: {txt}" for ln, txt in rows)
        return {
            "function": name,
            "start_line": start,
            "end_line": end,
            "full_span": {"start_line": span.start_line, "end_line": span.end_line},
            "snippet": snippet,
            "line_count": len(rows),
        }

    def line_text(self, line: int) -> str | None:
        if 1 <= line <= len(self.lines):
            return self.lines[line - 1]
        return None

    def find_quote_line(self, line: int, quote: str, *, radius: int = 2) -> int | None:
        """Return the exact line (nearest to ``line``) whose text contains ``quote``.

        Single-line match only — used to correct off-by-one line numbers from
        LLM extracts without accepting multi-line fuzz.
        """
        needle = collapse_ws(quote)
        if not needle or not (1 <= line <= len(self.lines) + radius):
            return None
        for offset in sorted(range(-radius, radius + 1), key=abs):
            ln = line + offset
            if 1 <= ln <= len(self.lines) and needle in collapse_ws(self.lines[ln - 1]):
                return ln
        return None

    def verify_quote(self, line: int, quote: str, *, radius: int = 2) -> bool:
        """True if collapsed quote appears on line±radius."""
        needle = collapse_ws(quote)
        if not needle:
            return False
        for ln in range(max(1, line - radius), min(len(self.lines), line + radius) + 1):
            if needle in collapse_ws(self.lines[ln - 1]):
                return True
            # also accept quote as substring of a short window
            window = collapse_ws("\n".join(self.lines[max(0, ln - 2) : ln + 1]))
            if needle in window:
                return True
        return False
