"""Real Power BI checks for independent fictional daily-grain business rules."""
import argparse,json,math,traceback,statistics,hashlib,sys,html
from pathlib import Path
from semantic_hotspots import query_to_csv
from semantic_hotspots.showcase import _rows
from semantic_hotspots.date_validation import query_definitions
# This developer check uses the independent test oracle from the checkout.
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tests'))
from production_oracle import expected,contexts
parser=argparse.ArgumentParser(description='Validate the fictional production lab in an explicitly selected local engine')
parser.add_argument('project',type=Path)
parser.add_argument('--server',required=True)
parser.add_argument('--database',required=True)
parser.add_argument('--adomd-path',type=Path)
parser.add_argument('--output',type=Path)
parser.add_argument('--benchmark',action='store_true',help='Run seven warm client samples only after all exact comparisons and oracle checks pass')
args=parser.parse_args()
project=args.project.resolve();out=(args.output or project/'engine-validation').resolve()
if out.exists() and any(out.iterdir()):parser.error('Output directory must be absent or empty')
out.mkdir(parents=True,exist_ok=True)
server,database=args.server,args.database
manifest=json.loads((project/'production-lab.json').read_text());data=json.loads((project/'daily-data.json').read_text())
results={'status':'started','checks':[],'benchmarks':[],'scope':'Fictional workload only; exact engine pair checks, independent arithmetic oracle, finite warm client timings.'}
def require(condition,detail='Validation condition failed'):
    if not condition:raise AssertionError(detail)

def save():(out/'results.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
def query(text,name,benchmark=False):
    path=out/(name+'.dax');target=out/(name+'.csv');target.unlink(missing_ok=True);path.write_text(text,encoding='utf-8')
    query_to_csv(path,target,server=server,database=database,adomd_path=args.adomd_path,timeout=90,benchmark=benchmark,warm=8)
    return _rows(target)
def filters(context):
    output=[]
    for key,table,column in [('region','Site','Region'),('site','Site','SiteKey'),('family','Product','Family'),('budget','Budget','Version'),('period','Periods','PeriodKey'),('product','Product','ProductKey'),('actual_enabled','Production Daily','Enabled'),('plan_enabled','Plan Daily','Enabled')]:
        if key in context:
            value=json.dumps(context[key]) if isinstance(context[key],str) else str(context[key])
            output.append("TREATAS({"+value+"},'"+table+"'["+column+"])")
    if 'dates' in context:
        dates=context['dates']
        output.append("TREATAS({"+','.join('DATE('+day.replace('-',',')+')' for day in dates)+"},'Calendar'[Day])" if dates else "FILTER(ALL('Calendar'[Day]),FALSE())")
    return output
save()
def write_report(results,out):
    comparisons=sum(c['rows'] for c in results['checks'])
    lines=[]
    for item in results['benchmarks']:
        times=item['warm_client_elapsed'];original=times['original']['median_ms'];candidate=times['candidate']['median_ms']
        delta=(candidate/original-1)*100 if original else 0
        assessment='Slower in this run; do not choose on structural counts' if delta>=0 else 'Lower warm client median in this run'
        lines.append('<tr><td>'+html.escape(item['measure'])+'</td><td>'+format(original,'.2f')+'</td><td>'+format(candidate,'.2f')+'</td><td>'+format(delta,'+.1f')+'%</td><td>'+assessment+'</td></tr>')
    report=('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>Production lab engine checks</title><style>body{font:16px/1.6 system-ui;margin:40px auto;max-width:1100px;padding:20px}td,th{text-align:left;padding:10px;border-bottom:1px solid #ddd}table{border-collapse:collapse}pre{white-space:pre-wrap}</style>'
        '<h1>Fictional production lab: engine checks passed</h1><p>'+str(comparisons)+' finite original/candidate comparisons passed exact equality and explicit BLANK checks. Matrix cells and selected contexts matched an independent source-row arithmetic oracle; the independent Python floating-point comparison uses a small tolerance.</p>'
        +('<h2>Warm client timings</h2><p>Seven measured samples per path, first priming iteration excluded. Order alternates between measures, not between individual samples. These include client overhead and are not server FE/SE traces or a production speedup prediction.</p><table><tr><th>Measure</th><th>Original ms</th><th>Candidate ms</th><th>Change</th><th>Observation</th></tr>'+''.join(lines)+'</table>' if lines else '<p>Benchmarking was not requested.</p>')
        +'<details><summary>Cases, scope and raw samples</summary><pre>'+html.escape(json.dumps(results,indent=2))+'</pre></details><p>Read <a href="results.json">results.json</a> and the adjacent DAX/CSV files for raw evidence. Fewer static operators do not establish lower engine cost. Original source definitions were not edited.</p>')
    (out/'report.html').write_text(report,encoding='utf-8')

try:
    results.update(rows=manifest['row_counts'])
    counts=query('EVALUATE ROW("Actual",COUNTROWS(\'Production Daily\'),"Plan",COUNTROWS(\'Plan Daily\'))','row-count')[0]
    require(int(counts['Actual'])==len(data['Production Daily']),counts)
    require(int(counts['Plan'])==len(data['Plan Daily']),counts)
    fingerprint=hashlib.sha256((project/'Showcase.SemanticModel/model.bim').read_bytes()).hexdigest()
    for index,case in enumerate(manifest['cases']):
        created=json.loads((project/case['directory']/'created-measure.json').read_text(encoding='utf-8'))
        before="'Metrics'["+case['name']+"]";after="'Metrics'["+case['candidate']+"]"
        for as_of in manifest['dates']:
            prefix=query_definitions('Metrics',case['name'],created['expression'],case['candidate'],created['date_controls'],as_of)
            expressions=[]
            for number,context in enumerate(contexts(data)):
                row='ROW("Context",'+str(number)+',"Before",'+before+',"After",'+after+',"Same",IF(ISBLANK('+before+')==ISBLANK('+after+') && '+before+'=='+after+',1,0),"BeforeBlank",IF(ISBLANK('+before+'),1,0),"AfterBlank",IF(ISBLANK('+after+'),1,0))'
                selection=filters(context)
                expressions.append('CALCULATETABLE('+row+','+','.join(selection)+')' if selection else row)
            rows=query(prefix+'EVALUATE UNION('+','.join(expressions)+')',str(index)+'-'+as_of)
            require(len(rows)==len(contexts(data)),len(rows))
            for row in rows:
                context=contexts(data)[int(row['Context'])];oracle=expected(data,as_of,context)[index]
                require(row['Same']=='1' and row['BeforeBlank']==row['AfterBlank'],(case['name'],as_of,context,row))
                actual=None if row['BeforeBlank']=='1' else float(row['Before'])
                require((actual is None)==(oracle is None),(case['name'],as_of,context,actual,oracle))
                if oracle is not None:require(math.isclose(actual,oracle,rel_tol=1e-10,abs_tol=1e-10),(case['name'],as_of,context,actual,oracle))
            results['checks'].append({'measure':case['name'],'date':as_of,'rows':len(rows),'nonblank':sum(r['BeforeBlank']=='0' for r in rows),'strict_equal':True,'oracle_matched':True});save()
            fields='"RegionValue",SELECTEDVALUE(\'Site\'[Region]),"FamilyValue",SELECTEDVALUE(\'Product\'[Family]),"PeriodValue",SELECTEDVALUE(\'Periods\'[PeriodKey]),"Before",'+before+',"After",'+after+',"Same",IF(ISBLANK('+before+')==ISBLANK('+after+') && '+before+'=='+after+',1,0),"BeforeBlank",IF(ISBLANK('+before+'),1,0),"AfterBlank",IF(ISBLANK('+after+'),1,0)'
            rows=query(prefix+'EVALUATE SUMMARIZECOLUMNS(\'Site\'[Region],\'Product\'[Family],\'Periods\'[PeriodKey],'+fields+')',str(index)+'-'+as_of+'-matrix')
            require(bool(rows),'Empty matrix validation')
            for row in rows:
                require(row['Same']=='1' and row['BeforeBlank']==row['AfterBlank'],(case['name'],as_of,'matrix',row))
                context={'region':row['RegionValue'],'family':row['FamilyValue'],'period':int(row['PeriodValue'])} if row['PeriodValue'] else None
                oracle=expected(data,as_of,context)[index] if context else None
                actual=None if row['BeforeBlank']=='1' else float(row['Before'])
                require((actual is None)==(oracle is None),(case['name'],as_of,context,actual,oracle))
                if oracle is not None:require(math.isclose(actual,oracle,rel_tol=1e-10,abs_tol=1e-10),(case['name'],as_of,context,actual,oracle))
            results['checks'].append({'measure':case['name'],'date':as_of,'kind':'matrix-leaves','rows':len(rows),'strict_equal':True,'oracle_matched':True});save()

        # Actual generated download query, not only the custom harness.
        download=project/case['directory']/'validation-dates/2024-02-29/validate-total.dax'
        rows=query(download.read_text(encoding='utf-8'),str(index)+'-download')
        require(rows and all(r['Equal']=='1' for r in rows),(case,rows))
        results['checks'].append({'measure':case['name'],'kind':'downloaded-query','rows':len(rows),'strict_equal':True})
        save()
    results['validation_status']='passed';save()
    for index,case in enumerate(manifest['cases'] if args.benchmark else []):
        created=json.loads((project/case['directory']/'created-measure.json').read_text(encoding='utf-8'))
        prefix=query_definitions('Metrics',case['name'],created['expression'],case['candidate'],created['date_controls'],'2025-01-01')
        require(any(c.get('nonblank',0)>0 for c in results['checks'] if c['measure']==case['name'] and c.get('date')=='2025-01-01'),'All-BLANK timing cohort is uninformative')
        timing={}
        # Reverse order on alternating measures; one excluded warm-up is built
        # into the client. Report warm client elapsed, not server FE/SE traces.
        order=['original','candidate'] if index%2==0 else ['candidate','original']
        for variant in order:
            name=case['name'] if variant=='original' else case['candidate']
            text=prefix+'EVALUATE SUMMARIZECOLUMNS(\'Site\'[Region],\'Product\'[Family],\'Periods\'[PeriodKey],"Value",\'Metrics\'['+name+'])'
            rows=query(text,str(index)+'-'+variant+'-timings',benchmark=True)
            require(len(rows)==8,(variant,rows))
            # The low-level client keeps every iteration, including priming.
            # Discard exactly the first here and preserve it in the raw CSV.
            durations=[float(r['TotalDuration_ms']) for r in rows[1:]]
            timing[variant]={'median_ms':statistics.median(durations),'samples_ms':durations,'excluded_warmup_sequence':1}
        results['benchmarks'].append({'measure':case['name'],'warm_client_elapsed':timing,'scope':'Seven warm client samples; excluded warm-up. No server traces or production speedup claim.'});save()
    require(hashlib.sha256((project/'Showcase.SemanticModel/model.bim').read_bytes()).hexdigest()==fingerprint)
    results['status']='passed';save()
    write_report(results,out)
except Exception:
    results['status']='failed';results['error']=traceback.format_exc();save();raise
print(json.dumps({'status':results['status'],'checks':len(results['checks']),'rows':sum(c['rows'] for c in results['checks']),'benchmarks':results['benchmarks']}))
