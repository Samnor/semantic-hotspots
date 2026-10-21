"""Progressive-disclosure HTML with no external scripts, fonts or network calls."""
from __future__ import annotations

import hashlib
import html


def _esc(value) -> str:
    return html.escape(str(value), quote=True)


def _anchor(prefix: str, value: str) -> str:
    return prefix + hashlib.sha256(value.encode('utf-8')).hexdigest()[:20]


def _number(value, unit: str = '') -> str:
    if value is None:
        return 'Unknown'
    return f'{value:,.3f}'.rstrip('0').rstrip('.') + (f' {unit}' if unit else '')


def render_html(analysis, chain_index: dict[str, str]) -> str:
    from .comparison import DAG_CSS
    from .analyzer import SCORE_DESCRIPTION, VERSION

    esc = _esc
    ranked = analysis.ranked
    entities = analysis.model.entities
    graph = analysis.graph
    by_id = {r['id']: r for r in ranked}
    rewrite_rows = {m['measure_id']: m for m in analysis.rewrite_plan.measures} if analysis.rewrite_plan else {}
    from .optimizer import render_rewrites
    anchors = {n: _anchor('measure-', n) for n in by_id}
    field, label, unit = {
        'risk': ('risk_score', 'Estimated review priority', 'points'),
        'query_cpu_context': ('query_cpu_ms_context', 'Recorded query-context CPU', 'ms'),
        'query_cu_context': ('query_cu_seconds_context', 'Recorded query-context cost', 'CU-s'),
    }[analysis.rank_by]
    measured = analysis.rank_by != 'risk'
    queries = analysis.profile.queries if analysis.profile else []
    query_anchors = {q['operation_id']: _anchor('query-', q['operation_id']) for q in queries}

    def primary(row):
        return row['risk_score'] if not measured else (row.get('runtime') or {}).get(field)

    largest = max((primary(r) or 0 for r in ranked), default=0)

    def bar(value):
        width = value / largest * 100 if value is not None and largest else 0
        return f'<div class="track" aria-hidden="true"><span style="width:{width:.3f}%"></span></div>'

    def link(node):
        entity = entities[node]
        name = esc(entity.qualified_name)
        return f'<a href="#{anchors[node]}">{name}</a>' if node in anchors else name

    def all_findings(row):
        return [(row['id'], f) for f in row['findings']] + [
            (n, f) for n, findings in row['inherited_findings'].items() for f in findings]

    def candidate(row):
        findings = all_findings(row)
        actionable = [(n, f) for n, f in findings if f['points'] > 0]
        return max(actionable, key=lambda pair: pair[1]['points'], default=None)

    def coverage(row):
        runtime = row.get('runtime') or {}
        if not measured:
            return 'Complete static chain' if row['chain_complete'] else 'Partial static chain'
        count = runtime.get('cu_coverage' if field == 'query_cu_seconds_context' else 'cpu_coverage', 0)
        total = runtime.get('potential_chain_query_contexts', 0)
        if not total:
            return 'No matched query contexts'
        return f'{count} of {total} contexts recorded' + (' · partial coverage' if count < total else '')

    def next_step(row):
        choice = candidate(row)
        if choice:
            node, finding = choice
            return (f'{esc(finding["title"])}. ' +
                    (f'Found in {link(node)}.' if node != row['id'] else 'Found in this measure.'))
        if not row['chain_complete']:
            return 'Resolve the missing definitions or dependency cycle before judging the full chain.'
        if measured:
            return 'Inspect the expensive query contexts and benchmark this measure separately to isolate its contribution.'
        return 'No named performance pattern was flagged. Capture runtime evidence to assess actual cost.'

    top_cards = []
    table_rows = []
    measure_details = []
    for position, row in enumerate(ranked, 1):
        anchor = anchors[row['id']]
        value = primary(row)
        findings = all_findings(row)
        name = esc(row['name'])
        runtime = row.get('runtime') or {}
        if position <= 3:
            top_cards.append(f'''<article class="priority-card"><small>PRIORITY {position}</small>
<h3><a href="#{anchor}">{name}</a></h3><div class="metric">{_number(value, unit)}</div>{bar(value)}
<small>{coverage(row)}</small><p>{next_step(row)}</p><a class="action" href="#{anchor}">Explore findings &rarr;</a></article>''')
        chain_status = 'Complete' if row['chain_complete'] else 'Partial'
        table_rows.append(f'''<tr data-name="{esc(row['table'] + ' ' + row['name'])}"><td>{position}</td>
<td><a href="#{anchor}">{name}</a><small>{esc(row['table'])}</small></td>
<td class="cost"><strong>{_number(value, unit)}</strong>{bar(value)}<small>{coverage(row)}</small></td>
<td>{row['transitive_measure_count']}</td><td>{len(findings)}</td><td><span class="badge {'partial' if not row['chain_complete'] else ''}">{chain_status}</span></td></tr>''')
        fix_html = []
        for ordinal, (owner, finding) in enumerate(sorted(findings, key=lambda pair: -pair[1]['points']), 1):
            reference = ''
            source_url = finding.get('source_url') or ''
            # All findings are generated internally, but keep URLs safe if the API is extended.
            if source_url.startswith(('https://', 'http://')):
                reference = f'<p><a href="{esc(source_url)}" rel="noreferrer">Rule reference</a></p>'
            owner_text = 'This measure' if owner == row['id'] else 'Dependency: ' + link(owner)
            fix_html.append(f'''<details class="finding"><summary>{ordinal}. {esc(finding['title'])} <span class="badge">{esc(finding['severity'])}</span></summary>
<p class="meta">{owner_text} · {esc(finding['rule'])} · expression line {esc(finding['line'])} · confidence: {esc(finding['confidence'])}</p>
<p><b>Evidence.</b> {esc(finding['evidence'])}</p><p><b>Candidate change.</b> {esc(finding['suggestion'])}</p>
<p><b>How to verify.</b> {esc(finding['verification'])}</p>{reference}</details>''')
        direct = ''.join(f'<li>{link(n)} <small>{esc(entities[n].kind)}</small></li>' for n in row['direct_dependencies'])
        dependency_rows = []
        chain_nodes = [row['id']] + graph.dependency_ids(row['id'])
        for node in chain_nodes:
            deps = graph.dependencies[node]
            if not deps:
                continue
            info = graph.syntax.get(node)
            for dep in sorted(deps):
                refs = [r for r in info.references if r.target == dep] if info else []
                contexts = sorted({' → '.join(r.contexts) or 'Expression root' for r in refs})
                context = '; '.join(contexts) if refs else 'Metadata dependency'
                dependency_rows.append(f'<tr><td>{link(node)}</td><td>→ {link(dep)}</td><td>{esc(context)}</td><td>{len(refs)}</td></tr>')
        nonmeasure_definitions = []
        for node in chain_nodes:
            entity = entities[node]
            if node not in anchors and entity.expression:
                nonmeasure_definitions.append(f'<details><summary>{esc(entity.kind)}: {esc(entity.qualified_name)}</summary><pre>{esc(entity.expression)}</pre></details>')
        chain_html = f'''<details id="{anchor}-chain"><summary>Dependency chain · {row['transitive_measure_count']} upstream measures</summary>
<p>Follow a measure link to inspect its own findings and definition. Arrows run from a consumer to its dependencies. Each edge appears once; shared branches are not expanded repeatedly.</p>
<h4>Direct dependencies</h4><ul class="dependencies">{direct or '<li>No resolved direct dependencies.</li>'}</ul>
<details><summary>All dependency edges · {len(dependency_rows)}</summary><div class="scroll"><table><thead><tr><th>Consumer</th><th>Depends on</th><th>Enclosing calls</th><th>Text references</th></tr></thead><tbody>{''.join(dependency_rows)}</tbody></table></div>
<p>Text references are not execution counts. This is a static dependency graph, not a physical query plan.</p>{''.join(nonmeasure_definitions)}</details></details>'''
        evidence_html = ''
        if analysis.profile:
            operation_ids = set(runtime.get('operation_ids', []))
            matched = [q for q in queries if q['operation_id'] in operation_ids]
            evidence_rows = []
            for q in matched:
                kind = 'Direct reference' if row['id'] in q['measure_ids'] else 'Potential dependency'
                evidence_rows.append(f'''<tr><td><a href="#{query_anchors[q['operation_id']]}">{esc(q['operation_id'])}</a></td>
<td>{kind}</td><td>{_number(q['cpu_ms'], 'ms')}</td><td>{_number(q['recorded_cu_seconds'], 'CU-s')}</td></tr>''')
            evidence_html = f'''<details id="{anchor}-runtime"><summary>Runtime evidence · {len(matched)} matching query contexts</summary>
<p>{runtime.get('direct_query_contexts', 0)} directly reference this measure; {runtime.get('potential_chain_query_contexts', 0)} include it in a possible dependency chain. A potential dependency does not prove that branch executed.</p>
<p>Recorded context CPU: <b>{_number(runtime.get('query_cpu_ms_context'), 'ms')}</b> ({runtime.get('cpu_coverage', 0)} contexts). Recorded context cost: <b>{_number(runtime.get('query_cu_seconds_context'), 'CU-s')}</b> ({runtime.get('cu_coverage', 0)} contexts).</p>
<p>These are whole-query costs. They overlap across measures and are not isolated measure timings. Click an operation to see which measures it directly requested.</p>
<div class="scroll"><table><thead><tr><th>Query operation</th><th>Association</th><th>Recorded CPU</th><th>Recorded cost</th></tr></thead><tbody>{''.join(evidence_rows) or '<tr><td colspan="4">No matched query contexts. Cost is unknown.</td></tr>'}</tbody></table></div></details>'''
        rewrite = rewrite_rows.get(row['id'])
        rewrite_html = (f'<details><summary>Guarded rewrite proposals · {len(rewrite["candidates"])} · unvalidated</summary>' + render_rewrites(rewrite) + '</details>') if rewrite else ''
        unresolved = ''.join(f'<li>{link(n)}</li>' for n in row['incomplete_dependency_ids'])
        relevant_cycles = [c for c in graph.cycles if set(c).intersection(chain_nodes)]
        cycle_html = ''.join('<li>' + ' → '.join(link(n) for n in cycle) + '</li>' for cycle in relevant_cycles)
        score_rows = ''.join(f'<tr><td>{esc(key.replace("_", " ").capitalize())}</td><td>{_number(value)}</td></tr>' for key, value in row['score_breakdown'].items())
        entity = entities[row['id']]
        measure_details.append(f'''<details class="measure-detail" id="{anchor}"><summary><span class="rank">{position:02d}</span> {name} <span class="summary-cost">{_number(value, unit)}</span></summary>
<div class="measure-overview"><small>{esc(row['table'])} · {chain_status.lower()} static chain</small><p>{next_step(row)}</p>
<p>{row['transitive_measure_count']} upstream measures · {len(row['dependent_measures'])} downstream measures · {len(row['findings'])} local findings · {len(findings) - len(row['findings'])} findings in dependencies.</p></div>
<details id="{anchor}-fixes"><summary>Findings and candidate fixes · {len(findings)}</summary>{''.join(fix_html) or '<p>No named performance pattern was flagged. This does not establish that the measure is cheap.</p>'}</details>
{rewrite_html}{chain_html}{evidence_html}
<details id="{anchor}-dax"><summary>DAX definition and source</summary><p class="meta">{esc(entity.source)} · source line {esc(entity.line)}</p><pre>{esc(entity.expression or 'Definition missing')}</pre></details>
<details id="{anchor}-score"><summary>Score, completeness and filter context</summary><p>Estimated review priority: <b>{_number(row['risk_score'], 'points')}</b>. {esc(SCORE_DESCRIPTION)}</p>
<table><tbody>{score_rows}</tbody></table><p>Dependency depth: {_number(row['dependency_depth'])}. A complete static chain means all supported references resolved; it does not guarantee runtime correctness or full engine binding.</p>
{('<h4>Missing or unresolved definitions</h4><ul>' + unresolved + '</ul>') if unresolved else ''}
{('<h4>Dependency cycle witnesses</h4><ul>' + cycle_html + '</ul>') if cycle_html else ''}
<p>Potential filter-context tables: {esc(', '.join(row['potential_filter_context_tables']) or 'None inferred')}.</p></details>
<p><a href="{esc(chain_index[row['id']])}">Download chain JSON</a> · <a href="#ranking">Back to ranking ↑</a></p></details>''')

    # The summary counts each captured operation once, never the overlapping measure totals.
    def unique_metric(key, unit):
        known = [q[key] for q in queries if q[key] is not None]
        value = sum(known) if known else None
        return f'<strong>{_number(value, unit)}</strong><small>{len(known)} of {len(queries)} captured operations recorded · counted once</small>'

    telemetry = ''
    if analysis.profile:
        query_details = []
        query_field = 'recorded_cu_seconds' if any(q['recorded_cu_seconds'] is not None for q in queries) else 'cpu_ms'
        for q in sorted(queries, key=lambda q: (q[query_field] is None, -(q[query_field] or 0), q['operation_id'])):
            selected = ', '.join(link(n) for n in q['measure_ids']) or 'No bound model measures'
            query_details.append(f'''<details id="{query_anchors[q['operation_id']]}"><summary>{esc(q['operation_id'])} · {_number(q['recorded_cu_seconds'], 'CU-s')} · {_number(q['cpu_ms'], 'ms CPU')}</summary>
<p>Directly requested measures: {selected}.</p><p>Whole-query cost is shared by these measures and their possible dependencies. It cannot be added to other measures' context totals.</p>
<dl><dt>Dataset</dt><dd>{esc(q['dataset_id'])}</dd><dt>Started</dt><dd>{esc(q['started'])}</dd>
<dt>Duration</dt><dd>{_number(q['duration_ms'], 'ms')}</dd><dt>QueryEnd observed</dt><dd>{'Yes' if q['query_end_seen'] else 'No'}</dd>
<dt>Execution metrics observed</dt><dd>{'Yes' if q['execution_metrics_seen'] else 'No'}</dd><dt>DirectQuery requests observed</dt><dd>{q['direct_query_requests']}</dd></dl>
{('<p>Errors: ' + esc('; '.join(q['errors'])) + '</p>') if q['errors'] else ''}</details>''')
        telemetry = f'''<section><h2>Query evidence</h2><details id="query-evidence"><summary>Inspect {len(queries)} captured query operations · highest recorded cost first</summary>
<p>These are the unique captured operations behind the summary totals. Missing costs remain unknown; durations and CPU are not converted into CU.</p>{''.join(query_details)}</details></section>'''
    unique_findings = {(node, f['rule'], f['line']) for row in ranked for node, f in all_findings(row)}
    partial = sum(not row['chain_complete'] for row in ranked)
    runtime_kpis = (f'<div class="stat"><span>Recorded capture cost</span>{unique_metric("recorded_cu_seconds", "CU-s")}</div>'
                    f'<div class="stat"><span>Recorded capture CPU</span>{unique_metric("cpu_ms", "ms")}</div>') if analysis.profile else (
                    '<div class="stat"><span>Runtime evidence</span><strong>Not supplied</strong><small>Use semantic and capacity logs to rank observed query contexts</small></div>')
    explanation = ('Measures with the largest recorded query-context totals appear first. A measure may inherit the same expensive contexts as its dependencies. '
                   'These costs overlap across measures: do not add them together or interpret them as isolated measure timings. '
                   'Partial coverage is a recorded subtotal, not an estimate of missing cost. Unknown values appear last.') if measured else (
                   'Measures with the highest estimated review priority appear first. Model definitions cannot establish actual CPU or CU cost. '
                   'The points rank patterns to investigate; they are not predicted execution time. Unknown scores appear last.')
    lead = (f'Start with <a href="#{anchors[ranked[0]["id"]]}">{esc(ranked[0]["name"])}</a>. ' + next_step(ranked[0])) if ranked else 'This model contains no measures.'
    warnings = ''.join(f'<li>{esc(w)}</li>' for w in analysis.warnings)
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Semantic hotspots · {esc(analysis.model.name)}</title><style>{_CSS}{DAG_CSS}</style></head><body>
<a class="skip" href="#ranking">Skip to measure ranking</a><main>
<header><div class="eyebrow">SEMANTIC HOTSPOTS <span>MODEL REVIEW · OFFLINE</span></div>
<h1>Find the measures worth<br>investigating first.</h1><p class="model-name">{esc(analysis.model.name)}</p>
<nav aria-label="Report sections"><a href="#overview">Overview</a><a href="#ranking">Measure ranking</a><a href="#details">Measure details</a><a href="#coverage">Coverage</a></nav></header>
<section id="overview"><div class="section-label">01 / OVERVIEW</div><h2>{'Recorded cost, highest first' if measured else 'Review priority, highest first'}</h2>
<p class="lead">{lead}</p><div class="stats"><div class="stat"><span>Measures analyzed</span><strong>{len(ranked)}</strong><small>{len(analysis.model.tables)} tables · {partial} partial chains</small></div>
<div class="stat"><span>Distinct findings</span><strong>{len(unique_findings)}</strong><small>Deduplicated across shared dependencies</small></div>{runtime_kpis}</div>
<div class="interpretation"><b>Ranking: {label}.</b> {explanation}</div>
<div class="priorities">{''.join(top_cards)}</div></section>
<section id="ranking"><div class="section-label">02 / MEASURE RANKING</div><h2>Where to focus</h2><p>Click a measure for its explanation, candidate fixes and supporting evidence.</p>
<div class="search-row"><label for="search">Find a measure or table<input id="search" type="search" placeholder="Type a name…" aria-controls="ranking-body"></label>
<p id="search-count" role="status" aria-live="polite">Showing {len(ranked)} of {len(ranked)} measures</p></div>
<div class="scroll"><table class="ranking-table"><thead><tr><th scope="col">#</th><th scope="col">Measure</th><th scope="col">{label} ↓</th><th scope="col">Upstream measures</th><th scope="col">Findings in chain</th><th scope="col">Static chain</th></tr></thead>
<tbody id="ranking-body">{''.join(table_rows)}</tbody></table></div><p id="no-results" hidden>No matching measures. Clear the search to restore the ranking.</p></section>
<section id="details"><div class="section-label">03 / MEASURE DETAILS</div><h2>From finding to evidence</h2><p>Open a measure, then choose the level of detail you need. Candidate changes include checks to run before adopting them.</p>{''.join(measure_details) or '<p>No measures to inspect.</p>'}</section>
{telemetry}<section id="coverage"><div class="section-label">04 / COVERAGE</div><h2>How to read these findings</h2>
<details><summary>Scoring and ranking method</summary><p>{esc(SCORE_DESCRIPTION)}</p><p>Automatic ranking uses matched native CU observations when available, otherwise matched CPU observations, otherwise static review priority. A caller can explicitly override this choice. Equal values are ordered by stable measure ID. Bars scale to the largest known value in this ranking; they are not percentages of total cost.</p></details>
<details><summary>Coverage and diagnostics · {len(analysis.warnings)}</summary><ul>{warnings or '<li>No additional model-level diagnostics.</li>'}</ul></details>
<p>Graphs show static possibilities, not physical engine execution plans. No model files are changed and no formulas are executed. Validate equivalent values, BLANKs, totals and measured performance when testing a candidate change.</p>
<p><a href="analysis.json">Full analysis JSON</a> · <a href="chain-index.json">Dependency chain index</a>{' · <a href="rewrite-plan.json">Guarded rewrite plan JSON</a>' if analysis.rewrite_plan else ''}</p></section>
<footer>semantic-hotspots {esc(VERSION)} · Self-contained HTML · No external assets or network requests</footer></main>
<script>{_SCRIPT}</script></body></html>'''


_CSS = '''
:root{color-scheme:light;--ink:#182f34;--muted:#52666a;--teal:#087969;--line:#d7e1df;--paper:#fff;--wash:#f3f6f4}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:var(--wash);color:var(--ink);font:15px/1.6 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
main{max-width:1280px;margin:auto;padding:0 42px}a{color:var(--teal);text-decoration-thickness:1px;text-underline-offset:3px}a:hover{color:#034e43}a:focus-visible,summary:focus-visible,input:focus-visible{outline:3px solid #d77c32;outline-offset:4px}
header{padding:36px 0 26px;border-bottom:1px solid var(--line)}.eyebrow,.section-label{font-size:11px;letter-spacing:.12em;font-weight:750;color:var(--muted)}.eyebrow{color:var(--teal)}.eyebrow span{float:right;color:var(--muted)}h1{font-size:clamp(32px,4.3vw,54px);letter-spacing:-.04em;line-height:1.13;font-weight:650;margin:30px 0 18px}h2{font-size:28px;line-height:1.25;letter-spacing:-.025em;margin:10px 0 18px}h3{font-size:17px;line-height:1.4;margin:13px 0 20px}h4{margin-bottom:8px}p{margin:12px 0}.model-name{color:var(--muted);overflow-wrap:anywhere}nav{display:flex;gap:28px;flex-wrap:wrap;margin-top:24px;font-size:13px}nav a{color:var(--ink);text-decoration:none}section{padding:36px 0 12px;scroll-margin-top:16px}.lead{font-size:18px;max-width:1000px}.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin:26px 0 18px}.stat{padding:18px 20px;background:var(--paper);border:1px solid var(--line);border-radius:8px}.stat span{display:block;font-size:12px;color:var(--muted)}.stat strong{display:block;font-size:27px;font-weight:650;letter-spacing:-.03em;margin:5px 0}.stat small{font-size:11px}small,.meta{display:block;font-size:12px;color:var(--muted);overflow-wrap:anywhere}.interpretation{font-size:13px;border-left:3px solid var(--teal);padding:12px 18px;background:#e6efeb;color:#284e46}.priorities{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px;margin-top:24px}.priority-card{background:var(--paper);border:1px solid var(--line);border-top:3px solid var(--teal);border-radius:8px;padding:22px;display:flex;flex-direction:column}.priority-card h3 a{text-decoration:none;color:var(--ink);overflow-wrap:anywhere}.priority-card p{font-size:13px}.priority-card .action{margin-top:auto;font-weight:650;font-size:13px}.metric{font-size:25px;letter-spacing:-.03em;font-weight:650}.track{height:5px;background:#e3ebe8;margin:9px 0 10px;border-radius:4px;overflow:hidden}.track span{display:block;height:100%;background:var(--teal);border-radius:4px}.search-row{display:flex;justify-content:space-between;align-items:end;gap:20px;margin:24px 0 16px}.search-row label{font-size:12px;color:var(--muted);width:min(100%,430px)}input{display:block;width:100%;padding:11px 14px;margin-top:5px;font:inherit;font-size:14px;background:white;border:1px solid #b9cac4;border-radius:6px}.search-row p{font-size:12px;color:var(--muted);margin:0 0 10px}.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:13px;background:var(--paper)}th{text-align:left;color:var(--muted);font-weight:600;font-size:11px;background:#e8eeeb;white-space:nowrap}th,td{padding:13px 15px;border-bottom:1px solid var(--line);vertical-align:top}td{overflow-wrap:anywhere}.ranking-table td:nth-child(2){min-width:230px;max-width:410px}.ranking-table td:first-child{color:var(--muted)}.ranking-table td.cost{min-width:240px}.cost strong{font-weight:650}.cost small{font-size:11px}.cost .track{max-width:190px}.badge{display:inline-block;font-size:10px;font-weight:600;padding:2px 7px;border-radius:4px;background:#e5f0e9;color:#235a45;white-space:nowrap}.badge.partial{background:#fff0d9;color:#815018}
details{border:1px solid var(--line);background:white;border-radius:7px;padding:15px 18px;margin:12px 0;scroll-margin-top:20px}details[open]{padding-bottom:18px}summary{cursor:pointer;font-weight:600;overflow-wrap:anywhere}summary::marker{color:var(--teal)}details details{background:#fafcfb;margin:12px 0;padding:12px 16px;font-size:13px}.measure-detail{padding:19px 22px}.measure-detail>summary{font-size:15px}.summary-cost{float:right;font-size:13px;color:var(--muted);margin-left:16px}.rank{display:inline-block;color:var(--teal);font-size:12px;margin-right:12px}.measure-overview{padding:12px 0 6px;font-size:14px}.finding p{max-width:1000px}.finding .badge{margin-left:8px}.dependencies{padding-left:22px}.dependencies li{margin:7px 0}.dependencies small{display:inline}pre{background:#edf2ef;padding:18px;white-space:pre-wrap;overflow-wrap:anywhere;font:12px/1.7 ui-monospace,SFMono-Regular,Consolas,monospace;border-radius:5px}dl{display:grid;grid-template-columns:190px 1fr;font-size:13px;gap:8px 20px}dt{font-weight:600}dd{margin:0;overflow-wrap:anywhere}footer{font-size:11px;color:var(--muted);margin-top:40px;padding:22px 0 32px;border-top:1px solid var(--line)}.skip{position:absolute;left:12px;top:-100px;background:white;padding:8px}.skip:focus{top:12px}[hidden]{display:none!important}
@media(max-width:800px){main{padding:0 20px}.priorities{grid-template-columns:1fr}.eyebrow span{float:none;display:block;margin-top:8px}.stats{grid-template-columns:repeat(2,minmax(0,1fr))}.summary-cost{float:none;display:block;margin:7px 0 0 22px}.search-row{align-items:start;flex-direction:column;gap:6px}dl{grid-template-columns:1fr}dd{margin-bottom:10px}}
@media(prefers-reduced-motion:reduce){html{scroll-behavior:auto}}
@media print{body{background:white}main{padding:0}nav,.search-row,.skip{display:none}.priorities{break-inside:avoid}details{break-inside:avoid}a{color:inherit}}
'''

_SCRIPT = '''
(function(){
  const search=document.getElementById('search');
  const rows=Array.from(document.querySelectorAll('#ranking-body tr'));
  search.addEventListener('input',function(){
    const query=this.value.toLocaleLowerCase().trim();let count=0;
    rows.forEach(function(row){row.hidden=!row.dataset.name.toLocaleLowerCase().includes(query);if(!row.hidden)count++;});
    document.getElementById('search-count').textContent='Showing '+count+' of '+rows.length+' measures';
    document.getElementById('no-results').hidden=count!==0;
  });
  function reveal(hash){
    const target=document.getElementById(hash.slice(1));if(!target)return;
    let parent=target;while(parent){if(parent.tagName==='DETAILS')parent.open=true;parent=parent.parentElement;}
    target.scrollIntoView({block:'start'});
  }
  document.addEventListener('click',function(event){
    const anchor=event.target.closest('a[href^="#"]');if(anchor)reveal(anchor.hash);
  });
  window.addEventListener('hashchange',function(){reveal(location.hash);});
  if(location.hash)reveal(location.hash);
})();
'''
