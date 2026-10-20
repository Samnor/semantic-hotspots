"""Join offline semantic and native capacity logs without attributing a query to each leaf."""
from __future__ import annotations

import csv
import datetime as dt
import json
import math
from collections import defaultdict
from pathlib import Path

from .dax import Resolver, tokenize
from .graph import DependencyGraph
from .model import Model, ModelError


def read_rows(source) -> list[dict]:
    if isinstance(source, (str, Path)):
        p = Path(source)
        if p.suffix.lower() == '.csv':
            with p.open(encoding='utf-8-sig', newline='') as f:
                return list(csv.DictReader(f))
        with p.open(encoding='utf-8-sig') as f:
            source = json.load(f)
    if isinstance(source, dict):
        source = source.get('rows', source.get('value'))
    if not isinstance(source, list) or not all(isinstance(x, dict) for x in source):
        raise ModelError('Telemetry must be a JSON array, {"rows": [...]}, {"value": [...]}, or CSV with documented fields')
    return source


def _key(row: dict) -> tuple[str, str]:
    return str(row.get('ItemId', '')).casefold(), str(row.get('OperationId', '')).casefold()


def _number(value) -> float | None:
    if value is None or value == '':
        return None
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ModelError(f'Expected a finite nonnegative telemetry number; received {value!r}')
    return result


def _date(value: str) -> dt.datetime:
    try:
        result = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError as exc:
        raise ModelError(f'Invalid telemetry timestamp: {value!r}') from exc
    if result.tzinfo is None:
        raise ModelError('Telemetry time-window filtering requires timezone-aware timestamps')
    return result


class RuntimeProfile:
    """Whole-query context metrics. These are inclusive and non-additive across measures."""
    def __init__(self, queries: list[dict], diagnostics: list[str], dataset_id: str):
        self.queries = queries
        self.diagnostics = diagnostics
        self.dataset_id = dataset_id

    @classmethod
    def from_logs(cls, model: Model, semantic_logs, capacity_logs=None, *, dataset_id: str | None = None,
                  start: str | None = None, end: str | None = None) -> RuntimeProfile:
        rows = read_rows(semantic_logs)
        if any(x.get('OperationName') == 'QueryBegin' and (not x.get('ItemId') or not x.get('OperationId')) for x in rows):
            raise ModelError('Each QueryBegin requires ItemId and OperationId in the documented normalized log format')
        available = {str(x.get('ItemId', '')).casefold() for x in rows if x.get('OperationName') == 'QueryBegin'}
        if dataset_id is None:
            if len(available) != 1:
                raise ModelError(f'Expected exactly one dataset in semantic logs; found {len(available)}. Set dataset_id explicitly.')
            dataset_id = next(iter(available))
        dataset_id = dataset_id.casefold()
        if dataset_id not in available:
            raise ModelError('Selected dataset has no QueryBegin rows in the supplied semantic logs')
        grouped = defaultdict(list)
        diagnostics: list[str] = []
        for row in rows:
            if _key(row)[0] == dataset_id and _key(row)[1]:
                grouped[_key(row)].append(row)
        costs: dict[tuple[str, str], list[float]] = defaultdict(list)
        seen: dict[str, tuple] = {}
        eligible_capacity_rows = 0
        if capacity_logs is not None:
            for row in read_rows(capacity_logs):
                if _key(row)[0] != dataset_id or row.get('OperationName') != 'Query':
                    continue
                eligible_capacity_rows += 1
                if not row.get('OperationId'):
                    raise ModelError('Native capacity Query rows require OperationId')
                event = row.get('EventId')
                if not event:
                    raise ModelError('Native capacity rows require EventId for deduplication')
                amount = _number(row.get('CUSeconds'))
                if amount is None:
                    diagnostics.append(f'Capacity event {event} has no CUSeconds; cost remains unknown.')
                    continue
                identity = (_key(row), amount)
                if event in seen:
                    if seen[event] != identity:
                        raise ModelError(f'Conflicting native capacity rows share EventId {event!r}')
                    continue
                seen[event] = identity
                costs[_key(row)].append(amount)
            if not eligible_capacity_rows:
                diagnostics.append('No normalized native capacity Query rows matched the selected dataset; all costs remain unknown. Check ItemId, OperationName and the exporter schema.')
        resolver = Resolver(model)
        queries = []
        lower, upper = _date(start) if start else None, _date(end) if end else None
        if lower and upper and lower >= upper:
            raise ModelError('start must be earlier than end')
        for key, trace in sorted(grouped.items()):
            begins = [x for x in trace if x.get('OperationName') == 'QueryBegin']
            if not begins:
                continue
            begin = min(begins, key=lambda x: str(x.get('Timestamp', '')))
            if lower or upper:
                time = _date(begin.get('Timestamp', ''))
                if lower and time < lower or upper and time >= upper:
                    continue
            query = str(begin.get('EventText', ''))
            try:
                tokens = tokenize(query)
                if any(t.kind == 'NAME' and t.value.upper() in {'MEASURE', 'FUNCTION'} for t in tokens):
                    diagnostics.append(f'Query {key[1]} has query-scoped definitions; skipped to avoid false model attribution.')
                    continue
                refs = resolver.scan(query).references
            except ModelError as exc:
                diagnostics.append(f'Query {key[1]} could not be analyzed: {exc}')
                continue
            roots = sorted({r.target for r in refs if model.entities[r.target].kind == 'measure'})
            ends = [x for x in trace if x.get('OperationName') == 'QueryEnd']
            metric_rows = [x for x in trace if x.get('OperationName') == 'ExecutionMetrics']
            metrics = []
            for row in metric_rows:
                try:
                    value = row.get('EventText') or {}
                    value = json.loads(value) if isinstance(value, str) else value
                    if isinstance(value, dict) and 'discoverType' not in value:
                        metrics.append((str(row.get('Timestamp', '')), value))
                except json.JSONDecodeError:
                    diagnostics.append(f'Query {key[1]} contains malformed ExecutionMetrics; CPU remains unknown if no valid record exists.')
            metrics.sort(key=lambda x: x[0])
            metric = metrics[-1][1] if metrics else {}
            if len({str(m.get('totalCpuTimeMs')) for _, m in metrics}) > 1:
                diagnostics.append(f'Query {key[1]} contains multiple differing CPU metrics; latest query metric used.')
            dq = [x for x in trace if x.get('OperationName') == 'DirectQueryEnd']
            queries.append({'operation_id': key[1], 'dataset_id': dataset_id,
                            'started': begin.get('Timestamp'), 'measure_ids': roots,
                            'query_end_seen': bool(ends), 'execution_metrics_seen': bool(metrics),
                            'cpu_ms': _number(metric.get('totalCpuTimeMs')),
                            'duration_ms': _number(metric.get('durationMs')),
                            'recorded_cu_seconds': sum(costs[key]) if key in costs else None,
                            'direct_query_requests': len(dq),
                            'errors': [x.get('EventText') for x in trace if x.get('OperationName') == 'Error']})
        if not queries:
            raise ModelError('No eligible query records in the requested dataset/time window')
        return cls(queries, diagnostics, dataset_id)

    def metrics(self, graph: DependencyGraph) -> dict[str, dict]:
        result: dict[str, dict] = {}
        for measure in graph.model.measures:
            direct = [q for q in self.queries if measure.id in q['measure_ids']]
            context = [q for q in self.queries if any(graph.mask(n) & (1 << graph.index[measure.id]) for n in q['measure_ids'])]
            def total(field):
                present = [q[field] for q in context if q[field] is not None]
                return sum(present) if present else None
            result[measure.id] = {
                'interpretation': 'Inclusive whole-query context, potentially shared with other measures. Not an isolated measure cost or proof that a branch executed.',
                'direct_query_contexts': len(direct), 'potential_chain_query_contexts': len(context),
                'query_cpu_ms_context': total('cpu_ms'),
                'query_cu_seconds_context': total('recorded_cu_seconds'),
                'cpu_coverage': sum(q['cpu_ms'] is not None for q in context),
                'cu_coverage': sum(q['recorded_cu_seconds'] is not None for q in context),
                'unrecorded_cost_query_ids': [q['operation_id'] for q in context if q['recorded_cu_seconds'] is None],
                'operation_ids': [q['operation_id'] for q in context],
            }
        return result

    def to_dict(self) -> dict:
        return {'dataset_id': self.dataset_id, 'queries': self.queries, 'diagnostics': self.diagnostics,
                'attribution': 'Whole-query context metrics are inclusive/non-additive. No per-leaf measured cost is inferred.'}
