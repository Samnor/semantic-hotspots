"""Review single-root measure proposals without a matrix or source-model edits."""
from __future__ import annotations

import copy
import hashlib
import html
import json
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .comparison import (DAG_CSS, _svg, compare_measure_dags, computation_dag,
                         dag_to_text, write_dag_comparison)
from .graph import DependencyGraph
from .model import Entity, Model, ModelError
from .optimizer import RULES, plan_rewrites
from .parser import load_model, parse_model
from .progress import emit
from .date_validation import date_controls, query_definitions
from .date_validation import validation_dates as parse_validation_dates


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
        landing=data.get('landing_visual');landings=data.get('landing_visuals',[])
        (root/'visual-contexts.json').write_text(json.dumps(landings,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        if landing:
            (root/'landing-visual.json').write_text(json.dumps(landing,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        for c in data['candidates']:
            folder=root/'candidates'/c['id']
            write_dag_comparison(c['dag_comparison'],folder)
            (folder/'proposed.dax').write_text(c['after'],encoding='utf-8')
            available=c.get('validation_available',True) and not (data.get('date_controls') or {}).get('blocked_reasons')
            if available:
                _write_validation_files(folder,data['table'],data['name'],c['after'],data['validation_alias'],landings,data.get('date_controls'))
            else:
                (folder/'validation-requirements.json').write_text(json.dumps({'pending_helpers':c.get('pending_helpers',[]),
                    'dependency_issues':data['original_dag']['dependency_issues'],'date_controls':data.get('date_controls')},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
            url='candidates/'+c['id']+'/'
            context_link=(' · <a href="'+url+'visual-context-queries.zip" download>Visual context queries</a>') if landings and available else ''
            validation_link=(' · <a href="'+url+'validate-total.dax" download>Total validation query</a>') if available else ' · Validation needs actual helper definitions or date controls'
            items.append('<article><h3>'+esc(c['title'])+'</h3><p>Correctness: not tested · Performance: unknown</p><pre>'+esc(c['after'])+'</pre><p><a href="'+url+'report.html">Compare original and proposed DAGs</a> · <a href="'+url+'proposed.dax" download>Download proposed DAX</a>'+validation_link+context_link+'</p></article>')
        diagnostics=''.join('<li>'+esc(d)+'</li>' for d in data['diagnostics'])
        page=('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
              '<title>Optimize one measure</title><style>'+_CSS+DAG_CSS+'</style><main><h1>Optimize one measure</h1><h2>'+esc(data['measure'])+'</h2>'
              '<p>Only this measure’s proposed expression changes. Supported improved helper paths may be specialized into this root; original helper definitions and source tables remain unchanged. No source model is edited and no DAX is executed.</p>'
              +('<h3>Optional landing visual: '+esc(landing['title'])+'</h3><p>Its axes, filters and totals are retained; displayed values target this measure only. Queries are representative contexts, not the exact rendered visual query.</p><p><a href="landing-visual.json">Landing context and grain review</a></p>' if landing else '<p>'+str(len(landings))+' optional visual contexts selected. <a href="visual-contexts.json">Inspect contexts</a></p>' if landings else '<p>No landing visual selected. Measure review remains independent of matrices.</p>')+
              '<h3>Original DAX</h3><pre>'+esc(data['expression'] or 'Definition missing')+'</pre><p><a href="original.dax" download>Original DAX</a> · <a href="original.dag.json">Original DAG JSON</a> · <a href="original.txt">Full chain and definitions</a></p>'
              '<h3>Original query dependency view</h3><div class="dag-diagram">'+_svg(data['original_dag'],data['original_dag']['query_time_node_ids'])+'</div>'
              '<p>This is static lineage. The full chain download retains refresh lineage, unknowns and diagnostics.</p><h2>Proposals for this measure</h2>'
              +(''.join(items) if items else '<p>No automatic rewrite is available in the supported subset. This does not establish that the measure is optimal.</p>')
              +('<ul>'+diagnostics+'</ul>' if diagnostics else '')
              +'<h2>Validate before applying</h2><p>Choose a proposal and download its total validation query. Run it against the same loaded model in Power BI Desktop’s DAX query view or a compatible query client. The query defines a temporary candidate; it does not update your model. An Equal value of 1 checks only this scalar total, including BLANK flags. An all-BLANK result is insufficient evidence. Test the actual grouped, filtered and total contexts, then benchmark separately.</p></main></html>')
        (root/'report.html').write_text(page,encoding='utf-8')
        return {'html':root/'report.html','json':root/'measure-review.json'}


def _validation_query(table, name, expression, alias_name, workload=None, controls=None, as_of=None):
    original=Entity('measure',table,name).qualified_name
    alias=Entity('measure',table,alias_name).qualified_name
    fields=(f'"Original", {original},\n    "Candidate", {alias},\n'
            f'    "OriginalBlank", ISBLANK({original}),\n    "CandidateBlank", ISBLANK({alias}),\n'
            f'    "Equal", IF(ISBLANK({original}) == ISBLANK({alias}) && {original} == {alias}, 1, 0)')
    if workload is not None:
        marker='"Value_1", '+original
        if workload['query'].count(marker)!=1:raise ModelError('Unexpected landing query projection')
        query=workload['query'].replace(marker,fields)
    else:query='EVALUATE\nROW(\n    '+fields+'\n)\n'
    return query_definitions(table,name,expression,alias_name,controls,as_of)+query


def _write_landing_queries(folder, table, name, expression, alias, landing, controls=None, as_of=None):
    if not landing or not landing['workloads']:return
    target=folder/'landing-queries';target.mkdir()
    index=[]
    original=Entity('measure',table,name).qualified_name
    candidate=Entity('measure',table,alias).qualified_name
    for workload in landing['workloads']:
        marker='"Value_1", '+original
        if workload['query'].count(marker)!=1:raise ModelError('Unexpected landing query projection')
        files={}
        definitions=query_definitions(table,name,expression,alias,controls,as_of)
        queries={'validation':_validation_query(table,name,expression,alias,workload,controls,as_of),
                 'baseline':definitions+workload['query'] if controls else workload['query'],
                 'candidate':definitions+workload['query'].replace(marker,'"Value_1", '+candidate)}
        for kind,query in queries.items():
            filename=workload['id']+'-'+kind+'.dax';files[kind]=filename
            (target/filename).write_text(query,encoding='utf-8')
        index.append({'scenario':workload['scenario'],'group_by':workload['group_by'],'filters':workload['filters'],'queries':files})
    (target/'contexts.json').write_text(json.dumps({'spec':landing['spec'],'cases':index,
        'validation_status':'not_run','performance_status':'unknown',
        'as_of':(as_of or controls['dates'][0]) if controls else None,
        'scope':'Representative prefix contexts. Not an exact reproduction of the rendered visual.'},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    with zipfile.ZipFile(folder/'landing-queries.zip','w',zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(target.iterdir()):archive.write(path,path.name)


def _write_visual_queries(folder, table, name, expression, alias, landings, controls=None, as_of=None):
    if not landings:return None
    contexts=folder/'visual-contexts';contexts.mkdir()
    index=[]
    for i,landing in enumerate(landings,1):
        target=contexts/str(i);target.mkdir()
        (target/'context.json').write_text(json.dumps(landing,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        _write_landing_queries(target,table,name,expression,alias,landing,controls,as_of)
        index.append({'matrix_id':landing['matrix_id'],'directory':str(i),'query_count':len(landing['workloads']),
                      'diagnostics':landing['diagnostics']})
    (contexts/'index.json').write_text(json.dumps({'visuals':index,'validation_status':'not_run',
        'scope':'Each visual is a separate context; filters are not merged.'},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    archive=folder/'visual-context-queries.zip'
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:
        for path in sorted(contexts.rglob('*')):
            if path.is_file() and path.suffix!='.zip':z.write(path,path.relative_to(contexts).as_posix())
    if len(landings)==1:_write_landing_queries(folder,table,name,expression,alias,landings[0],controls,as_of)
    return archive


def _write_validation_files(folder,table,name,expression,alias,landings,controls=None):
    (folder/'validate-total.dax').write_text(_validation_query(table,name,expression,alias,controls=controls),encoding='utf-8')
    _write_visual_queries(folder,table,name,expression,alias,landings,controls)
    if controls:
        (folder/'date-controls.json').write_text(json.dumps(controls,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        for as_of in controls['dates']:
            dated=folder/'validation-dates'/as_of;dated.mkdir(parents=True)
            (dated/'validate-total.dax').write_text(_validation_query(table,name,expression,alias,controls=controls,as_of=as_of),encoding='utf-8')
            _write_visual_queries(dated,table,name,expression,alias,landings,controls,as_of)


def _landing_context(model, owner, spec, analysis, progress):
    from .matrices import MatrixSpec, analyze_matrices, measure_landing_spec
    from .project import _select_analysis
    source_spec=spec if isinstance(spec,MatrixSpec) else MatrixSpec.from_dict(spec)
    scoped=measure_landing_spec(model,owner.id,source_spec)
    context=analyze_matrices(model,[scoped],analysis=analysis,progress=progress)
    try:
        _select_analysis(model,context,scoped.id);eligible=True;reason=None
    except ModelError as exc:eligible=False;reason=str(exc)
    context_row=context.matrices[0]
    landing={'matrix_id':scoped.id,'title':scoped.title,'original_values':copy.deepcopy(source_spec.values),
             'spec':scoped.to_dict(),'diagnostics':context_row['diagnostics'],
             'grain_advice':context_row['grain_advice'],'summary_export':{'eligible':eligible,'reason':reason},
             'workloads':context.workloads if scoped.context_complete else [],'warnings':context.warnings}
    if not scoped.context_complete:
        landing['diagnostics'].append('Review the landing visual context before generating its validation queries.')
    return landing


def review_measure(source, measure, *, table=None, analysis=None, progress=None, landing_matrix=None, landing_matrices=None, validation_dates=None):
    """Select one output root, optionally specializing improved helper paths.

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
        elif any(model.entities[n].kind=='measure' for n in graph.dependency_ids(owner.id)):
            # Whole-model scans retain bounded local proposals. A selected root
            # also gets a bounded attempt to specialize improved helper paths.
            row=plan_rewrites(model,graph=graph,measure=owner.id,progress=progress).measures[0]
    else:
        row=plan_rewrites(model,graph=graph,measure=owner.id,progress=progress).measures[0]
    original=computation_dag(model,owner.id,graph=graph)
    if landing_matrix is not None and landing_matrices is not None:
        raise ModelError('Supply landing_matrix or landing_matrices, not both')
    specs=landing_matrices if landing_matrices is not None else [landing_matrix] if landing_matrix is not None else []
    if not isinstance(specs,(list,tuple)):raise ModelError('Visual contexts must be a list of matrix specifications')
    landings=[_landing_context(model,owner,spec,analysis,progress) for spec in specs]
    if len({c['matrix_id'] for c in landings})!=len(landings):raise ModelError('Select each visual context once')
    landing=landings[0] if len(landings)==1 else None
    candidates=copy.deepcopy(row['candidates'])
    for c in candidates:
        c['title']='; '.join(dict.fromkeys(RULES[s['rule']]['title'] for s in c['steps']))
        if c['combined']:c['title']='Combined proposal: '+c['title']
        if landings:c['dag_comparison']['context']={'landing_visuals':[c['spec'] for c in landings],'selected_measure_id':owner.id}
    diagnostics=list(row['diagnostics'])
    if not candidates and not diagnostics:
        diagnostics.append('No guarded rule matched this root or its supported helper paths. No timing or optimality conclusion follows.')
    alias='__sh_focus_'+hashlib.sha256(owner.id.encode()).hexdigest()[:20]
    names={e.name.casefold() for e in model.entities.values()}
    while alias.casefold() in names:alias+='x'
    return MeasureReview({'schema_version':'1.0','kind':'single-measure-review',
        'measure_id':owner.id,'measure':owner.qualified_name,'table':owner.table,'name':owner.name,
        'scope':'root-expression-only','expression':owner.expression,'original_dag':original,
        'diagnostics':diagnostics,'candidates':candidates,'validation_alias':alias,
        'validation_dates':parse_validation_dates(validation_dates),
        'date_controls':date_controls(model,owner,dates=validation_dates,graph=graph),
        'affected_downstream_measures':list(row['affected_downstream_measures']),
        'validation_status':'not_run','performance_status':'unknown','landing_visual':landing,'landing_visuals':landings})


def write_measure_proposal(model, measure, expression, directory, *, graph=None, landing_visual=None, landing_visuals=None, validation_dates=None):
    """Export a user-authored comparison, DAX and a scalar-total probe only."""
    owner=model.measure(measure)
    comparison=compare_measure_dags(model,owner.id,proposed_expression=expression,graph=graph)
    landings=landing_visuals if landing_visuals is not None else [landing_visual] if landing_visual else []
    if landings:comparison['context']={'landing_visuals':[c['spec'] for c in landings],'selected_measure_id':owner.id}
    root=Path(directory);files=write_dag_comparison(comparison,root)
    (root/'proposed.dax').write_text(expression,encoding='utf-8')
    alias='__sh_focus_custom'
    names={e.name.casefold() for e in model.entities.values()}
    while alias.casefold() in names:alias+='x'
    controls=date_controls(model,owner,expression,dates=validation_dates,graph=graph)
    valid=all(comparison[p]['dependency_complete'] for p in ['original','proposed']) and not (controls or {}).get('blocked_reasons')
    files['validation']=None
    if valid:
        files['validation']=root/'validate-total.dax'
        _write_validation_files(root,owner.table,owner.name,expression,alias,landings,controls)
    reasons=[issue['message'] for p in ['original','proposed'] for issue in comparison[p].get('dependency_issues',[])]+(controls or {}).get('blocked_reasons',[])
    files['validation_reason']=None if valid else 'Validation queries withheld: '+' '.join(dict.fromkeys(reasons))
    return files
