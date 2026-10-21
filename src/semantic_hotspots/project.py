"""Portable, one-visual PBIP showcases of guarded summary-table candidates.

Generated artifacts are experiments. No Desktop/engine validation, source model
mutation, deployment or performance improvement is implied by exporting them.
"""
from __future__ import annotations

import base64
import copy
import gzip
import hashlib
import html
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .comparison import compare_measure_dags, write_dag_comparison, render_dag_comparison, DAG_CSS
from .graph import DependencyGraph
from .matrices import analyze_matrices, _column
from .model import Entity, Model, ModelError
from .optimizer import logical_plan, _plain, _type
from .parser import load_model, parse_model
from .simulation import SimulationConfig, simulate, _project, _m_string, _file_hash

_BASE = 'https://developer.microsoft.com/json-schemas/'
_SCHEMAS = {
    'project': 'fabric/pbip/pbipProperties/1.0.0/schema.json',
    'pbism': 'fabric/item/semanticModel/definitionProperties/1.0.0/schema.json',
    'pbir': 'fabric/item/report/definitionProperties/2.0.0/schema.json',
    'version': 'fabric/item/report/definition/versionMetadata/1.0.0/schema.json',
    'report': 'fabric/item/report/definition/report/3.0.0/schema.json',
    'pages': 'fabric/item/report/definition/pagesMetadata/1.0.0/schema.json',
    'page': 'fabric/item/report/definition/page/2.0.0/schema.json',
    'visual': 'fabric/item/report/definition/visualContainer/2.0.0/schema.json',
}
_TYPES = {'int64': 'int64', 'double': 'double', 'decimal': 'decimal',
          'string': 'string', 'boolean': 'boolean', 'datetime': 'dateTime'}
_EMBED_LIMIT = 32 * 1024 * 1024


def _json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')


def _doc(kind, **data):
    return {'$schema': _BASE+_SCHEMAS[kind], **data}


def _fresh_name(proposed, existing):
    names={n.casefold() for n in existing}; name=proposed; suffix=2
    while name.casefold() in names:
        name=f'{proposed} {suffix}'; suffix+=1
    return name


def _quoted(value):
    return '"'+value.replace('"','""')+'"'


def _selection(model, matrices, matrix_id):
    analysis=analyze_matrices(model,matrices)
    return _select_analysis(model,analysis,matrix_id)


def _select_analysis(model, analysis, matrix_id):
    """Apply the same export guards to an already computed matrix analysis."""
    rejected=[]
    for row in analysis.matrices:
        if matrix_id is not None and row['id']!=matrix_id:continue
        spec=row['spec']; candidates=row['grain_advice']['candidates']
        reasons=list(row['diagnostics'])
        if not spec['context_complete']:reasons.append('A complete matrix contract is required.')
        if spec['filters'] or spec['filter_measures'] or spec['drill_states']:
            reasons.append('This initial PBIR exporter requires unfiltered axes and default drill state; filters/states are not silently dropped.')
        if not all(spec[k] for k in ['row_subtotals','column_subtotals','grand_total']):
            reasons.append('Non-default subtotal/total settings need a supported PBIR translation.')
        if len(candidates)!=1:reasons.append('Exactly one supported fact-summary candidate is required.')
        for candidate in candidates:
            reasons+=candidate['blockers']
            fact=candidate['fact_table']
            context=spec['rows']+spec['columns']+spec['retain']
            if any(p[0].casefold()==fact.casefold() for p in context):
                reasons.append('Fact-column axes/retained filters need remapping; paired measures cannot share that context safely.')
            if not candidate['relationship_preserving_grain']:
                reasons.append('This exporter requires at least one retained grouping key.')
            if any(_column(model,p) is None or _type(_column(model,p)) not in _TYPES for p in candidate['relationship_preserving_grain']):
                reasons.append('Summary grouping-key types must be known and supported.')
        if not reasons and row['chain_complete']:
            return analysis,row,candidates[0],rejected
        rejected.append({'matrix_id':row['id'],'reasons':list(dict.fromkeys(reasons or ['Incomplete measure chain.']))})
    if matrix_id is not None and not rejected:raise ModelError('Requested matrix ID was not found')
    reason='; '.join(f'{r["matrix_id"]}: '+ ' '.join(r['reasons']) for r in rejected)
    raise ModelError('No guarded one-visual showcase can be exported. '+reason)


def _summary(model, row, candidate):
    """Build a refresh-time DAX summary and recomposed scalar measures."""
    fact=candidate['fact_table']; keys=candidate['relationship_preserving_grain']
    name=_fresh_name('Showcase Summary', model.tables)
    columns=[]; used=[]; key_names={}
    def column(label,typ):
        label=_fresh_name(label,used); used.append(label)
        if typ not in _TYPES:raise ModelError('Summary column type is unsupported or unknown')
        columns.append({'name':label,'dataType':_TYPES[typ],'type':'calculatedTableColumn',
                        'sourceColumn':'['+label.replace(']',']]')+']','isHidden':True})
        return label
    for pair in keys:
        source=_column(model,pair)
        key_names[source.id]=column(source.name,_type(source))
        # Grouping fields retain their physical table qualifier in the result
        # schema. Binding them as extension fields ([Key]) loses the column
        # during Desktop loading and invalidates the summary relationship.
        columns[-1]['sourceColumn']=source.table+'['+source.name.replace(']',']]')+']'
    states={}; expressions=[]; bindings=[]
    for state in candidate['component_states']:
        op=state['function']; entity=model.entities[state['target']]
        if op=='DISTINCTCOUNT':
            if entity.id not in key_names:raise ModelError('Distinct key was not retained')
            states[(op,entity.id)]=Entity('column',name,key_names[entity.id]).qualified_name
            continue
        typ='int64' if op in {'COUNT','COUNTROWS'} else _type(entity)
        label=column('State '+str(len(bindings)+1),typ)
        states[(op,entity.id)]=Entity('column',name,label).qualified_name
        expressions += [_quoted(label), state['expression']]
        bindings.append({'column':label,'function':op,'source':entity.qualified_name})
    # Retain groups even when every requested aggregate is BLANK.
    row_label=column('Source rows','int64')
    expressions += [_quoted(row_label), 'COUNTROWS('+Entity('table',fact,'').qualified_name+')']
    group_columns=[_column(model,p).qualified_name for p in keys]
    args=[Entity('table',fact,'').qualified_name]+group_columns+expressions
    summarized='SUMMARIZE(\n    '+',\n    '.join(args)+'\n)'
    # SUMMARIZE preserves original grouping-column names. Rename only aggregate
    # state collisions; generated state names never collide with retained keys.
    table={'name':name,'isHidden':True,'columns':columns,'partitions':[{'name':'Summary candidate','mode':'import',
           'source':{'type':'calculated','expression':summarized}}]}
    cache={}; active=set()
    def measure(identity):
        if identity in cache:return cache[identity]
        if identity in active:raise ModelError('Cyclic scalar recipe')
        active.add(identity); owner=model.entities[identity]
        tree,_=logical_plan(owner.expression or '',model,owner)
        text=render(tree,owner);active.remove(identity);cache[identity]=text;return text
    def render(node,owner):
        node=_plain(node); op=node.operation
        if op=='measure':return '('+measure(node.target)+')'
        if op=='literal' and node.result_type=='number':return node.value
        if op=='unary':return '('+node.value+render(node.children[0],owner)+')'
        if op=='binary' and node.value in {'+','-','*','/'}:
            return '('+render(node.children[0],owner)+' '+node.value+' '+render(node.children[1],owner)+')'
        if op=='DIVIDE' and len(node.children) in {2,3}:
            return 'DIVIDE('+', '.join(render(c,owner) for c in node.children)+')'
        if op=='SUMX':op='SUM';target=_plain(node.children[1]).target
        elif op in {'SUM','MIN','MAX','COUNT','COUNTROWS','AVERAGE','DISTINCTCOUNT'}:target=_plain(node.children[0]).target
        else:raise ModelError('Scalar recipe needs unsupported context: '+owner.qualified_name)
        if op=='AVERAGE':return 'DIVIDE(SUM('+states[('SUM',target)]+'), SUM('+states[('COUNT',target)]+'))'
        if (op,target) not in states:raise ModelError('Scalar recipe lacks an aggregation state')
        aggregate=op if op in {'MIN','MAX','DISTINCTCOUNT'} else 'SUM'
        return aggregate+'('+states[(op,target)]+')'
    measure_names=[e.name for e in model.measures]; changes=[]
    for identity in row['value_measure_ids']:
        owner=model.entities[identity]
        label=_fresh_name('Candidate '+owner.name,measure_names);measure_names.append(label)
        result={'name':label,'expression':measure(identity),'displayFolder':'Showcase candidates',
                'description':'Unvalidated summary-table candidate for the declared showcase context only.'}
        metadata={k.casefold():v for k,v in owner.metadata.items()}
        fmt=metadata.get('formatstring',metadata.get('format_string'))
        if isinstance(fmt,str):result['formatString']=fmt
        changes.append({'measure_id':identity,'table':owner.table,'original_name':owner.name,
                        'before':owner.expression,'candidate':result})
    paths=list({hop['relationship']:hop for path in candidate['filter_paths'] for hop in path[:1]}.values())
    links=[]
    for i,hop in enumerate(paths):
        child=_column(model,[hop['from_table'],hop['from_column']]);parent=_column(model,[hop['to_table'],hop['to_column']])
        if not child or not parent:raise ModelError('Summary relationship endpoint is missing')
        links.append({'name':_fresh_name('Showcase summary link '+str(i+1),[r.name for r in model.relationships]),
                      'fromTable':name,'fromColumn':key_names[child.id], 'toTable':parent.table,'toColumn':parent.name,
                      'fromCardinality':'many','toCardinality':'one','crossFilteringBehavior':'oneDirection','isActive':True})
    return table,links,changes,bindings,row_label


def _projection(kind,pair,label):
    return {'field':{kind:{'Expression':{'SourceRef':{'Entity':pair[0]}},'Property':pair[1]}},
            'queryRef':pair[0]+'.'+pair[1],'nativeQueryRef':label,'displayName':label,'active':True}


def _literal(value):
    return {'expr':{'Literal':{'Value':"'"+value.replace("'","''")+"'"}}}


def _report(root,spec,changes):
    report=root/'Showcase.Report'; definition=report/'definition'; page='Showcase'; visual='Comparison'
    _json(root/'Showcase.pbip',_doc('project',version='1.0',artifacts=[{'report':{'path':'Showcase.Report'}}]))
    _json(report/'definition.pbir',_doc('pbir',version='4.0',datasetReference={'byPath':{'path':'../Showcase.SemanticModel'}}))
    _json(root/'Showcase.SemanticModel/definition.pbism',_doc('pbism',version='1.0',settings={'qnaEnabled':False}))
    _json(definition/'version.json',_doc('version',version='2.0.0'))
    _json(definition/'report.json',_doc('report',themeCollection={}))
    _json(definition/'pages/pages.json',_doc('pages',pageOrder=[page],activePageName=page))
    _json(definition/f'pages/{page}/page.json',_doc('page',name=page,displayName='Measure showcase',displayOption='FitToWidth',width=1280,height=720))
    roles={}
    for role,key in [('Rows','rows'),('Columns','columns')]:
        if spec[key]:roles[role]={'projections':[_projection('Column',p,p[1]) for p in spec[key]]}
    values=[]
    for change in changes:
        values += [_projection('Measure',[change['table'],change['original_name']],'Baseline · '+change['original_name']),
                   _projection('Measure',[change['table'],change['candidate']['name']],'Candidate · '+change['original_name'])]
    roles['Values']={'projections':values}
    body={'visualType':'pivotTable','query':{'queryState':roles},
          'objects':{'columnHeaders':[{'properties':{'autoSizeColumnWidth':{'expr':{'Literal':{'Value':'true'}}},
                                                  'columnAdjustment':_literal('growToFit')}}]},
          'visualContainerObjects':{'title':[{'properties':{'show':{'expr':{'Literal':{'Value':'true'}}},'text':_literal(spec['title'])}}],
                                    'subTitle':[{'properties':{'show':{'expr':{'Literal':{'Value':'true'}}},
                                               'text':_literal('Baseline vs summary candidate · synthetic data · validation and timing not run')}}]}}
    _json(definition/f'pages/{page}/visuals/{visual}/visual.json',_doc('visual',name=visual,
          position={'x':24,'y':24,'z':0,'width':1232,'height':672,'tabOrder':0},visual=body))


def _embed(bim,bundle):
    bim['model']['defaultPowerBIDataSourceVersion']='powerBI_V3'
    tables={t['name']:t for t in bim['model']['tables']}; size=0
    for entry in bundle.manifest['tables']:
        if entry['kind']!='synthetic-physical':continue
        path=bundle.directory/entry['csv'];size+=path.stat().st_size
        if size>_EMBED_LIMIT:raise ModelError('Embedded showcase data exceeds 32 MiB; reduce the synthetic row counts')
        value=base64.b64encode(gzip.compress(path.read_bytes(),mtime=0)).decode('ascii')
        partition=tables[entry['name']]['partitions'][0]
        expected='File.Contents(SyntheticDataRoot & "/" & '+_m_string(path.name)+')'
        expression=partition['source']['expression']
        if expression.count(expected)!=1:raise ModelError('Unexpected synthetic CSV partition expression')
        partition['source']['expression']=expression.replace(expected,
            'Binary.Decompress(Binary.FromText('+_m_string(value)+', BinaryEncoding.Base64), Compression.GZip)')
    bim['model'].pop('expressions',None)
    return size


def _comparison_queries(group_by,changes):
    groups=[Entity('column',p[0],p[1]).qualified_name for p in group_by]
    projections={'baseline':[],'candidate':[],'validation':[]}
    for i,change in enumerate(changes,1):
        before=Entity('measure',change['table'],change['original_name']).qualified_name
        after=Entity('measure',change['table'],change['candidate']['name']).qualified_name
        projections['baseline'].append(f'"Value_{i}", {before}')
        projections['candidate'].append(f'"Value_{i}", {after}')
        projections['validation'] += [f'"Original_{i}", {before}',f'"Candidate_{i}", {after}',
            f'"Equal_{i}", IF(ISBLANK({before}) == ISBLANK({after}) && {before} == {after}, 1, 0)',
            f'"OriginalBlank_{i}", IF(ISBLANK({before}), 1, 0)',f'"CandidateBlank_{i}", IF(ISBLANK({after}), 1, 0)']
    result={}
    for kind,fields in projections.items():
        expression=('SUMMARIZECOLUMNS('+', '.join(groups+fields)+')') if groups else 'ROW('+', '.join(fields)+')'
        result[kind]='EVALUATE\n'+expression+'\n'
    return result


def _queries(root,analysis,row,changes,summary,fact):
    results=[]
    for workload in analysis.workloads:
        if workload['matrix_id']!=row['id']:continue
        files={}
        for kind,query in _comparison_queries(workload['group_by'],changes).items():
            filename=f'queries/{workload["id"]}-{kind}.dax'
            (root/filename).parent.mkdir(exist_ok=True);(root/filename).write_text(query,encoding='utf-8',newline='\n')
            files[kind]={'file':filename,'sha256':hashlib.sha256(query.encode()).hexdigest()}
        results.append({'scenario':workload['scenario'],'group_by':workload['group_by'],'queries':files})
    counts='EVALUATE\nROW("DetailRows", COUNTROWS('+Entity('table',fact,'').qualified_name+'), "SummaryRows", COUNTROWS('+Entity('table',summary,'').qualified_name+'))\n'
    (root/'queries/row-counts.dax').write_text(counts,encoding='utf-8',newline='\n')
    return results


@dataclass
class ProjectExport:
    directory: Path
    manifest: dict

    @property
    def project_path(self):return self.directory/'Showcase.pbip'

    @property
    def model_path(self):return self.directory/'Showcase.SemanticModel/model.bim'


def export_project(source,matrices,directory,*,matrix_id=None,config=None):
    """Export exactly one comparison matrix and a new mock-data Import model.

    Only a complete, unfiltered, default-drill contract with one supported fact
    recipe is eligible. Original objects remain intact in the isolated copy;
    selected candidate measures read a refresh-time DAX summary. No automatic
    aggregation mappings or production deployment are created.
    """
    model=source if isinstance(source,Model) else parse_model(source) if isinstance(source,dict) else load_model(source)
    analysis,row,candidate,rejected=_selection(model,matrices,matrix_id)
    for key in ['rows','columns','retain']:
        row['spec'][key]=[[_column(model,p).table,_column(model,p).name] for p in row['spec'][key]]
    table,links,changes,bindings,row_label=_summary(model,row,candidate)
    raw=_project(model)
    needed={model.entities[n].table for n in row['chain_node_ids']}
    needed.update(_column(model,p).table for p in row['spec']['rows']+row['spec']['columns']+row['spec']['retain'])
    needed.update(hop['to_table'] for path in candidate['filter_paths'] for hop in path)
    chain=set(row['chain_node_ids']);relation_names={hop['relationship'] for path in candidate['filter_paths'] for hop in path}
    raw['tables']=[t for t in raw['tables'] if t['name'] in needed]
    for obj in raw['tables']:
        obj['columns']=[c for c in obj.get('columns',[]) if not c.get('expression')]
        obj['measures']=[m for m in obj.get('measures',[]) if Entity('measure',obj['name'],m['name']).id in chain]
    raw['relationships']=[r for r in raw.get('relationships',[]) if r['name'] in relation_names]
    # Use the metadata projection, excluding original sources/roles/identity.
    projected={'name':'Showcase source','compatibilityLevel':1601,'model':raw}
    settings=copy.deepcopy(config.__dict__ if isinstance(config,SimulationConfig) else {} if config is None else config)
    settings=SimulationConfig.from_dict(settings)
    if not isinstance(settings.tables,dict):raise ModelError('Showcase tables config must be an object')
    if settings.matrices or settings.workloads:
        raise ModelError('Pass the matrix contract separately; showcase config does not accept extra matrices/workloads')
    settings.matrices=[];settings.workloads=[];settings.data_root=r'C:\showcase\simulation\tables'
    if set(settings.tables)-needed:raise ModelError('Showcase config contains tables outside the selected matrix chain/context')
    destination=Path(directory).resolve()
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise ModelError('Project output must be absent or empty; existing files are not overwritten')
    destination.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.showcase-',dir=destination.parent) as temporary:
        root=Path(temporary)/'project';root.mkdir()
        bundle=simulate(projected,root/'simulation',config=settings)
        bim=json.loads(bundle.model_path.read_text(encoding='utf-8'))
        embedded=_embed(bim,bundle)
        bim['name']='Showcase';bim['model']['tables'].append(table)
        for change in changes:
            owner=next(t for t in bim['model']['tables'] if t['name']==change['table'])
            owner.setdefault('measures',[]).append(change['candidate'])
        bim['model'].setdefault('relationships',[]).extend(links)
        # Verify generated bindings before committing output. This is a static
        # graph check, not Desktop, refresh, result or type validation.
        generated=parse_model(bim);graph=DependencyGraph(generated)
        for change in changes:
            identity=generated.measure(change['candidate']['name'],change['table']).id
            nodes=[identity]+graph.dependency_ids(identity)
            if any(n in graph.blocked or (n in graph.syntax and graph.syntax[n].unresolved) for n in nodes):
                raise ModelError('Generated candidate has unresolved/cyclic dependencies')
        _json(root/'Showcase.SemanticModel/model.bim',bim)
        _report(root,row['spec'],changes)
        cases=_queries(root,analysis,row,changes,table['name'],candidate['fact_table'])
        manifest={'schema_version':'1.0','kind':'power-bi-showcase-project','matrix_id':row['id'],
                  'visual_count':1,'page_count':1,'strategy':'refresh_time_relationship_key_summary',
                  'spec':row['spec'],'summary_table':table['name'],'source_fact':candidate['fact_table'],
                  'grain':candidate['relationship_preserving_grain'],'component_bindings':bindings,
                  'row_count_column':row_label,'added_relationships':links,'changes':changes,'skipped_matrices':rejected,
                  'embedded_csv_bytes':embedded,'seed':settings.seed,'cases':cases,
                  'model_sha256':_file_hash(root/'Showcase.SemanticModel/model.bim'),
                  'desktop_validation':'not_run','engine_validation':'not_run','performance_status':'unknown',
                  'predicted_speedup':None,'warnings':[
                      'Candidate measures are experimental and restricted to the declared dimension/filter contract.',
                      'The summary is materialized on refresh and adds model size/refresh work; it does not prove query speedup.',
                      'Both original detail and summary remain for comparison. No automatic aggregation mappings are configured.',
                      'The combined showcase visual runs both paths; use separate baseline/candidate queries for A/B timing.',
                      'Refresh the project, inspect nonblank results, then compare exact values and BLANK flags at every generated context.',
                      'Floating-point regrouping can change results. Synthetic tests do not establish production/RLS equivalence.',
                      'The PBIR layout uses default hierarchy/total behavior, not the captured original visual query.',
                      'Desktop opening, refresh and DAX execution have not been tested by this exporter.']}
        comparisons = []
        for change in changes:
            comparison = compare_measure_dags(model, change['measure_id'], proposed_source=generated,
                                              proposed_measure=change['candidate']['name'], proposed_table=change['table'])
            comparison['context'] = {'matrix': row['spec'], 'summary_grain': manifest['grain'],
                                     'added_relationships': links}
            folder = 'dag-comparisons/' + hashlib.sha256(change['measure_id'].encode()).hexdigest()[:20]
            write_dag_comparison(comparison, root/folder)
            comparisons.append((change, comparison, folder))
        manifest['dag_comparisons'] = [{'measure_id': c['measure_id'], 'directory': folder,
                                        'original_root': d['original']['root'], 'proposed_root': d['proposed']['root']}
                                       for c, d, folder in comparisons]
        _json(root/'showcase.json',manifest)
        _json(root/'matrix-specs.json',{'schema_version':'1.0','matrices':[row['spec']]})
        instructions=('POWER BI SHOWCASE\n\n1. Open Showcase.pbip in a current Power BI Desktop on Windows with PBIP/PBIR support.\n'
                      '2. Refresh the model. Synthetic CSV data is embedded in the project M partitions; no external sources or path edits are needed.\n'
                      '3. Inspect the single matrix: Baseline and Candidate values share the same dimension context. This is an unvalidated candidate, not a measured improvement.\n'
                      '4. Launch python -m semantic_hotspots studio for guided generation/validation, or run showcase-run on this project with an explicit local server/database. Use the Windows [engine] extra and engine-setup for the official client.\n'
                      '   py -m semantic_hotspots showcase-run . --server localhost:PORT --database DATABASE\n'
                      '   For manual inspection, run each queries/*-validation.dax: all Equal flags must be 1, BLANK flags must match, and meaningful nonblank baseline values must exist for every measure pair.\n'
                      '   py -m semantic_hotspots query queries/QUERY-validation.dax --server localhost:PORT --database DATABASE -o validation.csv\n'
                      '5. Only after correctness checks, time the separate baseline/candidate queries on the same model/data/hardware and cache settings. The combined visual is not a speed comparison.\n'
                      '   Use query --benchmark --warm 5 for each baseline/candidate file with separate -o CSV paths. Timings include client result reading and are not server CPU/FE/SE.\n'
                      '6. Run queries/row-counts.dax for actual engine row counts. Summary refresh/memory overhead and unsupported future slicers must be evaluated separately.\n'
                      '7. Read showcase.json for the exact summary grain, DAX changes, query fingerprints and limitations.\n\n'
                      'The simulation subdirectory is a separate inspectable mock-data bundle. Its file-based BIM is not the embedded-data Showcase model.\n'
                      'PBIX packaging/saving requires Power BI Desktop. The library does not deploy, publish, overwrite the source, or claim engine validation.\n')
        (root/'OPEN-IN-DESKTOP.txt').write_text(instructions,encoding='utf-8')
        from .report import _CSS
        change_html=''.join('<details><summary>'+html.escape(c['original_name'])+' · original and proposed DAGs</summary>'+render_dag_comparison(d)+'<p><a href="'+folder+'/report.html">Open comparison and download DAGs</a></p></details>' for c,d,folder in comparisons)
        warnings=''.join('<li>'+html.escape(w)+'</li>' for w in manifest['warnings'])
        (root/'showcase.html').write_text(f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Power BI showcase</title><style>{_CSS}{DAG_CSS}</style></head><body><main><h1>One matrix. Two computation paths.</h1><p>{html.escape(row["title"])}</p><p>One page, one matrix, {len(changes)} baseline/candidate measure pairs. Engine validation: not run. Performance: unknown.</p><h2>New summary grain</h2><pre>{html.escape(json.dumps(manifest["grain"]))}</pre><p>This refresh-time summary retains relationship keys and component states. It adds memory and refresh work.</p><h2>Inspect the changes</h2>{change_html}<h2>Open and validate</h2><pre>{html.escape(instructions)}</pre><ul>{warnings}</ul><p><a href="showcase.json">Full manifest</a> · <a href="OPEN-IN-DESKTOP.txt">Desktop instructions</a> · <a href="Showcase.pbip">PBIP entry point (download the whole project)</a></p></main></body></html>',encoding='utf-8')
        # Stage all generation away from the destination, then commit atomically.
        if destination.exists():destination.rmdir()
        os.replace(root,destination)
    return ProjectExport(destination,manifest)
