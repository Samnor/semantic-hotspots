"""Public analysis API and transparent, non-cost static priority scoring."""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .optimizer import RewritePlan

from .graph import DependencyGraph
from .model import Model, ModelError
from .parser import load_model, parse_model
from .profile import RuntimeProfile
from .rules import inspect_expression, relationship_findings

VERSION = '0.7.1'
SCORE_DESCRIPTION = (
    'Ordinal review priority, not elapsed time, engine work, row scans or CU. '
    'Each nonempty expression starts at 1 point; named rules add their documented points. '
    'The longest dependency-path sum is combined with 0.5*log2(1+transitive measure count). '
    'Relationship-context review points are added at the root. Branches are not assumed to all execute.'
)


@dataclass
class Analysis:
    model: Model
    graph: DependencyGraph
    ranked: list[dict]
    rank_by: str
    warnings: list[str]
    profile: RuntimeProfile | None = None
    rewrite_plan: RewritePlan | None = None

    def to_dict(self) -> dict:
        return {'schema_version': '1.0', 'package_version': VERSION,
                'model': {'name': self.model.name, 'source_format': self.model.source_format,
                          'measure_count': len(self.model.measures), 'table_count': len(self.model.tables),
                          'tables': [{'name': t.name, 'storage_modes': sorted(t.storage_modes) or ['unknown']}
                                     for t in self.model.tables.values()],
                          'relationship_count': len(self.model.relationships)},
                'rank_by': self.rank_by, 'static_score_definition': SCORE_DESCRIPTION,
                'warnings': self.warnings, 'ranked_measures': self.ranked,
                'dependency_graph': self.graph.to_dict(),
                'runtime': self.profile.to_dict() if self.profile else None,
                'rewrite_plan': self.rewrite_plan.to_dict() if self.rewrite_plan else None}

    def to_json(self, path: str | Path | None = None) -> str:
        text = json.dumps(self.to_dict(), ensure_ascii=False, indent=2, allow_nan=False) + '\n'
        if path is not None:
            Path(path).write_text(text, encoding='utf-8')
        return text

    def write(self, directory: str | Path) -> dict[str, Path]:
        """Write JSON, self-contained HTML and one reusable dependency graph per measure."""
        from .report import render_html
        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True)
        graphs = root / 'chains'
        graphs.mkdir(exist_ok=True)
        index = {}
        for m in self.model.measures:
            slug = re.sub(r'[^a-zA-Z0-9_-]+', '-', m.name).strip('-')[:65] or 'unnamed'
            filename = f'measure-{slug}-{hashlib.sha256(m.id.encode()).hexdigest()[:12]}.json'
            (graphs / filename).write_text(json.dumps(self.graph.to_dict(m.id), ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            index[m.id] = 'chains/' + filename
        (root / 'chain-index.json').write_text(json.dumps(index, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        if self.rewrite_plan:
            (root / 'rewrite-plan.json').write_text(json.dumps(self.rewrite_plan.to_dict(), ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
        self.to_json(root / 'analysis.json')
        (root / 'report.html').write_text(render_html(self, index), encoding='utf-8')
        files = {'json': root / 'analysis.json', 'html': root / 'report.html', 'chain_index': root / 'chain-index.json'}
        if self.rewrite_plan:
            files['rewrite_plan'] = root / 'rewrite-plan.json'
        return files


def analyze(model: Model | str | Path | dict, *, profile: RuntimeProfile | None = None,
            rank_by: str | None = None) -> Analysis:
    if isinstance(model, dict):
        model = parse_model(model)
    elif not isinstance(model, Model):
        model = load_model(model)
    graph = DependencyGraph(model)
    warnings = list(dict.fromkeys(model.warnings + graph.diagnostics))
    if graph.cycles:
        warnings.append('Dependency cycles found. Blocked measures have no acyclic risk score or evaluation order; see cycle_witnesses.')
    if any(e.kind == 'calculation_item' for e in model.entities.values()):
        warnings.append('Calculation groups are present. Their runtime application/precedence is not inferred from a model file alone.')
    findings = {n: inspect_expression(model.entities[n], info, model) for n, info in graph.syntax.items()}
    local = {n: (1 if model.entities[n].expression else 0) + sum(f.points for f in findings.get(n, [])) for n in graph.ids}
    path_scores = {}
    for n in graph.order:
        path_scores[n] = local[n] + max((path_scores[d] for d in graph.dependencies[n]), default=0)
    runtime = profile.metrics(graph) if profile else {}
    if profile:
        warnings.extend(profile.diagnostics)
        warnings.append('Runtime columns contain inclusive whole-query context costs shared by measures. Do not sum them across measures or call them isolated measure timings.')
        missing_ends = sum(not q['query_end_seen'] for q in profile.queries)
        missing_cpu = sum(q['cpu_ms'] is None for q in profile.queries)
        missing_cu = sum(q['recorded_cu_seconds'] is None for q in profile.queries)
        if missing_ends or missing_cpu or missing_cu:
            warnings.append(f'Telemetry completeness: {missing_ends} queries without QueryEnd, {missing_cpu} without CPU, {missing_cu} without native CU. Missing values stay unknown; collect the full capture window.')
        if not any(q['measure_ids'] for q in profile.queries):
            warnings.append('No runtime query references could be bound to model measures; runtime ranking has no matched contexts.')
    # Prefer native observations only when they can actually be bound to measures.
    # A recorded zero is usable evidence; a missing observation is not zero.
    if rank_by is None:
        if any(m.get('query_cu_seconds_context') is not None for m in runtime.values()):
            rank_by = 'query_cu_context'
        elif any(m.get('query_cpu_ms_context') is not None for m in runtime.values()):
            rank_by = 'query_cpu_context'
        else:
            rank_by = 'risk'
    if rank_by not in {'risk', 'query_cpu_context', 'query_cu_context'}:
        raise ModelError(f'Unsupported ranking: {rank_by}')
    if rank_by != 'risk' and profile is None:
        raise ModelError('Runtime ranking requires RuntimeProfile; static complexity cannot supply measured CPU/CU')
    ranked = []
    measure_mask = sum(1 << graph.index[m.id] for m in model.measures)
    for measure in model.measures:
        bits = graph.mask(measure.id)
        transitive_count = (bits & measure_mask).bit_count() - 1
        context_findings, context = relationship_findings(measure.id, graph)
        context_points = sum(f.points for f in context_findings)
        breadth = .5 * math.log2(1 + transitive_count)
        score = round(path_scores[measure.id] + breadth + context_points, 3) if measure.id in path_scores else None
        dependency_ids = graph.dependency_ids(measure.id)
        incomplete = [n for n in [measure.id] + dependency_ids
                      if (model.entities[n].kind in {'measure', 'function', 'calculation_item'} and not model.entities[n].expression)
                      or (n in graph.syntax and (graph.syntax[n].unresolved or any('Unmatched' in x or 'Unterminated' in x for x in graph.syntax[n].diagnostics)))]
        ranked.append({'id': measure.id, 'table': measure.table, 'name': measure.name,
                       'risk_score': score,
                       'score_breakdown': {'local_points': local[measure.id], 'longest_dependency_path_points': path_scores.get(measure.id),
                                           'breadth_points': round(breadth, 3), 'relationship_context_points': context_points},
                       'dependency_depth': graph.depths.get(measure.id), 'transitive_measure_count': transitive_count,
                       'direct_dependencies': sorted(graph.dependencies[measure.id]),
                       'dependent_measures': [m.id for m in model.measures if m.id != measure.id and graph.mask(m.id) & (1 << graph.index[measure.id])],
                       'potential_filter_context_tables': context,
                       'chain_complete': not incomplete and measure.id not in graph.blocked,
                       'incomplete_dependency_ids': incomplete,
                       'functions': graph.syntax[measure.id].functions if measure.id in graph.syntax else {},
                       'findings': [f.to_dict() for f in findings.get(measure.id, []) + context_findings],
                       'inherited_findings': {n: [f.to_dict() for f in findings[n]] for n in dependency_ids if findings.get(n)},
                       'runtime': runtime.get(measure.id)})
    def sort_key(item):
        if rank_by == 'risk':
            value = item['risk_score']
        else:
            field = 'query_cpu_ms_context' if rank_by == 'query_cpu_context' else 'query_cu_seconds_context'
            value = (item.get('runtime') or {}).get(field)
        return (value is None, -(value or 0), item['id'])
    ranked.sort(key=sort_key)
    from .optimizer import plan_rewrites
    rewrites = plan_rewrites(model, graph=graph, ranked_ids=[r['id'] for r in ranked])
    return Analysis(model, graph, ranked, rank_by, list(dict.fromkeys(warnings)), profile, rewrites)
