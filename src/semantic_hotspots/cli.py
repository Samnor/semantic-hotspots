from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .analyzer import VERSION, analyze
from .graph import DependencyGraph
from .model import ModelError
from .parser import load_model
from .profile import RuntimeProfile
from .simulation import SimulationConfig, simulate
from .benchmark import collect_benchmarks, run_benchmarks
from .experiments import prepare_rewrite_experiment, collect_rewrite_results, run_rewrite_experiment


def _positive(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError('must be positive')
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='semantic-hotspots', description='Offline semantic-model lineage and explainable hotspot triage')
    parser.add_argument('--version', action='version', version=VERSION)
    commands = parser.add_subparsers(dest='command', required=True)
    scan = commands.add_parser('analyze', help='Rank measures and write HTML/JSON plus chain graphs')
    scan.add_argument('model', type=Path)
    scan.add_argument('-o', '--output', type=Path, default=Path('semantic-hotspots-report'))
    scan.add_argument('--top', type=_positive, default=20)
    scan.add_argument('--rank-by', choices=['risk', 'query_cpu_context', 'query_cu_context'],
                      help='Override automatic ranking: native query-context CU, then CPU, then static risk')
    scan.add_argument('--semantic-logs', type=Path)
    scan.add_argument('--capacity-logs', type=Path)
    scan.add_argument('--dataset-id')
    scan.add_argument('--start', help='Timezone-aware ISO timestamp, inclusive QueryBegin bound')
    scan.add_argument('--end', help='Timezone-aware ISO timestamp, exclusive QueryBegin bound')
    chain = commands.add_parser('chain', help='Export the dependency chain of one measure')
    chain.add_argument('model', type=Path)
    chain.add_argument('measure')
    chain.add_argument('--table')
    chain.add_argument('--format', choices=['json', 'text', 'dot'], default='json')
    chain.add_argument('-o', '--output', type=Path)
    chain.add_argument('--max-nodes', type=_positive, default=200, help='Text preview limit; JSON/DOT remain complete')
    mock = commands.add_parser('simulate', help='Generate seeded mock CSVs, an isolated Import BIM, workloads and HTML; does not execute DAX')
    mock.add_argument('model', type=Path)
    mock.add_argument('-o', '--output', type=Path, default=Path('synthetic-lab'))
    mock.add_argument('--config', type=Path, help='JSON SimulationConfig settings')
    mock.add_argument('--seed', type=int)
    mock.add_argument('--fact-rows', type=_positive)
    mock.add_argument('--dimension-rows', type=_positive)
    mock.add_argument('--data-root', help='Absolute CSV tables path on the engine machine')
    bench = commands.add_parser('benchmark', help='Run dscmd 3.6+ on an already loaded synthetic model, or collect existing CSV results')
    bench.add_argument('bundle', type=Path)
    bench.add_argument('--dscmd', type=Path)
    bench.add_argument('--server')
    bench.add_argument('--database')
    bench.add_argument('--results', type=Path)
    bench.add_argument('--cold', type=int, default=0, help='Cold iterations clear the selected database cache; use a separate simulation model')
    bench.add_argument('--warm', type=int, default=3)
    bench.add_argument('--timeout', type=_positive, default=300, help='Per-workload timeout in seconds')
    bench.add_argument('--collect-only', action='store_true', help='Read existing result CSVs without contacting an engine')
    bench.add_argument('--trace-verified', action='store_true', help='Assert server traces were independently verified; otherwise CPU/FE/SE stay unknown')
    experiment = commands.add_parser('experiment', help='Prepare guarded rewrite candidates and paired validation/timing queries from a synthetic bundle')
    experiment.add_argument('bundle', type=Path)
    experiment.add_argument('-o', '--output', type=Path, default=Path('rewrite-experiment'))
    experiment.add_argument('--top', type=_positive, default=20)
    run = commands.add_parser('experiment-run', help='Validate rewrite results in an engine before timing, or collect retained results')
    run.add_argument('experiment', type=Path)
    run.add_argument('--dscmd', type=Path)
    run.add_argument('--server')
    run.add_argument('--database')
    run.add_argument('--results', type=Path)
    run.add_argument('--cold', type=int, default=0, help='Cold iterations clear the selected simulation database cache')
    run.add_argument('--warm', type=int, default=3)
    run.add_argument('--timeout', type=_positive, default=300)
    run.add_argument('--collect-only', action='store_true')
    run.add_argument('--trace-verified', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.command == 'experiment':
            output = prepare_rewrite_experiment(args.bundle, args.output, top=args.top)
            print(f'Rewrite proposals and paired queries: {output}\nHTML: {output / "experiment.html"}\nEngine validation and timing have not run.')
            return 0
        if args.command == 'experiment-run':
            output = args.results or args.experiment / 'experiment-results'
            if args.collect_only:
                result = collect_rewrite_results(args.experiment, output, trace_verified=args.trace_verified)
                result.write(output)
            else:
                if not args.dscmd or not args.server or not args.database:
                    raise ModelError('experiment-run needs --dscmd, --server and --database, or --collect-only')
                result = run_rewrite_experiment(args.experiment, dscmd=args.dscmd, server=args.server,
                                                database=args.database, results_directory=output,
                                                cold=args.cold, warm=args.warm, timeout=args.timeout,
                                                trace_verified=args.trace_verified)
            print(f'Rewrite evidence: {(output / "experiment.html").resolve()}\nEquality is limited to tested cases; observed durations are not production predictions.')
            candidates = result.to_dict()['ranked_candidates']
            return 0 if candidates and all(c['validation_status'] == 'passed_tested_cases' and c['performance_status'] in {
                'observed_improvement', 'no_observed_improvement', 'mixed_results'} for c in candidates) else 1
        if args.command == 'simulate':
            settings = json.loads(args.config.read_text(encoding='utf-8-sig')) if args.config else {}
            settings = SimulationConfig.from_dict(settings).__dict__.copy()
            for option in ['seed', 'fact_rows', 'dimension_rows', 'data_root']:
                if getattr(args, option) is not None:
                    settings[option] = getattr(args, option)
            bundle = simulate(args.model, args.output, config=settings)
            print(f'Synthetic Import model: {bundle.model_path.resolve()}\nHTML: {bundle.report_path.resolve()}')
            print('Mock data generated. DAX performance is unknown until benchmarked in an engine; see RUN-INSTRUCTIONS.txt.')
            return 0
        if args.command == 'benchmark':
            output = args.results or args.bundle / 'benchmark-results'
            if args.collect_only:
                result = collect_benchmarks(args.bundle, output, trace_verified=args.trace_verified)
                result.write(output)
            else:
                if not args.dscmd or not args.server or not args.database:
                    raise ModelError('benchmark needs --dscmd, --server and --database, or --collect-only')
                result = run_benchmarks(args.bundle, dscmd=args.dscmd, server=args.server, database=args.database,
                                        results_directory=output, cold=args.cold, warm=args.warm,
                                        timeout=args.timeout, trace_verified=args.trace_verified)
            print(f'Benchmark HTML: {(output / "benchmark.html").resolve()}\nTimings are synthetic-workload durations, not production predictions or CU estimates.')
            failed = any(r['error'] for case in result.cases for r in case['rows'])
            incomplete = any(not case['rows'] for case in result.cases) or any('expected ' in d or 'dscmd failed' in d for d in result.diagnostics)
            return 1 if failed or incomplete else 0
        model = load_model(args.model)
        if args.command == 'analyze':
            if args.capacity_logs and not args.semantic_logs:
                raise ModelError('--capacity-logs requires --semantic-logs for query attribution')
            if (args.start or args.end or args.dataset_id) and not args.semantic_logs:
                raise ModelError('Telemetry filters require --semantic-logs')
            profile = RuntimeProfile.from_logs(model, args.semantic_logs, args.capacity_logs,
                                               dataset_id=args.dataset_id, start=args.start, end=args.end) if args.semantic_logs else None
            result = analyze(model, profile=profile, rank_by=args.rank_by)
            files = result.write(args.output)
            print(f'{len(model.measures)} measures; rank={result.rank_by}. Static points are not measured cost.')
            if profile:
                print('Runtime totals are inclusive, shared query contexts; do not sum them across measures.')
            for i, row in enumerate(result.ranked[:args.top], 1):
                score = 'unknown' if row['risk_score'] is None else str(row['risk_score'])
                runtime = row.get('runtime') or {}
                print(f"{i:>3}. risk={score:>7}  {row['table']}[{row['name']}]  upstream={row['transitive_measure_count']}  complete={row['chain_complete']}"
                      + (f"  query-context CPU ms={runtime.get('query_cpu_ms_context')}  CU-s={runtime.get('query_cu_seconds_context')}" if profile else ''))
            print(f"HTML: {files['html'].resolve()}\nJSON: {files['json'].resolve()}")
            if result.warnings:
                print(f'{len(result.warnings)} coverage/interpretation warnings in the report.')
        else:
            measure = model.measure(args.measure, args.table)
            graph = DependencyGraph(model)
            data = graph.to_dict(measure.id)
            if args.format == 'json':
                output = json.dumps(data, ensure_ascii=False, indent=2) + '\n'
            elif args.format == 'dot':
                nodes = '\n'.join(f'  {json.dumps(n["id"])} [label={json.dumps(n["table"] + "[" + n["name"] + "]", ensure_ascii=False)}];' for n in data['nodes'])
                edges = '\n'.join(f'  {json.dumps(e["from"])} -> {json.dumps(e["to"])};' for e in data['edges'])
                output = f'digraph dependencies {{\n  // consumer -> dependency\n{nodes}\n{edges}\n}}\n'
            else:
                ids = graph.dependency_ids(measure.id, True)
                output = f'{measure.qualified_name}\nArrows: consumer -> dependency\n'
                for n in ids[:args.max_nodes]:
                    output += graph.model.entities[n].qualified_name + '\n'
                    output += ''.join('  -> ' + graph.model.entities[d].qualified_name + '\n' for d in sorted(graph.dependencies[n]))
                if len(ids) > args.max_nodes:
                    output += f'Preview truncated: {len(ids) - args.max_nodes} nodes omitted. Use JSON for the full graph.\n'
                if not data['is_acyclic']:
                    output += 'CYCLE: no dependency-first evaluation order is available.\n'
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(output, encoding='utf-8')
            else:
                print(output, end='')
        return 0
    except (ModelError, OSError, ValueError) as exc:
        print(f'semantic-hotspots: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
