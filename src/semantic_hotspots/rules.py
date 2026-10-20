"""Explainable triage patterns. Points are ordinal heuristics, never CU estimates."""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass

from .dax import ITERATORS, ExpressionInfo, tokenize
from .graph import DependencyGraph
from .model import Entity, Model

DOC_FILTER = 'https://learn.microsoft.com/en-us/dax/best-practices/dax-avoid-avoid-filter-as-filter-argument'
DOC_VAR = 'https://learn.microsoft.com/en-us/dax/best-practices/dax-variables'
DOC_CALC = 'https://learn.microsoft.com/en-us/dax/calculate-function-dax'
DOC_STORAGE = 'https://learn.microsoft.com/en-us/power-bi/transform-model/desktop-storage-mode'


@dataclass
class Finding:
    rule: str
    severity: str
    title: str
    points: int
    evidence: str
    suggestion: str
    verification: str
    line: int = 1
    source_url: str | None = None
    confidence: str = 'structural pattern; runtime impact unverified'

    def to_dict(self) -> dict:
        return asdict(self)


def inspect_expression(entity: Entity, info: ExpressionInfo, model: Model) -> list[Finding]:
    text = entity.expression or ''
    findings: list[Finding] = []
    def add(rule, title, points, evidence, suggestion, verification, start=0, url=None, severity='review'):
        findings.append(Finding(rule, severity, title, points, evidence, suggestion, verification,
                                text.count('\n', 0, start) + 1, url))
    nested = [c for c in info.calls if c.name in ITERATORS and any(a in ITERATORS for a in c.ancestors)]
    if nested:
        c = nested[0]
        add('DAX001', 'Nested row iteration', 6, f'{c.name} inside {", ".join(c.ancestors)}',
            'Inspect iterator grain and cardinality. Test whether an equivalent grouped/source aggregation can replace repeated row work; do not remove an iterator solely because it is present.',
            'Compare leaf values, native subtotals, grand totals, BLANK behavior and source requests for representative filters.', c.start)
    callback_refs = [r for r in info.references if any(c in ITERATORS for c in r.contexts)
                     and model.entities[r.target].kind == 'measure']
    transitions = [c for c in info.calls if c.name in {'CALCULATE', 'CALCULATETABLE'}
                   and any(a in ITERATORS for a in c.ancestors)]
    if callback_refs or transitions:
        add('DAX002', 'Context-sensitive work inside an iterator', 6,
            f'{len(callback_refs)} textual measure references and {len(transitions)} CALCULATE calls inside iterators; not evaluation counts.',
            'Profile the child chain under the actual iteration key. Consider pre-aggregation at that exact grain. Preserve row-to-filter context transition and the intended total calculation.',
            'Check repeated grouping labels versus unique keys, single/multi selections, missing mappings and totals.',
            callback_refs[0].start if callback_refs else transitions[0].start, DOC_CALC)
    for c in info.calls:
        is_filter_argument = any(parent.name in {'CALCULATE', 'CALCULATETABLE'}
                                 and any(a <= c.start < b for a, b in parent.arguments[1:])
                                 for parent in info.calls)
        if c.name == 'FILTER' and is_filter_argument:
            predicates = c.arguments[1:] if len(c.arguments) > 1 else []
            uses_measure = any(a <= r.start < b and model.entities[r.target].kind == 'measure'
                               for a, b in predicates for r in info.references)
            if uses_measure:
                add('DAX003', 'Measure predicate in a filter argument', 5, c.argument(text, 1)[:240],
                    'This measure predicate can require per-row evaluation. Investigate a smaller equivalent filter domain or a source aggregation. It cannot simply become a Boolean CALCULATE filter argument while it still references a measure.',
                    'Verify that any domain reduction preserves rows and totals; compare engine CPU and source requests.', c.start, DOC_FILTER)
            else:
                first = tokenize(c.argument(text, 0))
                broad = len(first) == 1 and first[0].kind in {'NAME', 'QUOTED'} and first[0].value.casefold() in {n.casefold() for n in model.tables}
                if broad:
                    add('DAX004', 'Whole-table FILTER under CALCULATE', 3, c.argument(text, 0),
                        'If the predicate can be expressed with supported column Boolean filters, test that form. Use KEEPFILTERS where intersection semantics are required. Arbitrary FILTER expressions are not automatically replaceable.',
                        'Compare existing-filter intersection, OR logic, BLANKs and totals; benchmark especially for Import tables.', c.start, DOC_FILTER)
        if c.name == 'SUMX':
            arg = tokenize(c.argument(text, 1))
            domain = tokenize(c.argument(text, 0))
            if len(arg) == 2 and arg[0].kind in {'NAME', 'QUOTED'} and arg[1].kind == 'BRACKET' and len(domain) == 1 and arg[0].value.casefold() == domain[0].value.casefold() and any(
                e.kind == 'column' and e.table.casefold() == arg[0].value.casefold()
                and e.name.casefold() == arg[1].value.casefold() for e in model.entities.values()
            ):
                add('DAX005', 'Iterator sums one physical column', 2, text[c.start:c.end][:240],
                    f'Test SUM({c.argument(text, 1)}) instead of this SUMX when the domain is the same physical table. Treat this as a candidate, not an automatic rewrite.',
                    'Verify numeric data type, BLANK behavior, filter context and totals; measure before/after.', c.start)
    counts = Counter(r.target for r in info.references if model.entities[r.target].kind == 'measure')
    repeated = [(model.entities[n].qualified_name, count) for n, count in counts.items() if count > 1]
    if repeated:
        add('DAX006', 'Repeated textual measure references', 2, '; '.join(f'{n}: {c}' for n, c in repeated),
            'A VAR may reuse a repeated result only where its row and filter context is identical. Do not move a child measure outside CALCULATE or an iterator when that changes its evaluation context. The engine may already reuse some results.',
            'Test contexts separately and compare server timings; textual repetition does not prove repeated execution.', url=DOC_VAR)
    if 'TREATAS' in info.functions:
        add('DAX007', 'Virtual relationship/filter transfer', 2, f'{info.functions["TREATAS"]} TREATAS calls',
            'Measure the transferred value-set cardinality and inspect pushed-down SQL. Test a physical relationship or smaller equivalent transfer only if it preserves model semantics; TREATAS itself is not inherently a defect.',
            'Check disconnected slicers, KEEPFILTERS versus replacement, unmapped keys, multi-select and totals.')
    if any(n in info.functions for n in {'CROSSJOIN', 'GENERATE', 'GENERATEALL'}):
        add('DAX008', 'Potentially expanding intermediate table', 5,
            ', '.join(n for n in ('CROSSJOIN', 'GENERATE', 'GENERATEALL') if n in info.functions),
            'Inspect actual intermediate row counts. Reduce the input domain or aggregate before expansion only where the business calculation remains equivalent.',
            'Use server timings/query plans; never infer materialized row counts by multiplying source counts alone.')
    if 'FORMAT' in info.functions and entity.kind == 'measure':
        add('DAX009', 'Numeric output may be converted to text', 2, 'FORMAT is present in a measure expression',
            'If numeric output is intended, test a model format string or dynamic format string instead. Keep FORMAT when the actual result must be text.',
            'Verify result type, sorting, conditional formatting and downstream arithmetic.')
    if any(n in info.functions for n in {'TODAY', 'NOW', 'UTCNOW', 'UTCTODAY'}):
        add('DAX010', 'Time-dependent result', 0, 'Current-date/time function in the chain',
            'Use a fixed reference date for controlled comparisons. Preserve the required live date semantics in the production definition.',
            'Test date boundaries and relevant time zones.', severity='info')
    if any(n in info.functions for n in {'SELECTEDMEASURE', 'SELECTEDMEASURENAME', 'ISSELECTEDMEASURE'}):
        add('DAX011', 'Dynamic calculation-group dependency', 0, 'Selected-measure function is present',
            'Profile the calculation item together with each selected measure. Static analysis cannot substitute the runtime selected measure.',
            'Validate calculation-group precedence, format strings and the actual visual query.', severity='info')
    if info.unresolved or info.diagnostics:
        for diagnostic in info.diagnostics:
            add('PARSE002', 'Conservative expression analysis', 0, diagnostic,
                'Inspect the original expression and, for authoritative engine bindings, compare an INFO.CALCDEPENDENCY export.',
                'Do not treat this static projection as a compiled plan.', severity='info')
    if info.unresolved:
        add('PARSE001', 'Unresolved or ambiguous references', 0,
            '; '.join((f"{x['table']}" if x['table'] else '') + f"[{x['name']}]: {x['reason']}" for x in info.unresolved),
            'Supply missing model metadata or qualify ambiguous references. Virtual/extension columns can also be unresolved; do not invent measure edges.',
            'Compare engine dependency metadata before relying on chain completeness.', severity='warning')
    return findings


def relationship_findings(root: str, graph: DependencyGraph) -> tuple[list[Finding], list[str]]:
    model = graph.model
    sources = {model.entities[n].table for n in graph.dependency_ids(root, True)
               if model.entities[n].kind in {'column', 'table'} and model.entities[n].table in model.tables}
    # Follow possible dimension -> fact filter ancestors. Explicit DAX can override
    # these paths, so this is model context, not evidence every edge was used.
    context = set(sources)
    changed = True
    relevant = []
    canonical = {n.casefold(): n for n in model.tables}
    while changed:
        changed = False
        for r in model.relationships:
            if not r.active:
                continue
            from_table = canonical.get(r.from_table.casefold(), r.from_table)
            to_table = canonical.get(r.to_table.casefold(), r.to_table)
            pairs = [(to_table, from_table)]
            if r.from_cardinality.casefold() == 'one' and r.to_cardinality.casefold() == 'many':
                pairs = [(from_table, to_table)]
            if r.cross_filter.casefold() == 'bothdirections' or r.from_cardinality.casefold() == r.to_cardinality.casefold():
                pairs.extend([(from_table, to_table), (to_table, from_table)])
            for parent, child in pairs:
                if child in context:
                    if r not in relevant:
                        relevant.append(r)
                    if parent not in context:
                        context.add(parent)
                        changed = True
    findings = []
    direct_query = [n for n in context if n in model.tables and 'directquery' in model.tables[n].storage_modes]
    imports = [n for n in context if n in model.tables and 'import' in model.tables[n].storage_modes]
    if direct_query and imports:
        findings.append(Finding('MODEL001', 'review', 'Import/DirectQuery filter context', 3,
            f'Potential filter context includes DirectQuery: {", ".join(sorted(direct_query))}; Import: {", ".join(sorted(imports))}.',
            'Inspect cross-storage joins and literal value transfers in native queries. Test eligible Dual dimensions with compatible sources, freshness and memory requirements; do not switch storage modes automatically.',
            'Compare the same values, totals, source requests, CPU and native CU across both modes. Include refresh/source costs separately.', source_url=DOC_STORAGE))
    both = [r.name for r in relevant if r.cross_filter.casefold() == 'bothdirections']
    many = [r.name for r in relevant if r.from_cardinality.casefold() == r.to_cardinality.casefold() == 'many']
    if both or many:
        findings.append(Finding('MODEL002', 'review', 'Complex possible filter paths', 2,
            f'Bidirectional relationships: {both}; many-to-many relationships: {many}.',
            'Inspect active propagation paths and ambiguity before proposing a simpler model. A relationship count alone does not establish query cost.',
            'Test visual interactions, totals and explicit USERELATIONSHIP/CROSSFILTER behavior.'))
    return findings, sorted(context)
