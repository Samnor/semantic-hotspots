"""DAX lexical and call structure analysis, not a DAX compiler or execution planner.

Offsets refer to the unchanged input. Strings/comments never create references.
Unknown names, virtual-column scope ambiguity, and dynamic calculation behavior
are surfaced rather than silently treated as resolved engine dependencies.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field

from .model import Entity, Model, ModelError

ITERATORS = frozenset({"SUMX", "AVERAGEX", "MINX", "MAXX", "COUNTX", "COUNTAX", "PRODUCTX",
                       "CONCATENATEX", "RANKX", "FILTER", "ADDCOLUMNS", "SELECTCOLUMNS",
                       "GENERATE", "GENERATEALL", "STDEVX.P", "STDEVX.S", "VARX.P", "VARX.S"})
_KEYWORDS = frozenset({'RETURN', 'VAR', 'NOT', 'AND', 'OR', 'IN', 'EVALUATE', 'DEFINE',
                       'ORDER', 'BY', 'ASC', 'DESC', 'START', 'AT'})


@dataclass(frozen=True)
class Token:
    kind: str
    value: str
    start: int
    end: int


def tokenize(text: str) -> list[Token]:
    out: list[Token] = []
    i = 0
    while i < len(text):
        c = text[i]
        if c.isspace():
            i += 1
            continue
        if text[i:i + 2] in {"//", "--"}:
            end = text.find("\n", i)
            i = len(text) if end < 0 else end + 1
            continue
        if text.startswith("/*", i):
            start, depth = i, 1
            i += 2
            while i < len(text) and depth:
                if text.startswith("/*", i):
                    depth += 1
                    i += 2
                elif text.startswith("*/", i):
                    depth -= 1
                    i += 2
                else:
                    i += 1
            if depth:
                raise ModelError(f"Unterminated DAX comment at offset {start}")
            continue
        if c in {'"', "'", "["}:
            start, closing = i, "]" if c == "[" else c
            kind = {"[": "BRACKET", "'": "QUOTED", '"': "STRING"}[c]
            value = []
            i += 1
            while i < len(text):
                if text[i] == closing:
                    if i + 1 < len(text) and text[i + 1] == closing:
                        value.append(closing)
                        i += 2
                        continue
                    i += 1
                    break
                value.append(text[i])
                i += 1
            else:
                raise ModelError(f"Unterminated DAX {kind.lower()} at offset {start}")
            out.append(Token(kind, "".join(value), start, i))
            continue
        if c.isalpha() or c == "_":
            start = i
            i += 1
            while i < len(text) and (text[i].isalnum() or text[i] == "_"):
                i += 1
            out.append(Token("NAME", text[start:i], start, i))
            continue
        if c.isdigit() or (c == "." and i + 1 < len(text) and text[i + 1].isdigit()):
            start = i
            while i < len(text) and text[i].isdigit():
                i += 1
            if i < len(text) and text[i] == ".":
                i += 1
                while i < len(text) and text[i].isdigit():
                    i += 1
            if i < len(text) and text[i] in "eE":
                exponent = i + 1
                if exponent < len(text) and text[exponent] in "+-":
                    exponent += 1
                digits = exponent
                while exponent < len(text) and text[exponent].isdigit():
                    exponent += 1
                if exponent > digits:
                    i = exponent
            out.append(Token("NUMBER", text[start:i], start, i))
            continue
        out.append(Token("SYMBOL", c, i, i + 1))
        i += 1
    return out


@dataclass
class Call:
    name: str
    start: int
    end: int
    arguments: list[tuple[int, int]]
    ancestors: tuple[str, ...]

    def argument(self, expression: str, index: int) -> str:
        if index >= len(self.arguments):
            return ""
        a, b = self.arguments[index]
        return expression[a:b].strip()


@dataclass
class Reference:
    target: str
    start: int
    end: int
    contexts: tuple[str, ...]


@dataclass
class ExpressionInfo:
    functions: dict[str, int] = field(default_factory=dict)
    calls: list[Call] = field(default_factory=list)
    references: list[Reference] = field(default_factory=list)
    unresolved: list[dict] = field(default_factory=list)
    virtual_columns: list[str] = field(default_factory=list)
    variables: list[str] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)
    reference_blockers: list[str] = field(default_factory=list)


class Resolver:
    def __init__(self, model: Model):
        self.model = model
        self.qualified: dict[tuple[str, str], list[Entity]] = defaultdict(list)
        self.bare: dict[str, list[Entity]] = defaultdict(list)
        self.tables = {name.casefold(): table for name, table in model.tables.items()}
        self.functions = {e.name.casefold(): e for e in model.entities.values() if e.kind == "function"}
        for e in model.entities.values():
            if e.kind in {"measure", "column"}:
                self.qualified[e.table.casefold(), e.name.casefold()].append(e)
                self.bare[e.name.casefold()].append(e)

    def resolve(self, name: str, table: str | None = None, owner: Entity | None = None) -> tuple[str | None, str]:
        if table is not None:
            found = self.qualified.get((table.casefold(), name.casefold()), [])
        else:
            found = self.bare.get(name.casefold(), [])
            measures = [e for e in found if e.kind == "measure"]
            local_columns = [e for e in found if e.kind == "column" and owner and e.table == owner.table]
            # Unqualified measure names are globally resolved. A same-name local
            # column can shadow them in row context; do not guess in that case.
            found = measures + local_columns
        if len(found) == 1:
            return found[0].id, ""
        return None, "ambiguous reference" if found else "unknown reference or virtual column"

    def scan(self, text: str, owner: Entity | None = None) -> ExpressionInfo:
        tokens = tokenize(text)
        info = ExpressionInfo()
        stack: list[tuple[int, str | None, list[int]]] = []
        pairs: dict[int, tuple[int, list[int], tuple[str, ...]]] = {}
        call_names: dict[int, tuple[str, int]] = {}
        contexts: dict[int, tuple[str, ...]] = {}
        for i, t in enumerate(tokens):
            contexts[i] = tuple(x[1] for x in stack if x[1])
            if t.value == "(" and t.kind == "SYMBOL":
                name, start_index = None, i
                if i and tokens[i - 1].kind == "NAME":
                    start_index = i - 1
                    while start_index >= 2 and tokens[start_index - 1].value == "." and tokens[start_index - 2].kind == "NAME":
                        start_index -= 2
                    name = "".join(x.value for x in tokens[start_index:i]).upper()
                    call_names[i] = (name, start_index)
                stack.append((i, name, []))
            elif t.value == "," and t.kind == "SYMBOL" and stack:
                stack[-1][2].append(i)
            elif t.value == ")" and t.kind == "SYMBOL":
                if not stack:
                    raise ModelError(f"Unmatched DAX ')' at offset {t.start}")
                opening, name, commas = stack.pop()
                if name:
                    pairs[opening] = (i, commas, tuple(x[1] for x in stack if x[1]))
        if stack:
            raise ModelError("Unmatched DAX '(' in expression")
        for opening, (closing, commas, ancestors) in sorted(pairs.items()):
            name, start_index = call_names[opening]
            bounds = [tokens[opening].end] + [tokens[x].end for x in commas]
            ends = [tokens[x].start for x in commas] + [tokens[closing].start]
            args = [] if closing == opening + 1 else list(zip(bounds, ends))
            call = Call(name, tokens[start_index].start, tokens[closing].end, args, ancestors)
            info.calls.append(call)
        info.functions = dict(sorted(Counter(c.name for c in info.calls).items()))
        # Aliases are accepted only in explicit alias-creating function positions.
        aliases: set[str] = set()
        for call in info.calls:
            if call.name in {"ADDCOLUMNS", "SELECTCOLUMNS"}:
                for j in range(1, len(call.arguments), 2):
                    arg = tokenize(call.argument(text, j))
                    if len(arg) == 1 and arg[0].kind == "STRING":
                        aliases.add(arg[0].value.casefold())
        variables = {tokens[i + 1].value.casefold() for i, t in enumerate(tokens[:-1])
                     if t.kind == "NAME" and t.value.upper() == "VAR" and tokens[i + 1].kind == "NAME"}
        info.variables = sorted(variables)
        qualified_tokens: set[int] = set()
        for i, t in enumerate(tokens):
            if t.kind == "BRACKET":
                table = None
                if i and tokens[i - 1].kind in {"NAME", "QUOTED"} and not (
                    tokens[i - 1].kind == 'NAME' and tokens[i - 1].value.upper() in _KEYWORDS
                ):
                    table = tokens[i - 1].value
                    qualified_tokens.add(i - 1)
                if table is None and (t.value.casefold() in aliases or t.value.startswith("@")):
                    info.virtual_columns.append(t.value)
                    if t.value.casefold() in self.bare:
                        info.diagnostics.append(f"Virtual name [{t.value}] overlaps model metadata; lexical scope is conservative.")
                        info.unresolved.append({'table': None, 'name': t.value, 'offset': t.start,
                                                'reason': 'ambiguous virtual-column/model name scope'})
                    continue
                target, reason = self.resolve(t.value, table, owner)
                if target:
                    info.references.append(Reference(target, t.start, t.end, contexts[i]))
                else:
                    info.unresolved.append({"table": table, "name": t.value, "offset": t.start, "reason": reason})
        function_token_positions = {start for _, start in call_names.values()}
        for i, t in enumerate(tokens):
            if i in qualified_tokens or i in function_token_positions:
                continue
            if t.kind in {"NAME", "QUOTED"} and t.value.casefold() in self.tables and t.value.casefold() not in variables:
                if i and tokens[i - 1].kind == "NAME" and tokens[i - 1].value.upper() in {"VAR", "MEASURE"}:
                    continue
                info.references.append(Reference(self.tables[t.value.casefold()].id, t.start, t.end, contexts[i]))
        for call in info.calls:
            function = self.functions.get(call.name.casefold())
            if function:
                info.references.append(Reference(function.id, call.start, call.end, call.ancestors))
        if "SELECTEDMEASURE" in info.functions or "SELECTEDMEASURENAME" in info.functions:
            info.diagnostics.append("Calculation-group behavior depends on the selected measure; this graph cannot expand dynamic selected-measure dependencies.")
        scoped = bool(variables or aliases or info.virtual_columns or any(c.name in ITERATORS for c in info.calls))
        if scoped:
            info.diagnostics.append("Variable/table-alias scopes are analyzed conservatively; graph edges are static possibilities, not evaluated calls.")
        info.reference_blockers = list(info.diagnostics)
        if scoped:
            # Reconcile bracket references with lexical row scopes. A string
            # alias elsewhere in the expression is not evidence of visibility.
            from .optimizer import logical_plan
            tree, issues = logical_plan(text, self.model, owner)
            if tree.operation != 'opaque':
                bound = {}
                def visit(node):
                    if node.operation in {'column', 'measure', 'extension_column'}:
                        bracket = next((t for t in tokens if t.kind == 'BRACKET' and node.start <= t.start < node.end), None)
                        if bracket:bound[bracket.start] = node
                    for child in node.children:visit(child)
                visit(tree)
                offsets = set(bound)
                info.references = [r for r in info.references if r.start not in offsets]
                info.unresolved = [r for r in info.unresolved if r['offset'] not in offsets]
                info.virtual_columns = []
                for i, t in enumerate(tokens):
                    node = bound.get(t.start)
                    if node is None:continue
                    if node.operation == 'extension_column':
                        info.virtual_columns.append(t.value)
                    else:
                        info.references.append(Reference(node.target,t.start,t.end,contexts[i]))
                info.references.sort(key=lambda r:(r.start,r.end,r.target))
                info.reference_blockers.remove(info.diagnostics[-1])
                # The initial lexical pass cannot distinguish a same-name
                # column in an unrelated table from an actual scope collision.
                # Successful binding discharges those provisional warnings.
                info.reference_blockers = [r for r in info.reference_blockers if not r.startswith('Virtual name [')]
                info.diagnostics = [r for r in info.diagnostics if not r.startswith('Virtual name [')]
                info.diagnostics[-1] = 'VAR bindings and row-column scopes were resolved lexically; references describe static possibilities, not evaluated calls.'
            elif issues:
                info.reference_blockers[-1] = issues[0]
        return info
