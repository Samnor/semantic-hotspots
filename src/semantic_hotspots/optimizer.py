"""Bound, typed logical subset and guarded rewrite proposals, never physical plans.

Unknown syntax/context is opaque. Rewrites are proposals for engine validation;
no source model is changed and structural simplification predicts no timing.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .dax import Resolver, tokenize
from .graph import DependencyGraph
from .model import Model, ModelError, object_id
from .parser import load_model, parse_model

_NUMERIC = {'int64', 'double', 'decimal', 'fixeddecimal', 'currency'}
_PRECEDENCE = {'||': 1, '&&': 2, '=': 3, '==': 3, '<>': 3, '<': 3, '>': 3,
               '<=': 3, '>=': 3, '+': 4, '-': 4, '*': 5, '/': 5}
_COMPARISONS = {'=', '==', '<>', '<', '>', '<=', '>='}
RULES = {
    'OPT001': {'title': 'Replace a numeric physical-column SUMX with SUM',
               'preconditions': ['Physical base table and numeric physical column from that same table',
                                 'No iterator expression, measure callback, or calculated column',
                                 'Recognized enclosing syntax and context; complete static dependency chain'],
               'source_url': 'https://learn.microsoft.com/en-us/dax/sumx-function-dax'},
    'OPT002': {'title': 'Combine two nested row-local FILTER operations',
               'preconditions': ['Both filters preserve the same physical base-table rows and lineage',
                                 'Predicates use only same-table physical columns and compatible literals',
                                 'Only comparisons and Boolean conjunction/disjunction; no measures, calls, arithmetic, or context transition'],
               'source_url': 'https://learn.microsoft.com/en-us/dax/filter-function-dax'},
}


@dataclass
class LogicalNode:
    operation: str
    result_type: str
    start: int
    end: int
    children: list['LogicalNode'] = field(default_factory=list)
    target: str | None = None
    table: str | None = None
    value: str | None = None
    row_context: tuple[str, ...] = ()
    filter_context: tuple[str, ...] = ()
    supported: bool = True

    def to_dict(self):
        result = asdict(self)
        result['children'] = [child.to_dict() for child in self.children]
        result['source_span'] = [result.pop('start'), result.pop('end')]
        return result


def _type(entity):
    data = {k.casefold(): v for k, v in entity.metadata.items()}
    return str(data.get('data_type') or data.get('datatype') or 'unknown').casefold()


def _plain(node):
    while node.operation == 'group':
        node = node.children[0]
    return node


class _Parser:
    def __init__(self, expression, model, owner):
        self.text, self.model, self.owner = expression, model, owner
        self.tokens = tokenize(expression)
        if len(self.tokens) > 10000:
            raise ModelError('Expression exceeds the logical-subset token limit')
        self.i = 0
        self.resolver = Resolver(model)

    def peek(self, value=None):
        token = self.tokens[self.i] if self.i < len(self.tokens) else None
        return token if value is None else bool(token and token.kind == 'SYMBOL' and token.value == value)

    def take(self, value=None):
        token = self.peek()
        if token is None or (value is not None and not self.peek(value)):
            raise ModelError('Syntax outside the supported logical subset')
        self.i += 1
        return token

    def parse(self):
        result = self.expression()
        if self.peek() is not None:
            raise ModelError('Syntax outside the supported logical subset (including VAR/table constructors)')
        self.bind(result, (), ())
        return result

    def expression(self, minimum=0):
        token = self.take()
        if token.kind == 'SYMBOL' and token.value in {'+', '-'}:
            child = self.expression(6)
            node = LogicalNode('unary', 'unknown', token.start, child.end, [child], value=token.value)
        elif token.kind == 'SYMBOL' and token.value == '(':
            child = self.expression()
            end = self.take(')').end
            node = LogicalNode('group', 'unknown', token.start, end, [child])
        elif token.kind in {'NUMBER', 'STRING'}:
            if token.kind == 'NUMBER' and not re.fullmatch(r'\d+(?:\.\d+)?', token.value):
                raise ModelError('Unsupported numeric literal')
            node = LogicalNode('literal', 'number' if token.kind == 'NUMBER' else 'string', token.start, token.end, value=token.value)
        elif token.kind == 'NAME' and self.peek('('):
            self.take('(')
            args = []
            if not self.peek(')'):
                args.append(self.expression())
                while self.peek(','):
                    self.take(',')
                    args.append(self.expression())
            end = self.take(')').end
            node = LogicalNode(token.value.upper(), 'unknown', token.start, end, args)
        elif token.kind in {'NAME', 'QUOTED'}:
            table = self.resolver.tables.get(token.value.casefold())
            if self.peek() and self.peek().kind == 'BRACKET':
                column = self.take()
                target, _ = self.resolver.resolve(column.value, token.value, self.owner)
                if not target:
                    raise ModelError('Unbound qualified reference')
                entity = self.model.entities[target]
                node = LogicalNode(entity.kind, _type(entity), token.start, column.end, target=target, table=entity.table)
            elif table:
                node = LogicalNode('scan', 'table', token.start, token.end, target=table.id, table=table.name)
            else:
                raise ModelError('Variables and unbound names are outside the logical subset')
        elif token.kind == 'BRACKET':
            target, _ = self.resolver.resolve(token.value, owner=self.owner)
            if not target or self.model.entities[target].kind != 'measure':
                raise ModelError('Unqualified column scope is outside the logical subset')
            node = LogicalNode('measure', 'scalar', token.start, token.end, target=target)
        else:
            raise ModelError('Syntax outside the supported logical subset')
        while self.peek() and self.peek().kind == 'SYMBOL':
            op = self.peek().value
            if self.i + 1 < len(self.tokens) and self.tokens[self.i + 1].kind == 'SYMBOL':
                pair = op + self.tokens[self.i + 1].value
                if pair in _PRECEDENCE:
                    op = pair
            if op not in _PRECEDENCE or _PRECEDENCE[op] < minimum:
                break
            self.i += len(op)
            right = self.expression(_PRECEDENCE[op] + 1)
            node = LogicalNode('binary', 'unknown', node.start, right.end, [node, right], value=op)
        return node

    def bind(self, node, rows, filters, depth=0):
        if depth > 64:
            raise ModelError('Logical subset nesting exceeds 64 levels')
        node.row_context, node.filter_context = rows, filters
        for i, child in enumerate(node.children):
            child_rows, child_filters = rows, filters
            if node.operation in {'FILTER', 'SUMX'} and i == 1:
                child_rows = rows + ((node.children[0].table or 'unknown row scope'),)
            if node.operation == 'CALCULATE' and i == 0:
                child_filters += ('CALCULATE: transition/modified filters',)
            self.bind(child, child_rows, child_filters, depth+1)
        c, op = node.children, node.operation
        node.supported = all(x.supported for x in c)
        if op in {'scan', 'column', 'measure', 'literal'}:
            return
        if op == 'group':
            node.result_type, node.table = c[0].result_type, c[0].table
        elif op == 'unary':
            node.result_type = c[0].result_type
            node.supported &= node.result_type in _NUMERIC | {'number'}
        elif op == 'binary':
            node.result_type = 'boolean' if node.value in _COMPARISONS | {'&&', '||'} else 'number'
            node.supported &= all(x.result_type not in {'table', 'unknown'} for x in c)
        elif op in {'TRUE', 'FALSE'} and not c:
            node.result_type = 'boolean'
        elif op == 'SUM' and len(c) == 1 and _plain(c[0]).operation == 'column' and c[0].result_type in _NUMERIC:
            node.result_type = c[0].result_type
        elif op == 'SUMX' and len(c) == 2 and c[0].result_type == 'table' and c[1].result_type in _NUMERIC | {'number', 'scalar'}:
            node.result_type = c[1].result_type
        elif op == 'FILTER' and len(c) == 2 and c[0].result_type == 'table' and c[1].result_type == 'boolean':
            node.result_type, node.table = 'table', c[0].table
        elif op == 'COUNTROWS' and len(c) == 1 and c[0].result_type == 'table':
            node.result_type = 'int64'
        elif op == 'CALCULATE' and c and c[0].result_type not in {'table', 'unknown'} and all(x.result_type in {'table', 'boolean'} for x in c[1:]):
            node.result_type = c[0].result_type
        else:
            node.supported = False
        if op.casefold() in self.resolver.functions:
            node.supported = False


def logical_plan(expression, model, owner):
    """Return an explicitly partial logical tree; unknown syntax is never guessed."""
    try:
        if len(expression) > 100000:
            raise ModelError('Expression exceeds the logical-subset size limit')
        tree = _Parser(expression, model, owner).parse()
        if not tree.supported:
            return tree, ['An operation or operand type is outside the supported logical subset; rewrites withheld.']
        return tree, []
    except (ModelError, RecursionError) as exc:
        return LogicalNode('opaque', 'unknown', 0, len(expression), supported=False), [str(exc) or 'Logical subset nesting limit exceeded']


def _physical(node, model):
    entity = model.entities.get(node.target)
    table = model.entities.get(object_id('table', node.table or ''))
    return bool(entity and not entity.expression and table and not table.expression)


def _predicate(node, table, model):
    node = _plain(node)
    if node.operation == 'binary' and node.value in {'&&', '||'}:
        return all(_predicate(x, table, model) for x in node.children)
    if node.operation != 'binary' or node.value not in _COMPARISONS:
        return False
    def atom(item):
        item = _plain(item)
        if item.operation == 'unary' and _plain(item.children[0]).operation == 'literal':
            item = _plain(item.children[0])
        if item.operation == 'literal':
            return item.result_type
        if item.operation == 'column' and item.table == table and _physical(item, model):
            return 'number' if item.result_type in _NUMERIC else item.result_type
        return None
    types = [atom(x) for x in node.children]
    return types[0] in {'number', 'string'} and types[0] == types[1]


def _matches(tree, text, model):
    matches = []
    def visit(node):
        if node.operation == 'SUMX':
            table, column = map(_plain, node.children)
            if (table.operation == 'scan' and column.operation == 'column'
                    and column.table == table.table and column.result_type in _NUMERIC
                    and _physical(table, model) and _physical(column, model)):
                matches.append(('OPT001', node.start, node.end, f'SUM({text[column.start:column.end]})'))
        if node.operation == 'FILTER':
            inner = _plain(node.children[0])
            if inner.operation == 'FILTER':
                base = _plain(inner.children[0])
                if (base.operation == 'scan' and _physical(base, model)
                        and _predicate(inner.children[1], base.table, model)
                        and _predicate(node.children[1], base.table, model)):
                    p, q = inner.children[1], node.children[1]
                    matches.append(('OPT002', node.start, node.end,
                                    f'FILTER({text[base.start:base.end]}, ({text[p.start:p.end]}) && ({text[q.start:q.end]}))'))
        for child in node.children:
            visit(child)
    visit(tree)
    return matches


def _digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


@dataclass
class RewritePlan:
    measures: list[dict]

    @property
    def candidates(self):
        return [candidate for measure in self.measures for candidate in measure['candidates']]

    def to_dict(self):
        return {'schema_version': '1.0', 'kind': 'guarded-logical-rewrite-plan',
                'interpretation': 'Partial logical representation and unvalidated proposals; no physical plan or predicted speedup.',
                'rules': RULES, 'measures': self.measures}

    def write(self, directory):
        from .report import _CSS
        from .comparison import DAG_CSS, write_candidate_dags
        import html
        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True)
        write_candidate_dags(self.candidates, root/'dag-comparisons')
        (root/'rewrite-plan.json').write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')
        details = ''.join('<details><summary>' + html.escape(m['measure']) + f" · {len(m['candidates'])} candidates</summary>" + render_rewrites(m) + '</details>' for m in self.measures)
        (root/'rewrites.html').write_text(f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Rewrite proposals</title><style>{_CSS}{DAG_CSS}</style></head><body><main><h1>Guarded rewrite proposals</h1><p>Unvalidated candidates. No source changes, DAX execution, or predicted speedup. Review the guards and validate candidates before timing.</p>{details}</main></body></html>', encoding='utf-8')
        return {'json': root/'rewrite-plan.json', 'html': root/'rewrites.html'}


def plan_rewrites(source, *, graph=None, ranked_ids=None, progress=None):
    """Propose isolated edits and a bounded fixed-point combination per measure."""
    model = parse_model(source) if isinstance(source, dict) else source if isinstance(source, Model) else load_model(source)
    graph = graph or DependencyGraph(model, progress=progress)
    measures = list(model.measures)
    if ranked_ids is not None:
        positions = {n: i for i, n in enumerate(ranked_ids)}
        measures.sort(key=lambda m: positions.get(m.id, len(positions)))
    output = []
    dynamic = any(e.kind in {'calculation_item', 'function'} for e in model.entities.values())
    from .progress import emit
    emit(progress, 'rewrites', 'Check rewrite proposals', completed=0, total=len(measures), unit='measures')
    for i, measure in enumerate(measures, 1):
        emit(progress, 'rewrites', 'Check rewrite proposals', completed=i-1, total=len(measures), unit='measures', detail=measure.qualified_name)
        before = measure.expression or ''
        tree, diagnostics = logical_plan(before, model, measure)
        chain = [measure.id] + graph.dependency_ids(measure.id)
        if tree.result_type in {'table', 'unknown'}:
            diagnostics.append('Measure scalar result type is not established in the logical subset.')
        if any(e.kind == 'format_string' and e.metadata.get('for_measure') == measure.id for e in model.entities.values()):
            diagnostics.append('Dynamic format expression requires additional alias-context validation; rewrites withheld.')
        if any(n in graph.syntax and set(graph.syntax[n].functions).intersection({'NOW', 'TODAY', 'RAND', 'RANDBETWEEN'}) for n in chain):
            diagnostics.append('Time-dependent or random dependency requires controlled evaluation; rewrites withheld.')
        if dynamic:
            diagnostics.append('Calculation groups or model functions are present; query-scoped rewrite validation is withheld.')
        if measure.id in graph.blocked or any((model.entities[n].kind == 'measure' and not model.entities[n].expression)
                                             or (n in graph.syntax and (graph.syntax[n].unresolved or any('Unmatched' in d or 'Unterminated' in d for d in graph.syntax[n].diagnostics))) for n in chain):
            diagnostics.append('Static dependency chain is incomplete or cyclic; rewrites withheld.')
        candidates = []
        def candidate(after, steps, combined=False):
            if after == before or any(c['after'] == after for c in candidates):
                return
            after_tree, issues = logical_plan(after, model, measure)
            if issues:
                return
            from .comparison import compare_measure_dags
            candidates.append({'id': _digest(measure.id+'\x00'+before+'\x00'+after)[:20],
                               'measure_id': measure.id, 'measure': measure.qualified_name,
                               'before': before, 'after': after, 'before_sha256': _digest(before),
                               'after_sha256': _digest(after), 'steps': steps, 'combined': combined,
                               'logical_before': tree.to_dict(), 'logical_after': after_tree.to_dict(),
                               'direct_dependencies_before': sorted(graph.dependencies[measure.id]),
                               'direct_dependencies_after': sorted({r.target for r in Resolver(model).scan(after, measure).references}), 'validation_status': 'not_run',
                               'performance_status': 'unknown', 'predicted_speedup': None,
                               'dag_comparison': compare_measure_dags(model, measure.id, proposed_expression=after, graph=graph)})
        if not diagnostics:
            for rule, start, end, replacement in _matches(tree, before, model)[:32]:
                candidate(before[:start]+replacement+before[end:], [{'rule': rule, 'source_span': [start, end],
                                                                      'before': before[start:end], 'after': replacement}])
            current, steps = before, []
            for _ in range(16):
                current_tree, issues = logical_plan(current, model, measure)
                matches = _matches(current_tree, current, model) if not issues else []
                if not matches:
                    break
                rule, start, end, replacement = matches[0]
                steps.append({'rule': rule, 'source_span': [start, end], 'before': current[start:end], 'after': replacement})
                current = current[:start]+replacement+current[end:]
            if len(steps) > 1:
                candidate(current, steps, True)
            if len(steps) == 16:
                diagnostics.append('Combined search reached its 16-step bound; further rewrites may exist.')
        output.append({'measure_id': measure.id, 'measure': measure.qualified_name,
                       'logical_before': tree.to_dict(), 'diagnostics': diagnostics,
                       'affected_downstream_measures': [m.id for m in model.measures if m.id != measure.id and graph.mask(m.id) & (1 << graph.index[measure.id])],
                       'candidates': candidates})
        emit(progress, 'rewrites', 'Check rewrite proposals', completed=i, total=len(measures), unit='measures', detail=measure.qualified_name)
    return RewritePlan(output)


def render_rewrites(measure):
    """Escaped report fragment shared by standalone and ranked reports."""
    import html
    esc = lambda value: html.escape(str(value), quote=True)
    diagnostics = ''.join(f'<li>{esc(x)}</li>' for x in measure['diagnostics'])
    proposals = []
    for c in measure['candidates']:
        from .comparison import render_dag_comparison
        dags = ('<details><summary>Original and proposed measure DAGs</summary>' + render_dag_comparison(c['dag_comparison']) +
                '<p><a href="dag-comparisons/'+c['id']+'/report.html">Open comparison and download DAGs</a></p></details>') if c.get('dag_comparison') else ''
        rules = ', '.join(step['rule'] for step in c['steps'])
        proposals.append(f'<details><summary>{esc(rules)} · {"combined" if c["combined"] else "isolated"} · validation not run</summary><p>Performance unknown. Compare exact results and BLANK behavior before interpreting timings.</p><h4>Before</h4><pre>{esc(c["before"])}</pre><h4>Candidate</h4><pre>{esc(c["after"])}</pre>{dags}<details><summary>Rewrite steps and guards</summary><pre>{esc(json.dumps(c["steps"], indent=2))}</pre><pre>{esc(json.dumps({r: RULES[r] for r in {s["rule"] for s in c["steps"]}}, indent=2))}</pre></details><details><summary>Logical plan after</summary><pre>{esc(json.dumps(c["logical_after"], indent=2))}</pre></details></details>')
    return '<ul>'+diagnostics+'</ul>'+(''.join(proposals) or '<p>No supported rewrite candidate.</p>') + '<details><summary>Partial logical plan before</summary><pre>'+esc(json.dumps(measure['logical_before'], indent=2))+'</pre></details>'
