"""Self-contained matrix report with progressively disclosed grain evidence."""
from __future__ import annotations

import json
from .report import _CSS,_esc,_anchor,_number


def render_matrix_html(analysis):
    rows=[];details=[]
    definitions={n['id']:n for n in analysis.dependency_graph.get('nodes',[])}
    measured=analysis.rank_by=='highest_observed_synthetic_context_mean_ms'
    for i,row in enumerate(analysis.matrices,1):
        anchor=_anchor('matrix-',row['id']);spec=row['spec']
        priority=row['highest_observed_mean_ms'] if measured else row['priority']
        unit='ms' if measured else 'review points'
        candidates=row['grain_advice']['candidates']
        rows.append(f'<tr><td>{i}</td><td><a href="#{anchor}">{_esc(row["title"])}</a></td><td>{_number(priority,unit)}</td><td>{len(row["value_measure_ids"])}</td><td>{len(row["workload_ids"])}</td><td>{"Complete static chains" if row["chain_complete"] else "Partial context/chains"}</td></tr>')
        advice=[]
        for candidate in candidates:
            states=''.join(f'<li>{_esc(s["expression"])}</li>' for s in candidate['component_states'])
            instructions=''.join(f'<li>{_esc(s)}</li>' for s in candidate['rollup_instructions'])
            blockers=''.join(f'<li>{_esc(s)}</li>' for s in candidate['blockers'])
            probe=''
            if candidate.get('probe_file'):
                probe=f'<p><a href="{_esc(candidate["probe_file"])}">Grouping-count probe DAX</a></p><pre>{_esc(candidate["group_count_probe"])}</pre>'
            advice.append(f'''<details><summary>{_esc(candidate['fact_table'])} · {_esc(candidate['status'])}</summary>
<p><b>Earliest candidate stage:</b> {_esc(candidate['earliest_stage'])}</p>
<table><tbody><tr><th>Keep existing relationship keys</th><td>{_esc(candidate['relationship_preserving_grain'])}</td></tr><tr><th>Explore a coarser matrix/filter grain</th><td>{_esc(candidate['coarser_matrix_grain'])}</td></tr></tbody></table>
<p>{_esc(candidate['coarser_grain_note'])}</p><h4>Component states</h4><ul>{states}</ul><h4>How to roll up</h4><ul>{instructions}</ul>
<p>{_esc(candidate['probe_scope'])}</p><p>Source rows: Unknown · Grouped rows: Unknown · Speedup: Unknown. A group-count probe is not an A/B performance test.</p>
<details><summary>Required filter paths and blockers</summary><pre>{_esc(json.dumps(candidate['filter_paths'],indent=2))}</pre><ul>{blockers or '<li>No recognized static blocker; engine validation is still required.</li>'}</ul></details>{probe}</details>''')
        blockers=''.join(f'<li>{_esc(x)}</li>' for x in row['grain_advice']['blockers']+row['diagnostics'])
        iterator=''.join(f'<details><summary>{_esc(v["measure"])} · {_esc(v["function"])}</summary><pre>{_esc(v["input"])}</pre><p>{_esc(v["advice"])}</p></details>' for v in row['grain_advice']['iterator_review'])
        query_details=[]
        for workload in analysis.workloads:
            if workload['matrix_id']!=row['id']:continue
            query_details.append(f'<details><summary>{_esc(workload["scenario"])}</summary><p>Context: {_esc(workload["context_coverage"])}. {_esc(workload["interpretation"])}</p><pre>{_esc(workload["query"])}</pre><a href="{_esc(workload["query_file"])}">Query file</a></details>')
        measure_details=[]
        for measure in row['measure_findings']:
            findings=measure['findings']+[f for values in measure['inherited_findings'].values() for f in values]
            findings_html=''.join(f'<li>{_esc(f["title"])}: {_esc(f["suggestion"])}</li>' for f in findings)
            definition=definitions.get(measure['id'],{})
            measure_details.append(f'<details><summary>{_esc(measure["table"])}[{_esc(measure["name"])}] · {_number(measure["risk_score"],"points")}</summary><p>Static chain complete: {_esc(measure["chain_complete"])}. Upstream measures: {measure["transitive_measure_count"]}.</p><h4>DAX definition</h4><pre>{_esc(definition.get("expression") or "Definition missing")}</pre><ul>{findings_html}</ul><pre>{_esc(json.dumps({"direct_dependencies":measure["direct_dependencies"],"functions":measure["functions"]},indent=2))}</pre></details>')
        chain_ids=set(row['chain_node_ids'])
        edges=[e for e in analysis.dependency_graph.get('edges',[]) if e['from'] in chain_ids and e['to'] in chain_ids]
        upstream=''.join(f'<details><summary>{_esc(n.get("table"))}[{_esc(n.get("name"))}]</summary><pre>{_esc(n.get("expression") or "Definition missing")}</pre></details>'
                         for key,n in sorted(definitions.items()) if key in chain_ids and n.get('type',n.get('kind'))=='measure')
        chain_html='<details><summary>All unique measure definitions in these chains</summary>'+upstream+'</details><details><summary>Unique dependency edges and shared branches</summary><pre>'+_esc(json.dumps(edges,ensure_ascii=False,indent=2))+'</pre></details>'
        timing=''.join(f'<tr><td>{_esc(e["scenario"])}</td><td>{_esc(e["cache"])}</td><td>{_number(e["mean_ms"],"ms")}</td><td>{_esc(e["successes"])}/{_esc(e["attempts"])}</td></tr>' for e in row['benchmark_evidence'])
        details.append(f'''<details id="{anchor}"><summary>{i}. {_esc(row['title'])} · {_number(priority,unit)}</summary>
<p>{row['unique_chain_measure_count']} unique measures across value/filter chains. Shared branches: {_esc(row['shared_measure_ids'])}. Shared chains are not charged repeatedly as measured cost.</p>
<details><summary>Matrix axes, filters and drill contract</summary><pre>{_esc(json.dumps(spec,ensure_ascii=False,indent=2))}</pre></details>
<details><summary>Aggregation grain advice · {len(candidates)} source tables</summary>{''.join(advice) or '<p>No aggregation recipe can be established from supported definitions.</p>'}<ul>{blockers}</ul>{iterator}</details>
<details><summary>Measures and inherited findings</summary>{''.join(measure_details)}{chain_html}</details>
<details><summary>Detail, subtotal and grand-total workloads · {len(query_details)}</summary>{''.join(query_details) or '<p>Queries withheld because bindings or visual-filter semantics are incomplete.</p>'}</details>
<details><summary>Observed synthetic workload timings</summary><table><thead><tr><th>Context</th><th>Cache</th><th>Mean</th><th>Success/attempts</th></tr></thead><tbody>{timing or '<tr><td colspan="4">Not run / unknown.</td></tr>'}</tbody></table></details></details>''')
    warnings=''.join(f'<li>{_esc(w)}</li>' for w in analysis.warnings)
    provenance='<details><summary>Attached benchmark provenance</summary><pre>'+_esc(json.dumps(analysis.benchmark_context,ensure_ascii=False,indent=2))+'</pre></details>' if analysis.benchmark_context else ''
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Matrix grain analysis</title><style>{_CSS}</style></head><body><main>
<header><div class="eyebrow">SEMANTIC HOTSPOTS / MATRICES</div><h1>Keep the right detail.<br>Investigate fewer rows.</h1><p>{_esc(analysis.model_name)}</p></header>
<section><h2>{'Slowest observed contexts first' if measured else 'Highest static review priorities first'}</h2><p>{len(analysis.matrices)} matrices · {len(analysis.workloads)} multi-measure workload contexts. {'Synthetic baseline durations, not pre-aggregation improvements.' if measured else 'Static scores are not execution cost. No DAX engine execution is implied.'}</p>
<div class="scroll"><table><thead><tr><th>#</th><th>Matrix</th><th>Priority</th><th>Values</th><th>Contexts</th><th>Coverage</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div></section>
<section><h2>Inspect the grain contract</h2>{''.join(details)}</section><section>{provenance}<details><summary>Interpretation and limits</summary><ul>{warnings}</ul></details><p><a href="matrices.json">Full findings JSON</a> · <a href="matrix-specs.json">Editable normalized matrix specifications</a></p></section>
</main><script>function reveal(){{let n=document.getElementById(location.hash.slice(1));if(n){{n.open=true;n.scrollIntoView();}}}}addEventListener('hashchange',reveal);reveal();</script></body></html>'''
