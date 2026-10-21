"""Explain original/proposed computation DAGs without pretending to be an engine plan."""
from __future__ import annotations

import hashlib
import heapq
import html
import json
from collections import Counter, defaultdict
from pathlib import Path

from .dax import Resolver
from .graph import DependencyGraph
from .model import Model, ModelError
from .parser import load_model, parse_model


def _model(source):
    return source if isinstance(source, Model) else parse_model(source) if isinstance(source, dict) else load_model(source)


def _order(nodes, edges):
    deps = {n: set() for n in nodes}
    consumers = defaultdict(set)
    for e in edges:
        deps[e['from']].add(e['to'])
        consumers[e['to']].add(e['from'])
    remaining = {n: len(ds) for n, ds in deps.items()}
    ready = [n for n in nodes if not remaining[n]]
    heapq.heapify(ready)
    result = []
    while ready:
        n = heapq.heappop(ready)
        result.append(n)
        for parent in sorted(consumers[n]):
            remaining[parent] -= 1
            if not remaining[parent]:
                heapq.heappush(ready, parent)
    return result if len(result) == len(nodes) else None


def computation_dag(source, measure, *, table=None, expression=None, graph=None):
    """Expand a root's full static chain, including recognized scalar operators.

    Unsupported expressions remain opaque with their resolved static references.
    Calculated tables/columns are materialization boundaries, not work repeated
    for each query. An expression override affects only the root in this view.
    """
    from .optimizer import logical_plan
    model = _model(source)
    owner = model.measure(measure, table)
    graph = graph or DependencyGraph(model)
    if graph.model is not model:
        raise ModelError('The supplied graph must belong to the supplied model')
    if expression is not None and (not isinstance(expression, str) or not expression.strip()):
        raise ModelError('Proposed expression must be nonempty DAX')
    overrides = {owner.id: expression} if expression is not None else {}
    infos = dict(graph.syntax)
    dependencies = graph.dependencies
    if overrides:
        infos[owner.id] = Resolver(model).scan(expression, owner)
        old_refs = {r.target for r in graph.syntax[owner.id].references} if owner.id in graph.syntax else set()
        dependencies = dict(dependencies)
        dependencies[owner.id] = (dependencies[owner.id] - old_refs) | {r.target for r in infos[owner.id].references}
    wanted, pending = set(), [owner.id]
    while pending:
        n = pending.pop()
        if n not in wanted:
            wanted.add(n)
            pending.extend(dependencies[n] - wanted)
    nodes, edges, diagnostics = {}, [], {}
    materializations = []
    def edge(a, b, kind, **extra):
        edges.append({'from': a, 'to': b, 'kind': kind, **extra})
    for n in sorted(wanted):
        entity = model.entities[n]
        text = overrides.get(n, entity.expression)
        stored = bool(text and entity.kind in {'table', 'column'})
        phase = 'refresh' if stored else 'query' if text else 'stored_data'
        nodes[n] = {'id': n, 'type': entity.kind, 'label': entity.qualified_name,
                    'table': entity.table, 'name': entity.name, 'expression': text,
                    'definition_phase': phase, 'materialized': stored}
        if stored:
            materializations.append(n)
        info = infos.get(n)
        refs = {r.target for r in info.references} if info else set()
        for dep in sorted(dependencies[n] - refs):
            edge(n, dep, 'metadata_dependency')
        if not text:
            continue
        tree, issues = logical_plan(text, model, entity)
        if issues:
            diagnostics[n] = issues
        if tree.operation == 'opaque':
            op = 'operation:' + n + ':opaque'
            nodes[op] = {'id': op, 'type': 'operation', 'operation': 'opaque',
                         'label': 'Opaque DAX — inspect definition', 'expression': text,
                         'definition_phase': phase, 'supported': False, 'owner': n}
            edge(n, op, 'definition', phase=phase)
            for dep in sorted(refs):
                edge(op, dep, 'static_reference')
            continue
        represented = set()
        def expand(item, path):
            if item.operation in {'column', 'measure'}:
                represented.add(item.target)
                return item.target
            if item.operation == 'group':
                return expand(item.children[0], path + '.group')
            identity = 'operation:' + n + ':' + path
            label = item.operation.upper()
            if item.operation in {'binary', 'unary', 'literal'}:
                label = item.value
            nodes[identity] = {'id': identity, 'type': 'literal' if item.operation == 'literal' else 'operation',
                               'operation': item.operation, 'label': label,
                               'expression': text[item.start:item.end], 'source_span': [item.start, item.end],
                               'result_type': item.result_type, 'row_context': list(item.row_context),
                               'filter_context': list(item.filter_context), 'definition_phase': phase,
                               'supported': item.supported, 'owner': n}
            if item.operation == 'scan':
                represented.add(item.target)
                edge(identity, item.target, 'reads_table')
            for i, child in enumerate(item.children):
                edge(identity, expand(child, path + '.' + str(i)), 'argument', argument=i+1)
            return identity
        edge(n, expand(tree, 'root'), 'definition', phase=phase)
        # A parsed call may still have unmodeled semantics, for example a model
        # function. Preserve every resolver edge instead of silently losing it.
        for dep in sorted(refs - represented):
            edge(n, dep, 'static_reference')
    ordered = _order(nodes, edges)
    # Query reachability ends at stored definitions. Their refresh lineage is
    # still retained in the complete graph and available separately in HTML.
    outgoing = defaultdict(list)
    for e in edges:
        outgoing[e['from']].append(e)
    query, pending = set(), [owner.id]
    while pending:
        n = pending.pop()
        if n not in query:
            query.add(n)
            pending.extend(e['to'] for e in outgoing[n] if e.get('phase') != 'refresh')
    refresh, pending = set(), list(materializations)
    while pending:
        n = pending.pop()
        if n not in refresh:
            refresh.add(n)
            pending.extend(e['to'] for e in outgoing[n])
    unresolved = {n: infos[n].unresolved for n in sorted(wanted) if n in infos and infos[n].unresolved}
    missing = [n for n in sorted(wanted) if model.entities[n].kind == 'measure'
               and not overrides.get(n, model.entities[n].expression)]
    return {'schema_version': '1.0', 'kind': 'static-computation-dag', 'root': owner.id,
            'edge_meaning': 'consumer -> dependency; argument numbers describe syntax, not engine execution order',
            'is_acyclic': ordered is not None, 'evaluation_order': ordered,
            'dependency_complete': ordered is not None and not unresolved and not missing,
            'operator_complete': not any(n.get('operation') == 'opaque' for n in nodes.values()),
            'logical_subset_validated': not diagnostics,
            'unresolved': unresolved, 'missing_definitions': missing,
            'diagnostics': diagnostics, 'query_time_node_ids': sorted(query), 'refresh_time_node_ids': sorted(refresh),
            'materialization_node_ids': materializations,
            'nodes': [nodes[n] for n in sorted(nodes)],
            'edges': sorted(edges, key=lambda e: (e['from'], e['to'], e['kind'], e.get('argument', 0)))}


def compare_measure_dags(source, measure, *, table=None, proposed_expression=None,
                         proposed_source=None, proposed_measure=None, proposed_table=None, graph=None):
    """Compare a root-expression proposal, or a measure in a separate proposed model.

    No source is mutated and no candidate is declared correct or faster.
    Query-time diagrams stop at materializations; complete refresh lineage and
    exact definitions remain in each exported graph.
    """
    if (proposed_expression is None) == (proposed_source is None):
        raise ModelError('Supply exactly one of proposed_expression or proposed_source')
    if proposed_expression is not None and (proposed_measure is not None or proposed_table is not None):
        raise ModelError('A root expression override keeps the original measure identity')
    model = _model(source)
    owner = model.measure(measure, table)
    graph = graph or DependencyGraph(model)
    before = computation_dag(model, owner.id, graph=graph)
    if proposed_source is not None:
        other = _model(proposed_source)
        after = computation_dag(other, proposed_measure or owner.name, table=proposed_table or owner.table)
    else:
        after = computation_dag(model, owner.id, expression=proposed_expression, graph=graph)
    old, new = ({n['id']: n for n in g['nodes']} for g in [before, after])
    def signature(e):return (e['from'], e['to'], e['kind'], e.get('argument'))
    old_edges, new_edges = ({signature(e): e for e in g['edges']} for g in [before, after])
    def operations(g, phase='query_time_node_ids'):
        query = set(g[phase])
        return dict(sorted(Counter(n['operation'] for n in g['nodes']
                                   if n['id'] in query and n['type'] == 'operation').items()))
    changed = sorted(n for n in old.keys() & new.keys() if old[n] != new[n])
    def upstream(g):
        query = set(g['query_time_node_ids'])
        return sum(n['type'] == 'measure' and n['id'] != g['root'] and n['id'] in query for n in g['nodes'])
    return {'schema_version': '1.0', 'kind': 'measure-dag-comparison',
            'measure': owner.qualified_name, 'original': before, 'proposed': after,
            'difference': {'meaning': 'Graph membership/definition changes, not deletion or deployment of source objects.',
                           'only_original_node_ids': sorted(old.keys() - new.keys()),
                           'only_proposed_node_ids': sorted(new.keys() - old.keys()),
                           'changed_node_ids': changed,
                           'unchanged_node_ids': sorted(old.keys() & new.keys() - set(changed)),
                           'only_original_edges': [old_edges[n] for n in sorted(old_edges.keys()-new_edges.keys(), key=str)],
                           'only_proposed_edges': [new_edges[n] for n in sorted(new_edges.keys()-old_edges.keys(), key=str)],
                           'query_operators_original': operations(before), 'query_operators_proposed': operations(after),
                           'refresh_operators_original': operations(before,'refresh_time_node_ids'),
                           'refresh_operators_proposed': operations(after,'refresh_time_node_ids')},
            'summary': {'root_expression_changed': old[before['root']]['expression'] != new[after['root']]['expression'],
                        'upstream_measures_original': upstream(before), 'upstream_measures_proposed': upstream(after),
                        'introduced_materialization_ids': sorted(set(after['materialization_node_ids'])-set(before['materialization_node_ids']))},
            'validation_status': 'not_run', 'performance_status': 'unknown', 'predicted_speedup': None,
            'limitations': ['Static possibilities and parsed syntax, not a physical execution plan or engine execution counts.',
                            'Parsed calls outside the guarded logical subset are syntax only; type/context semantics are not established.',
                            'Opaque expressions retain resolved references and exact DAX; unsupported topology is not invented.',
                            'Refresh-time materialization adds storage and refresh work; fewer query nodes do not prove speedup.',
                            'Implicit relationship/filter propagation and runtime calculation-group application are not DAG edges.',
                            'Validate values, BLANK behavior and intended visual contexts before benchmarking.']}


def _svg(graph, wanted=None, highlights=None):
    wanted = set(wanted) if wanted is not None else {n['id'] for n in graph['nodes']}
    nodes = {n['id']: n for n in graph['nodes'] if n['id'] in wanted}
    edges = [e for e in graph['edges'] if e['from'] in wanted and e['to'] in wanted
             and (wanted != set(graph['query_time_node_ids']) or e.get('phase') != 'refresh')]
    order = _order(nodes, edges)
    if order is None:
        return '<p>Cycle detected: this view is not a DAG. Inspect the exact edges below.</p>'
    if len(nodes) > 80:
        return '<p>Diagram omitted above 80 nodes. The complete definitions, edge list and JSON are retained below.</p>'
    depth = {n: 0 for n in nodes}
    children = defaultdict(set)
    for e in edges:children[e['from']].add(e['to'])
    for n in reversed(order):
        for child in children[n]:depth[child] = max(depth[child], depth[n]+1)
    layers = defaultdict(list)
    for n in order:layers[depth[n]].append(n)
    width = max((len(row)*270 for row in layers.values()), default=270)
    height = (max(layers, default=0)+1)*100
    positions = {n: ((width-len(row)*270)/2+i*270+10, level*100+8)
                 for level,row in layers.items() for i,n in enumerate(sorted(row))}
    esc = lambda x: html.escape(str(x), quote=True)
    svg = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" role="img" aria-label="Consumer to dependency DAG"><title>Static computation DAG; arrows point to dependencies</title>']
    for e in edges:
        x,y=positions[e['from']];a,b=positions[e['to']];x+=125;y+=62;a+=125
        svg.append(f'<path d="M{x} {y} C{x} {y+22} {a} {b-22} {a} {b}" fill="none" stroke="#72908a"/><path d="M{a-4} {b-6} L{a} {b} L{a+4} {b-6}" fill="none" stroke="#72908a"/>')
    for identity,(x,y) in positions.items():
        n=nodes[identity];color='#fff2dc' if n.get('materialized') or n.get('definition_phase')=='refresh' else '#e7f2ed' if n['type'] in {'measure','operation'} else '#f1f4f3'
        status=(highlights or {}).get(identity,'')
        stroke={'only in original':'#b15a33','only in proposed':'#087969','definition changed':'#d18a18'}.get(status,'#a4bdb4')
        label=n['label'];short=label if len(label)<=33 else label[:30]+'…'
        subtitle='stored · built on refresh' if n.get('materialized') else n['type']
        if n['type']=='operation' and not n.get('supported',True):subtitle+=' · syntax only'
        svg.append(f'<g><title>{esc(label)}{esc(" · "+status if status else "")}</title><rect x="{x}" y="{y}" width="250" height="62" rx="7" fill="{color}" stroke="{stroke}" stroke-width="2"/><text x="{x+12}" y="{y+25}" font-family="system-ui,sans-serif" font-size="13" fill="#18383d">{esc(short)}</text><text x="{x+12}" y="{y+46}" font-family="system-ui,sans-serif" font-size="11" fill="#52666a">{esc(subtitle)}</text></g>')
    return ''.join(svg)+'</svg>'


DAG_CSS = '''.dag-pair{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px}.dag-side{min-width:0;overflow-wrap:anywhere;border:1px solid #ccdcd5;border-radius:8px;padding:16px;background:#fff}.dag-side h3{margin-top:0}.dag-diagram{overflow:auto;max-height:600px;background:#fafcfb}.dag-side pre{max-height:340px;overflow:auto}.dag-note{border-left:3px solid #087969;padding:10px 14px;background:#e7f2ed}.dag-edges{overflow-wrap:anywhere;font-size:13px}@media(max-width:850px){.dag-pair{grid-template-columns:1fr}}'''


def render_dag_comparison(comparison):
    """Self-contained, escaped fragment: paired diagrams then full node/edge detail."""
    esc = lambda x: html.escape(str(x), quote=True)
    sides=[]
    for key,title in [('original','Original measure DAG'),('proposed','Proposed measure DAG')]:
        g=comparison[key];nodes={n['id']:n for n in g['nodes']};root=nodes[g['root']]
        highlights={n:'only in '+key for n in comparison['difference']['only_'+key+'_node_ids']}
        highlights.update({n:'definition changed' for n in comparison['difference']['changed_node_ids']})
        definitions=''.join('<details><summary>'+esc(n['label'])+' · '+esc(n['type'])+' · '+esc(n['definition_phase'])+'</summary><pre>'+esc(n.get('expression') or 'Stored data / metadata object')+'</pre></details>' for n in g['nodes'])
        edge_list=''.join('<li>'+esc(nodes[e['from']]['label'])+' → '+esc(nodes[e['to']]['label'])+' <small>'+esc(e['kind'])+((' · argument '+str(e['argument'])) if 'argument' in e else '')+'</small></li>' for e in g['edges'])
        refresh=('<details><summary>Refresh-time lineage and materialization</summary><p>Stored definitions are built on refresh. These dependencies remain part of the full lineage; they are not assumed to run per query.</p><div class="dag-diagram">'+_svg(g,g['refresh_time_node_ids'],highlights)+'</div></details>') if g['materialization_node_ids'] else ''
        issues=('<p class="dag-note">Some syntax or bindings need review. Opaque DAX, unresolved references, missing definitions and logical-subset diagnostics remain in the full detail. Parsed calls outside that subset show syntax, not established type/context semantics.</p>') if g['diagnostics'] or not g['dependency_complete'] else ''
        sides.append(f'<section class="dag-side"><h3>{title}</h3><p>{esc(root["label"])}</p><pre>{esc(root["expression"] or "Definition missing")}</pre>{issues}<h4>Query-time dependency view</h4><div class="dag-diagram">{_svg(g,g["query_time_node_ids"],highlights)}</div>{refresh}<details><summary>Every node, definition and edge · {len(g["nodes"])} nodes / {len(g["edges"])} edges</summary>{definitions}<ol class="dag-edges">{edge_list}</ol><pre>{esc(json.dumps({k:g[k] for k in ["unresolved","missing_definitions","diagnostics","evaluation_order"]},ensure_ascii=False,indent=2))}</pre></details></section>')
    delta=comparison['difference']
    summary=comparison['summary']
    explanation=('<p>Upstream measure dependencies: '+str(summary['upstream_measures_original'])+' original → '+str(summary['upstream_measures_proposed'])+' proposed. Root DAX '+('changed.' if summary['root_expression_changed'] else 'is unchanged.')+' These are structural differences, not measured work or speedup.</p>')
    context=('<details><summary>Matrix context and retained grain</summary><pre>'+esc(json.dumps(comparison['context'],ensure_ascii=False,indent=2))+'</pre></details>') if comparison.get('context') else ''
    return ('<p class="dag-note">Arrows mean “depends on”. Proposed is a candidate, not a proven improvement. Query views stop at stored materializations; expand refresh lineage to see how they are built.</p>'+explanation+'<p class="meta">Borders: rust = only in original path; teal = only in proposed path; amber = changed definition. Gold fill = refresh work or a stored materialization. Source objects are not deleted.</p>'+context+'<div class="dag-pair">'+''.join(sides)+'</div><details><summary>What changed between the paths?</summary><p>Operators by phase (syntactic occurrences, not execution counts):</p><pre>'+esc(json.dumps({k:delta[k] for k in ['query_operators_original','query_operators_proposed','refresh_operators_original','refresh_operators_proposed']},indent=2))+'</pre><pre>'+esc(json.dumps(delta,ensure_ascii=False,indent=2))+'</pre></details>')


def dag_to_text(graph):
    """Spell out each shared node once; retain exact definitions and edge roles."""
    nodes = {n['id']: n for n in graph['nodes']}
    outgoing = defaultdict(list)
    for e in graph['edges']:outgoing[e['from']].append(e)
    lines = ['Root: '+graph['root'], graph['edge_meaning'],
             'Static lineage; this is not an engine execution schedule.']
    for identity in reversed(graph['evaluation_order'] or sorted(nodes)):
        n = nodes[identity]
        lines += ['', identity, n['label']+' · '+n['type']+' · '+n['definition_phase']]
        if n.get('materialized'):lines.append('Stored definition: built on refresh, not expanded per query.')
        if n.get('expression'):lines.append('DAX: '+n['expression'])
        for e in outgoing[identity]:
            role = e['kind'] + (' argument '+str(e['argument']) if 'argument' in e else '')
            lines.append('  -> '+e['to']+' ('+role+')')
    lines += ['', 'Unresolved: '+json.dumps(graph['unresolved'],ensure_ascii=False),
              'Missing definitions: '+json.dumps(graph['missing_definitions'],ensure_ascii=False),
              'Diagnostics: '+json.dumps(graph['diagnostics'],ensure_ascii=False)]
    return '\n'.join(lines)+'\n'


def write_dag_comparison(comparison, directory):
    """Write complete reusable JSON, Graphviz DOT and an offline HTML report."""
    from .report import _CSS
    root=Path(directory);root.mkdir(parents=True,exist_ok=True)
    def dump(name,value):
        p=root/name;p.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8');return p
    dump('original.dag.json',comparison['original']);dump('proposed.dag.json',comparison['proposed'])
    for key in ['original','proposed']:
        g=comparison[key]
        (root/(key+'.txt')).write_text(dag_to_text(g),encoding='utf-8')
        lines=['digraph computation {','  rankdir=TB;']
        for n in g['nodes']:lines.append('  '+json.dumps(n['id'],ensure_ascii=False)+' [label='+json.dumps(n['label'],ensure_ascii=False)+'];')
        for e in g['edges']:lines.append('  '+json.dumps(e['from'],ensure_ascii=False)+' -> '+json.dumps(e['to'],ensure_ascii=False)+' [label='+json.dumps(e['kind'])+'];')
        (root/(key+'.dot')).write_text('\n'.join(lines)+'\n}\n',encoding='utf-8')
    esc=lambda s:html.escape(str(s),quote=True)
    links=' · '.join(f'<a href="{name}">{label}</a>' for name,label in [('comparison.json','Comparison JSON'),('original.dag.json','Original DAG JSON'),('proposed.dag.json','Proposed DAG JSON'),('original.txt','Original text'),('proposed.txt','Proposed text'),('original.dot','Original DOT'),('proposed.dot','Proposed DOT')])
    (root/'report.html').write_text('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Original and proposed measure DAGs</title><style>'+_CSS+DAG_CSS+'</style><main><h1>Original and proposed measure DAGs</h1><p>'+esc(comparison['measure'])+'</p><p>Validation: not run · Performance: unknown</p>'+links+render_dag_comparison(comparison)+'<h2>Scope</h2><ul>'+''.join('<li>'+esc(s)+'</li>' for s in comparison['limitations'])+'</ul></main></html>',encoding='utf-8')
    return {'json':dump('comparison.json',comparison),'html':root/'report.html'}


def write_candidate_dags(candidates, directory):
    for c in candidates:
        if c.get('dag_comparison'):
            write_dag_comparison(c['dag_comparison'],Path(directory)/c['id'])
