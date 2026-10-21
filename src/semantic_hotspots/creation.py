"""Create one new, named measure from one root and optional visual contexts."""
from __future__ import annotations

import copy
import html
import json
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .comparison import compare_measure_dags, write_dag_comparison
from .focus import review_measure, _validation_query, _write_visual_queries
from .matrices import MatrixSpec
from .model import Entity, Model, ModelError
from .parser import load_model, parse_model
from .progress import emit


def _visuals_for_measure(model, owner, visuals):
    if visuals is None:return []
    if not isinstance(visuals,(list,tuple)):raise ModelError('Visual contexts must be a list')
    specs=[v if isinstance(v,MatrixSpec) else MatrixSpec.from_dict(v) for v in visuals]
    if len({s.id for s in specs})!=len(specs):raise ModelError('Select each visual context once')
    for spec in specs:
        displayed=[]
        for table,name in spec.values:
            try:displayed.append(model.measure(name,table).id)
            except ModelError:continue
        if owner.id not in displayed:raise ModelError('Every selected visual must directly display the selected measure')
    return specs


def _new_name(model, owner):
    # A portable Desktop formula name, unique across all existing object names.
    base=re.sub(r'''[\[\]=\r\n\t"'.,;:/\\]''',' ',owner.name)
    base=' '.join(base.split()) or 'Measure'
    base+=' (candidate)';name=base;number=2
    used={e.name.casefold() for e in model.entities.values()}
    while name.casefold() in used:name=f'{base} {number}';number+=1
    return name


def _preview_query(table, name, expression):
    qualified=Entity('measure',table,name).qualified_name
    return ('// DAX Studio / Power BI DAX query view: scalar preview only.\n'
            '// Defines a temporary measure for this query; does not save it in the model.\n'
            '// Not a correctness comparison or a benchmark. Run against the source model.\n'
            'DEFINE\n    MEASURE '+qualified+' =\n'+expression+'\n\n'
            'EVALUATE\nROW("Value", '+qualified+')\n')


@dataclass
class CreatedMeasure:
    data: dict

    def write(self, directory):
        root=Path(directory)
        if root.exists() and (not root.is_dir() or any(root.iterdir())):
            raise ModelError('New measure output must be absent or empty')
        root.mkdir(parents=True,exist_ok=True)
        d=self.data
        (root/'new-measure.dax').write_text(d['definition'],encoding='utf-8')
        (root/'preview-query.dax').write_text(_preview_query(d['table'],d['name'],d['expression']),encoding='utf-8')
        (root/'expression.dax').write_text(d['expression'],encoding='utf-8')
        (root/'HOW-TO-USE.txt').write_text(
            'Power BI Desktop measure\n'
            'Choose New measure in table '+d['table']+' and paste new-measure.dax.\n'
            'This file contains a measure definition, not a runnable DAX query.\n\n'
            'DAX Studio / Power BI DAX query view\n'
            'Connect to the source model, open preview-query.dax and run it.\n'
            'It contains DEFINE MEASURE and EVALUATE and returns one scalar total.\n'
            'The temporary measure exists only for this query; the model is not updated.\n'
            'This preview has not been executed or validated. It may fail if definitions or model context are missing.\n'
            'A returned value does not establish equivalence or improved performance.\n\n'
            'Validation queries, when available, are separate files and remain subject to the reported guards.\n',encoding='utf-8')
        (root/'measure.json').write_text(json.dumps(d['measure_object'],ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        (root/'created-measure.json').write_text(json.dumps(d,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
        write_dag_comparison(d['dag_comparison'],root/'dags')
        if d['validation_available']:
            (root/'validate-total.dax').write_text(_validation_query(d['table'],d['original_name'],d['expression'],d['name']),encoding='utf-8')
            _write_visual_queries(root,d['table'],d['original_name'],d['expression'],d['name'],d['visual_contexts'])
        elif d['visual_contexts']:
            (root/'visual-contexts.json').write_text(json.dumps(d['visual_contexts'],ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        esc=lambda x:html.escape(str(x),quote=True)
        links='<a href="new-measure.dax" download>Download Power BI measure</a> · <a href="preview-query.dax" download>Download DAX Studio query (preview)</a> · <a href="dags/report.html">Original and new measure DAGs</a>'
        if d['validation_available']:links+=' · <a href="validate-total.dax" download>Total validation query</a>'
        if (root/'visual-context-queries.zip').is_file():links+=' · <a href="visual-context-queries.zip" download>Visual contexts and queries</a>'
        (root/'report.html').write_text('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>New measure</title><style>body{font:16px/1.6 system-ui;margin:40px auto;padding:0 20px;max-width:950px;color:#18333d}pre{background:#f2f6f5;padding:20px;white-space:pre-wrap;overflow-wrap:anywhere}a{color:#087568}</style><h1>'+esc(d['name'])+'</h1><p>'+esc(d['explanation'])+'</p><pre>'+esc(d['definition'])+'</pre><p>'+links+'</p><p>Create a new measure in table '+esc(d['table'])+' and paste the definition above. The original model and helper definitions are unchanged.</p><p>For DAX Studio or Power BI DAX query view, use preview-query.dax. The measure definition alone is not a query. This preview returns one scalar total without saving a measure; it is not a correctness comparison or benchmark.</p><p>'+str(len(d['visual_contexts']))+' selected visual contexts. Their filters remain separate and are not baked into the new expression. Helpers remain dependencies in the same model.</p><p>Correctness: not tested · Performance: unknown</p><ul>'+''.join('<li>'+esc(w)+'</li>' for w in d['diagnostics'])+'</ul>',encoding='utf-8')
        with zipfile.ZipFile(root/'new-measure.zip','w',zipfile.ZIP_DEFLATED) as z:
            for path in sorted(root.rglob('*')):
                if path.is_file() and path.name!='new-measure.zip':z.write(path,path.relative_to(root).as_posix())
        return {'html':root/'report.html','json':root/'created-measure.json','dax':root/'new-measure.dax','query':root/'preview-query.dax','zip':root/'new-measure.zip'}


def _create_from_review(model, review, *, progress=None):
    d=review.data;owner=model.measure(d['measure_id'])
    if not owner.expression or not owner.expression.strip():raise ModelError('The selected measure has no expression to create from; supply its definition first')
    emit(progress,'create-measure','Create new measure',detail=owner.qualified_name)
    candidates=d['candidates']
    # Prefer the bounded combination; this is rule coverage, never a timing estimate.
    chosen=max(candidates,key=lambda c:(c['combined'],len(c['steps']),c['id'])) if candidates else None
    expression=chosen['after'] if chosen else owner.expression
    created=Entity('measure',owner.table,_new_name(model,owner),expression,metadata=copy.deepcopy(owner.metadata))
    other=copy.deepcopy(model);other.add(created)
    comparison=compare_measure_dags(model,owner.id,proposed_source=other,proposed_measure=created.id,
                                    graph=None)
    contexts=copy.deepcopy(d.get('landing_visuals',[]))
    comparison['context']={'selected_measure_id':owner.id,'visuals':[c['spec'] for c in contexts]}
    diagnostics=list(d['diagnostics'])
    if any(e.kind=='format_string' and e.metadata.get('for_measure')==owner.id for e in model.entities.values()):
        diagnostics.append('The original dynamic format expression is not copied; review formatting separately.')
    complete=comparison['original']['dependency_complete'] and comparison['proposed']['dependency_complete']
    blockers=comparison['original'].get('dependency_issues',[])
    if not complete:
        for issue in blockers:
            if issue['message'] not in diagnostics:diagnostics.append(issue['message'])
        diagnostics.append('Validation queries withheld because reference binding, definitions, or dependency order are not fully established. See the specific blockers above.')
    explanation = ('Created a new measure with '+str(len(chosen['steps']))+(' guarded simplification' if len(chosen['steps'])==1 else ' guarded simplifications')+'. Validate its results and performance before use.' if chosen else 'Created an unchanged copy. No supported guarded rewrite matched; no performance improvement is claimed.')
    reason_kind = ('rewritten' if chosen else 'incomplete_dependencies' if not complete else
                   'unsupported_syntax_or_semantics' if not comparison['original']['logical_subset_validated'] else
                   'guard_blocked' if any(not message.startswith('No guarded rule matched') for message in d['diagnostics']) else 'no_matching_rule')
    if reason_kind=='incomplete_dependencies':
        explanation='Created an unchanged copy. Dependency analysis could not establish every reference, definition, or dependency order. See the specific blockers below; no performance improvement is claimed.'
    elif reason_kind=='unsupported_syntax_or_semantics':
        explanation='Created an unchanged copy. The formula uses syntax or operations outside automatic rewrite coverage. See the reasons below; no performance improvement is claimed.'
    elif reason_kind=='guard_blocked':
        explanation='Created an unchanged copy. Automatic rewriting was withheld by a safety check. See the reasons below; no performance improvement is claimed.'
    comparison['creation'] = {'outcome':'rewritten' if chosen else 'unchanged_copy',
                              'explanation':explanation,'reason_kind':reason_kind,'reasons':diagnostics}
    measure_object={'name':created.name,'expression':expression}
    if owner.metadata.get('format_string'):measure_object['formatString']=owner.metadata['format_string']
    return CreatedMeasure({'schema_version':'1.0','kind':'created-measure','original_measure_id':owner.id,
        'original_name':owner.name,'measure_id':created.id,'name':created.name,'table':created.table,
        'expression':expression,'definition':created.name+' =\n'+expression+'\n','measure_object':measure_object,
        'outcome':'rewritten' if chosen else 'unchanged_copy','selected_candidate_id':chosen['id'] if chosen else None,
        'selection_basis':'Bounded combined guarded rewrite, otherwise an isolated guarded rewrite; no timing-based selection.',
        'explanation':explanation,'reason_kind':reason_kind,
        'steps':copy.deepcopy(chosen['steps']) if chosen else [],'visual_contexts':contexts,
        'dag_comparison':comparison,'diagnostics':diagnostics,'dependency_issues':blockers,'validation_available':complete,
        'validation_status':'not_run','performance_status':'unknown'})


def create_measure(source, measure, *, table=None, visuals=None, analysis=None, progress=None):
    """Create one separately named candidate, never changing the source model.

    Visuals must directly display this measure. They supply independent workload
    contexts, not filters to hard-code or evidence that a rewrite is faster.
    """
    model=source if isinstance(source,Model) else parse_model(source) if isinstance(source,dict) else load_model(source)
    owner=model.measure(measure,table)
    specs=_visuals_for_measure(model,owner,visuals)
    review=review_measure(model,owner.id,analysis=analysis,progress=progress,landing_matrices=specs)
    return _create_from_review(model,review,progress=progress)
