"""Read common exported TOM metadata without executing DAX, M, SQL, or scripts."""
from __future__ import annotations

import base64
import json
import re
from pathlib import Path

from .model import Entity, Model, ModelError, Relationship

_NAME = r"(?:'(?:''|[^'])*'|[^\s=:]+)"
_DECL = re.compile(rf"^(table|measure|column|calculationItem|function|partition|relationship|calculationGroup|formatStringDefinition)\b\s*({_NAME})?\s*(?:=\s*(.*))?$", re.I)
_PROP = re.compile(r"^(\w+)\s*:\s*(.*)$")
_EXPR_PROP = re.compile(r"^(source|expression)\s*=\s*(.*)$", re.I)


def _name(value: str | None) -> str:
    value = (value or "").strip()
    return value[1:-1].replace("''", "'") if value.startswith("'") and value.endswith("'") else value


def _expression(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, list) and all(isinstance(x, str) for x in value):
        return "\n".join(value)
    if isinstance(value, str):
        return value
    raise ModelError("Expected an expression string or array of expression lines")


def _decode(data: bytes) -> str:
    if data.startswith((b'\xff\xfe', b'\xfe\xff')):
        return data.decode('utf-16')
    try:
        return data.decode('utf-8-sig')
    except UnicodeError as exc:
        raise ModelError("Input must be UTF-8 or BOM-marked UTF-16 exported model text") from exc


def _add_format(model: Model, owner: Entity, obj: dict) -> None:
    fmt = obj.get("formatStringDefinition") or {}
    if fmt.get("expression") is not None:
        model.add(Entity("format_string", owner.table, owner.name + " (format string)",
                         _expression(fmt['expression']), owner.source, owner.line,
                         {"for_measure": owner.id} if owner.kind == 'measure' else {}))


def _json_model(raw: dict, source: str) -> Model:
    candidates = []
    def find(obj):
        if isinstance(obj, dict):
            if isinstance(obj.get('tables'), list):
                candidates.append(obj)
                return
            for key in ('model', 'database', 'createOrReplace', 'create', 'alter', 'sequence', 'operations'):
                if key in obj:
                    find(obj[key])
        elif isinstance(obj, list):
            for child in obj:
                find(child)
    find(raw)
    if len(candidates) != 1:
        raise ModelError(f"Expected one BIM/TMSL model; found {len(candidates)}. Export a complete model, not multiple operations/models.")
    obj = candidates[0]
    model = Model(str(raw.get('name', obj.get('name', 'Model'))), source_format='bim/tmsl')
    if obj.get('defaultMode'):
        model.warnings.append(f"Model defaultMode={obj['defaultMode']}; partitions without mode use this default.")
    for table in obj['tables']:
        t = model.add_table(table['name'], source)
        for partition in table.get('partitions', []):
            mode = partition.get('mode', obj.get('defaultMode'))
            if mode:
                t.storage_modes.add(str(mode).lower())
            ps = partition.get('source') or {}
            if str(ps.get('type', '')).casefold() == 'calculated':
                model.entities[t.id].expression = _expression(ps.get('expression'))
        for column in table.get('columns', []):
            model.add(Entity('column', t.name, column['name'], _expression(column.get('expression')), source,
                             metadata={'data_type': column.get('dataType'), 'source_column': column.get('sourceColumn'),
                                       'expression_context': column.get('expressionContext')}))
        for measure in table.get('measures', []):
            entity = model.add(Entity('measure', t.name, measure['name'], _expression(measure.get('expression')), source,
                                      metadata={'format_string': measure.get('formatString'), 'description': measure.get('description')}))
            _add_format(model, entity, measure)
        calc = table.get('calculationGroup') or {}
        for item in calc.get('calculationItems', []):
            entity = model.add(Entity('calculation_item', t.name, item['name'], _expression(item.get('expression')), source))
            _add_format(model, entity, item)
    for function in obj.get('functions', []):
        model.add(Entity('function', '', function['name'], _expression(function.get('expression')), source))
    for r in obj.get('relationships', []):
        model.relationships.append(Relationship(str(r.get('name', '')), r.get('fromTable', ''), r.get('fromColumn', ''),
                                                r.get('toTable', ''), r.get('toColumn', ''), r.get('isActive', True),
                                                r.get('crossFilteringBehavior', 'oneDirection'),
                                                r.get('fromCardinality', 'many'), r.get('toCardinality', 'one')))
    if obj.get('roles'):
        model.warnings.append('Roles/RLS are present. Permission filter expressions are not part of this measure-lineage projection.')
    return model


def _catalog(raw: dict, source: str) -> Model:
    model = Model('Measure catalog', source_format='measure-catalog')
    model.warnings.append('Partial measure catalog: missing columns, relationships and definitions remain unknown.')
    for name, info in raw.get('confirmed_table_storage', {}).items():
        model.add_table(name, source).storage_modes.add(str(info.get('mode', 'unknown')).lower())
    for measure in raw['measures']:
        model.add(Entity('measure', measure.get('table', 'Measures'), measure['name'],
                         _expression(measure.get('expression')), source,
                         metadata={'definition_status': measure.get('definition_status', 'unknown')}))
    return model


def _body(lines: list[str], i: int, indent: int, inline: str | None) -> tuple[str | None, int]:
    """Read a default expression, keeping object properties out of the DAX."""
    inline = inline if inline is not None else ''
    if inline.strip() == '```':
        end = i + 1
        while end < len(lines) and lines[end].strip() != '```':
            end += 1
        if end == len(lines):
            raise ModelError(f"Unclosed TMDL expression fence at line {i + 1}")
        boundary = len(lines[end]) - len(lines[end].lstrip())
        expr = '\n'.join(line[boundary:] if line[:boundary].isspace() else line for line in lines[i + 1:end])
        return expr, end + 1
    chunks = [inline] if inline else []
    end = i + 1
    # Expression indentation is one level deeper than object properties.
    while end < len(lines):
        line = lines[end]
        leading = len(line) - len(line.lstrip())
        if line.strip() and leading < indent + 8:
            break
        chunks.append(line[indent + 8:] if leading >= indent + 8 else '')
        end += 1
    return ('\n'.join(chunks).rstrip() or None), end


def _column_ref(text: str) -> tuple[str, str]:
    match = re.fullmatch(rf"\s*({_NAME})\.({_NAME})\s*", text)
    if not match:
        raise ModelError(f"Cannot parse TMDL relationship column reference: {text!r}")
    return _name(match[1]), _name(match[2])


def _tmdl_documents(documents: list[tuple[str, str]]) -> Model:
    model = Model(source_format='tmdl')
    for source, content in sorted(documents):
        lines = content.expandtabs(4).splitlines()
        stack: list[tuple[int, str, object]] = []
        i = 0
        while i < len(lines):
            text = lines[i].strip()
            indent = len(lines[i]) - len(lines[i].lstrip())
            if not text or text.startswith(('//', '///')):
                i += 1
                continue
            while stack and stack[-1][0] >= indent:
                stack.pop()
            match = _DECL.fullmatch(text)
            if match:
                kind, name, inline = match.groups()
                kind = kind.lower()
                name = _name(name)
                table = next((value for _, typ, value in reversed(stack) if typ == 'table'), None)
                obj = None
                if kind == 'table':
                    obj = model.add_table(name, source)
                elif kind == 'relationship':
                    obj = Relationship(name)
                    model.relationships.append(obj)
                elif kind in {'measure', 'column', 'calculationitem', 'function'}:
                    if kind != 'function' and table is None:
                        raise ModelError(f"{source}:{i + 1}: {kind} has no owning table")
                    expr, following = _body(lines, i, indent, inline) if inline is not None else (None, i + 1)
                    obj = model.add(Entity({'calculationitem': 'calculation_item'}.get(kind, kind),
                                           table.name if table else '', name, expr, source, i + 1))
                    stack.append((indent, kind, obj))
                    i = following
                    continue
                elif kind == 'partition':
                    obj = {'table': table, 'type': (inline or '').casefold()}
                elif kind == 'formatstringdefinition':
                    owner = next((value for _, typ, value in reversed(stack)
                                  if typ in {'measure', 'calculationitem'}), None)
                    obj = {'owner': owner}
                if obj is not None or kind == 'calculationgroup':
                    stack.append((indent, kind, obj))
                i += 1
                continue
            prop = _PROP.match(text)
            expr_prop = _EXPR_PROP.match(text)
            if prop and stack:
                key, value = prop.groups()
                typ, owner = stack[-1][1:]
                if typ == 'partition' and key.casefold() == 'mode' and owner['table']:
                    owner['table'].storage_modes.add(value.casefold())
                elif typ == 'relationship':
                    k = key.casefold()
                    if k in {'fromcolumn', 'tocolumn'}:
                        t, c = _column_ref(value)
                        setattr(owner, 'from_table' if k == 'fromcolumn' else 'to_table', t)
                        setattr(owner, 'from_column' if k == 'fromcolumn' else 'to_column', c)
                    elif k in {'isactive', 'crossfilteringbehavior', 'fromcardinality', 'tocardinality'}:
                        attr = {'isactive': 'active', 'crossfilteringbehavior': 'cross_filter',
                                'fromcardinality': 'from_cardinality', 'tocardinality': 'to_cardinality'}[k]
                        setattr(owner, attr, value.casefold() == 'true' if k == 'isactive' else value)
                elif isinstance(owner, Entity) and key.casefold() in {'datatype', 'formatstring', 'sourcecolumn', 'expressioncontext'}:
                    owner.metadata[key] = value
            elif expr_prop and stack:
                key, inline = expr_prop.groups()
                typ, owner = stack[-1][1:]
                # Property expression is one level below the declaration. Its
                # body is one additional level down, not two below this line.
                expr, following = _body(lines, i, indent - 4, inline)
                if typ == 'partition' and owner['type'] == 'calculated' and owner['table']:
                    model.entities[owner['table'].id].expression = expr
                elif typ == 'formatstringdefinition' and owner.get('owner'):
                    e = owner['owner']
                    model.add(Entity('format_string', e.table, e.name + ' (format string)', expr, source, i + 1,
                                     {'for_measure': e.id} if e.kind == 'measure' else {}))
                i = following
                continue
            elif re.match(r'^(role|tablePermission)\b', text, re.I):
                model.warnings.append(f'{source}:{i + 1}: security metadata is outside the measure-lineage projection.')
            i += 1
    if not model.tables and not model.entities:
        raise ModelError('No supported model definitions found in TMDL. Supply the full definition folder, not a perspective/reference file.')
    model.warnings.append('TMDL reader projects tables, columns, measures, partitions, relationships, functions and calculation items; it is not the TOM serializer or a full TMDL validator.')
    return model


def _parse_model(data: dict, source: str = '<memory>') -> Model:
    """Parse a BIM/TMSL object, Fabric definition envelope, or measure catalog."""
    if not isinstance(data, dict):
        raise ModelError('Expected a model JSON object')
    definition = data.get('definition', data)
    if isinstance(definition, dict) and isinstance(definition.get('parts'), list):
        docs, bims = [], []
        for part in definition['parts']:
            path = part.get('path', '')
            if not path.lower().endswith(('.bim', '.tmdl')):
                continue
            if part.get('payloadType', 'InlineBase64') != 'InlineBase64':
                raise ModelError(f'Unsupported definition payload type for {path}')
            try:
                body = _decode(base64.b64decode(part['payload'], validate=True))
            except (ValueError, KeyError, UnicodeError) as exc:
                raise ModelError(f'Invalid definition payload for {path}') from exc
            if path.lower().endswith('.bim'):
                bims.append(json.loads(body))
            else:
                docs.append((source + ':' + path, body))
        if len(bims) == 1 and not docs:
            return _json_model(bims[0], source)
        if docs and not bims:
            return _tmdl_documents(docs)
        raise ModelError('Definition must contain exactly one BIM model or a TMDL document set, not both')
    if 'measures' in data and 'tables' not in data and 'model' not in data:
        return _catalog(data, source)
    return _json_model(data, source)


def parse_model(data: dict, source: str = '<memory>') -> Model:
    """Read supported model dictionaries; malformed metadata raises ModelError."""
    try:
        return _parse_model(data, source)
    except ModelError:
        raise
    except (KeyError, TypeError, AttributeError, UnicodeError, json.JSONDecodeError) as exc:
        raise ModelError(f'Malformed model metadata in {source}: {exc}') from exc


def load_model(path: str | Path, *, max_file_bytes: int = 64 * 1024 * 1024) -> Model:
    """Read a .bim, TMSL/Fabric JSON, .tmdl, PBIP, or one semantic-model directory.

    PBIX/PBIT binary containers are deliberately not decoded. Export to PBIP/TMDL.
    No network access or evaluation of partition source expressions occurs.
    """
    path = Path(path)
    if not path.exists():
        raise ModelError(f'Model path does not exist: {path}')
    if path.suffix.casefold() in {'.pbix', '.pbit'}:
        raise ModelError('Binary PBIX/PBIT is not supported. Save/export the model as PBIP/TMDL or model.bim.')
    if path.suffix.casefold() == '.pbip' or path.is_dir():
        root = path.parent if path.is_file() else path
        candidates = sorted(p for p in root.iterdir() if p.is_dir() and p.name.casefold().endswith('.semanticmodel'))
        if len(candidates) > 1:
            raise ModelError('Multiple semantic models found. Pass the desired .SemanticModel directory explicitly.')
        if candidates:
            root = candidates[0]
        if (root / 'definition').is_dir():
            root = root / 'definition'
        files = sorted(root.rglob('*.tmdl'))
        bims = sorted(root.glob('*.bim'))
        if files and bims:
            raise ModelError('Both BIM and TMDL found. Pass the intended file/folder explicitly.')
        if files:
            docs = []
            for f in files:
                if f.stat().st_size > max_file_bytes:
                    raise ModelError(f'Input exceeds max_file_bytes: {f}')
                docs.append((str(f), _decode(f.read_bytes())))
            return _tmdl_documents(docs)
        if len(bims) == 1:
            path = bims[0]
        else:
            raise ModelError(f'Expected TMDL files or one model.bim in {root}')
    if path.stat().st_size > max_file_bytes:
        raise ModelError(f'Input exceeds max_file_bytes: {path}')
    body = _decode(path.read_bytes())
    if path.suffix.casefold() == '.tmdl':
        return _tmdl_documents([(str(path), body)])
    try:
        return parse_model(json.loads(body), str(path))
    except json.JSONDecodeError as exc:
        raise ModelError(f'Invalid model JSON in {path}: {exc}') from exc
