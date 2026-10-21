"""Matrix context, conservative aggregation advice and representative DAX workloads.

No aggregate table is deployed or measure rewritten. A specified matrix is a
filter/drill contract, not a substitute for the actual Power BI visual query.
"""
from __future__ import annotations

import copy
import hashlib
import json
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .dax import ITERATORS, Resolver
from .model import Model, ModelError, object_id
from .optimizer import logical_plan, _plain, _physical, _type, _NUMERIC
from .parser import load_model, parse_model


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _pairs(value, name):
    if not isinstance(value, list) or any(not isinstance(p, (list, tuple)) or len(p) != 2
                                         or not all(isinstance(x, str) and x for x in p) for p in value):
        raise ModelError(f'{name} must contain [table, name] pairs')
    result = [list(p) for p in value]
    if len({tuple(x.casefold() for x in p) for p in result}) != len(result):
        raise ModelError(f'{name} contains duplicate fields')
    return result


@dataclass
class MatrixSpec:
    id: str
    title: str = ''
    rows: list = field(default_factory=list)
    columns: list = field(default_factory=list)
    values: list = field(default_factory=list)
    filters: list = field(default_factory=list)
    filter_measures: list = field(default_factory=list)
    retain: list = field(default_factory=list)
    drill_states: list = field(default_factory=list)
    row_subtotals: bool = True
    column_subtotals: bool = True
    grand_total: bool = True
    context_complete: bool = False
    unsupported: list = field(default_factory=list)
    source: str = ''
    report_name: str = ''
    page_name: str = ''
    visual_id: str = ''

    @classmethod
    def from_dict(cls, raw):
        if not isinstance(raw, dict) or set(raw)-set(cls.__dataclass_fields__):
            raise ModelError('Matrix has unknown settings or is not an object')
        try:
            spec = cls(**copy.deepcopy(raw))
        except TypeError as exc:
            raise ModelError('Matrix needs an id and valid settings') from exc
        if not isinstance(spec.id, str) or not spec.id or not isinstance(spec.title, str):
            raise ModelError('Matrix needs a nonempty string id and string title')
        spec.title = spec.title or spec.id
        for key in ['rows','columns','values','filter_measures','retain']:
            setattr(spec, key, _pairs(getattr(spec,key), key))
        if len(spec.rows)+len(spec.columns) > 16:
            raise ModelError('At most 16 matrix axis fields are supported')
        _pairs(spec.rows+spec.columns, 'combined matrix axes')
        for key in ['row_subtotals','column_subtotals','grand_total','context_complete']:
            if not isinstance(getattr(spec,key),bool):
                raise ModelError(f'{key} must be Boolean')
        if not isinstance(spec.unsupported,list) or not all(isinstance(x,str) for x in spec.unsupported) or not isinstance(spec.source,str):
            raise ModelError('Matrix unsupported/source metadata is malformed')
        if any(not isinstance(getattr(spec,key),str) for key in ['report_name','page_name','visual_id']):
            raise ModelError('Matrix report, page and visual labels must be strings')
        _filters(spec.filters)
        if not isinstance(spec.drill_states,list):
            raise ModelError('drill_states must be a list')
        names = set()
        for state in spec.drill_states:
            if (not isinstance(state,dict) or set(state)-{'name','row_depth','column_depth','filters'}
                    or not isinstance(state.get('name'),str) or not state['name'] or state['name'] in names):
                raise ModelError('Drill states need unique names and optional depths/filters')
            names.add(state['name'])
            for key, count in [('row_depth',len(spec.rows)),('column_depth',len(spec.columns))]:
                depth=state.get(key,count)
                if isinstance(depth,bool) or not isinstance(depth,int) or not 0 <= depth <= count:
                    raise ModelError('Matrix drill depth is outside the configured hierarchy')
            _filters(state.get('filters',[]))
            _filters(spec.filters+state.get('filters',[]))
        return spec

    def to_dict(self):
        return asdict(self)


def _filters(filters):
    if not isinstance(filters,list):
        raise ModelError('Matrix filters must be a list')
    seen=set()
    for f in filters:
        if (not isinstance(f,dict) or set(f)!={'table','column','values'} or not isinstance(f['table'],str)
                or not isinstance(f['column'],str) or not isinstance(f['values'],list) or not f['values']):
            raise ModelError('Matrix filters need table, column and a nonempty values list')
        key=(f['table'].casefold(),f['column'].casefold())
        if key in seen:raise ModelError('Use one enumerated filter per column in each matrix context')
        seen.add(key)
        # Validate finite scalar literals; temporal conversion uses model types later.
        from .simulation import _dax_literal
        for value in f['values']:
            _dax_literal(value)


def _pbir_field(field,kind):
    if not isinstance(field,dict) or not isinstance(field.get(kind),dict):return None
    value=field[kind];expression=value.get('Expression')
    if not isinstance(expression,dict) or not isinstance(expression.get('SourceRef'),dict):return None
    entity=expression['SourceRef'].get('Entity');prop=value.get('Property')
    return [entity,prop] if isinstance(entity,str) and isinstance(prop,str) else None


def _pbir_column(field):
    return _pbir_field(field,'Column')


def _pbir_measure(field):
    return _pbir_field(field,'Measure')


def _pbir_query(visual):
    query=visual.get('query',{})
    if not isinstance(query,dict) or not isinstance(query.get('queryState',{}),dict):
        raise ModelError('Malformed PBIR query/queryState object')
    roles=query.get('queryState',{})
    for role in roles.values():
        if not isinstance(role,dict) or not isinstance(role.get('projections',[]),list):
            raise ModelError('Malformed PBIR query role/projections')
        if any(not isinstance(p,dict) for p in role.get('projections',[])):
            raise ModelError('Malformed PBIR projection object')
    return query,roles


def _pbir_has_filters(raw):
    if not isinstance(raw,dict) or not isinstance(raw.get('filterConfig',{}),dict):
        raise ModelError('Malformed PBIR filter metadata')
    filters=raw.get('filterConfig',{}).get('filters',[])
    if not isinstance(filters,list):raise ModelError('Malformed PBIR filter list')
    return bool(filters)


def _pbir_title(visual):
    """Read a literal title only; never evaluate a dynamic title expression."""
    objects=visual.get('visualContainerObjects')
    titles=objects.get('title') if isinstance(objects,dict) else None
    if not isinstance(titles,list):return ''
    for title in titles:
        properties=title.get('properties') if isinstance(title,dict) else None
        show=properties.get('show') if isinstance(properties,dict) else None
        for key in ['expr','Literal','Value']:
            show=show.get(key) if isinstance(show,dict) else None
        if show=='false':continue
        value=properties.get('text') if isinstance(properties,dict) else None
        for key in ['expr','Literal','Value']:
            value=value.get(key) if isinstance(value,dict) else None
        if isinstance(value,str) and len(value)>=2 and value.startswith("'") and value.endswith("'"):
            return value[1:-1].replace("''", "'").strip()
    return ''


def _read_pbir(path):
    if path.is_file():
        visual_paths=[path]
        report_root=None
    else:
        definition=path/'definition' if (path/'definition/pages').is_dir() else path
        if not (definition/'pages').is_dir():
            raise ModelError('Select an explicit PBIR .Report/definition folder, visual.json, or matrix-spec JSON')
        visual_paths=sorted((definition/'pages').glob('*/visuals/*/visual.json'))
        report_root=definition
    records=[]
    slicers=[]
    for file in visual_paths:
        raw=json.loads(file.read_text(encoding='utf-8-sig'))
        if not isinstance(raw,dict) or not isinstance(raw.get('visual',{}),dict):
            raise ModelError('Malformed PBIR visual object')
        visual=raw.get('visual',{})
        if visual.get('visualType') == 'slicer':
            _,roles=_pbir_query(visual)
            for role in roles.values():
                for projection in role.get('projections',[]):
                    col=_pbir_column(projection.get('field'))
                    if col and col not in slicers:slicers.append(col)
        if visual.get('visualType') != 'pivotTable':
            continue
        page_folder=file.parents[2] if file.parent.parent.name=='visuals' else None
        page=page_folder.name if page_folder else 'page'
        page_metadata=page_folder/'page.json' if page_folder else None
        page_label=page
        if page_metadata and page_metadata.is_file():
            metadata=json.loads(page_metadata.read_text(encoding='utf-8-sig'))
            if isinstance(metadata,dict) and isinstance(metadata.get('displayName'),str):
                page_label=metadata['displayName'] or page
        report_folder=report_root.parent if report_root and report_root.name=='definition' else report_root
        visual_id=str(raw.get('name',file.parent.name))
        spec={'id':page+'/'+visual_id, 'title':_pbir_title(visual) or visual_id,
              'report_name':report_folder.name.removesuffix('.Report') if report_folder else '',
              'page_name':page_label if page_folder else '', 'visual_id':visual_id,
              'rows':[], 'columns':[], 'values':[], 'source':str(file), 'unsupported':[], 'context_complete':False}
        query,roles=_pbir_query(visual)
        for role, target in [('Rows','rows'),('Columns','columns'),('Values','values')]:
            for projection in roles.get(role,{}).get('projections',[]):
                field_value=projection.get('field')
                pair=_pbir_measure(field_value) if target=='values' else _pbir_column(field_value)
                if pair:spec[target].append(pair)
                else:spec['unsupported'].append(f'PBIR {role} projection needs manual binding (implicit aggregate, hierarchy, parameter or visual calculation).')
        if _pbir_has_filters(raw):
            spec['unsupported'].append('PBIR visual filters are not translated; supply explicit filters or the captured visual query.')
        if set(roles)-{'Rows','Columns','Values'}:
            spec['unsupported'].append('Additional PBIR query roles are not interpreted.')
        if any(k in query for k in ['sortDefinition','expansionStates']):
            spec['unsupported'].append('PBIR sort/expansion behavior is not reproduced by representative workloads.')
        if report_root:
            for metadata in [report_root/'report.json',file.parents[2]/'page.json']:
                if metadata.is_file() and _pbir_has_filters(json.loads(metadata.read_text(encoding='utf-8-sig'))):
                    spec['unsupported'].append('Report/page filters require explicit translation; their absence is not assumed.')
        records.append(spec)
    for spec in records:
        spec['retain']=copy.deepcopy(slicers)
    if not records:
        raise ModelError('No pivotTable matrices found in the selected PBIR input')
    return records


def load_matrices(source):
    """Read explicit specs or common PBIR bindings, preserving unsupported scope."""
    if isinstance(source,(str,Path)):
        path=Path(source)
        if path.is_dir():
            raw=_read_pbir(path)
        else:
            data=json.loads(path.read_text(encoding='utf-8-sig'))
            raw=_read_pbir(path) if isinstance(data,dict) and 'visual' in data else data
    else:
        raw=copy.deepcopy(source)
    if isinstance(raw,dict):
        if set(raw)-{'schema_version','matrices'} or raw.get('schema_version','1.0')!='1.0':
            raise ModelError('Unsupported matrix specification envelope')
        raw=raw.get('matrices')
    if not isinstance(raw,list) or not raw:
        raise ModelError('Expected a nonempty list of matrix specifications')
    specs=[MatrixSpec.from_dict(asdict(v) if isinstance(v,MatrixSpec) else v) for v in raw]
    if len({s.id for s in specs})!=len(specs):
        raise ModelError('Matrix IDs must be unique')
    return specs


def _column(model,pair):
    matches=[e for e in model.entities.values() if e.kind=='column' and e.table.casefold()==pair[0].casefold() and e.name.casefold()==pair[1].casefold()]
    return matches[0] if len(matches)==1 else None


def _unique(pairs):
    return list(dict.fromkeys(tuple(p) for p in pairs))


def _relationship_paths(model, fact, dimension):
    """All simple active regular many-to-one paths from fact towards dimensions."""
    if fact==dimension:return [[]]
    canonical={t.casefold():t for t in model.tables}
    edges={}
    for r in model.relationships:
        if not r.active:continue
        if (r.from_cardinality.casefold(),r.to_cardinality.casefold())==('many','one'):
            child,parent,child_col,parent_col=r.from_table,r.to_table,r.from_column,r.to_column
        elif (r.from_cardinality.casefold(),r.to_cardinality.casefold())==('one','many'):
            child,parent,child_col,parent_col=r.to_table,r.from_table,r.to_column,r.from_column
        else:continue
        if r.cross_filter.casefold() not in {'onedirection','single'}:continue
        child=canonical.get(child.casefold(),child);parent=canonical.get(parent.casefold(),parent)
        edges.setdefault(child,[]).append((parent,child_col,parent_col,r.name))
    pending=deque([(fact,[],{fact})]); paths=[]
    while pending:
        table,path,seen=pending.popleft()
        for parent,child_col,parent_col,name in edges.get(table,[]):
            if parent in seen:continue
            extended=path+[{'from_table':table,'from_column':child_col,'to_table':parent,'to_column':parent_col,'relationship':name}]
            if parent==dimension:
                paths.append(extended)
                if len(paths)>1:return paths
            elif len(extended)<32:
                pending.append((parent,extended,seen|{parent}))
                if len(pending)>4096:return [[],[]]  # Ambiguous/too complex; never guess a path.
    return paths


def _components(model, graph, roots):
    """Recognize recomposable scalar expressions; all other contexts block advice."""
    components={}; blockers=[]; active=set(); memo={}
    def visit_measure(identity):
        if identity in memo:return memo[identity]
        if identity in active:
            blockers.append('Cyclic measure chain has no safe aggregation recipe.');return False
        active.add(identity)
        owner=model.entities[identity]
        if not owner.expression:
            blockers.append(f'{owner.qualified_name}: definition missing.');ok=False
        else:
            tree,_=logical_plan(owner.expression,model,owner)
            ok=visit(tree,owner)
        active.remove(identity);memo[identity]=ok;return ok
    def state(op,node):
        components[(op,node.target)]={'function':op,'target':node.target,'table':node.table,
                                      'column':model.entities[node.target].name if node.operation=='column' else None,
                                      'expression':f'{op}({model.entities[node.target].qualified_name})'}
    def visit(node,owner):
        node=_plain(node);op=node.operation
        if op=='measure':return visit_measure(node.target)
        if op=='literal' and node.result_type=='number':return True
        if op=='unary':return visit(node.children[0],owner)
        if op=='binary' and node.value in {'+','-','*','/'}:
            values=[visit(c,owner) for c in node.children];return all(values)
        if op=='DIVIDE' and len(node.children) in {2,3}:
            values=[visit(c,owner) for c in node.children];return all(values)
        if op in {'SUM','MIN','MAX','AVERAGE','COUNT','DISTINCTCOUNT'} and len(node.children)==1:
            column=_plain(node.children[0])
            if (column.operation=='column' and _physical(column,model)
                    and (op=='DISTINCTCOUNT' or column.result_type in _NUMERIC)):
                if op=='AVERAGE':state('SUM',column);state('COUNT',column)
                else:state(op,column)
                return True
        if op=='COUNTROWS' and len(node.children)==1:
            table=_plain(node.children[0])
            if table.operation=='scan' and _physical(table,model):state(op,table);return True
        if op=='SUMX' and len(node.children)==2:
            table,column=map(_plain,node.children)
            if (table.operation=='scan' and column.operation=='column' and table.table==column.table
                    and column.result_type in _NUMERIC and _physical(table,model) and _physical(column,model)):
                state('SUM',column);return True
        blockers.append(f'{owner.qualified_name}: {op} needs context/grain review; no automatic aggregation recipe.')
        return False
    for root in roots:visit_measure(root)
    # Even when a parent is opaque, its supported leaf states can guide manual
    # review. Parent blockers remain, so this never authorizes a rewrite/probe.
    for root in roots:
        for identity in graph.dependency_ids(root):
            if model.entities[identity].kind=='measure':visit_measure(identity)
    return list(components.values()),list(dict.fromkeys(blockers))


def _grain_advice(model,analysis,spec,roots,context_columns):
    graph=analysis.graph
    components,blockers=_components(model,graph,roots)
    chain=set()
    for root in roots:chain.update([root]+graph.dependency_ids(root))
    iterators=[];sensitive_columns=[]
    for identity in sorted(chain):
        info=graph.syntax.get(identity)
        if not info:continue
        for ref in info.references:
            entity=model.entities[ref.target]
            if entity.kind=='column' and (not ref.contexts or ref.contexts[-1] not in {'SUM','MIN','MAX','AVERAGE','COUNT'}):
                sensitive_columns.append([entity.table,entity.name])
        for call in info.calls:
            if call.name in ITERATORS and call.name not in {'FILTER'}:
                iterators.append({'measure':model.entities[identity].qualified_name,'function':call.name,
                                  'input':call.argument(model.entities[identity].expression or '',0),
                                  'advice':'Retain the iteration key until its row-dependent calculation is complete. A grouped/source intermediate before this iterator requires equivalence tests.'})
        if info.unresolved or identity in graph.blocked:
            blockers.append('Unresolved or cyclic dependencies prevent a complete grain contract.')
    context_columns=[list(p) for p in _unique(context_columns+sensitive_columns)]
    if any(e.kind in {'calculation_item','function','format_string'} for e in model.entities.values()):
        blockers.append('Calculation groups, model functions or dynamic formats need runtime-context validation.')
    if spec.filter_measures:
        blockers.append('Measure-filter predicate/grain is unspecified; evaluate the actual visual filter before discarding detail.')
    blockers.extend(spec.unsupported)
    if not spec.context_complete:
        blockers.append('Filter/drill contract is not declared complete; additional slicers, interactions or security may require finer keys.')
    for warning in model.warnings:
        if any(s in warning.casefold() for s in ['rls','roles/','partial measure','permission']):
            blockers.append('Security or incomplete model metadata requires additional grain review.');break
    by_fact={}
    for component in components:by_fact.setdefault(component['table'],[]).append(component)
    candidates=[]
    if len(by_fact)>1:
        blockers.append('Multiple fact tables: build separate summaries and validate their combination; do not join facts at a guessed grain.')
    for fact,states in by_fact.items():
        reasons=[];keys=[];paths=[]
        for pair in context_columns:
            column=_column(model,pair)
            if not column:
                reasons.append(f'Unbound context column {pair}.');continue
            if column.table==fact:
                keys.append(pair);continue
            options=_relationship_paths(model,fact,column.table)
            if len(options)!=1 or not options[0]:
                reasons.append(f'No unique active regular filter path from {column.table} to {fact}.');continue
            path=options[0];paths.append(path)
            keys.append([path[0]['from_table'],path[0]['from_column']])
        for state in states:
            if state['function']=='DISTINCTCOUNT':keys.append([fact,state['column']])
        keys=[list(p) for p in _unique(keys)]
        if any(_column(model,k) is None for k in keys):reasons.append('Relationship endpoint columns are missing from model metadata.')
        # Complex relationships touching this candidate may add unseen filtering paths.
        if any(r.active and
               (r.cross_filter.casefold() not in {'onedirection','single'} or r.from_cardinality.casefold()==r.to_cardinality.casefold())
               for r in model.relationships):
            reasons.append('The model contains active complex relationship paths whose irrelevance to this summary is not established.')
        retained_distinct=[[fact,s['column']] for s in states if s['function']=='DISTINCTCOUNT']
        instructions=[]
        for state in states:
            function=state['function']
            if function=='DISTINCTCOUNT':instructions.append('Retain the distinct key and recompute DISTINCTCOUNT after filtering; never sum subgroup distinct counts.')
            elif function in {'COUNT','COUNTROWS'}:instructions.append(f'Store {state["expression"]}; roll counts up with SUM, preserving empty/BLANK behavior.')
            else:instructions.append(f'Store {state["expression"]}; roll up with {function}.')
        if any('AVERAGE' in (graph.syntax.get(n).functions if graph.syntax.get(n) else {}) for n in chain):
            instructions.append('Recompute AVERAGE from SUM and nonblank numeric COUNT; never average subgroup averages.')
        instructions.append('Re-evaluate scalar arithmetic/ratios after combining component states; test detail, subtotals, grand totals and blanks.')
        candidate_blockers=list(dict.fromkeys(blockers+reasons))
        probe=None
        if not candidate_blockers:
            filters=_filter_dax(model,spec.filters)
            base=model.entities[object_id('table',fact)].qualified_name
            key_dax=[_column(model,p).qualified_name for p in keys]
            grouped=f'COALESCE(COUNTROWS(SUMMARIZE({base}, '+', '.join(key_dax)+')), 0)' if key_dax else f'IF(COALESCE(COUNTROWS({base}), 0) > 0, 1, 0)'
            query=f'ROW("SourceRows", COALESCE(COUNTROWS({base}), 0), "GroupedRows", {grouped})'
            probe='EVALUATE\n'+(f'CALCULATETABLE({query}, '+', '.join(filters)+')' if filters else query)+'\n'
        candidates.append({'fact_table':fact,'status':'guarded_candidate_pending_engine_validation' if not candidate_blockers else 'needs_review',
                           'relationship_preserving_grain':keys,'filter_paths':paths,
                           'coarser_matrix_grain':[list(p) for p in _unique(context_columns+retained_distinct)],
                           'coarser_grain_note':'Exploratory matrix/filter grain. Dropping relationship keys requires explicit dimension remapping, filter/security coverage and equivalence tests.',
                           'component_states':states,'rollup_instructions':list(dict.fromkeys(instructions)),
                           'blockers':candidate_blockers,'retained_expression_columns':[list(p) for p in _unique(sensitive_columns)],'group_count_probe':probe,
                           'probe_scope':'Matrix base filters only; evaluate drill-state filters separately.',
                           'source_rows':None,'grouped_rows':None,'predicted_speedup':None,
                           'earliest_stage':'Aggregate physical rows into these states before recomposable scalar arithmetic. Keep row-dependent predicates/iteration keys until their calculations finish.'})
    return {'candidates':candidates,'blockers':list(dict.fromkeys(blockers)), 'iterator_review':iterators,
            'interpretation':'Grain candidates are unvalidated advice. Group-count probes measure distinct combinations, not Cartesian products or engine scans. No tables are created.'}


def _filter_dax(model,filters):
    from .simulation import _column_type,_dax_literal
    clauses=[]
    for f in filters:
        col=_column(model,[f['table'],f['column']])
        if not col:raise ModelError('Unbound matrix filter column')
        clauses.append('TREATAS({'+', '.join(_dax_literal(v,_column_type(col,{})) for v in f['values'])+'}, '+col.qualified_name+')')
    return clauses


def measure_landing_spec(model, measure, matrix):
    """Copy a visual contract, replacing only its displayed value measures."""
    owner=model.measure(measure)
    spec=matrix if isinstance(matrix,MatrixSpec) else MatrixSpec.from_dict(matrix)
    raw=spec.to_dict()
    raw['values']=[[owner.table,owner.name]]
    return MatrixSpec.from_dict(raw)


def _workloads(model,spec,measure_ids,fingerprint):
    results=[]
    states=spec.drill_states or [{'name':'expanded','row_depth':len(spec.rows),'column_depth':len(spec.columns)}]
    for state in states:
        rd,cd=state.get('row_depth',len(spec.rows)),state.get('column_depth',len(spec.columns))
        row_depths=list(range(rd+1)) if spec.row_subtotals else [rd]
        col_depths=list(range(cd+1)) if spec.column_subtotals else [cd]
        levels=list(dict.fromkeys([(r,c) for r in row_depths for c in col_depths]+([(0,0)] if spec.grand_total else [])))
        if not spec.grand_total and (rd or cd):levels=[p for p in levels if p!=(0,0)]
        filters=spec.filters+state.get('filters',[])
        clauses=_filter_dax(model,filters)
        for r,c in levels:
            groups=spec.rows[:r]+spec.columns[:c]
            projection=[f'"Value_{i+1}", {model.entities[n].qualified_name}' for i,n in enumerate(measure_ids)]
            args=[_column(model,p).qualified_name for p in groups]+clauses+projection
            query='EVALUATE\nSUMMARIZECOLUMNS(\n    '+',\n    '.join(args)+'\n)\n'
            if not groups and not clauses:query='EVALUATE\nROW('+', '.join(projection)+')\n'
            scenario=f'{state["name"]}: rows={r}, columns={c}'
            identity=_hash([spec.to_dict(),scenario,query,fingerprint])[:20]
            results.append({'id':identity,'matrix_id':spec.id,'measure_id':object_id('matrix','',spec.id),
                            'measure_ids':list(measure_ids),'measure':spec.title,'scenario':scenario,
                            'row_depth':r,'column_depth':c,'group_by':groups,'filters':copy.deepcopy(filters),
                            'query':query,'query_file':'queries/matrix-'+identity+'.dax',
                            'context_coverage':'declared complete' if spec.context_complete else 'partial/representative',
                            'interpretation':'One multi-measure prefix-context query. Not an exact reproduction of matrix rendering, TopN, visual filters or ROLLUPADDISSUBTOTAL flags.'})
            if len(results)>256:raise ModelError('Matrix workload expansion exceeds 256 contexts; specify fewer drill states/levels')
    return results


@dataclass
class MatrixAnalysis:
    matrices: list[dict]
    workloads: list[dict]
    model_name: str
    warnings: list[str]
    rank_by: str = 'maximum_known_measure_review_priority'
    dependency_graph: dict = field(default_factory=dict)
    benchmark_context: dict = field(default_factory=dict)

    def to_dict(self):
        return {'schema_version':'1.0','kind':'matrix-grain-analysis','model':self.model_name,
                'rank_by':self.rank_by,'warnings':self.warnings,'matrices':self.matrices,'workloads':self.workloads,
                'dependency_graph':self.dependency_graph,'benchmark_context':self.benchmark_context}

    def attach_benchmarks(self,benchmark):
        data=benchmark.to_dict() if hasattr(benchmark,'to_dict') else benchmark
        if not isinstance(data,dict) or data.get('kind')!='synthetic-engine-benchmarks' or not isinstance(data.get('cases'),list):
            raise ModelError('Expected a synthetic benchmark.json or BenchmarkResults')
        self.benchmark_context={key:copy.deepcopy(data.get(key)) for key in ['source_model','seed']}
        self.benchmark_context['dataset_verification']='Query identities verify definitions/context, not synthetic CSV contents or generation settings. Review the original simulation manifest.'
        expected={w['id']:w for w in self.workloads}; matched={}
        for case in data['cases']:
            if not isinstance(case,dict):raise ModelError('Matrix benchmark cases must be objects')
            if case.get('id') not in expected:continue
            if case.get('query')!=expected[case['id']]['query']:raise ModelError('Matrix benchmark query differs from the generated context')
            if case['id'] in matched:raise ModelError('Duplicate matrix benchmark workload ID')
            matched[case['id']]=case
        self.warnings.append(f'Matched {len(matched)} of {len(self.workloads)} exact matrix workload identities; unmatched contexts stay unknown.')
        known=False
        for row in self.matrices:
            evidence=[]
            for workload in self.workloads:
                if workload['matrix_id']!=row['id'] or workload['id'] not in matched:continue
                case=matched[workload['id']]
                if not isinstance(case.get('phases'),dict):raise ModelError('Matrix benchmark phases must be an object')
                for cache,phase in case['phases'].items():
                    if cache not in {'cold','warm'} or not isinstance(phase,dict):raise ModelError('Invalid matrix benchmark phase')
                    value=phase.get('mean_ms')
                    if value is not None:
                        import math
                        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<0:
                            raise ModelError('Matrix benchmark means must be finite nonnegative numbers')
                        if not isinstance(phase.get('successes'),int) or isinstance(phase['successes'],bool) or not isinstance(phase.get('attempts'),int) or isinstance(phase['attempts'],bool) or not 1<=phase['successes']<=phase['attempts']:
                            raise ModelError('A known matrix mean needs successful iteration counts')
                        known=True
                    evidence.append({'workload_id':case['id'],'scenario':workload['scenario'],'cache':cache,
                                     'mean_ms':value,'successes':phase.get('successes'), 'attempts':phase.get('attempts')})
            row['benchmark_evidence']=evidence
            row['highest_observed_mean_ms']=max((e['mean_ms'] for e in evidence if e['mean_ms'] is not None),default=None)
        if known:
            self.rank_by='highest_observed_synthetic_context_mean_ms'
            self.matrices.sort(key=lambda r:(r['highest_observed_mean_ms'] is None,-(r['highest_observed_mean_ms'] or 0),r['id']))
        self.warnings.append('Attached timings are inclusive synthetic multi-measure contexts. They do not measure isolated values, rendering, production latency, or pre-aggregation speedup; partial iterations remain visible.')
        return self

    def write(self,directory):
        from .matrix_report import render_matrix_html
        root=Path(directory);root.mkdir(parents=True,exist_ok=True)
        (root/'queries').mkdir(exist_ok=True)
        for workload in self.workloads:(root/workload['query_file']).write_text(workload['query'],encoding='utf-8')
        for row in self.matrices:
            for i,candidate in enumerate(row['grain_advice']['candidates']):
                if candidate['group_count_probe']:
                    filename='queries/grain-'+_hash([row['id'],i])[:20]+'.dax'
                    candidate['probe_file']=filename
                    (root/filename).write_text(candidate['group_count_probe'],encoding='utf-8')
        (root/'matrices.json').write_text(json.dumps(self.to_dict(),ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
        (root/'matrix-specs.json').write_text(json.dumps({'schema_version':'1.0','matrices':[r['spec'] for r in self.matrices]},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        (root/'report.html').write_text(render_matrix_html(self),encoding='utf-8')
        return {'json':root/'matrices.json','html':root/'report.html','specs':root/'matrix-specs.json'}


def analyze_matrices(source,matrices,*,benchmark=None,analysis=None,progress=None):
    from .analyzer import analyze
    from .progress import emit
    model=source if isinstance(source,Model) else parse_model(source) if isinstance(source,dict) else load_model(source)
    specs=load_matrices(matrices)
    if analysis is not None and analysis.model is not model:
        raise ModelError('Reused analysis must belong to the same unmodified model instance')
    analysis=analysis or analyze(model,progress=progress,include_rewrites=False); by_id={r['id']:r for r in analysis.ranked}
    fingerprint=_hash([[(e.id,e.expression,_type(e)) for e in sorted(model.entities.values(),key=lambda e:e.id)],
                       [asdict(r) for r in model.relationships]])
    output=[]; workloads=[]
    emit(progress, 'matrices', 'Review matrix grains', completed=0, total=len(specs), unit='matrices')
    for i,spec in enumerate(specs,1):
        emit(progress, 'matrices', 'Review matrix grains', completed=i-1, total=len(specs), unit='matrices', detail=spec.title)
        diagnostics=list(spec.unsupported); values=[]; filters=[]
        def measure(pair):
            try:return model.measure(pair[1],pair[0]).id
            except ModelError:diagnostics.append(f'Unbound measure {pair}; definition not invented.');return None
        for pair in spec.values:
            identity=measure(pair)
            if identity:values.append(identity)
        for pair in spec.filter_measures:
            identity=measure(pair)
            if identity:filters.append(identity)
        context=spec.rows+spec.columns+spec.retain+[[f['table'],f['column']] for f in spec.filters]
        for state in spec.drill_states:context += [[f['table'],f['column']] for f in state.get('filters',[])]
        context=[[entity.table,entity.name] if (entity:=_column(model,p)) else p for p in context]
        context=[list(p) for p in _unique(context)]
        for pair in context:
            if not _column(model,pair):diagnostics.append(f'Unbound context column {pair}; data type/grain remains unknown.')
        if not values:diagnostics.append('Matrix has no bound explicit value measures.')
        roots=list(dict.fromkeys(values+filters))
        grain=_grain_advice(model,analysis,spec,roots,context)
        # Missing schema/value bindings also withhold executable grouping probes.
        if diagnostics:
            for candidate in grain['candidates']:
                candidate['blockers']=list(dict.fromkeys(candidate['blockers']+diagnostics))
                candidate['status']='needs_review';candidate['group_count_probe']=None
        if roots and not all(by_id[n]['chain_complete'] for n in roots):
            diagnostics.append('A value/filter chain is incomplete; restore its definitions/bindings before workload generation.')
        queries=[]
        if not diagnostics and not spec.filter_measures:
            queries=_workloads(model,spec,values,fingerprint);workloads+=queries
        elif spec.filter_measures:
            diagnostics.append('Workloads withheld: visual-level measure-filter predicates/grain are not modeled.')
        scores=[by_id[n]['risk_score'] for n in roots if by_id[n]['risk_score'] is not None]
        chain=set()
        for identity in roots:chain.update([identity]+analysis.graph.dependency_ids(identity))
        output.append({'id':spec.id,'title':spec.title,'spec':spec.to_dict(),'value_measure_ids':values,'filter_measure_ids':filters,
                       'priority':max(scores,default=None),'chain_complete':bool(values) and not diagnostics and all(by_id[n]['chain_complete'] for n in roots),
                       'chain_node_ids':sorted(chain),'unique_chain_measure_count':sum(model.entities[n].kind=='measure' for n in chain),
                       'shared_measure_ids':sorted(n for n in chain if model.entities[n].kind=='measure' and sum(n==v or n in analysis.graph.dependency_ids(v) for v in values)>1),
                       'measure_findings':[copy.deepcopy(by_id[n]) for n in roots],
                       'grain_advice':grain,'diagnostics':list(dict.fromkeys(diagnostics)),
                       'workload_ids':[w['id'] for w in queries],'highest_observed_mean_ms':None,'benchmark_evidence':[]})
        emit(progress, 'matrices', 'Review matrix grains', completed=i, total=len(specs), unit='matrices', detail=spec.title)
    output.sort(key=lambda r:(r['priority'] is None,-(r['priority'] or 0),r['id']))
    result=MatrixAnalysis(output,workloads,model.name,[
        'Static matrix priority is the maximum known value/filter measure review score, not summed cost or a cell-count multiplier.',
        'Prefix-context queries exercise configured totals/drill states; they do not reproduce the complete Power BI query, TopN, show-items-with-no-data, visual calculations, field parameters, interaction filters or rendering.',
        'PBIR imports bind common explicit Column/Measure projections only. Active slicer values and formatting/drill state must be reviewed explicitly.',
        'Aggregation advice/probes do not create summary tables, verify numerical equivalence, infer observed group counts, or claim speedup.'])
    result.dependency_graph=analysis.graph.to_dict()
    return result.attach_benchmarks(benchmark) if benchmark is not None else result
