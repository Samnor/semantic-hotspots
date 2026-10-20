"""Deterministic source-free tables and a separate Import-mode benchmark model.

Data generation does not evaluate DAX. Relationships constrain shared key domains;
unsupported calculated-key relationships fail rather than inventing computed keys.
"""
from __future__ import annotations

import copy
import csv
import hashlib
import json
import math
import random
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from .analyzer import analyze
from .model import Model, ModelError
from .parser import load_model, parse_model

_TYPES = {'int64': 'Int64.Type', 'double': 'type number', 'decimal': 'Currency.Type',
          'string': 'type text', 'boolean': 'type logical', 'datetime': 'type datetime'}
_CANONICAL = {'int64': 'int64', 'double': 'double', 'decimal': 'decimal',
              'string': 'string', 'boolean': 'boolean', 'datetime': 'dateTime'}


@dataclass
class SimulationConfig:
    seed: int = 42
    fact_rows: int = 10000
    dimension_rows: int = 100
    tables: dict = field(default_factory=dict)
    workloads: list[dict] = field(default_factory=list)
    matrices: list[dict] = field(default_factory=list)
    top: int = 20
    data_root: str = r'C:\semantic-hotspots\mock\tables'

    @classmethod
    def from_dict(cls, value: dict) -> SimulationConfig:
        if not isinstance(value, dict):
            raise ModelError('Simulation config must be a JSON object')
        unknown = set(value) - set(cls.__dataclass_fields__)
        if unknown:
            raise ModelError('Unknown simulation config settings: ' + ', '.join(sorted(unknown)))
        return cls(**value)


@dataclass
class SimulationBundle:
    directory: Path
    manifest: dict

    @property
    def model_path(self) -> Path:
        return self.directory / 'synthetic-model.bim'

    @property
    def report_path(self) -> Path:
        return self.directory / 'simulation.html'


def _positive(value, name, allow_zero=False):
    if isinstance(value, bool) or not isinstance(value, int) or value < (0 if allow_zero else 1):
        raise ModelError(f'{name} must be an integer >= {0 if allow_zero else 1}')
    return value


def _fraction(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ModelError(f'{name} must be a finite fraction from 0 to 1')
    return value


def _filename(prefix, value, suffix):
    return prefix + hashlib.sha256(value.encode('utf-8')).hexdigest()[:20] + suffix


def _file_hash(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _m_string(value):
    return '"' + str(value).replace('#', '#(#)').replace('"', '""').replace('\r', '#(cr)').replace('\n', '#(lf)').replace('\t', '#(tab)') + '"'


def _column_type(entity, override):
    metadata = {k.casefold(): v for k, v in entity.metadata.items()}
    value = override.get('type') or metadata.get('data_type') or metadata.get('datatype')
    return str(value).casefold() if value else None


def _template(source, model):
    """Keep full BIM model metadata where available; never retain source partitions."""
    raw = source if isinstance(source, dict) else None
    if raw is None and model.source_format == 'bim/tmsl':
        paths = {e.source for e in model.entities.values() if e.source}
        if len(paths) == 1:
            path = Path(next(iter(paths)))
            if path.is_file():
                from .parser import _decode
                raw = json.loads(_decode(path.read_bytes()))
    if raw is None:
        return None, 1601
    found = []
    compatibility = raw.get('compatibilityLevel', 1601)
    def visit(obj):
        nonlocal compatibility
        if isinstance(obj, dict):
            compatibility = obj.get('compatibilityLevel', compatibility)
            if isinstance(obj.get('tables'), list):
                found.append(obj)
            else:
                for key in ('model', 'database', 'createOrReplace', 'create', 'alter', 'sequence', 'operations'):
                    if key in obj:
                        visit(obj[key])
        elif isinstance(obj, list):
            for child in obj:
                visit(child)
    visit(raw)
    return (copy.deepcopy(found[0]), compatibility) if len(found) == 1 else (None, 1601)


def _project(model):
    if any(e.kind in {'calculation_item', 'function', 'format_string'} for e in model.entities.values()):
        raise ModelError('Simulation export of calculation groups, UDFs or dynamic formats requires a full BIM/TMSL input, not the metadata projection')
    tables = []
    for table in model.tables.values():
        columns = []
        for e in model.entities.values():
            if e.kind == 'column' and e.table == table.name:
                col = {'name': e.name}
                typ = _column_type(e, {})
                if typ:
                    col['dataType'] = _CANONICAL.get(typ, typ)
                if e.expression:
                    col.update(type='calculated', expression=e.expression)
                else:
                    col['sourceColumn'] = e.name
                columns.append(col)
        measures = []
        for m in model.measures:
            if m.table == table.name:
                measure = {'name': m.name, 'expression': m.expression}
                fmt = m.metadata.get('format_string', m.metadata.get('formatString'))
                if fmt is not None:
                    measure['formatString'] = fmt
                measures.append(measure)
        obj = {'name': table.name, 'columns': columns, 'measures': measures}
        if model.entities[table.id].expression:
            obj['partitions'] = [{'name': table.name, 'source': {'type': 'calculated', 'expression': model.entities[table.id].expression}}]
        tables.append(obj)
    return {'culture': 'en-US', 'tables': tables, 'relationships': [
        {'name': r.name, 'fromTable': r.from_table, 'fromColumn': r.from_column,
         'toTable': r.to_table, 'toColumn': r.to_column, 'fromCardinality': r.from_cardinality,
         'toCardinality': r.to_cardinality, 'isActive': r.active, 'crossFilteringBehavior': r.cross_filter}
        for r in model.relationships]}


class _Domain:
    def __init__(self, typ, size, spec, *, key_domain=False):
        self.typ, self.size, self.spec = typ, size, spec
        self.key_domain = key_domain
        values = spec.get('values')
        self.values = values
        if values is not None:
            if not isinstance(values, list) or not values or any(v is None for v in values):
                raise ModelError('values must be a nonempty list without null; use null_fraction for blanks')
            if len({json.dumps(v, sort_keys=True) for v in values}) != len(values):
                raise ModelError('Explicit domain values must be distinct')
            self.size = len(values)
            for v in values:
                if typ == 'string' and (not isinstance(v, str) or not v):
                    raise ModelError('String domain values must be nonempty strings')
                if typ == 'boolean' and not isinstance(v, bool):
                    raise ModelError('Boolean domain values must be JSON true/false')
                if typ in {'int64', 'double', 'decimal'} and (isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)):
                    raise ModelError('Numeric domain values must be finite numbers')
                if typ == 'int64' and (not isinstance(v, int) or not -(2**63) <= v < 2**63):
                    raise ModelError('int64 values must fit a signed 64-bit integer')
                if typ == 'datetime':
                    self._date(v)
                if typ == 'decimal' and abs(v) > 922337203685477:
                    raise ModelError('decimal values must fit fixed-decimal Currency.Type')
            if typ == 'decimal':
                self.values = values = [round(v, 4) for v in values]
            if len(set(values)) != len(values):
                raise ModelError('Explicit domain values must be distinct after type conversion')
        if typ == 'boolean':
            self.size = min(self.size, 2)
        self.minimum = spec.get('min', 1 if typ == 'int64' else 0)
        self.maximum = spec.get('max', self.minimum + max(size - 1, 1))
        if typ in {'int64', 'double', 'decimal'}:
            for bound in [self.minimum, self.maximum]:
                if isinstance(bound, bool) or not isinstance(bound, (int, float)) or not math.isfinite(bound):
                    raise ModelError('Numeric min/max must be finite numbers')
            if self.maximum < self.minimum:
                raise ModelError('max must be >= min')
            if typ != 'int64' and not math.isfinite(self.maximum - self.minimum):
                raise ModelError('Numeric range is too wide for finite generated values')
            if typ == 'decimal' and max(abs(self.minimum), abs(self.maximum)) > 922337203685477:
                raise ModelError('decimal min/max must fit fixed-decimal Currency.Type')
            if typ == 'int64':
                if not all(isinstance(v, int) and -(2**63) <= v < 2**63 for v in [self.minimum, self.maximum]):
                    raise ModelError('int64 min/max must fit signed 64-bit integers')
                self.size = min(self.size, self.maximum - self.minimum + 1)
            if self.maximum == self.minimum:
                self.size = 1
        self.start = self._date(spec.get('start', '2024-01-01'))
        self.end = self._date(spec.get('end', '2026-12-31'))
        if self.end < self.start:
            raise ModelError('Date end must be >= start')
        if typ == 'datetime' and values is None:
            self.size = min(self.size, (self.end - self.start).days + 1)

    @staticmethod
    def _date(value):
        if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            raise ModelError('Dates must be ISO date strings')
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ModelError('Dates must be YYYY-MM-DD strings') from exc

    def value(self, index):
        if self.values is not None:
            result = self.values[index]
        elif self.typ == 'string':
            result = f'Category_{index + 1:08d}'
        elif self.typ == 'boolean':
            result = bool(index)
        elif self.typ == 'datetime':
            # Date keys stay contiguous; ordinary date domains span the requested window.
            days = index if self.key_domain else index * (self.end - self.start).days // max(self.size - 1, 1)
            result = (self.start + timedelta(days=days)).isoformat()
        elif self.typ == 'int64':
            result = self.minimum + index
        else:
            result = self.minimum + (self.maximum - self.minimum) * index / max(self.size - 1, 1)
            result = round(result, 4 if self.typ == 'decimal' else 6)
        if self.typ == 'datetime':
            return str(result) + 'T00:00:00'
        if self.typ == 'boolean':
            return 'true' if result else 'false'
        return result


def _dax_literal(value, typ=None):
    if value is None:
        return 'BLANK()'
    if isinstance(value, bool):
        return 'TRUE()' if value else 'FALSE()'
    if isinstance(value, str):
        if typ == 'datetime':
            d = _Domain._date(value)
            return f'DATE({d.year}, {d.month}, {d.day})'
        return '"' + value.replace('"', '""') + '"'
    if isinstance(value, (int, float)) and math.isfinite(value):
        return str(value)
    raise ModelError('Workload filters require string, finite number, boolean or null values')


def _workload_queries(model, config, ranked):
    selected = ranked[:config.top]
    workloads = [{'name': 'total', 'group_by': [], 'filters': []}] + config.workloads
    names = set()
    columns = {(e.table, e.name): e for e in model.entities.values() if e.kind == 'column'}
    results = []
    def col(pair):
        if not isinstance(pair, (list, tuple)) or len(pair) != 2 or tuple(pair) not in columns:
            raise ModelError(f'Unknown workload column: {pair!r}; use [table, column]')
        return columns[tuple(pair)].qualified_name
    for w in workloads:
        if not isinstance(w, dict) or set(w) - {'name', 'group_by', 'filters'} or not isinstance(w.get('name'), str):
            raise ModelError('Workloads need name, optional group_by and filters')
        if w['name'] in names:
            raise ModelError('Workload names must be unique (total is reserved)')
        names.add(w['name'])
        groups = [col(pair) for pair in w.get('group_by', [])]
        if len(set(groups)) != len(groups):
            raise ModelError('Workload group_by columns must be distinct')
        filters = []
        for f in w.get('filters', []):
            if not isinstance(f, dict) or set(f) != {'table', 'column', 'values'} or not isinstance(f['values'], list) or not f['values']:
                raise ModelError('Workload filters need table, column and a nonempty values list')
            column_name = col([f['table'], f['column']])
            entity = columns[(f['table'], f['column'])]
            override = config.tables.get(entity.table, {}).get('columns', {}).get(entity.name, {})
            typ = _column_type(entity, override)
            filters.append('TREATAS({' + ', '.join(_dax_literal(v, typ) for v in f['values']) + '}, ' + column_name + ')')
        for row in selected:
            measure = model.entities[row['id']]
            query = ('EVALUATE\nSUMMARIZECOLUMNS(\n    ' + ',\n    '.join(groups + filters + ['"Result", ' + measure.qualified_name]) + '\n)\n')
            if not groups and not filters:
                query = 'EVALUATE\nROW("Result", ' + measure.qualified_name + ')\n'
            identity = hashlib.sha256((row['id'] + '\x00' + w['name']).encode()).hexdigest()[:20]
            results.append({'id': identity, 'measure_id': row['id'], 'measure': measure.qualified_name,
                            'scenario': w['name'], 'group_by': w.get('group_by', []), 'filters': w.get('filters', []),
                            'query_file': 'queries/query-' + identity + '.dax', 'query': query})
    return results


def simulate(source: Model | str | Path | dict, directory: str | Path, *,
             config: SimulationConfig | dict | None = None) -> SimulationBundle:
    """Write seeded CSVs, a disconnected Import BIM, workload queries and HTML.

    The output directory must be absent or empty. Generation streams rows to disk;
    actual column cardinality tracking is bounded at 10,000 values per column.
    """
    if config is None:
        config = SimulationConfig()
    elif isinstance(config, dict):
        config = SimulationConfig.from_dict(config)
    if not isinstance(config, SimulationConfig):
        raise ModelError('config must be SimulationConfig or a dictionary')
    _positive(config.fact_rows, 'fact_rows'); _positive(config.dimension_rows, 'dimension_rows')
    _positive(config.top, 'top')
    if not isinstance(config.seed, int) or isinstance(config.seed, bool):
        raise ModelError('seed must be an integer')
    if not isinstance(config.tables, dict) or not isinstance(config.workloads, list) or not isinstance(config.matrices, list):
        raise ModelError('tables must be an object; workloads and matrices must be lists')
    if not isinstance(config.data_root, str) or not config.data_root:
        raise ModelError('data_root must be a nonempty path on the engine machine')
    model = source if isinstance(source, Model) else parse_model(source) if isinstance(source, dict) else load_model(source)
    root = Path(directory)
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise ModelError('Simulation output must be a new or empty directory; existing files are not overwritten')
    if not model.tables:
        raise ModelError('Simulation needs table and column metadata, not an empty model')
    if model.source_format == 'measure-catalog':
        raise ModelError('Simulation needs a full table/column export; a partial measure catalog cannot establish source schemas')
    matrix_analysis = None
    if config.matrices:
        from .matrices import analyze_matrices
        matrix_analysis = analyze_matrices(model, config.matrices)
    raw, compatibility = _template(source, model)
    full_template = raw is not None
    if raw is None:
        raw = _project(model)
    if isinstance(compatibility, bool) or not isinstance(compatibility, int):
        raise ModelError('BIM compatibilityLevel must be an integer')
    warnings = ['Synthetic data and Import-mode timings are experimental evidence, not predictions of production latency or Fabric CU.',
                'All original source partitions, connections, M expressions, refresh policies and roles are excluded from the synthetic model.',
                'Default distributions are assumptions. Configure exact category values and representative workloads; generic categories can produce BLANKs.']
    if not full_template:
        warnings.append('BIM generated from the supported metadata projection. Supply a full BIM/TMSL export for better metadata fidelity.')
    warnings.extend(model.warnings)
    if compatibility < 1400:
        compatibility = 1601
        warnings.append('Synthetic compatibility level raised to 1601 to support M-based Import partitions; validate with the target engine.')
    unknown_tables = set(config.tables) - set(model.tables)
    if unknown_tables:
        raise ModelError('Unknown config tables: ' + ', '.join(sorted(unknown_tables)))
    table_objects = {t['name']: t for t in raw['tables']}
    calculated_tables = {t.name for t in model.tables.values() if model.entities[t.id].expression or table_objects[t.name].get('calculationGroup')}
    for name in calculated_tables:
        if any(p.get('source', {}).get('type') != 'calculated' for p in table_objects[name].get('partitions', [])):
            raise ModelError(f'{name}: calculated objects with non-calculated source partitions cannot be isolated safely')
    physical = {e.id: e for e in model.entities.values() if e.kind == 'column' and not e.expression and e.table not in calculated_tables}
    all_columns = {(e.table.casefold(), e.name.casefold()): e for e in model.entities.values() if e.kind == 'column'}
    one_keys = set()
    relations = []
    for r in model.relationships:
        ends = []
        cards = [r.from_cardinality.casefold(), r.to_cardinality.casefold()]
        if any(c not in {'one', 'many'} for c in cards):
            raise ModelError(f'Unsupported relationship cardinality: {r.name}')
        for table, column in [(r.from_table, r.from_column), (r.to_table, r.to_column)]:
            entity = all_columns.get((table.casefold(), column.casefold()))
            if entity is None or entity.id not in physical:
                raise ModelError(f'Relationship {r.name}: synthetic keys require physical columns; {table}[{column}] is missing or calculated')
            ends.append(entity.id)
        one_keys.update(n for n, card in zip(ends, cards) if card == 'one')
        relations.append((r, ends, cards))
    lookup_tables = {physical[n].table for n in one_keys}
    row_counts, specs, types = {}, {}, {}
    allowed_column = {'type', 'values', 'distinct', 'null_fraction', 'skew', 'min', 'max', 'start', 'end'}
    for table in model.tables.values():
        table_spec = config.tables.get(table.name, {})
        if not isinstance(table_spec, dict) or set(table_spec) - {'rows', 'columns'}:
            raise ModelError(f'{table.name}: allowed table settings are rows and columns')
        cols = table_spec.get('columns', {})
        if not isinstance(cols, dict):
            raise ModelError('Table columns config must be an object')
        actual = {e.name for e in physical.values() if e.table == table.name}
        if set(cols) - actual:
            raise ModelError(f'{table.name}: unknown, calculated or non-source column overrides: {sorted(set(cols) - actual)}')
        default = config.dimension_rows if table.name in lookup_tables else config.fact_rows
        if not actual:
            default = 1
        row_counts[table.name] = _positive(table_spec.get('rows', default), f'{table.name}.rows')
        for entity in physical.values():
            if entity.table != table.name:
                continue
            spec = cols.get(entity.name, {})
            if not isinstance(spec, dict) or set(spec) - allowed_column:
                raise ModelError(f'{entity.qualified_name}: unknown column settings')
            specs[entity.id] = spec
            types[entity.id] = _column_type(entity, spec)
            if types[entity.id] is not None and types[entity.id] not in _TYPES:
                raise ModelError(f'Unsupported synthetic data type {types[entity.id]} for {entity.qualified_name}')
            _fraction(spec.get('null_fraction', 0), 'null_fraction'); _fraction(spec.get('skew', 0), 'skew')
            if 'distinct' in spec:
                _positive(spec['distinct'], 'distinct')
            if entity.id in one_keys and (spec.get('null_fraction', 0) or spec.get('skew', 0)):
                raise ModelError('One-side keys must be unique and non-null; remove null_fraction/skew')
    parent = {n: n for n in physical}
    def find(n):
        while parent[n] != n:
            parent[n] = parent[parent[n]]
            n = parent[n]
        return n
    for _, ends, _ in relations:
        a, b = map(find, ends)
        parent[b] = a
    components = {}
    for n in physical:
        components.setdefault(find(n), []).append(n)
    domains, limits = {}, {}
    for nodes in components.values():
        keys = [n for n in nodes if n in one_keys]
        specified_types = {types[n] for n in nodes if types[n]}
        if len(specified_types) > 1:
            raise ModelError('Related columns must have matching synthetic types: ' + ', '.join(physical[n].qualified_name for n in nodes))
        typ = next(iter(specified_types), 'int64' if keys or len(nodes) > 1 else 'string')
        for n in nodes:
            if types[n] is None:
                warnings.append(f'{physical[n].qualified_name}: missing data type assumed {typ}; override it in config.')
            types[n] = typ
        # Key domains are shared by all connected endpoints, including inactive relationships.
        specified = [{k: v for k, v in specs[n].items() if k not in {'type', 'null_fraction', 'skew'}} for n in nodes]
        domain_spec = {}
        for spec in specified:
            for k, v in spec.items():
                if k in domain_spec and domain_spec[k] != v:
                    raise ModelError('Conflicting shared-key domain settings; configure related endpoints consistently')
                domain_spec[k] = v
        required = max((row_counts[physical[n].table] for n in keys), default=1)
        size = domain_spec.get('distinct', max(required, min(100, *(row_counts[physical[n].table] for n in nodes)) ))
        if 'values' in domain_spec:
            size = len(domain_spec['values']) if isinstance(domain_spec['values'], list) else 0
        domain = _Domain(typ, size, domain_spec, key_domain=bool(keys))
        if keys and domain.size < required:
            raise ModelError('Key domain is too small for one-side uniqueness; increase distinct/values/date range or reduce rows')
        if keys and typ == 'string' and domain.values is not None and len({v.casefold() for v in domain.values}) != len(domain.values):
            raise ModelError('String key values must be distinct under case-insensitive comparison')
        # Rounded floating point values can collide: verify unique keys before writing files.
        if keys and typ in {'double', 'decimal'}:
            previous = None
            for index in range(required):
                value = domain.value(index)
                if index and value == previous:
                    raise ModelError('Numeric key domain loses uniqueness through rounding; use int64 or a wider range')
                previous = value
        common = min((row_counts[physical[n].table] for n in keys), default=domain.size)
        for n in nodes:
            domains[n] = domain
            limits[n] = row_counts[physical[n].table] if n in one_keys else min(common, domain.size)
    for r, ends, cards in relations:
        if cards == ['one', 'one'] and row_counts[physical[ends[0]].table] != row_counts[physical[ends[1]].table]:
            raise ModelError(f'One-to-one relationship {r.name} needs equal configured row counts for complete matching keys')
    analysis = analyze(model)
    workloads = _workload_queries(model, config, analysis.ranked)
    if matrix_analysis:
        workloads += copy.deepcopy(matrix_analysis.workloads)
        warnings.append('Matrix queries are representative multi-measure contexts; aggregation advice is separate and no aggregate table is built.')
    # Validate all settings and schemas before creating output files.
    root.mkdir(parents=True, exist_ok=True)
    (root/'tables').mkdir(); (root/'queries').mkdir()
    table_summaries = []
    for table in model.tables.values():
        if table.name in calculated_tables:
            table_summaries.append({'name': table.name, 'kind': 'engine-calculated', 'rows': None, 'columns': []})
            continue
        columns = [e for e in physical.values() if e.table == table.name]
        filename = _filename('table-', table.name, '.csv')
        column_stats = {e.id: {'name': e.name, 'type': _CANONICAL[types[e.id]], 'domain_size': limits[e.id], 'nulls': 0, 'observed': set(), 'distinct_complete': True} for e in columns}
        rngs = {e.id: random.Random(int.from_bytes(hashlib.sha256(f'{config.seed}:{e.id}'.encode()).digest(), 'big')) for e in columns}
        with (root/'tables'/filename).open('w', encoding='utf-8', newline='') as stream:
            writer = csv.writer(stream, lineterminator='\n')
            writer.writerow([e.name for e in columns] or ['__SyntheticRow'])
            for index in range(row_counts[table.name]):
                record = []
                for e in columns:
                    rng, spec, domain, count = rngs[e.id], specs[e.id], domains[e.id], limits[e.id]
                    if e.id not in one_keys and rng.random() < spec.get('null_fraction', 0):
                        value = ''
                        column_stats[e.id]['nulls'] += 1
                    else:
                        chosen = index if e.id in one_keys else (rng.randrange(max(1, int(count*.05))) if rng.random() < spec.get('skew', 0) else rng.randrange(count))
                        value = domain.value(chosen)
                        stat = column_stats[e.id]
                        if len(stat['observed']) < 10000:
                            stat['observed'].add(value)
                        elif value not in stat['observed']:
                            stat['distinct_complete'] = False
                    record.append(value)
                writer.writerow(record or [index + 1])
        for stat in column_stats.values():
            stat['distinct_observed'] = len(stat.pop('observed'))
        summary = {'name': table.name, 'kind': 'synthetic-physical', 'rows': row_counts[table.name],
                   'csv': 'tables/' + filename, 'sha256': _file_hash(root/'tables'/filename),
                   'columns': list(column_stats.values()), 'dummy_row_column': not columns}
        table_summaries.append(summary)
        obj = table_objects[table.name]
        if not columns:
            obj.setdefault('columns', []).append({'name': '__SyntheticRow', 'sourceColumn': '__SyntheticRow', 'dataType': 'int64', 'isHidden': True})
        physical_by_name = {e.name: e for e in columns}
        type_pairs = []
        for column in obj.get('columns', []):
            if column['name'] in physical_by_name:
                e = physical_by_name[column['name']]
                column['sourceColumn'] = e.name
                column['dataType'] = _CANONICAL[types[e.id]]
                type_pairs.append('{' + _m_string(e.name) + ', ' + _TYPES[types[e.id]] + '}')
        if not columns:
            type_pairs.append('{"__SyntheticRow", Int64.Type}')
        headers = '{' + ', '.join(_m_string(e.name) for e in columns) + '}' if columns else '{"__SyntheticRow"}'
        expression = ('let\n    Source = Csv.Document(File.Contents(SyntheticDataRoot & "/" & ' + _m_string(filename) + '), [Delimiter=",", Encoding=65001, QuoteStyle=QuoteStyle.Csv]),\n'
                      '    Headers = Table.PromoteHeaders(Source, [PromoteAllScalars=true]),\n'
                      '    Blanks = Table.ReplaceValue(Headers, "", null, Replacer.ReplaceValue, ' + headers + '),\n'
                      '    Typed = Table.TransformColumnTypes(Blanks, {' + ', '.join(type_pairs) + '}, "en-US")\nin\n    Typed')
        obj['partitions'] = [{'name': 'Synthetic data', 'mode': 'import', 'source': {'type': 'm', 'expression': expression}}]
    for obj in table_objects.values():
        obj.pop('refreshPolicy', None)
        obj.pop('excludeFromModelRefresh', None)
        for partition in obj.get('partitions', []):
            partition['mode'] = 'import'
    for key in ['dataSources', 'expressions', 'roles', 'annotations', 'queryGroups']:
        raw.pop(key, None)
    raw['defaultMode'] = 'import'
    raw['expressions'] = [{'name': 'SyntheticDataRoot', 'kind': 'm', 'expression': _m_string(config.data_root)}]
    name = 'Synthetic_' + hashlib.sha256(model.name.encode()).hexdigest()[:12]
    bim = {'name': name, 'compatibilityLevel': compatibility, 'model': raw}
    (root/'synthetic-model.bim').write_text(json.dumps(bim, ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')
    for w in workloads:
        (root/w['query_file']).write_text(w['query'], encoding='utf-8')
    manifest = {'schema_version': '1.0', 'kind': 'synthetic-benchmark-bundle', 'source_model': model.name,
                'synthetic_model_name': name, 'seed': config.seed, 'engine_execution': 'not_run',
                'storage_mode': 'import', 'source_storage_modes': {t.name: sorted(t.storage_modes) for t in model.tables.values()},
                'metadata_fidelity': 'BIM template with replaced sources' if full_template else 'supported metadata projection',
                'config': copy.deepcopy(config.__dict__), 'warnings': list(dict.fromkeys(warnings)),
                'tables': table_summaries, 'relationships': [{'name': r.name, 'active': r.active, 'cardinalities': cards,
                                                            'key_domain_shared': True, 'nulls_are_deliberate': True} for r, _, cards in relations],
                'workloads': copy.deepcopy(workloads)}
    (root/'simulation.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')
    analysis.write(root/'analysis')
    if matrix_analysis:
        matrix_analysis.write(root/'matrix-analysis')
    from .benchmark import render_simulation_html
    (root/'simulation.html').write_text(render_simulation_html(manifest), encoding='utf-8')
    (root/'RUN-INSTRUCTIONS.txt').write_text(
        'SYNTHETIC BENCHMARK BUNDLE\n\n'
        '1. Copy this whole directory to Windows. No real source connection is needed.\n'
        '2. Edit model.expressions[SyntheticDataRoot].expression in synthetic-model.bim to the absolute tables directory on the engine machine (M quoted string).\n'
        '3. Open the BIM in Tabular Editor or deploy it to a separate local SSAS Tabular database. Refresh/process the synthetic Import tables. Do not replace your production model. Deployment and processing are not performed by this library.\n'
        '4. Install the optional [engine] extra for direct Windows ADOMD access. See README for the release-wheel install command. DAX Studio is optional. The Microsoft ADOMD DLL must be installed separately (commonly with Desktop).\n'
        '5. Use engine-sessions to list open Desktop endpoints/databases, then select the intended synthetic model explicitly. Example:\n'
        '   py -m semantic_hotspots benchmark . --server localhost:PORT --database '+name+' --warm 3 --cold 0\n'
        '6. Read benchmark.html. Compare identical data, workloads, hardware and cache settings before/after a DAX change.\n\n'
        'CSV generation is not a DAX benchmark. Import results do not reproduce DirectQuery network/source latency or Fabric capacity accounting. Scalar totals alone may be BLANK; configure representative group_by and filters.\n', encoding='utf-8')
    return SimulationBundle(root, manifest)
