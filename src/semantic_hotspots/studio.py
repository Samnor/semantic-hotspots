"""Local browser workflow. No hosted service, dependencies or source uploads."""
from __future__ import annotations

import copy
import importlib.util
import json
import mimetypes
import os
import secrets
import sys
import tempfile
import threading
import time
import webbrowser
import zipfile
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from urllib.parse import unquote, urlsplit, parse_qs

from .analyzer import analyze
from .engine import desktop_sessions, query_to_csv
from .matrices import analyze_matrices, load_matrices, MatrixSpec
from .model import ModelError
from .parser import load_model
from .project import export_project, _select_analysis
from .showcase import run_showcase, _rows, _tokens
from .performance import read_performance_analyzer
from .client_setup import install_adomd
from .progress import emit
from .focus import review_measure, write_measure_proposal


_ANALYSIS_STAGES = [('load', 'Read model'), ('dependencies', 'Trace dependencies'),
                    ('ranking', 'Review measures'), ('rewrites', 'Check rewrite proposals'),
                    ('matrices', 'Review matrix grains'), ('reports', 'Write reports')]
_ACTIONS = {'inspect': 'Analyze model', 'demo': 'Analyze synthetic demo', 'matrix': 'Review matrix',
            'generate': 'Build comparison', 'open': 'Open in Power BI Desktop',
            'discover': 'Find Desktop models', 'setup-client': 'Install official client',
            'run': 'Validate and benchmark', 'measure':'Review selected measure',
            'measure-proposal':'Compare proposed measure'}


def _matrix_measure_ids(model, spec):
    """Bound value slots only: helpers and visual filter measures are not slots."""
    result=[]
    for table,name in spec.values:
        try:owner=model.measure(name,table)
        except ModelError:continue
        if owner.id not in result:result.append(owner.id)
    return result


def _label_matrix_choices(choices):
    labels=[' / '.join(x for x in [c['spec']['report_name'],c['spec']['page_name'],c['title']] if x) for c in choices]
    for choice,label in zip(choices,labels):
        choice['visual_label']=label+(' · '+choice['id'] if labels.count(label)>1 else '')


class Studio:
    def __init__(self, workspace, output):
        self.workspace = Path(workspace).resolve()
        if not self.workspace.is_dir():
            raise ModelError('Studio workspace must be an existing directory')
        self.output = Path(output).resolve()
        self.output.mkdir(parents=True, exist_ok=True)
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.RLock()
        self.state = {'busy': False, 'progress': 'Choose a project or try the demo', 'error': None,
                      'model': None, 'matrices': [], 'measures': [], 'sessions': [], 'project': None,
                      'result': None, 'windows': sys.platform == 'win32',
                      'pythonnet': importlib.util.find_spec('pythonnet') is not None,
                      'workspace': str(self.workspace), 'operation': None, 'measure_review':None,'matrix_warning':None}
        self._started = self._stage_started = self._updated = self._finished = None
        self.model = None
        self.analysis = None
        self.measure_review = None
        self.specs = []
        self.config = {}
        self.performance = None
        self.artifacts = {}

    def snapshot(self):
        with self.lock:
            snapshot = copy.deepcopy(self.state)
            if snapshot['operation'] is not None:
                now = self._finished if self._finished is not None else time.monotonic()
                snapshot['operation'].update(elapsed_seconds=max(0, now-self._started),
                                             stage_elapsed_seconds=max(0, now-self._stage_started),
                                             seconds_since_update=max(0, now-self._updated))
            return snapshot

    def change(self, **values):
        with self.lock:
            self.state.update(values)

    def report(self, event):
        """Record actual worker checkpoints; polling does not advance progress."""
        with self.lock:
            self.state['progress'] = event['label']
            operation = self.state['operation']
            if operation is None or operation['status'] != 'running':
                return
            now = time.monotonic()
            if event['stage'] != operation['stage']:
                for stage in operation['stages']:
                    if stage['status'] == 'running':
                        stage.update(status='completed', elapsed_seconds=now-self._stage_started)
                self._stage_started = now
            if not any(s['id'] == event['stage'] for s in operation['stages']):
                operation['stages'].append({'id': event['stage'], 'label': event['label'], 'status': 'pending'})
            for stage in operation['stages']:
                if stage['id'] == event['stage']:
                    stage.update(status='running',label=event['label'])
            operation.update(event)
            self._updated = now

    def inventory(self):
        with self.lock:
            if self.state['operation'] is not None:
                self.state['operation']['inventory'] = {'tables': len(self.model.tables),
                    'measures': len(self.model.measures), 'relationships': len(self.model.relationships),
                    'matrices': len(self.specs)}

    def path(self, relative):
        if not isinstance(relative, str):
            raise ModelError('Choose a workspace path')
        path = (self.workspace / relative).resolve()
        if not path.is_relative_to(self.workspace) or not path.exists():
            raise ModelError('Choose an existing path inside the selected workspace')
        return path

    def browse(self, relative='.'):
        path = self.path(relative)
        if not path.is_dir():
            raise ModelError('Choose a folder')
        entries = []
        for child in sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.casefold())):
            resolved = child.resolve()
            if child.name.startswith('.') or not resolved.is_relative_to(self.workspace) or resolved == self.output:
                continue
            if child.is_dir() or child.suffix.casefold() in {'.pbip', '.bim', '.tmdl', '.json', '.dll'}:
                entries.append({'name': child.name, 'path': str(child.relative_to(self.workspace)), 'directory': child.is_dir()})
        return {'path': str(path.relative_to(self.workspace)),
                'parent': str(path.parent.relative_to(self.workspace)) if path != self.workspace else None,
                'entries': entries}

    def new_artifact(self):
        key = secrets.token_hex(8)
        root = self.output / key
        with self.lock:
            self.artifacts[key] = root
        return key, root

    def artifact(self, key, relative):
        with self.lock:
            root = self.artifacts.get(key)
        if root is None:
            raise ModelError('Unknown artifact')
        path = (root / relative).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ModelError('Unknown artifact file')
        return path

    def analyze_current(self):
        self.analysis = None
        analysis = analyze(self.model, progress=self.report,include_rewrites=False)
        key, root = self.new_artifact()
        measures = copy.deepcopy(analysis.ranked)
        queries = self.performance['queries'] if self.performance else []
        for measure in measures:
            measure['recorded_dax_context_ms'] = max((q['duration_ms'] for q in queries if measure['id'] in q['measure_ids'] and q['duration_ms'] is not None), default=None)
        if any(m['recorded_dax_context_ms'] is not None for m in measures):
            measures.sort(key=lambda m: (m['recorded_dax_context_ms'] is None, -(m['recorded_dax_context_ms'] or 0), -(m['risk_score'] or 0)))
        choices = []
        emit(self.report, 'matrices', 'Review matrix grains', completed=0, total=len(self.specs), unit='matrices',
             detail='No matrix contexts found; you can build one after analysis.' if not self.specs else 'Check retained grain and export eligibility')
        matrix_analysis = None
        if self.specs:
            matrix_analysis = analyze_matrices(self.model, self.specs, analysis=analysis, progress=self.report)
            for i,row in enumerate(matrix_analysis.matrices,1):
                emit(self.report, 'matrices', 'Review matrix grains', completed=i-1, total=len(self.specs), unit='eligibility checks', detail=row['title'])
                eligible, reason = True, ''
                try:
                    _select_analysis(self.model, matrix_analysis, row['id'])
                except ModelError as exc:
                    eligible, reason = False, str(exc)
                spec=next(s for s in self.specs if s.id==row['id'])
                choices.append({**row, 'eligible': eligible, 'blocked_reason': reason,
                                'measure_ids':_matrix_measure_ids(self.model,spec)})
            _label_matrix_choices(choices)
            suffixes = [c['id'].rsplit('/',1)[-1] for c in choices]
            for choice in choices:
                suffix = choice['id'].rsplit('/',1)[-1]
                valid_ids = {choice['id']} | ({suffix} if suffixes.count(suffix) == 1 else set())
                choice['recorded_dax_context_ms'] = max((q['duration_ms'] for q in queries if q['visual_id'] in valid_ids and q['duration_ms'] is not None), default=None)
            if any(c['recorded_dax_context_ms'] is not None for c in choices):
                choices.sort(key=lambda c: (c['recorded_dax_context_ms'] is None, -(c['recorded_dax_context_ms'] or 0), -(c['priority'] or 0)))
        analysis.write(root, progress=self.report)
        emit(self.report, 'reports', 'Write reports', detail='Save matrix review and finish Studio results')
        if matrix_analysis:
            matrix_analysis.write(root / 'matrices')
        if self.performance:
            (root/'performance.json').write_text(json.dumps(self.performance, ensure_ascii=False, indent=2), encoding='utf-8')
        self.analysis=analysis
        self.measure_review=None
        self.change(model={'name':self.model.name, 'source_format':self.model.source_format,
                           'measure_count':len(self.model.measures), 'table_count':len(self.model.tables),
                           'tables':[{'name':t.name,'storage_modes':sorted(t.storage_modes) or ['unknown']}
                                     for t in self.model.tables.values()], 'relationship_count':len(self.model.relationships)},
                    measures=measures, performance=self.performance,
                    matrices=choices, columns=[[e.table, e.name] for e in self.model.entities.values() if e.kind == 'column'],
                    measure_choices=[[e.table, e.name] for e in self.model.measures],
                    analysis_url=f'/artifacts/{key}/report.html',
                    matrix_url=f'/artifacts/{key}/matrices/report.html' if self.specs else None,
                    project=None, result=None, measure_review=None)

    def dispatch(self, action, data):
        if action == 'inspect':
            emit(self.report, 'load', 'Read model', detail='Read the selected semantic model')
            path = self.path(data['model'])
            model = load_model(path)
            emit(self.report, 'load', 'Read model', detail=f'Model loaded: {len(model.tables)} tables, {len(model.measures)} measures. Read matrix contexts.')
            matrix_warning=None
            try:
                if data.get('matrices'):
                    specs = load_matrices(self.path(data['matrices']))
                else:
                    search = path.parent if path.is_file() or path.name.casefold().endswith('.semanticmodel') else path
                    reports = list(search.glob('*.Report'))
                    if len(reports) > 1:
                        raise ModelError('Several reports found; choose one report folder for optional landing visuals')
                    specs = load_matrices(reports[0]) if reports else []
            except (OSError,ValueError,ModelError) as exc:
                specs=[];matrix_warning='Optional landing matrices were not loaded: '+str(exc)+'. Measure analysis remains available.'
            if data.get('performance'):
                emit(self.report, 'load', 'Read model', detail='Match recorded Performance Analyzer query contexts')
            recording = read_performance_analyzer(model, self.path(data['performance'])) if data.get('performance') else None
            self.model, self.specs, self.config, self.performance = model, specs, {}, recording
            self.change(loaded_path=str(path),matrix_warning=matrix_warning)
            self.inventory()
            self.analyze_current()
        elif action == 'demo':
            emit(self.report, 'load', 'Read model', detail='Open the bundled fictional model and matrix contexts')
            key, root = self.new_artifact()
            root.mkdir()
            for name in ['demo.bim', 'demo-matrices.json', 'demo-config.json']:
                (root / name).write_bytes(files('semantic_hotspots').joinpath('assets', name).read_bytes())
            self.model = load_model(root / 'demo.bim')
            self.specs = load_matrices(root / 'demo-matrices.json')
            self.config = json.loads((root / 'demo-config.json').read_text())
            self.performance = None
            self.change(loaded_path='Built-in fictional demo',matrix_warning=None)
            self.inventory()
            self.analyze_current()
        elif action == 'matrix':
            if self.model is None:
                raise ModelError('Open a model first')
            if data.get('reviewed') is not True:
                raise ModelError('Review the matrix context before continuing')
            if data.get('id'):
                spec = next((s for s in self.specs if s.id == data['id']), None)
                if spec is None:
                    raise ModelError('Choose an existing matrix')
                if data.get('measure') and self.model.measure(data['measure']).id not in _matrix_measure_ids(self.model,spec):
                    raise ModelError('Choose a matrix that directly displays the selected measure')
                raw = spec.to_dict()
                # Keep every unsupported flag/filter/retained dimension intact.
                raw['context_complete'] = True
            else:
                selected=self.model.measure(data['measure']) if data.get('measure') else None
                raw = {'id': 'custom-matrix', 'title': 'Selected matrix', 'rows': data.get('rows', []),
                       'columns': data.get('columns', []), 'values': [[selected.table,selected.name]] if selected else data.get('values', []), 'context_complete': True}
                columns = {(e.table, e.name) for e in self.model.entities.values() if e.kind == 'column'}
                measures = {(e.table, e.name) for e in self.model.measures}
                if any(tuple(p) not in columns for p in raw['rows'] + raw['columns']) or any(tuple(p) not in measures for p in raw['values']):
                    raise ModelError('Choose bound columns and measures from the loaded model')
            spec = MatrixSpec.from_dict(raw)
            self.specs = [s for s in self.specs if s.id != spec.id] + [spec]
            self.inventory()
            self.analyze_current()
            if data.get('measure'):
                self.dispatch('measure',{'measure':data['measure'],'landing_matrix':spec.id})
        elif action == 'measure':
            if self.analysis is None:
                raise ModelError('Analyze a model successfully before choosing a measure')
            selected=data.get('measure')
            if not isinstance(selected,str) or not selected:
                raise ModelError('Choose a measure from the loaded model')
            landing_id=data.get('landing_matrix')
            landing=None
            if landing_id:
                landing=next((s for s in self.specs if s.id==landing_id),None)
                if landing is None:raise ModelError('Choose an existing optional landing matrix')
                if self.model.measure(selected).id not in _matrix_measure_ids(self.model,landing):
                    raise ModelError('Choose a matrix that directly displays the selected measure, or continue without visual context')
            review=review_measure(self.model,selected,analysis=self.analysis,progress=self.report,landing_matrix=landing)
            key,root=self.new_artifact()
            emit(self.report,'measure','Review selected measure',detail='Write this measure’s proposals and dependency chain')
            review.write(root/'measure')
            value={k:copy.deepcopy(review.data[k]) for k in ['measure_id','measure','expression','diagnostics','affected_downstream_measures']}
            base=f'/artifacts/{key}/measure/'
            value.update(url=base+'report.html',original_dag=base+'original.dag.json',chain=base+'original.txt',
                         original_dax=base+'original.dax',candidates=[],custom=None,landing_visual=None)
            if review.data['landing_visual']:
                context=review.data['landing_visual']
                value['landing_visual']={k:copy.deepcopy(context[k]) for k in ['matrix_id','title','original_values','spec','diagnostics','grain_advice','summary_export']}
                value['landing_visual'].update(context_url=base+'landing-visual.json',workload_count=len(context['workloads']))
            for c in review.data['candidates']:
                url=base+'candidates/'+c['id']+'/'
                value['candidates'].append({'id':c['id'],'title':c['title'],'after':c['after'],
                    'url':url+'report.html','dax':url+'proposed.dax','validation':url+'validate-total.dax',
                    'landing_queries':url+'landing-queries.zip' if review.data['landing_visual'] and review.data['landing_visual']['workloads'] else None})
            self.measure_review=review
            self.change(measure_review=value,project=None,result=None,sessions=[])
        elif action == 'measure-proposal':
            if self.analysis is None or self.measure_review is None:
                raise ModelError('Review a selected measure first')
            if data.get('measure') != self.measure_review.data['measure_id']:
                raise ModelError('The selected measure changed; review it before comparing a proposal')
            expression=data.get('expression')
            if not isinstance(expression,str) or not expression.strip():
                raise ModelError('Enter the proposed measure expression')
            key,root=self.new_artifact()
            emit(self.report,'measure-proposal','Compare proposed measure',detail='Trace this root expression and retain its full helper chain')
            result=write_measure_proposal(self.model,data['measure'],expression,root/'proposal',graph=self.analysis.graph,
                                          landing_visual=self.measure_review.data['landing_visual'])
            value=self.snapshot()['measure_review']
            url=f'/artifacts/{key}/proposal/'
            value['custom']={'after':expression,'url':url+'report.html','dax':url+'proposed.dax',
                             'validation':url+'validate-total.dax' if result['validation'] else None,
                             'validation_reason':result['validation_reason'],
                             'landing_queries':url+'landing-queries.zip' if (root/'proposal/landing-queries.zip').is_file() else None}
            self.change(measure_review=value)
        elif action == 'generate':
            if self.model is None:
                raise ModelError('Open a model first')
            selected=data.get('measure')
            focus=self.snapshot()['measure_review']
            if not selected or not focus or focus['measure_id']!=selected or not focus['landing_visual'] or focus['landing_visual']['matrix_id']!=data.get('matrix'):
                raise ModelError('Review one measure with an optional landing visual before building its comparison')
            settings = copy.deepcopy(self.config)
            for name, default, maximum in [('fact_rows', 5000, 1000000), ('dimension_rows', 12, 10000)]:
                value = data.get(name, default)
                if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
                    raise ModelError(f'{name} must be between 1 and {maximum}')
                settings[name] = value
            key, root = self.new_artifact()
            emit(self.report, 'generate', 'Build comparison', detail='Generate mock data, the summary candidate and paired DAGs')
            project = export_project(self.model, self.specs, root / 'project', matrix_id=data['matrix'], config=settings,measure=selected)
            emit(self.report, 'archive', 'Package comparison', detail='Create the downloadable project ZIP')
            with zipfile.ZipFile(root / 'showcase.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
                for path in project.directory.rglob('*'):
                    if path.is_file():
                        archive.write(path, str(Path('showcase') / path.relative_to(project.directory)))
            self.change(project={'key': key, 'directory': str(project.directory),
                                 'url': f'/artifacts/{key}/project/showcase.html',
                                 'zip': f'/artifacts/{key}/showcase.zip', 'manifest': project.manifest}, result=None)
        elif action == 'open':
            project = self.snapshot()['project']
            if not project:
                raise ModelError('Generate a comparison first')
            if sys.platform != 'win32':
                raise ModelError('Open the downloaded project in Power BI Desktop on Windows')
            os.startfile(str(self.artifact(project['key'], 'project/Showcase.pbip')))
        elif action == 'setup-client':
            installed = install_adomd()
            self.change(client_path=str(installed), client_setup='Official pinned x64 .NET Framework client installed')
        elif action in {'discover', 'run'}:
            options = {'dotnet_runtime': data.get('runtime', 'netfx')}
            if data.get('adomd'):
                # Explicit SDK selection can be outside the model workspace.
                # It is used only by the isolated client, never served as a file.
                options['adomd_path'] = Path(data['adomd']).expanduser().resolve()
            if action == 'discover':
                emit(self.report, 'discover', 'Find Desktop models', detail='Probe local Desktop endpoints')
                sessions = desktop_sessions(timeout=8, **options)
                project = self.snapshot()['project']
                # Friendly matching hints do not select a model or replace the
                # runner's binding/data checks. Stale entries remain visible.
                expected = {}
                if project:
                    bim = json.loads(self.artifact(project['key'], 'project/Showcase.SemanticModel/model.bim').read_text(encoding='utf-8'))
                    expected = {(t['name'], m['name']): _tokens(m['expression'])
                                for t in bim['model']['tables'] for m in t.get('measures', [])}
                for i,session in enumerate(sessions,1):
                    emit(self.report, 'discover', 'Find Desktop models', completed=i-1, total=len(sessions), unit='endpoints', detail='Check loaded definitions for a comparison match')
                    session['models'] = []
                    for index, database in enumerate(session['databases']):
                        hint = {'database': database, 'label': f'Loaded model {index+1}', 'matches_project': False}
                        try:
                            with tempfile.TemporaryDirectory() as temporary:
                                root = Path(temporary)
                                values = []
                                for kind in ['TABLES', 'MEASURES']:
                                    query = root / (kind+'.dax'); output = root / (kind+'.csv')
                                    query.write_text('EVALUATE INFO.'+kind+'()\n', encoding='utf-8')
                                    query_to_csv(query, output, server=session['server'], database=database, timeout=8, **options)
                                    values.append(_rows(output))
                                table_ids = {t['ID']: t['Name'] for t in values[0]}
                                actual = {(table_ids.get(m['TableID']), m['Name']): _tokens(m['Expression']) for m in values[1]}
                                matches = bool(expected) and all(actual.get(k) == v for k, v in expected.items())
                                hint.update(matches_project=matches, label=('Comparison definitions match' if matches else 'Other loaded model') + f' · {len(values[1])} measures')
                        except Exception as exc:
                            hint['error'] = str(exc)
                        session['models'].append(hint)
                self.change(sessions=sessions)
                if not any(s['databases'] for s in sessions if not s['error']):
                    raise ModelError('No reachable Desktop model found. Open and refresh the comparison. Expand Engine client setup to check the official client.')
            else:
                project = self.snapshot()['project']
                if not project:
                    raise ModelError('Generate a comparison first')
                selected = (data.get('server'), data.get('database'))
                allowed = {(s['server'], d) for s in self.snapshot()['sessions'] if not s['error'] for d in s['databases']}
                if selected not in allowed:
                    raise ModelError('Choose a discovered loaded model first')
                key, root = self.new_artifact()
                result = run_showcase(self.artifact(project['key'], 'project/showcase.json').parent,
                                      server=selected[0], database=selected[1], results_directory=root,
                                      progress=lambda text: emit(self.report, 'run', 'Validate and benchmark', detail=text), **options)
                self.change(result={'url': f'/artifacts/{key}/report.html', 'data': result.data})
        else:
            raise ModelError('Unknown Studio action')

    def start(self, action, data):
        with self.lock:
            if self.state['busy']:
                raise ModelError('An operation is already running')
            title = _ACTIONS.get(action, 'Studio operation')
            stages = _ANALYSIS_STAGES if action in {'inspect','demo','matrix'} else [(action,title)]
            self._started = self._stage_started = self._updated = time.monotonic()
            self._finished = None
            self.state.update(busy=True, error=None, progress=stages[0][1], operation={
                'id':secrets.token_hex(8), 'action':action, 'title':title, 'status':'running',
                'stage':stages[0][0], 'label':stages[0][1], 'detail':'Starting…',
                'completed':None, 'total':None, 'unit':None,
                'stages':[{'id':identity,'label':label,'status':'running' if i==0 else 'pending'}
                          for i,(identity,label) in enumerate(stages)]})
        def work():
            failure = None
            try:
                self.dispatch(action, data)
            except Exception as exc:
                failure = str(exc) or type(exc).__name__
            finally:
                with self.lock:
                    self._finished = time.monotonic()
                    operation = self.state['operation']
                    operation['status'] = 'failed' if failure else 'completed'
                    operation['message'] = (('Stopped during '+operation['label']+'. '+failure) if failure else
                        'Analysis complete. Review the ranked measures and matrix contexts below.' if action in {'inspect','demo','matrix'} else title+' complete.')
                    for stage in operation['stages']:
                        if stage['status'] == 'running':
                            stage.update(status='failed' if failure else 'completed', elapsed_seconds=self._finished-self._stage_started)
                    self.state.update(busy=False, error=failure, progress=operation['message'])
        worker = threading.Thread(target=work, daemon=True)
        worker.start()
        return worker


def make_server(workspace, output, *, port=0):
    studio = Studio(workspace, output)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, status, body, content_type='application/json'):
            if not isinstance(body, bytes):
                body = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "frame-ancestors 'none'")
            if self.path == '/api/session' and status == 200:
                self.send_header('Set-Cookie', f'studio={studio.token}; HttpOnly; SameSite=Strict; Path=/')
            self.end_headers()
            self.wfile.write(body)

        def allowed_host(self):
            return self.headers.get('Host') == f'127.0.0.1:{self.server.server_port}'

        def authenticated(self, cookie=False):
            value = self.headers.get('Authorization', '').removeprefix('Bearer ')
            if cookie:
                jar = SimpleCookie()
                try:
                    jar.load(self.headers.get('Cookie', ''))
                    value = jar['studio'].value if 'studio' in jar else value
                except Exception:
                    return False
            return secrets.compare_digest(value.encode('utf-8'), studio.token.encode('utf-8'))

        def do_GET(self):
            if not self.allowed_host():
                return self.send(403, {'error': 'Invalid local host'})
            parsed = urlsplit(self.path)
            try:
                if parsed.path == '/':
                    return self.send(200, files('semantic_hotspots').joinpath('assets', 'studio.html').read_bytes(), 'text/html; charset=utf-8')
                if not self.authenticated(cookie=parsed.path.startswith('/artifacts/')):
                    return self.send(403, {'error': 'Open the URL printed by the Studio command'})
                if parsed.path == '/api/state':
                    return self.send(200, studio.snapshot())
                if parsed.path == '/api/browse':
                    return self.send(200, studio.browse(parse_qs(parsed.query).get('path', ['.'])[0]))
                if parsed.path.startswith('/artifacts/'):
                    _, _, key, name = unquote(parsed.path).split('/', 3)
                    path = studio.artifact(key, name)
                    return self.send(200, path.read_bytes(), mimetypes.guess_type(path.name)[0] or 'application/octet-stream')
                return self.send(404, {'error': 'Unknown route'})
            except (OSError, ValueError, ModelError) as exc:
                return self.send(400, {'error': str(exc)})

        def do_POST(self):
            if not self.allowed_host() or not self.authenticated() or self.headers.get('Origin') != f'http://127.0.0.1:{self.server.server_port}':
                return self.send(403, {'error': 'Invalid local session or origin'})
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 65536:
                    raise ModelError('Request must contain at most 64 KiB of JSON')
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ModelError('Expected a JSON object')
                if self.path == '/api/session':
                    return self.send(200, {'connected': True})
                if self.path != '/api/action':
                    return self.send(404, {'error': 'Unknown route'})
                studio.start(data.pop('action'), data)
                return self.send(202, {'started': True, 'operation':studio.snapshot()['operation']})
            except (OSError, ValueError, KeyError, ModelError) as exc:
                return self.send(400, {'error': str(exc)})

    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.studio = studio
    server.daemon_threads = True
    return server


def serve_studio(workspace='.', *, output=None, port=0, open_browser=True):
    root = Path(workspace).resolve()
    server = make_server(root, output or root / '.semantic-hotspots-ui', port=port)
    url = f'http://127.0.0.1:{server.server_port}/#{server.studio.token}'
    print(f'Semantic Hotspots Studio: {url}\nWorkspace: {root}\nLocal files stay on this computer. Press Ctrl+C to stop.', flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
