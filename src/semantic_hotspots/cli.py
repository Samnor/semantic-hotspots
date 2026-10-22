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
from .matrices import analyze_matrices, load_matrices
from .project import export_project
from .engine import desktop_sessions, query_to_csv
from .showcase import run_showcase


def _positive(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError('must be positive')
    return parsed


def _client_options(parser):
    parser.add_argument('--engine', choices=['auto', 'adomd', 'dscmd'], default='auto',
                        help='Default: direct ADOMD unless --dscmd is supplied')
    parser.add_argument('--adomd-path', type=Path, help='Installed Microsoft ADOMD client DLL or directory')
    parser.add_argument('--dotnet-runtime', choices=['netfx', 'coreclr'], default='netfx')


def _engine_options(args):
    return dict(engine=args.engine, adomd_path=args.adomd_path, dotnet_runtime=args.dotnet_runtime)


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
    compare = commands.add_parser('compare-dags', help='Spell out original and proposed computation DAGs as HTML, JSON and DOT')
    compare.add_argument('model', type=Path)
    compare.add_argument('measure')
    compare.add_argument('--table')
    proposed = compare.add_mutually_exclusive_group(required=True)
    proposed.add_argument('--expression-file', type=Path, help='DAX for a root-expression proposal')
    proposed.add_argument('--candidate-model', type=Path, help='Separate proposed model, including any new summary tables')
    compare.add_argument('--candidate-measure')
    compare.add_argument('--candidate-table')
    compare.add_argument('-o', '--output', type=Path, default=Path('dag-comparison'))
    focus = commands.add_parser('optimize-measure', help='Review proposals for exactly one root measure; no matrix or model edits')
    focus.add_argument('model', type=Path)
    focus.add_argument('measure')
    focus.add_argument('--table')
    focus.add_argument('--as-of', action='append', help='Fixed validation date YYYY-MM-DD; repeat for boundary dates. Production TODAY() stays live')
    focus.add_argument('--definitions', type=Path, help='Matching BIM/TMDL export containing actual missing helper definitions')
    focus.add_argument('--matrices', type=Path, help='Optional matrix specifications or PBIR report folder')
    focus.add_argument('--landing-matrix', help='Explicit optional landing matrix ID; requires --matrices')
    focus.add_argument('-o', '--output', type=Path, default=Path('measure-review'))
    create = commands.add_parser('create-measure', help='Create a new named measure from one root and optional visual contexts')
    create.add_argument('model', type=Path)
    create.add_argument('measure')
    create.add_argument('--table')
    create.add_argument('--as-of', action='append', help='Fixed validation date YYYY-MM-DD; repeat for boundary dates. Production TODAY() stays live')
    create.add_argument('--definitions', type=Path, help='Matching BIM/TMDL export containing actual missing helper definitions')
    create.add_argument('--matrices', type=Path)
    create.add_argument('--visual', action='append', default=[], help='Optional visual ID; repeat for multiple contexts')
    create.add_argument('-o', '--output', type=Path, default=Path('new-measure'))
    mock = commands.add_parser('simulate', help='Generate seeded mock CSVs, an isolated Import BIM, workloads and HTML; does not execute DAX')
    mock.add_argument('model', type=Path)
    mock.add_argument('-o', '--output', type=Path, default=Path('synthetic-lab'))
    mock.add_argument('--matrices', type=Path, help='Matrix specs or a PBIR .Report folder; adds multi-measure contexts')
    mock.add_argument('--config', type=Path, help='JSON SimulationConfig settings')
    mock.add_argument('--seed', type=int)
    mock.add_argument('--fact-rows', type=_positive)
    mock.add_argument('--dimension-rows', type=_positive)
    mock.add_argument('--data-root', help='Absolute CSV tables path on the engine machine')
    bench = commands.add_parser('benchmark', help='Query an already loaded synthetic model through ADOMD or optional dscmd')
    bench.add_argument('bundle', type=Path)
    bench.add_argument('--dscmd', type=Path)
    _client_options(bench)
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
    _client_options(run)
    run.add_argument('--server')
    run.add_argument('--database')
    run.add_argument('--results', type=Path)
    run.add_argument('--cold', type=int, default=0, help='Cold iterations clear the selected simulation database cache')
    run.add_argument('--warm', type=int, default=3)
    run.add_argument('--timeout', type=_positive, default=300)
    run.add_argument('--collect-only', action='store_true')
    run.add_argument('--trace-verified', action='store_true')
    compute = commands.add_parser('compute-compare', help='Compare native interactive CU, CPU and latency for captured paired interactions')
    compute.add_argument('capture', type=Path)
    compute.add_argument('--semantic-logs', type=Path, required=True)
    compute.add_argument('--capacity-logs', type=Path, help='Native CloudEvents or normalized operation rows with explicit capacity and throttle fields')
    compute.add_argument('-o', '--output', type=Path, default=Path('compute-comparison'))
    sessions = commands.add_parser('engine-sessions', help='Find local Desktop endpoints and list their databases through ADOMD (Windows)')
    sessions.add_argument('--server', help='Probe an explicit local endpoint instead of workspace files')
    sessions.add_argument('--adomd-path', type=Path)
    sessions.add_argument('--dotnet-runtime', choices=['netfx', 'coreclr'], default='netfx')
    sessions.add_argument('--timeout', type=_positive, default=30)
    query = commands.add_parser('query', help='Run one local DAX query through ADOMD and save its result CSV (Windows)')
    query.add_argument('file', type=Path)
    query.add_argument('--server', required=True)
    query.add_argument('--database', required=True)
    query.add_argument('--adomd-path', type=Path)
    query.add_argument('--dotnet-runtime', choices=['netfx', 'coreclr'], default='netfx')
    query.add_argument('--timeout', type=_positive, default=300)
    query.add_argument('--benchmark', action='store_true', help='Write warm elapsed-time CSV instead of query values; does not validate correctness')
    query.add_argument('--warm', type=_positive, default=3)
    query.add_argument('-o', '--output', type=Path, required=True)
    matrix = commands.add_parser('matrices', help='Analyze matrix hierarchies, dependencies and candidate aggregation grains')
    matrix.add_argument('model', type=Path)
    matrix.add_argument('--matrices', type=Path, required=True, help='Explicit matrix-spec JSON or common PBIR .Report/definition folder')
    matrix.add_argument('-o', '--output', type=Path, default=Path('matrix-report'))
    matrix.add_argument('--benchmark-results', type=Path, help='Attach matching benchmark.json workload durations')
    project = commands.add_parser('project', help='Export one comparison matrix and a separate mock-data PBIP model with a guarded summary candidate')
    project.add_argument('model', type=Path)
    project.add_argument('--matrices', type=Path, required=True)
    project.add_argument('--matrix', help='Choose a specific matrix ID; default is the highest-priority eligible matrix')
    project.add_argument('--measure', help='Target one measure, using the chosen matrix as its landing visual')
    project.add_argument('--table', help='Qualify the selected measure')
    project.add_argument('--config', type=Path, help='Synthetic data settings for tables in the selected context')
    project.add_argument('--fact-rows', type=_positive)
    project.add_argument('--dimension-rows', type=_positive)
    project.add_argument('-o', '--output', type=Path, default=Path('powerbi-showcase'))
    showcase_run = commands.add_parser('showcase-run', help='Validate a generated showcase before timing separate baseline/candidate queries')
    showcase_run.add_argument('project', type=Path)
    showcase_run.add_argument('--server', required=True)
    showcase_run.add_argument('--database', required=True)
    showcase_run.add_argument('--adomd-path', type=Path)
    showcase_run.add_argument('--dotnet-runtime', choices=['netfx', 'coreclr'], default='netfx')
    showcase_run.add_argument('--warm', type=_positive, default=7, help='5–50 measured iterations plus one excluded warm-up')
    showcase_run.add_argument('--timeout', type=_positive, default=120)
    showcase_run.add_argument('-o', '--output', type=Path)
    studio = commands.add_parser('studio', help='Open a guided local browser workflow; no sources are uploaded')
    studio.add_argument('--workspace', type=Path, default=Path('.'))
    studio.add_argument('-o', '--output', type=Path)
    studio.add_argument('--port', type=int, default=0, help='Loopback port; default chooses a free port')
    studio.add_argument('--no-browser', action='store_true')
    setup = commands.add_parser('engine-setup', help='Explicitly download/check/install the pinned official x64 Windows ADOMD client')
    setup.add_argument('-o', '--output', type=Path, help='Fresh install directory; default is the per-user local cache')
    production=commands.add_parser('production-lab',help='Create a fictional daily-production model, three complex root proposals, DAGs and one Power BI matrix')
    production.add_argument('-o','--output',type=Path,default=Path('daily-production-lab'))
    production.add_argument('--days',type=_positive,default=112)
    production.add_argument('--sites',type=_positive,default=3)
    production.add_argument('--products',type=_positive,default=4)
    chain_lab = commands.add_parser('production-chain-lab', help='Export six sequential daily-production measures, one candidate, one matrix and both DAGs')
    chain_lab.add_argument('-o', '--output', type=Path, default=Path('production-chain-lab'))
    chain_lab.add_argument('--days', type=_positive, default=112)
    chain_lab.add_argument('--sites', type=_positive, default=3)
    chain_lab.add_argument('--products', type=_positive, default=4)
    args = parser.parse_args(argv)
    try:
        if args.command == 'production-chain-lab':
            from .production_chain import create_production_chain_lab
            root = create_production_chain_lab(args.output, days=args.days, sites=args.sites, products=args.products)
            print(f'Six-stage chain: {root / "report.html"}\nOpen Showcase.pbip and refresh. Native correctness and CU consumption have not been tested by this export.')
            return 0
        if args.command == 'compute-compare':
            from .compute import compare_compute
            result = compare_compute(args.capture, args.semantic_logs, args.capacity_logs)
            files = result.write(args.output)
            print(f'Interactive compute report: {files["html"].resolve()}')
            print('Primary outcome: recorded interactive CU-seconds. CPU and latency are separate; missing cost stays unknown.')
            return 0
        if args.command == 'production-lab':
            from .production_lab import create_production_lab
            output=create_production_lab(args.output,days=args.days,sites=args.sites,products=args.products)
            print(f'Production lab: {output}\nOpen report.html or refresh Showcase.pbip in Power BI Desktop. No engine execution or speedup is implied.')
            return 0
        if args.command == 'engine-setup':
            from .client_setup import install_adomd
            print(f'Official client installed: {install_adomd(args.output)}')
            print('Use the Windows [engine] pip extra for Python.NET; open and refresh the comparison in Desktop.')
            return 0
        if args.command == 'studio':
            from .studio import serve_studio
            if not 0 <= args.port <= 65535:
                raise ModelError('port must be between 0 and 65535')
            serve_studio(args.workspace, output=args.output, port=args.port, open_browser=not args.no_browser)
            return 0
        if args.command == 'showcase-run':
            result = run_showcase(args.project, server=args.server, database=args.database,
                                  results_directory=args.output, adomd_path=args.adomd_path,
                                  dotnet_runtime=args.dotnet_runtime, warm=args.warm, timeout=args.timeout,
                                  progress=lambda message: print(message, flush=True))
            print(f'Validation report: {result.report_path}\nPerformance: {result.data["performance_status"]}')
            return 0 if result.data['status'] == 'completed' else 1
        if args.command == 'engine-sessions':
            result = desktop_sessions(server=args.server, adomd_path=args.adomd_path,
                                      dotnet_runtime=args.dotnet_runtime, timeout=args.timeout)
            print(json.dumps(result, ensure_ascii=True, indent=2))
            if not result:
                print('No Desktop workspace endpoints found. Open a model or supply --server localhost:PORT.', file=sys.stderr)
            return 0 if result and all(not r['error'] for r in result) else 1
        if args.command == 'query':
            output = query_to_csv(args.file, args.output, server=args.server, database=args.database,
                                  adomd_path=args.adomd_path, dotnet_runtime=args.dotnet_runtime, timeout=args.timeout,
                                  benchmark=args.benchmark, warm=args.warm)
            print(f'Engine CSV: {output}')
            print('Client elapsed timings only; no CPU/FE/SE or correctness verdict.' if args.benchmark else
                  'Query executed; correctness depends on inspecting the returned values and validation flags.')
            return 0
        if args.command == 'project':
            settings=json.loads(args.config.read_text(encoding='utf-8-sig')) if args.config else {}
            settings=SimulationConfig.from_dict(settings).__dict__.copy()
            for option in ['fact_rows','dimension_rows']:
                if getattr(args,option) is not None:settings[option]=getattr(args,option)
            result=export_project(args.model,args.matrices,args.output,matrix_id=args.matrix,config=settings,
                                  measure=args.measure,table=args.table)
            print(f'Power BI project: {result.project_path}\nOne page, one comparison matrix. Embedded synthetic data; refresh in Desktop.')
            print('Summary candidate exported. Desktop/engine validation not run; performance is unknown.')
            return 0
        if args.command == 'matrices':
            benchmark = json.loads(args.benchmark_results.read_text(encoding='utf-8-sig')) if args.benchmark_results else None
            result = analyze_matrices(args.model, args.matrices, benchmark=benchmark)
            files = result.write(args.output)
            print(f'{len(result.matrices)} matrices; {len(result.workloads)} representative multi-measure contexts. Rank={result.rank_by}.')
            print(f'HTML: {files["html"].resolve()}\nAggregation advice is unvalidated; no summary tables or model changes were made.')
            return 0
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
                if not args.server or not args.database:
                    raise ModelError('experiment-run needs --server and --database, or --collect-only')
                result = run_rewrite_experiment(args.experiment, dscmd=args.dscmd, server=args.server,
                                                database=args.database, results_directory=output,
                                                cold=args.cold, warm=args.warm, timeout=args.timeout,
                                                trace_verified=args.trace_verified, **_engine_options(args))
            print(f'Rewrite evidence: {(output / "experiment.html").resolve()}\nEquality is limited to tested cases; observed durations are not production predictions.')
            candidates = result.to_dict()['ranked_candidates']
            return 0 if candidates and all(c['validation_status'] == 'passed_tested_cases' and c['performance_status'] in {
                'observed_improvement', 'no_observed_improvement', 'mixed_results'} for c in candidates) else 1
        if args.command == 'simulate':
            settings = json.loads(args.config.read_text(encoding='utf-8-sig')) if args.config else {}
            settings = SimulationConfig.from_dict(settings).__dict__.copy()
            if args.matrices:
                settings['matrices'] = [s.to_dict() for s in load_matrices(args.matrices)]
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
                if not args.server or not args.database:
                    raise ModelError('benchmark needs --server and --database, or --collect-only')
                result = run_benchmarks(args.bundle, dscmd=args.dscmd, server=args.server, database=args.database,
                                        results_directory=output, cold=args.cold, warm=args.warm,
                                        timeout=args.timeout, trace_verified=args.trace_verified, **_engine_options(args))
            print(f'Benchmark HTML: {(output / "benchmark.html").resolve()}\nTimings are synthetic-workload durations, not production predictions or CU estimates.')
            failed = any(r['error'] for case in result.cases for r in case['rows'])
            incomplete = any(not case['rows'] for case in result.cases) or any('expected ' in d or 'failed or timed out' in d for d in result.diagnostics)
            return 1 if failed or incomplete else 0
        model = load_model(args.model)
        if args.command in {'create-measure','optimize-measure'} and args.definitions:
            from .helpers import recover_helpers
            model=recover_helpers(model,args.definitions,args.measure,table=args.table).model
        if args.command == 'create-measure':
            from .creation import create_measure
            if bool(args.matrices)!=bool(args.visual):raise ModelError('Supply --matrices and at least one --visual together, or neither')
            specs=load_matrices(args.matrices) if args.matrices else []
            by_id={s.id:s for s in specs}
            if any(v not in by_id for v in args.visual):raise ModelError('A requested visual context was not found')
            result=create_measure(model,args.measure,table=args.table,visuals=[by_id[v] for v in args.visual],validation_dates=args.as_of)
            files=result.write(args.output)
            print(f'New measure: {result.data["name"]}\n{result.data["explanation"]}\nDAX: {files["dax"].resolve()}\nHTML: {files["html"].resolve()}\nSource model unchanged.')
            return 0
        if args.command == 'optimize-measure':
            from .focus import review_measure
            if bool(args.matrices)!=bool(args.landing_matrix):
                raise ModelError('Supply both --matrices and --landing-matrix to choose an optional landing visual')
            landing=None
            if args.matrices:
                landing=next((s for s in load_matrices(args.matrices) if s.id==args.landing_matrix),None)
                if landing is None:raise ModelError('Requested landing matrix was not found')
            review=review_measure(model,args.measure,table=args.table,landing_matrix=landing,validation_dates=args.as_of)
            files=review.write(args.output)
            print(f'Selected measure: {review.data["measure"]}\nRoot-only proposals: {len(review.data["candidates"])}\nHTML: {files["html"].resolve()}\nSource model unchanged. Correctness not tested; performance unknown.')
            return 0
        if args.command == 'compare-dags':
            from .comparison import compare_measure_dags, write_dag_comparison
            result = compare_measure_dags(model, args.measure, table=args.table,
                proposed_expression=args.expression_file.read_text(encoding='utf-8-sig') if args.expression_file else None,
                proposed_source=args.candidate_model, proposed_measure=args.candidate_measure,
                proposed_table=args.candidate_table)
            files = write_dag_comparison(result, args.output)
            print(f'Original/proposed DAGs: {files["html"].resolve()}\nComplete JSON and DOT graphs retained. Validation not run; performance unknown.')
            return 0
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
