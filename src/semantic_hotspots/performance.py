"""Read the documented Performance Analyzer v1 event forest, offline.

Format reference: microsoft/powerbi-desktop-samples, Performance Analyzer schema
and Export File Format documentation. DSE query durations end at first row;
they are neither isolated measure costs nor CPU/FE/SE measurements.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from .dax import Resolver, tokenize
from .model import Model, ModelError
from .parser import load_model


def read_performance_analyzer(model, source):
    """Return recorded DAX query contexts and exact visual-ID associations.

    No fuzzy title join, additive event-tree duration, per-leaf cost, or model
    identity attestation is inferred. Unknown event types remain unclassified.
    """
    if not isinstance(model, Model):
        model = load_model(model)
    raw = json.loads(Path(source).read_text(encoding='utf-8-sig')) if isinstance(source, (str, Path)) else source
    if not isinstance(raw, dict) or raw.get('version') != '1.0.0' or not isinstance(raw.get('events'), list):
        raise ModelError('Expected a Performance Analyzer version 1.0.0 export with events')
    events = {}
    for event in raw['events']:
        if not isinstance(event, dict) or any(not isinstance(event.get(k), str) or not event[k] for k in ['id','name','component','start']):
            raise ModelError('Malformed Performance Analyzer event')
        if event['id'] in events or not isinstance(event.get('metrics', {}), dict):
            raise ModelError('Duplicate event ID or malformed event metrics')
        events[event['id']] = event
    diagnostics, queries = [], []
    def timestamp(value):
        try:
            parsed = dt.datetime.fromisoformat(value.replace('Z','+00:00'))
            if parsed.tzinfo is None:
                raise ValueError('Missing timezone')
            return parsed
        except (ValueError, TypeError, AttributeError) as exc:
            raise ModelError('Performance Analyzer timestamps must include a timezone') from exc
    # Validate the forest even if a cycle lies outside DAX-query ancestry.
    visited = set()
    for event in events.values():
        active = set(); current = event
        while current and current['id'] not in visited:
            if current['id'] in active:
                raise ModelError('Cyclic Performance Analyzer event ancestry')
            active.add(current['id'])
            parent = current.get('parentId')
            if parent is not None and not isinstance(parent, str):
                raise ModelError('Performance Analyzer parentId must be a string')
            if parent and parent not in events:
                diagnostics.append('Missing parent event; some visual associations are unknown.')
            current = events.get(parent)
        visited.update(active)
        start = timestamp(event['start'])
        if event.get('end') is not None and timestamp(event['end']) < start:
            raise ModelError('Performance Analyzer event ends before it starts')
    resolver = Resolver(model)
    for event in events.values():
        if event['name'] == 'Metrics Truncated':
            diagnostics.append('The recording contains truncated metrics; coverage is incomplete.')
        if (event['name'], event['component']) != ('Execute DAX Query', 'DSE'):
            continue
        metrics = event.get('metrics', {})
        query = metrics.get('QueryText')
        roots = []; errors = []
        if not isinstance(query, str) or not query.strip():
            errors.append('No query text; measure bindings are unknown.')
        else:
            try:
                tokens = tokenize(query)
                if any(t.kind == 'NAME' and t.value.upper() in {'MEASURE', 'FUNCTION'} for t in tokens):
                    errors.append('Query-scoped definitions; measure attribution withheld.')
                else:
                    scan = resolver.scan(query)
                    roots = sorted({r.target for r in scan.references if model.entities[r.target].kind == 'measure'})
                    if scan.unresolved:
                        errors.append('Some references are unresolved; bindings are partial.')
            except ModelError as exc:
                errors.append('Query bindings could not be parsed: '+str(exc))
        current = event; visual = None
        while current:
            if (current['name'], current['component']) == ('Visual Container Lifecycle', 'Report Canvas'):
                visual = current.get('metrics', {}); break
            current = events.get(current.get('parentId'))
        failed = metrics.get('Error', False) is not False or metrics.get('Canceled', False) is not False
        duration = None
        if event.get('end') is not None and not failed:
            duration = (timestamp(event['end']) - timestamp(event['start'])).total_seconds() * 1000
        visual = visual or {}
        visual_id = visual.get('visualId')
        visual_title = visual.get('visualTitle')
        queries.append({'event_id': event['id'], 'visual_id': visual_id if isinstance(visual_id, str) else None,
                        'visual_title': visual_title if isinstance(visual_title, str) else None, 'query': query,
                        'measure_ids': roots, 'duration_ms': duration, 'failed_or_canceled': failed,
                        'diagnostics': errors})
    if not queries:
        diagnostics.append('No documented DSE Execute DAX Query events found; recorded DAX durations remain unknown.')
    return {'kind': 'performance-analyzer-recording', 'queries': queries,
            'diagnostics': list(dict.fromkeys(diagnostics)),
            'interpretation': 'Recorded DSE wall-clock query context through first row; shared by referenced measures. Not isolated measure cost, complete result-reading time, CPU, FE/SE or model identity verification. Event durations are not summed.'}
