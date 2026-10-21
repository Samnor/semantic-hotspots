"""Recover actual definitions from explicit exports or local engine metadata.

No missing expression is synthesized and no source model/file is modified.
"""
from __future__ import annotations

import copy
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .dax import tokenize
from .graph import DependencyGraph, dependency_issues
from .model import Entity, Model, ModelError
from .parser import load_model, parse_model


def _model(source):
    return source if isinstance(source,Model) else parse_model(source) if isinstance(source,dict) else load_model(source)


def _defined(entity):
    return bool(entity and entity.expression and entity.expression.strip())


def _signature(expression):
    # DAX function/keyword/variable names are case-insensitive; quoted string
    # payloads remain exact (e.g. EXACT and case-sensitive output text).
    return [(t.kind,t.value.casefold() if t.kind == 'NAME' else t.value)
            for t in tokenize(expression or '')]


def helper_requirements(source,measure,*,table=None):
    model=_model(source);owner=model.measure(measure,table);graph=DependencyGraph(model)
    chain={owner.id,*graph.dependency_ids(owner.id)}
    return [i for i in dependency_issues(model,graph.syntax,chain,cyclic=owner.id in graph.blocked)
            if i['code'] in {'missing_definition','unresolved_reference'}]


@dataclass
class HelperRecovery:
    model: Model
    recovered: list[str]
    remaining: list[dict]
    provenance: str


def recover_helpers(source, supplement, measure, *, table=None, require_root_match=False, provenance='explicit model export'):
    """Merge only reachable missing definitions/metadata into a new model copy.

    When a supplement includes the root, it must match. Local engine metadata
    always requires that anchor. Existing definitions are never overwritten.
    """
    original=_model(source);extra=_model(supplement);owner=original.measure(measure,table)
    anchor=extra.entities.get(owner.id)
    if require_root_match and not _defined(anchor):
        raise ModelError('The loaded model does not contain the selected root definition')
    if _defined(anchor) and _signature(anchor.expression)!=_signature(owner.expression):
        raise ModelError('Selected root definition differs; choose the matching model/export')
    # Validate overlapping definitions before using the supplement. A same-name
    # helper from a different revision must not silently change the source.
    for identity,entity in sorted(extra.entities.items(),key=lambda pair:pair[1].kind!='table'):
        old=original.entities.get(identity)
        if _defined(old) and _defined(entity) and _signature(old.expression)!=_signature(entity.expression):
            raise ModelError('Conflicting existing definition: '+old.qualified_name)
        if old and old.kind in {'column','table'} and not _defined(old) and _defined(entity):
            raise ModelError('The supplement changes a known physical object into a calculated object: '+old.qualified_name)
    combined=copy.deepcopy(original)
    for identity,entity in sorted(extra.entities.items(),key=lambda pair:pair[1].kind!='table'):
        if identity not in combined.entities:
            if entity.kind!='table':combined.add(copy.deepcopy(entity))
            else:
                target=combined.add_table(entity.table,entity.source)
                combined.entities[target.id]=copy.deepcopy(entity)
        else:
            current=combined.entities[identity]
            if current.kind=='measure' and not _defined(current) and _defined(entity):
                current.expression=entity.expression;current.source=entity.source
                current.metadata=dict(current.metadata,definition_provenance=provenance)
            if current.kind in {'column','measure'}:
                for key,value in entity.metadata.items():
                    missing = current.metadata.get(key) is None if current.kind=='measure' else not current.metadata.get(key)
                    if missing and value is not None:current.metadata[key]=copy.deepcopy(value)
    # Resolve names in the combined catalog, then retain only the selected chain
    # and entities already present in the source. This recovers transitive helpers
    # without adding every unrelated measure to the user's analysis.
    graph=DependencyGraph(combined)
    wanted={owner.id,*graph.dependency_ids(owner.id)}
    relevant_issues=dependency_issues(combined,graph.syntax,wanted,cyclic=owner.id in graph.blocked)
    if any(i['code']=='unresolved_reference' and 'ambiguous' in i['message'].lower() for i in relevant_issues):
        raise ModelError('Supplement leaves an ambiguous reference in the selected chain; supply unambiguous matching definitions')
    # Unknown bare names remain unresolved when metadata offers duplicates.
    # Do not turn an ambiguous supplement into an arbitrary name choice.
    result=copy.deepcopy(original);recovered=[]
    for identity in sorted(wanted,key=lambda identity:(combined.entities[identity].kind!='table',identity)):
        entity=combined.entities[identity]
        old=result.entities.get(identity)
        if old is None:
            if entity.kind!='table':result.add(copy.deepcopy(entity))
            else:
                target=result.add_table(entity.table,entity.source)
                result.entities[target.id]=copy.deepcopy(entity)
            if entity.kind=='measure' and _defined(entity):recovered.append(identity)
        elif old.kind=='measure' and not _defined(old) and _defined(entity):
            old.expression=entity.expression;old.source=entity.source
            old.metadata=dict(old.metadata,definition_provenance=provenance)
            recovered.append(identity)
        elif old.kind=='column':
            old.metadata=copy.deepcopy(entity.metadata)
        if old is not None and old.kind=='measure':
            for key,value in entity.metadata.items():
                if old.metadata.get(key) is None and value is not None:old.metadata[key]=copy.deepcopy(value)
    # Context-sensitive models cannot be silently presented as ordinary measure
    # catalogs. Retain explicit markers even if they are not direct references.
    for entity in extra.entities.values():
        if entity.kind=='format_string' and entity.metadata.get('for_measure') not in result.entities:
            continue
        if entity.kind in {'function','calculation_item','format_string'} and entity.id not in result.entities:
            result.add(copy.deepcopy(entity))
    for warning in extra.warnings:
        if warning not in result.warnings:result.warnings.append(warning)
    result.warnings.append('Helper definitions supplemented from '+provenance+'; original files and existing expressions were not changed.')
    remaining=helper_requirements(result,owner.id)
    return HelperRecovery(result,recovered,remaining,provenance)


def recover_helpers_from_desktop(source, measure, *, table=None, server, database,
                                 adomd_path=None, dotnet_runtime='netfx', timeout=30):
    """Read metadata from one explicitly selected local Desktop catalog."""
    from .engine import local_server, query_to_csv
    from .showcase import _rows
    original=_model(source);owner=original.measure(measure,table)
    server=local_server(server)
    metadata={}
    with tempfile.TemporaryDirectory() as temporary:
        root=Path(temporary)
        # INFO views expose friendly names and actual declared types. They read
        # model metadata, not fact rows, partition SQL or connection strings.
        for kind in ['TABLES','COLUMNS','MEASURES']:
            query=root/(kind+'.dax');output=root/(kind+'.csv')
            query.write_text('EVALUATE INFO.VIEW.'+kind+'()\n',encoding='utf-8')
            query_to_csv(query,output,server=server,database=database,adomd_path=adomd_path,
                         dotnet_runtime=dotnet_runtime,timeout=timeout)
            metadata[kind]=_rows(output)
    extra=Model('Local Desktop metadata supplement',source_format='desktop-info-views')
    for row in metadata['TABLES']:
        name=row.get('Name')
        if not name:raise ModelError('Desktop table metadata has no names')
        t=extra.add_table(name)
        if row.get('Expression'):extra.entities[t.id].expression=row['Expression']
        if row.get('CalculationGroupPrecedence') not in {None,''}:
            extra.add(Entity('calculation_item',name,'Metadata: calculation group present','SELECTEDMEASURE()'))
    types={'Text':'string','String':'string','Whole Number':'int64','Int64':'int64',
           'Decimal Number':'double','Double':'double','Fixed Decimal Number':'decimal','Currency':'decimal',
           'Date/Time':'datetime','DateTime':'datetime','Date':'datetime','True/False':'boolean','Boolean':'boolean'}
    for row in metadata['COLUMNS']:
        table_name,name=row.get('Table'),row.get('Name')
        if not table_name or not name:raise ModelError('Desktop column metadata is incomplete')
        # Unknown enum spellings remain unknown rather than being inferred.
        typ=types.get(row.get('DataType'),str(row.get('DataType') or '').lower() or None)
        extra.add(Entity('column',table_name,name,row.get('Expression') or None,
                         metadata={'data_type':typ,'source_column':row.get('SourceColumn')}))
    for row in metadata['MEASURES']:
        table_name,name=row.get('Table'),row.get('Name')
        if not table_name or not name:raise ModelError('Desktop measure metadata is incomplete')
        extra.add(Entity('measure',table_name,name,row.get('Expression') or None,
                         metadata={'format_string':row.get('FormatString')}))
        if row.get('FormatStringDefinition'):
            from .parser import _format_expression
            _format_expression(extra,extra.measure(name,table_name),row['FormatStringDefinition'])
    return recover_helpers(original,extra,owner.id,require_root_match=True,
                           provenance='matching local Desktop model metadata')
