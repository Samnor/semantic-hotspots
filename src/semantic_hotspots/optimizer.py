"""Bound, typed logical subset and guarded rewrite proposals, never physical plans.

Unknown syntax/context is opaque. Rewrites are proposals for engine validation;
no source model is changed and structural simplification predicts no timing.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .dax import Resolver, tokenize
from .graph import DependencyGraph, dependency_issues
from .model import Model, ModelError, object_id
from .parser import load_model, parse_model

_NUMERIC = {'int64', 'double', 'decimal', 'fixeddecimal', 'currency'}
_PRECEDENCE = {'||': 1, '&&': 2, '=': 3, '==': 3, '<>': 3, '<': 3, '>': 3,
               '<=': 3, '>=': 3, '+': 4, '-': 4, '*': 5, '/': 5}
_COMPARISONS = {'=', '==', '<>', '<', '>', '<=', '>='}
RULES = {
    'OPT001': {'title': 'Replace a numeric physical-column SUMX with SUM',
               'preconditions': ['Physical base table and numeric physical column from that same table',
                                 'No iterator expression, measure callback, or calculated column',
                                 'Bound rewritten operands and preserved surrounding helper callbacks/context'],
               'source_url': 'https://learn.microsoft.com/en-us/dax/sumx-function-dax'},
    'OPT002': {'title': 'Combine two nested row-local FILTER operations',
               'preconditions': ['Both filters preserve the same physical base-table rows and lineage',
                                 'Predicates use only same-table physical columns and compatible literals',
                                 'Only comparisons and Boolean conjunction/disjunction; no measures, calls, arithmetic, or context transition'],
               'source_url': 'https://learn.microsoft.com/en-us/dax/filter-function-dax'},
    'OPT003': {'title': 'Remove a blank-only filter from a stored table SUMX',
               'preconditions': ['The FILTER input is a lexically bound table variable, directly or through a filtered variable',
                                 'The predicate is exactly NOT ISBLANK of the column summed by SUMX',
                                 'Both references bind to the same physical or extension column; no measure reevaluation',
                                 'Only the SUMX input changes; the table binding and other consumers are retained'],
               'source_url': 'https://learn.microsoft.com/en-us/dax/sumx-function-dax'},
}


@dataclass
class LogicalNode:
    operation: str
    result_type: str
    start: int
    end: int
    children: list['LogicalNode'] = field(default_factory=list)
    target: str | None = None
    table: str | None = None
    value: str | None = None
    row_context: tuple[str, ...] = ()
    filter_context: tuple[str, ...] = ()
    supported: bool = True
    # Output schema only: extensions have expression identities, never fabricated
    # physical lineage. Row contexts carry these schemas separately from labels.
    table_columns: dict[str, dict] = field(default_factory=dict)
    lexical_bindings: dict[str, str] = field(default_factory=dict)

    def to_dict(self):
        result = asdict(self)
        result['children'] = [child.to_dict() for child in self.children]
        result['source_span'] = [result.pop('start'), result.pop('end')]
        return result


def _type(entity):
    data = {k.casefold(): v for k, v in entity.metadata.items()}
    return str(data.get('data_type') or data.get('datatype') or 'unknown').casefold()


def _plain(node):
    while node.operation == 'group':
        node = node.children[0]
    return node


class _Parser:
    def __init__(self, expression, model, owner):
        self.text, self.model, self.owner = expression, model, owner
        self.tokens = tokenize(expression)
        if len(self.tokens) > 10000:
            raise ModelError('Expression exceeds the logical-subset token limit')
        self.i = 0
        self.resolver = Resolver(model)
        self.scopes = [{}]
        self.variables = {}

    def keyword(self, value):
        token = self.peek()
        return bool(token and token.kind == 'NAME' and token.value.upper() == value)

    def variable(self, name):
        return next((scope[name.casefold()] for scope in reversed(self.scopes) if name.casefold() in scope), None)

    def block(self, token):
        """Retain lexical bindings; never inline, move or reevaluate a VAR."""
        self.scopes.append({})
        bindings = []
        while True:
            name = self.take()
            if (name.kind != 'NAME' or not re.fullmatch(r'(?:__)?[A-Za-z][A-Za-z0-9_]*', name.value)
                    or name.value.upper() in {'VAR', 'RETURN', 'TRUE', 'FALSE', 'NOT', 'IN', 'VISIBLE'}
                    or name.value.casefold() in self.resolver.tables or name.value.casefold() in self.scopes[-1]):
                raise ModelError('Unsupported or duplicate VAR name at offset '+str(name.start))
            self.take('=')
            value = self.expression()
            identity = 'local:'+str(name.start)+':'+name.value.casefold()
            binding = LogicalNode('variable_definition', 'unknown', name.start, value.end,
                                  [value], target=identity, value=name.value)
            bindings.append(binding)
            self.variables[identity] = binding
            self.scopes[-1][name.value.casefold()] = identity
            if not self.keyword('VAR'):
                break
            self.take()
        if not self.keyword('RETURN'):
            raise ModelError('VAR block is missing RETURN at offset '+str(token.start))
        self.take()
        body = self.expression()
        self.scopes.pop()
        return LogicalNode('let', 'unknown', token.start, body.end, bindings+[body])

    def peek(self, value=None):
        token = self.tokens[self.i] if self.i < len(self.tokens) else None
        return token if value is None else bool(token and token.kind == 'SYMBOL' and token.value == value)

    def take(self, value=None):
        token = self.peek()
        if token is None or (value is not None and not self.peek(value)):
            raise ModelError('Syntax outside the supported logical subset')
        self.i += 1
        return token

    def parse(self):
        result = self.expression()
        if self.peek() is not None:
            raise ModelError('Syntax outside the supported logical subset at offset '+str(self.peek().start))
        self.bind(result, (), ())
        return result

    def expression(self, minimum=0):
        token = self.take()
        if token.kind == 'NAME' and token.value.upper() == 'VAR':
            node = self.block(token)
        elif token.kind == 'NAME' and token.value.upper() == 'NOT' and not self.peek('('):
            child = self.expression(3)
            node = LogicalNode('NOT', 'unknown', token.start, child.end, [child])
        elif token.kind == 'SYMBOL' and token.value in {'+', '-'}:
            child = self.expression(6)
            node = LogicalNode('unary', 'unknown', token.start, child.end, [child], value=token.value)
        elif token.kind == 'SYMBOL' and token.value == '(':
            child = self.expression()
            end = self.take(')').end
            node = LogicalNode('group', 'unknown', token.start, end, [child])
        elif token.kind in {'NUMBER', 'STRING'}:
            if token.kind == 'NUMBER' and not re.fullmatch(r'\d+(?:\.\d+)?', token.value):
                raise ModelError('Unsupported numeric literal')
            node = LogicalNode('literal', 'number' if token.kind == 'NUMBER' else 'string', token.start, token.end, value=token.value)
        elif token.kind == 'NAME' and self.peek('('):
            self.take('(')
            args = []
            if not self.peek(')'):
                args.append(self.expression())
                while self.peek(','):
                    self.take(',')
                    args.append(self.expression())
            end = self.take(')').end
            node = LogicalNode(token.value.upper(), 'unknown', token.start, end, args)
        elif token.kind in {'NAME', 'QUOTED'}:
            table = self.resolver.tables.get(token.value.casefold())
            variable = self.variable(token.value) if token.kind == 'NAME' else None
            if variable:
                if self.peek() and self.peek().kind == 'BRACKET':
                    raise ModelError('Table-variable qualified columns are outside the logical subset')
                node = LogicalNode('variable_reference', 'unknown', token.start, token.end,
                                   target=variable, value=token.value,
                                   lexical_bindings={k:v for scope in self.scopes for k,v in scope.items()})
            elif self.peek() and self.peek().kind == 'BRACKET':
                column = self.take()
                target, _ = self.resolver.resolve(column.value, token.value, self.owner)
                if not target:
                    raise ModelError('Unbound qualified reference')
                entity = self.model.entities[target]
                node = LogicalNode(entity.kind, 'scalar' if entity.kind == 'measure' else _type(entity),
                                   token.start, column.end, target=target, table=entity.table)
            elif table:
                node = LogicalNode('scan', 'table', token.start, token.end, target=table.id, table=table.name)
            else:
                raise ModelError('Variables and unbound names are outside the logical subset')
        elif token.kind == 'BRACKET':
            # A bracket can denote a model measure or an iterator's row column.
            # Decide only after the first table argument has been bound.
            node = LogicalNode('unbound_reference', 'unknown', token.start, token.end, value=token.value)
        else:
            raise ModelError('Syntax outside the supported logical subset')
        while self.peek() and self.peek().kind == 'SYMBOL':
            op = self.peek().value
            if self.i + 1 < len(self.tokens) and self.tokens[self.i + 1].kind == 'SYMBOL':
                pair = op + self.tokens[self.i + 1].value
                if pair in _PRECEDENCE:
                    op = pair
            if op not in _PRECEDENCE or _PRECEDENCE[op] < minimum:
                break
            self.i += len(op)
            right = self.expression(_PRECEDENCE[op] + 1)
            node = LogicalNode('binary', 'unknown', node.start, right.end, [node, right], value=op)
        return node

    def bind(self, node, rows, filters, depth=0, schemas=()):
        if depth > 64:
            raise ModelError('Logical subset nesting exceeds 64 levels')
        node.row_context, node.filter_context = rows, filters
        for i, child in enumerate(node.children):
            child_rows, child_filters = rows, filters
            child_schemas = schemas
            if ((node.operation in {'FILTER', 'SUMX'} and i == 1)
                    or (node.operation in {'ADDCOLUMNS', 'SELECTCOLUMNS'} and i >= 2 and i % 2 == 0)):
                source = node.children[0]
                child_rows = rows + ((source.table or 'table expression at '+str(source.start)),)
                child_schemas += (source.table_columns if source.result_type == 'table' else None,)
            if node.operation == 'CALCULATE' and i == 0:
                child_filters += ('CALCULATE: transition/modified filters',)
            self.bind(child, child_rows, child_filters, depth+1, child_schemas)
        c, op = node.children, node.operation
        node.supported = all(x.supported for x in c)
        if op == 'scan':
            node.table_columns = {e.name.casefold(): self.column_schema(e) for e in self.model.entities.values()
                                  if e.kind == 'column' and e.table == node.table}
            return
        if op == 'unbound_reference':
            self.bind_reference(node, schemas)
            return
        if op in {'column', 'measure', 'literal'}:
            return
        if op == 'variable_reference':
            binding = self.variables[node.target]
            node.result_type, node.table, node.supported = binding.result_type, binding.table, binding.supported
            node.table_columns = dict(binding.table_columns)
        elif op == 'variable_definition':
            node.result_type, node.table = c[0].result_type, c[0].table
            node.table_columns = dict(c[0].table_columns)
            node.supported &= node.result_type not in {'unknown', 'filter_modifier'}
        elif op == 'let':
            node.result_type, node.table = c[-1].result_type, c[-1].table
            node.table_columns = dict(c[-1].table_columns)
        elif op == 'group':
            node.result_type, node.table = c[0].result_type, c[0].table
            node.table_columns = dict(c[0].table_columns)
        elif op == 'unary':
            node.result_type = c[0].result_type
            node.supported &= node.result_type in _NUMERIC | {'number'}
        elif op == 'binary':
            node.result_type = 'boolean' if node.value in _COMPARISONS | {'&&', '||'} else 'number'
            node.supported &= all(x.result_type not in {'table', 'unknown'} for x in c)
        elif op in {'TRUE', 'FALSE'} and not c:
            node.result_type = 'boolean'
        elif op == 'BLANK' and not c:
            node.result_type = 'blank'
        elif op == 'TODAY' and not c:
            # Recognize the type, but retain the separate controlled-evaluation
            # guard in plan_rewrites. Binding is not authorization to rewrite.
            node.result_type = 'datetime'
        elif op == 'DATE' and len(c) == 3 and all(x.result_type in _NUMERIC | {'number'} for x in c):
            node.result_type = 'datetime'
        elif op == 'YEAR' and len(c) == 1 and c[0].result_type in {'datetime', 'scalar', 'blank'}:
            node.result_type = 'int64'
        elif op == 'ISBLANK' and len(c) == 1 and c[0].result_type not in {'table', 'unknown'}:
            node.result_type = 'boolean'
        elif op == 'NOT' and len(c) == 1 and c[0].result_type == 'boolean':
            node.result_type = 'boolean'
        elif op == 'IF' and len(c) in {2, 3} and c[0].result_type == 'boolean' and all(x.result_type not in {'table', 'unknown'} for x in c[1:]):
            types = {x.result_type for x in c[1:]} - {'blank'}
            node.result_type = next(iter(types)) if len(types) == 1 else 'scalar'
        elif op == 'SWITCH' and len(c) >= 3 and all(x.result_type not in {'table', 'unknown', 'filter_modifier'} for x in c):
            results = c[2::2] + (c[-1:] if len(c) % 2 == 0 else [])
            types = {x.result_type for x in results} - {'blank'}
            node.result_type = next(iter(types)) if len(types) == 1 else 'scalar'
        elif op == 'DIVIDE' and len(c) in {2, 3} and all(x.result_type in _NUMERIC | {'number', 'scalar', 'blank'} for x in c):
            alternate = _plain(c[2]) if len(c) == 3 else None
            node.result_type = 'number'
            node.supported &= alternate is None or alternate.operation == 'literal' or (alternate.operation == 'BLANK' and not alternate.children)
        elif op == 'REMOVEFILTERS' and all(x.operation in {'scan', 'column'} for x in c):
            node.result_type = 'filter_modifier'
        elif op == 'SUM' and len(c) == 1 and _plain(c[0]).operation == 'column' and c[0].result_type in _NUMERIC:
            node.result_type = c[0].result_type
        elif op in {'MAX', 'MIN'} and len(c) == 1 and _plain(c[0]).operation == 'column':
            node.result_type = c[0].result_type if c[0].result_type != 'unknown' else 'scalar'
        elif op == 'VALUES' and len(c) == 1 and _plain(c[0]).operation in {'scan', 'column'}:
            base = _plain(c[0])
            node.result_type, node.table = 'table', base.table
            node.table_columns = (dict(base.table_columns) if base.operation == 'scan'
                                  else {self.model.entities[base.target].name.casefold(): self.column_schema(self.model.entities[base.target])})
        elif op in {'ADDCOLUMNS', 'SELECTCOLUMNS'} and len(c) >= 3 and len(c) % 2 == 1 and c[0].result_type == 'table':
            node.result_type, node.table = 'table', c[0].table
            node.table_columns = dict(c[0].table_columns) if op == 'ADDCOLUMNS' else {}
            for i in range(1, len(c), 2):
                alias, value = c[i:i+2]
                if alias.operation != 'literal' or alias.result_type != 'string' or not alias.value:
                    raise ModelError(op+' requires a literal nonempty column name at offset '+str(alias.start))
                key = alias.value.casefold()
                if key in node.table_columns:
                    raise ModelError('Duplicate table column ['+alias.value+'] at offset '+str(alias.start))
                node.table_columns[key] = {'name': alias.value, 'kind': 'extension_column',
                    'target': 'extension:'+str(alias.start), 'result_type': value.result_type,
                    'expression_span': [value.start, value.end]}
                node.supported &= value.result_type not in {'table', 'unknown', 'filter_modifier'}
        elif op == 'SUMX' and len(c) == 2 and c[0].result_type == 'table' and c[1].result_type in _NUMERIC | {'number', 'scalar', 'blank'}:
            node.result_type = c[1].result_type
        elif op == 'FILTER' and len(c) == 2 and c[0].result_type == 'table' and c[1].result_type == 'boolean':
            node.result_type, node.table = 'table', c[0].table
            node.table_columns = dict(c[0].table_columns)
        elif op == 'ISEMPTY' and len(c) == 1 and c[0].result_type == 'table':
            node.result_type = 'boolean'
        elif op == 'COUNTROWS' and len(c) == 1 and c[0].result_type == 'table':
            node.result_type = 'int64'
        elif op == 'CALCULATE' and c and c[0].result_type not in {'table', 'unknown', 'filter_modifier'} and all(x.result_type in {'table', 'boolean', 'filter_modifier'} for x in c[1:]):
            node.result_type = c[0].result_type
        else:
            node.supported = False
        if op.casefold() in self.resolver.functions:
            node.supported = False

    @staticmethod
    def column_schema(entity):
        return {'name': entity.name, 'kind': 'column', 'target': entity.id, 'result_type': _type(entity)}

    def bind_reference(self, node, schemas):
        key = node.value.casefold()
        candidates = self.resolver.bare.get(key, [])
        row_column = None
        for schema in reversed(schemas):
            if schema is None:
                self.reference_error(node, 'unknown input table schema')
            if key in schema:
                row_column = schema[key]
                break
        if row_column:
            if any(e.kind == 'measure' for e in candidates):
                self.reference_error(node, 'ambiguous row-column/model-measure name scope')
            node.operation = row_column['kind']
            node.target, node.result_type = row_column['target'], row_column['result_type']
            node.table = self.model.entities[node.target].table if node.operation == 'column' else None
            return
        target, _ = self.resolver.resolve(node.value, owner=self.owner)
        if target and self.model.entities[target].kind == 'measure':
            node.operation, node.target, node.result_type = 'measure', target, 'scalar'
            return
        self.reference_error(node, 'no matching column in the active row scope or unique model measure')

    def reference_error(self, node, reason):
        line = self.text.count('\n', 0, node.start)+1
        raise ModelError('Cannot bind ['+node.value+'] at line '+str(line)+', offset '+str(node.start)+
                         ' to a supported measure or row-column scope: '+reason+'. Automatic rewriting is withheld.')


def logical_plan(expression, model, owner):
    """Return an explicitly partial logical tree; unknown syntax is never guessed."""
    try:
        if len(expression) > 100000:
            raise ModelError('Expression exceeds the logical-subset size limit')
        tree = _Parser(expression, model, owner).parse()
        if not tree.supported:
            def unsupported(node):
                if node.operation == 'variable_reference':return []
                children = [reason for child in node.children for reason in unsupported(child)]
                if children or node.supported:return children
                label = 'table VAR' if node.operation == 'variable_definition' and node.result_type == 'table' else node.operation
                return [label+' at offset '+str(node.start)]
            return tree, ['Automatic rewrite coverage is missing for '+', '.join(dict.fromkeys(unsupported(tree)))+'. The parsed structure is retained; rewrites withheld.']
        return tree, []
    except (ModelError, RecursionError) as exc:
        return LogicalNode('opaque', 'unknown', 0, len(expression), supported=False), [str(exc) or 'Logical subset nesting limit exceeded']


def _physical(node, model):
    entity = model.entities.get(node.target)
    table = model.entities.get(object_id('table', node.table or ''))
    return bool(entity and not entity.expression and table and not table.expression)


def _predicate(node, table, model):
    node = _plain(node)
    if node.operation == 'binary' and node.value in {'&&', '||'}:
        return all(_predicate(x, table, model) for x in node.children)
    if node.operation != 'binary' or node.value not in _COMPARISONS:
        return False
    def atom(item):
        item = _plain(item)
        if item.operation == 'unary' and _plain(item.children[0]).operation == 'literal':
            item = _plain(item.children[0])
        if item.operation == 'literal':
            return item.result_type
        if item.operation == 'column' and item.table == table and _physical(item, model):
            return 'number' if item.result_type in _NUMERIC else item.result_type
        return None
    types = [atom(x) for x in node.children]
    return types[0] in {'number', 'string'} and types[0] == types[1]


def _matches(tree, text, model):
    matches = []
    definitions = {}
    def collect(node):
        if node.operation == 'variable_definition':definitions[node.target] = node.children[0]
        for child in node.children:collect(child)
    collect(tree)
    def visit(node):
        if node.operation == 'SUMX':
            table, column = map(_plain, node.children)
            if (table.operation == 'scan' and column.operation == 'column'
                    and column.table == table.table and column.result_type in _NUMERIC
                    and _physical(table, model) and _physical(column, model)):
                matches.append(('OPT001', node.start, node.end, f'SUM({text[column.start:column.end]})'))
            filtered = table
            if table.operation == 'variable_reference' and table.target in definitions:
                filtered = _plain(definitions[table.target])
            if filtered.operation == 'FILTER':
                base, predicate = map(_plain, filtered.children)
                predicate = _plain(predicate.children[0]) if predicate.operation == 'NOT' and len(predicate.children) == 1 else None
                tested = _plain(predicate.children[0]) if predicate and predicate.operation == 'ISBLANK' and len(predicate.children) == 1 else None
                if (base.operation == 'variable_reference' and base.result_type == 'table'
                        and (table.operation != 'variable_reference'
                             or table.lexical_bindings.get(base.value.casefold()) == base.target)
                        and column.operation in {'column', 'extension_column'} and tested
                        and tested.operation == column.operation and tested.target == column.target):
                    matches.append(('OPT003', node.start, node.end,
                                    f'SUMX({text[base.start:base.end]}, {text[column.start:column.end]})'))
        if node.operation == 'FILTER':
            inner = _plain(node.children[0])
            if inner.operation == 'FILTER':
                base = _plain(inner.children[0])
                if (base.operation == 'scan' and _physical(base, model)
                        and _predicate(inner.children[1], base.table, model)
                        and _predicate(node.children[1], base.table, model)):
                    p, q = inner.children[1], node.children[1]
                    matches.append(('OPT002', node.start, node.end,
                                    f'FILTER({text[base.start:base.end]}, ({text[p.start:p.end]}) && ({text[q.start:q.end]}))'))
        for child in node.children:
            visit(child)
    visit(tree)
    return matches


def _type_gap_only(tree):
    """Permit local rules around untyped metadata, never unknown syntax/calls.

    Each rule still proves its own operands. This does not establish types or
    semantics for the surrounding expression; engine validation stays separate.
    """
    nodes = []
    def visit(node):
        nodes.append(node)
        for child in node.children:visit(child)
    visit(tree)
    definitions = {n.target:n.children[0] for n in nodes if n.operation == 'variable_definition'}
    arities = {'TRUE':{0},'FALSE':{0},'BLANK':{0},'TODAY':{0},'DATE':{3},'YEAR':{1},
               'ISBLANK':{1},'NOT':{1},'IF':{2,3},'DIVIDE':{2,3},'SUM':{1},'MAX':{1},
               'MIN':{1},'VALUES':{1},'SUMX':{2},'FILTER':{2},'ISEMPTY':{1},'COUNTROWS':{1}}
    structural = {'scan','column','measure','literal','variable_reference','variable_definition',
                  'let','group','unary','binary','extension_column'}
    def unknown(node, seen=frozenset()):
        if node.operation == 'column' and node.result_type == 'unknown':return True
        if node.operation == 'variable_reference' and node.target not in seen:
            return unknown(definitions[node.target],seen|{node.target})
        if node.operation == 'extension_column' and node.result_type == 'unknown':return True
        return any(unknown(c,seen) for c in node.children)
    for node in nodes:
        op,c = node.operation,node.children
        if op in arities:
            if len(c) not in arities[op]:return False
        elif op in {'ADDCOLUMNS','SELECTCOLUMNS'}:
            if len(c)<3 or len(c)%2!=1:return False
        elif op == 'SWITCH':
            if len(c)<3:return False
        elif op == 'CALCULATE':
            if not c:return False
        elif op != 'REMOVEFILTERS' and op not in structural:return False
        if op == 'DIVIDE' and len(c)==3 and not (_plain(c[2]).operation=='literal' or _plain(c[2]).operation=='BLANK'):return False
        if op in {'IF','NOT'} and c[0].result_type not in {'boolean','unknown'}:return False
        if op == 'FILTER' and c[1].result_type not in {'boolean','unknown'}:return False
        if not node.supported and node.operation != 'variable_reference' and not unknown(node):return False
    return tree.operation != 'opaque'


def _preserved_callbacks(before, after):
    """A local rule must not move, duplicate or drop an opaque helper call."""
    def references(tree):
        result=[]
        def visit(node):
            if node.operation=='measure':result.append((node.target,node.row_context,node.filter_context))
            for child in node.children:visit(child)
        visit(tree)
        return result
    return references(before)==references(after)


def _digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


@dataclass
class RewritePlan:
    measures: list[dict]

    @property
    def candidates(self):
        return [candidate for measure in self.measures for candidate in measure['candidates']]

    def to_dict(self):
        return {'schema_version': '1.0', 'kind': 'guarded-logical-rewrite-plan',
                'interpretation': 'Partial logical representation and unvalidated proposals; no physical plan or predicted speedup.',
                'rules': RULES, 'measures': self.measures}

    def write(self, directory):
        from .report import _CSS
        from .comparison import DAG_CSS, write_candidate_dags
        import html
        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True)
        write_candidate_dags(self.candidates, root/'dag-comparisons')
        (root/'rewrite-plan.json').write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')
        details = ''.join('<details><summary>' + html.escape(m['measure']) + f" · {len(m['candidates'])} candidates</summary>" + render_rewrites(m) + '</details>' for m in self.measures)
        (root/'rewrites.html').write_text(f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Rewrite proposals</title><style>{_CSS}{DAG_CSS}</style></head><body><main><h1>Guarded rewrite proposals</h1><p>Unvalidated candidates. No source changes, DAX execution, or predicted speedup. Review the guards and validate candidates before timing.</p>{details}</main></body></html>', encoding='utf-8')
        return {'json': root/'rewrite-plan.json', 'html': root/'rewrites.html'}


def plan_rewrites(source, *, graph=None, ranked_ids=None, progress=None, measure=None, table=None):
    """Propose isolated edits and a bounded fixed-point combination per measure."""
    model = parse_model(source) if isinstance(source, dict) else source if isinstance(source, Model) else load_model(source)
    graph = graph or DependencyGraph(model, progress=progress)
    if table is not None and measure is None:
        raise ModelError('A table qualifier requires a selected measure')
    measures = [model.measure(measure,table)] if measure is not None else list(model.measures)
    if ranked_ids is not None:
        positions = {n: i for i, n in enumerate(ranked_ids)}
        measures.sort(key=lambda m: positions.get(m.id, len(positions)))
    output = []
    dynamic = any(e.kind in {'calculation_item', 'function'} for e in model.entities.values())
    from .progress import emit
    emit(progress, 'rewrites', 'Check rewrite proposals', completed=0, total=len(measures), unit='measures')
    for i, measure in enumerate(measures, 1):
        emit(progress, 'rewrites', 'Check rewrite proposals', completed=i-1, total=len(measures), unit='measures', detail=measure.qualified_name)
        before = measure.expression or ''
        tree, diagnostics = logical_plan(before, model, measure)
        notes = []
        if diagnostics and _type_gap_only(tree):
            notes.append('Some surrounding column types are unknown. Only independently guarded local rewrites are proposed; no surrounding type or runtime semantics are inferred.')
            diagnostics = []
        chain = [measure.id] + graph.dependency_ids(measure.id)
        if tree.result_type in {'table', 'unknown'} and not diagnostics:
            diagnostics.append('Measure scalar result type is not established in the logical subset.')
        if any(e.kind == 'format_string' and e.metadata.get('for_measure') == measure.id for e in model.entities.values()):
            diagnostics.append('Dynamic format expression requires additional alias-context validation; rewrites withheld.')
        for n in chain:
            info = graph.syntax.get(n)
            for call in info.calls if info else []:
                if call.name in {'NOW','TODAY','RAND','RANDBETWEEN'}:
                    entity = model.entities[n]
                    line = entity.expression.count('\n',0,call.start)+1
                    if call.name=='TODAY' and not call.arguments and entity.kind=='measure':
                        notes.append('TODAY() in '+entity.qualified_name+' at line '+str(line)+
                                     ' is preserved. Validation uses explicit fixed dates through query-scoped measure definitions; the generated measure keeps live TODAY().')
                    else:
                        diagnostics.append('Controlled evaluation required for '+call.name+'() in '+entity.qualified_name+
                                           ' at line '+str(line)+'. Automatic rewriting is withheld until date/time or random evaluation can be controlled.')
        if dynamic:
            diagnostics.append('Calculation groups or model functions are present; query-scoped rewrite validation is withheld.')
        issues = dependency_issues(model,graph.syntax,chain,cyclic=measure.id in graph.blocked)
        pending_helpers = [issue for issue in issues if issue['code']=='missing_definition' and issue['owner_id']!=measure.id]
        diagnostics.extend(issue['message'] for issue in issues if issue not in pending_helpers)
        if pending_helpers:
            notes.extend(issue['message'] for issue in pending_helpers)
            notes.append('Missing helpers remain opaque and unchanged. Independent local proposals are allowed, but validation queries require their actual definitions first.')
        candidates = []
        def candidate(after, steps, combined=False):
            if after == before or any(c['after'] == after for c in candidates):
                return
            after_tree, issues = logical_plan(after, model, measure)
            if (issues and not _type_gap_only(after_tree)) or not _preserved_callbacks(tree,after_tree):
                return
            def today_calls(text):
                return [(text[c.start:c.end],c.ancestors) for c in Resolver(model).scan(text,measure).calls if c.name=='TODAY']
            if today_calls(before)!=today_calls(after):return
            from .comparison import compare_measure_dags
            comparison = compare_measure_dags(model, measure.id, proposed_expression=after, graph=graph)
            candidates.append({'id': _digest(measure.id+'\x00'+before+'\x00'+after)[:20],
                               'measure_id': measure.id, 'measure': measure.qualified_name,
                               'before': before, 'after': after, 'before_sha256': _digest(before),
                               'after_sha256': _digest(after), 'steps': steps, 'combined': combined,
                               'logical_before': tree.to_dict(), 'logical_after': after_tree.to_dict(),
                               'direct_dependencies_before': sorted(graph.dependencies[measure.id]),
                               'direct_dependencies_after': sorted({r.target for r in Resolver(model).scan(after, measure).references}), 'validation_status': 'not_run',
                               'performance_status': 'unknown', 'predicted_speedup': None,
                               'validation_available': comparison['original']['dependency_complete'] and comparison['proposed']['dependency_complete'],
                               'pending_helpers': [issue['owner_id'] for issue in pending_helpers],
                               'guard_evidence': {'scope':'root-local structural proposal; not an equivalence proof',
                                   'helper_callbacks_preserved':True,'today_calls_preserved':True,
                                   'missing_helper_definitions_assumed':False},
                               'dag_comparison': comparison})
        if not diagnostics:
            for rule, start, end, replacement in _matches(tree, before, model)[:32]:
                candidate(before[:start]+replacement+before[end:], [{'rule': rule, 'source_span': [start, end],
                                                                      'before': before[start:end], 'after': replacement}])
            current, steps = before, []
            for _ in range(16):
                current_tree, issues = logical_plan(current, model, measure)
                matches = _matches(current_tree, current, model) if not issues or _type_gap_only(current_tree) else []
                if not matches:
                    break
                rule, start, end, replacement = matches[0]
                steps.append({'rule': rule, 'source_span': [start, end], 'before': current[start:end], 'after': replacement})
                current = current[:start]+replacement+current[end:]
            if len(steps) > 1:
                candidate(current, steps, True)
            if len(steps) == 16:
                diagnostics.append('Combined search reached its 16-step bound; further rewrites may exist.')
        output.append({'measure_id': measure.id, 'measure': measure.qualified_name,
                       'logical_before': tree.to_dict(), 'diagnostics': diagnostics+notes,
                       'affected_downstream_measures': [m.id for m in model.measures if m.id != measure.id and graph.mask(m.id) & (1 << graph.index[measure.id])],
                       'candidates': candidates})
        emit(progress, 'rewrites', 'Check rewrite proposals', completed=i, total=len(measures), unit='measures', detail=measure.qualified_name)
    return RewritePlan(output)


def render_rewrites(measure):
    """Escaped report fragment shared by standalone and ranked reports."""
    import html
    esc = lambda value: html.escape(str(value), quote=True)
    diagnostics = ''.join(f'<li>{esc(x)}</li>' for x in measure['diagnostics'])
    proposals = []
    for c in measure['candidates']:
        from .comparison import render_dag_comparison
        dags = ('<details><summary>Original and proposed measure DAGs</summary>' + render_dag_comparison(c['dag_comparison']) +
                '<p><a href="dag-comparisons/'+c['id']+'/report.html">Open comparison and download DAGs</a></p></details>') if c.get('dag_comparison') else ''
        rules = ', '.join(step['rule'] for step in c['steps'])
        proposals.append(f'<details><summary>{esc(rules)} · {"combined" if c["combined"] else "isolated"} · validation not run</summary><p>Performance unknown. Compare exact results and BLANK behavior before interpreting timings.</p><h4>Before</h4><pre>{esc(c["before"])}</pre><h4>Candidate</h4><pre>{esc(c["after"])}</pre>{dags}<details><summary>Rewrite steps and guards</summary><pre>{esc(json.dumps(c["steps"], indent=2))}</pre><pre>{esc(json.dumps({r: RULES[r] for r in {s["rule"] for s in c["steps"]}}, indent=2))}</pre></details><details><summary>Logical plan after</summary><pre>{esc(json.dumps(c["logical_after"], indent=2))}</pre></details></details>')
    return '<ul>'+diagnostics+'</ul>'+(''.join(proposals) or '<p>No supported rewrite candidate.</p>') + '<details><summary>Partial logical plan before</summary><pre>'+esc(json.dumps(measure['logical_before'], indent=2))+'</pre></details>'
