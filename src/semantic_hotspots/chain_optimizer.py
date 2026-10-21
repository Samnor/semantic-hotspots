"""Bounded specialization of improved helper paths, preserving measure callbacks.

This is source-level proposal generation, not a DAX engine or a physical plan.
"""
from __future__ import annotations

from .optimizer import logical_plan, plan_rewrites


def _nodes(tree):
    yield tree
    for child in tree.children:yield from _nodes(child)


def specialize_helpers(model,graph,owner,root_expression,root_steps):
    chain={owner.id,*graph.dependency_ids(owner.id)}
    helpers=[n for n in graph.order if n in chain and model.entities[n].kind=='measure' and n!=owner.id]
    if not helpers or len(helpers)>32:return None
    if any(e.kind in {'function','calculation_item','format_string'} for e in model.entities.values()):return None
    rewritten={}
    for identity in [*helpers,owner.id]:
        entity=model.entities[identity]
        if not entity.expression:return None
        original_tree,issues=logical_plan(entity.expression,model,entity)
        if issues:
            # An opaque helper can remain an opaque callback, but its body must
            # never be transplanted into another lexical or row scope.
            if identity==owner.id:return None
            continue
        if identity==owner.id:
            expression,steps=root_expression,[dict(s,owner_id=identity) for s in root_steps]
        else:
            local=plan_rewrites(model,graph=graph,measure=identity,include_helper_rewrites=False).candidates
            chosen=max(local,key=lambda c:(c['combined'],len(c['steps']),c['id'])) if local else None
            expression=chosen['after'] if chosen else entity.expression
            steps=[dict(s,owner_id=identity) for s in chosen['steps']] if chosen else []
        tree,issues=logical_plan(expression,model,entity)
        if issues:continue
        edits=[];expanded=[];proofs=[];projected_length=len(expression)
        oversized=False
        for node in _nodes(tree):
            if node.operation not in {'measure','column','scan'}:continue
            target=model.entities[node.target]
            child=rewritten.get(node.target) if node.operation=='measure' else None
            if child:
                replacement='CALCULATE(\n'+child['expression']+'\n)'
                edits.append((node.start,node.end,replacement))
                expanded.append(node.target);proofs.extend(child['steps'])
                proofs.append({'rule':'OPT005','owner_id':identity,'helper_id':node.target,
                               'source_span':[node.start,node.end],
                               'before':expression[node.start:node.end],'after':replacement})
            else:
                # Bind identifiers in the helper's own scope before moving its
                # text. Caller extension aliases cannot capture model objects.
                edits.append((node.start,node.end,target.qualified_name))
            projected_length+=len(edits[-1][2])-(node.end-node.start)
            if projected_length>50000:
                oversized=True;break
        if oversized:continue
        changed=bool(steps or expanded)
        if not changed:continue
        for start,end,replacement in sorted(edits,reverse=True):expression=expression[:start]+replacement+expression[end:]
        if len(expression)>50000:continue
        _,issues=logical_plan(expression,model,entity)
        if issues:continue
        # Only descendants with actual work reductions are transplanted. Merely
        # expanding an unchanged helper does not create an optimization claim.
        rewritten[identity]={'expression':expression,'steps':steps+proofs,'expanded':expanded}
    result=rewritten.get(owner.id)
    if not result or not result['expanded']:return None
    result['evidence']={'kind':'bounded-helper-specialization','source_helpers_unchanged':True,
        'context_transition':'Every transplanted measure call is wrapped in CALCULATE.',
        'reference_binding':'Model references qualified in their original lexical scope.',
        'expanded_helpers':sorted({s['helper_id'] for s in result['steps'] if s['rule']=='OPT005'}),
        'scope':'One new root measure. Guarded local changes are specialized through actual definitions; not an equivalence or speedup proof.'}
    return result
