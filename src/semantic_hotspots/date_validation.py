"""Fixed-date query overrides; production expressions retain their live clock."""
from __future__ import annotations

import calendar
from datetime import date

from .dax import Resolver
from .graph import DependencyGraph
from .model import Entity, ModelError


def validation_dates(values=None):
    if values is None:
        today=date.today()
        values=[today,date(today.year,today.month,1),
                date(today.year,today.month,calendar.monthrange(today.year,today.month)[1]),
                date(today.year,1,1),date(today.year,12,31)]
    if not isinstance(values,(list,tuple)) or not 1<=len(values)<=12:
        raise ModelError('Validation dates must be a list of 1–12 ISO dates')
    result=[]
    for value in values:
        try:
            parsed=value if type(value) is date else date.fromisoformat(value)
        except (TypeError,ValueError):raise ModelError('Use validation dates in YYYY-MM-DD format') from None
        if parsed.year<1900:raise ModelError('Validation dates must be in years 1900–9999 for exact DAX DATE semantics')
        if parsed.isoformat() not in result:result.append(parsed.isoformat())
    return result


def freeze_today(expression, as_of):
    parsed=date.fromisoformat(validation_dates([as_of])[0])
    replacement=f'DATE({parsed.year}, {parsed.month}, {parsed.day})'
    # Call/token parsing excludes strings, comments and similarly named objects.
    # A model UDF named TODAY is rejected by the planner, not replaced here.
    from .model import Model
    calls=[c for c in Resolver(Model()).scan(expression).calls if c.name=='TODAY' and not c.arguments]
    for call in reversed(calls):expression=expression[:call.start]+replacement+expression[call.end:]
    return expression


def date_controls(model, owner, expression=None, *, dates=None, graph=None):
    requested=validation_dates(dates)
    graph=graph or DependencyGraph(model)
    wanted={owner.id,*graph.dependency_ids(owner.id)}
    if expression is not None:
        for ref in Resolver(model).scan(expression,owner).references:
            wanted.update([ref.target,*graph.dependency_ids(ref.target)])
    definitions=[];blocked=[]
    if any(e.kind=='function' and e.name.casefold()=='today' for e in model.entities.values()):
        blocked.append('A model function named TODAY prevents built-in clock substitution')
    for identity in sorted(wanted):
        entity=model.entities[identity]
        text=entity.expression
        if not text:continue
        calls=graph.syntax.get(identity)
        calls=calls.calls if calls else Resolver(model).scan(text,entity).calls
        if any(c.name=='TODAY' for c in calls):
            if entity.kind!='measure':
                blocked.append('TODAY in a stored/calculated table or column cannot be frozen by query-scoped measure overrides: '+entity.qualified_name)
            elif any(c.name=='TODAY' and c.arguments for c in calls):
                blocked.append('Unsupported TODAY arguments in '+entity.qualified_name)
            else:definitions.append({'id':identity,'table':entity.table,'name':entity.name,'expression':text})
        if any(c.name in {'NOW','RAND','RANDBETWEEN'} for c in calls):
            blocked.append('Date controls do not freeze NOW or random evaluation in '+entity.qualified_name)
    proposed_calls=Resolver(model).scan(expression,owner).calls if expression is not None else []
    if any(c.name in {'NOW','RAND','RANDBETWEEN'} or (c.name=='TODAY' and c.arguments) for c in proposed_calls):
        blocked.append('The proposed expression contains unsupported clock arguments or time/random evaluation')
    if any(c.name=='TODAY' for c in proposed_calls):
        if not any(d['id']==owner.id for d in definitions):
            definitions.append({'id':owner.id,'table':owner.table,'name':owner.name,'expression':owner.expression})
    if not definitions and not blocked:return None
    if definitions:
        clocks={d['id'] for d in definitions}
        # A query-scoped helper override does not replace references already
        # compiled into a stored caller. Re-declare every affected caller and
        # both roots, without inlining or changing the production definitions.
        affected={owner.id,*clocks}
        affected.update(n for n in wanted if model.entities[n].kind=='measure'
                        and clocks.intersection(graph.dependency_ids(n)))
        order=list(dict.fromkeys([*graph.order,*sorted(affected)]))
        definitions=[{'id':n,'table':model.entities[n].table,'name':model.entities[n].name,
                      'expression':model.entities[n].expression} for n in order
                     if n in affected and model.entities[n].expression]
    return {'kind':'fixed-as-of-dates','dates':requested,'definitions':definitions,
            'blocked_reasons':blocked,'production_today_preserved':True,
            'scope':'Query-scoped overrides of the original, candidate, clock measures and their affected callers. Stored data is not refreshed or changed. Finite dates are not universal equivalence evidence.'}


def query_definitions(table, name, expression, alias, controls=None, as_of=None):
    """Return DEFINE declarations; override originals only in a dated query."""
    qualified=Entity('measure',table,alias).qualified_name
    if not controls:
        return f'DEFINE MEASURE {qualified} = {expression}\n'
    if controls['blocked_reasons']:raise ModelError(' '.join(controls['blocked_reasons']))
    as_of=as_of or controls['dates'][0]
    definitions=[]
    original=Entity('measure',table,name)
    for item in controls['definitions']:
        if item['id']==original.id:continue
        target=Entity('measure',item['table'],item['name']).qualified_name
        definitions.append('    MEASURE '+target+' =\n'+freeze_today(item['expression'],as_of))
    original_text=next((d['expression'] for d in controls['definitions'] if d['id']==original.id),None)
    if original_text is not None:
        definitions.append('    MEASURE '+original.qualified_name+' =\n'+freeze_today(original_text,as_of))
    definitions.append('    MEASURE '+qualified+' =\n'+freeze_today(expression,as_of))
    return ('// Validation as of '+as_of+'. TODAY is frozen in these query-scoped measures only.\n'
            '// The exported production measure retains live TODAY(). No source/model definitions are saved.\n'
            'DEFINE\n'+'\n\n'.join(definitions)+'\n\n')
