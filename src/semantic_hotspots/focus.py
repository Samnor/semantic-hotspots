"""Review root-only measure proposals without a matrix or source-model edits."""
from __future__ import annotations

import copy
import hashlib
import html
import json
from dataclasses import dataclass
from pathlib import Path

from .comparison import (DAG_CSS, _svg, compare_measure_dags, computation_dag,
                         dag_to_text, write_dag_comparison)
from .graph import DependencyGraph
from .model import Entity, Model, ModelError
from .optimizer import RULES, plan_rewrites
from .parser import load_model, parse_model
from .progress import emit


@dataclass
class MeasureReview:
    data: dict

    def to_dict(self):
        return json.loads(json.dumps(self.data,ensure_ascii=False,allow_nan=False))

    def write(self, directory):
        from .report import _CSS
        root=Path(directory)
        if root.exists() and (not root.is_dir() or any(root.iterdir())):
            raise ModelError('Measure review output must be absent or empty')
        root.mkdir(parents=True,exist_ok=True)
        data=self.data;esc=lambda s:html.escape(str(s),quote=True)
        (root/'measure-review.json').write_text(json.dumps(data,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
        (root/'original.dax').write_text(data['expression'] or '',encoding='utf-8')
        (root/'original.dag.json').write_text(json.dumps(data['original_dag'],ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        (root/'original.txt').write_text(dag_to_text(data['original_dag']),encoding='utf-8')
        items=[]
        for c in data['candidates']:
            folder=root/'candidates'/c['id']
            write_dag_comparison(c['dag_comparison'],folder)
            (folder/'proposed.dax').write_text(c['after'],encoding='utf-8')
            (folder/'validate-total.dax').write_text(_validation_query(data['table'],data['name'],c['after'],data['validation_alias']),encoding='utf-8')
            url='candidates/'+c['id']+'/'
            items.append('<article><h3>'+esc(c['title'])+'</h3><p>Correctness: not tested · Performance: unknown</p><pre>'+esc(c['after'])+'</pre><p><a href="'+url+'report.html">Compare original and proposed DAGs</a> · <a href="'+url+'proposed.dax" download>Download proposed DAX</a> · <a href="'+url+'validate-total.dax" download>Download total validation query</a></p></article>')
        diagnostics=''.join('<li>'+esc(d)+'</li>' for d in data['diagnostics'])
        page=('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
              '<title>Optimize one measure</title><style>'+_CSS+DAG_CSS+'</style><main><h1>Optimize one measure</h1><h2>'+esc(data['measure'])+'</h2>'
              '<p>Only this measure’s proposed expression changes. Helper measures and source tables remain context. No source model is edited and no DAX is executed.</p>'
              '<h3>Original DAX</h3><pre>'+esc(data['expression'] or 'Definition missing')+'</pre><p><a href="original.dax" download>Original DAX</a> · <a href="original.dag.json">Original DAG JSON</a> · <a href="original.txt">Full chain and definitions</a></p>'
              '<h3>Original query dependency view</h3><div class="dag-diagram">'+_svg(data['original_dag'],data['original_dag']['query_time_node_ids'])+'</div>'
              '<p>This is static lineage. The full chain download retains refresh lineage, unknowns and diagnostics.</p><h2>Proposals for this measure</h2>'
              +(''.join(items) if items else '<p>No automatic rewrite is available in the supported subset. This does not establish that the measure is optimal.</p>')
              +('<ul>'+diagnostics+'</ul>' if diagnostics else '')
              +'<h2>Validate before applying</h2><p>Choose a proposal and download its total validation query. Run it against the same loaded model in Power BI Desktop’s DAX query view or a compatible query client. The query defines a temporary candidate; it does not update your model. An Equal value of 1 checks only this scalar total, including BLANK flags. An all-BLANK result is insufficient evidence. Test the actual grouped, filtered and total contexts, then benchmark separately.</p></main></html>')
        (root/'report.html').write_text(page,encoding='utf-8')
        return {'html':root/'report.html','json':root/'measure-review.json'}


def _validation_query(table, name, expression, alias_name):
    original=Entity('measure',table,name).qualified_name
    alias=Entity('measure',table,alias_name).qualified_name
    return (f'DEFINE MEASURE {alias} = {expression}\nEVALUATE\nROW(\n'
            f'    "Original", {original},\n    "Candidate", {alias},\n'
            f'    "OriginalBlank", ISBLANK({original}),\n    "CandidateBlank", ISBLANK({alias}),\n'
            f'    "Equal", IF(ISBLANK({original}) == ISBLANK({alias}) && {original} == {alias}, 1, 0)\n)\n')


def review_measure(source, measure, *, table=None, analysis=None, progress=None):
    """Select exactly one root for proposals; include helpers only as lineage.

    Reusing an analysis requires its same unmodified model instance. No measure
    expression is changed, and no correctness or performance claim is made.
    """
    model=source if isinstance(source,Model) else parse_model(source) if isinstance(source,dict) else load_model(source)
    owner=model.measure(measure,table)
    if analysis is not None and analysis.model is not model:
        raise ModelError('Reused analysis must belong to the same unmodified model instance')
    emit(progress,'measure','Review selected measure',detail=owner.qualified_name)
    graph=analysis.graph if analysis is not None else DependencyGraph(model,progress=progress)
    if analysis is not None and analysis.rewrite_plan is not None:
        row=next((r for r in analysis.rewrite_plan.measures if r['measure_id']==owner.id),None)
        if row is None:row=plan_rewrites(model,graph=graph,measure=owner.id,progress=progress).measures[0]
    else:
        row=plan_rewrites(model,graph=graph,measure=owner.id,progress=progress).measures[0]
    original=computation_dag(model,owner.id,graph=graph)
    candidates=copy.deepcopy(row['candidates'])
    for c in candidates:
        c['title']='; '.join(dict.fromkeys(RULES[s['rule']]['title'] for s in c['steps']))
        if c['combined']:c['title']='Combined proposal: '+c['title']
    diagnostics=list(row['diagnostics'])
    if not candidates and not diagnostics:
        diagnostics.append('No guarded rule matched this root expression. Helpers are inspected as dependencies, not automatically rewritten. No timing or optimality conclusion follows.')
    alias='__sh_focus_'+hashlib.sha256(owner.id.encode()).hexdigest()[:20]
    names={e.name.casefold() for e in model.entities.values()}
    while alias.casefold() in names:alias+='x'
    return MeasureReview({'schema_version':'1.0','kind':'single-measure-review',
        'measure_id':owner.id,'measure':owner.qualified_name,'table':owner.table,'name':owner.name,
        'scope':'root-expression-only','expression':owner.expression,'original_dag':original,
        'diagnostics':diagnostics,'candidates':candidates,'validation_alias':alias,
        'affected_downstream_measures':list(row['affected_downstream_measures']),
        'validation_status':'not_run','performance_status':'unknown'})


def write_measure_proposal(model, measure, expression, directory, *, graph=None):
    """Export a user-authored comparison, DAX and a scalar-total probe only."""
    owner=model.measure(measure)
    comparison=compare_measure_dags(model,owner.id,proposed_expression=expression,graph=graph)
    root=Path(directory);files=write_dag_comparison(comparison,root)
    (root/'proposed.dax').write_text(expression,encoding='utf-8')
    alias='__sh_focus_custom'
    names={e.name.casefold() for e in model.entities.values()}
    while alias.casefold() in names:alias+='x'
    (root/'validate-total.dax').write_text(_validation_query(owner.table,owner.name,expression,alias),encoding='utf-8')
    return files
