"""Independent fictional daily-production workload; never reconstructed user data."""
from __future__ import annotations

import base64
import copy
import csv
import gzip
import io
import json
from datetime import date,timedelta
from pathlib import Path

from .creation import create_measure
from .model import ModelError
from .parser import parse_model
from .project import _json,_report

ROOTS=['Production rate by period','Stock coverage by period','Target variance by period']


def production_model():
    """A complete 24-measure, eight-table model with long shared helper paths."""
    measures={
        'Raw produced units':"SUM('Production Daily'[ProducedUnits])",
        'Produced per day':"SUMX(VALUES('Production Daily'[Day]), [Raw produced units])",
        'Raw rejected units':"SUM('Production Daily'[RejectedUnits])",
        'Rejected per day':"SUMX(VALUES('Production Daily'[Day]), CALCULATE([Raw rejected units]))",
        'Net produced units':('VAR gross=[Produced per day] VAR rejected=[Rejected per day] '
                              'RETURN IF(ISBLANK(gross) && ISBLANK(rejected),BLANK(),gross-rejected)'),
        'Eligible actual units':"CALCULATE([Net produced units], 'Production Daily'[Enabled] == 1)",
        'Visible actual units':'VAR quantity=[Eligible actual units] RETURN IF(ISBLANK(quantity),BLANK(),quantity)',
        'Calendar workdays':"SUMX(VALUES('Calendar'[Day]), CALCULATE(SUM('Calendar'[Workday])))",
        'Actual daily rate':'DIVIDE([Visible actual units], [Calendar workdays])',
        'Raw planned units':"SUM('Plan Daily'[PlannedUnits])",
        'Planned per day':"SUMX(VALUES('Plan Daily'[Day]), [Raw planned units])",
        'Planned rejects per day':"SUMX(VALUES('Plan Daily'[Day]), CALCULATE(SUM('Plan Daily'[RejectedUnits])))",
        'Net planned units':('VAR gross=[Planned per day] VAR rejected=[Planned rejects per day] '
                             'RETURN IF(ISBLANK(gross) && ISBLANK(rejected),BLANK(),gross-rejected)'),
        'Eligible planned units':"CALCULATE([Net planned units], 'Plan Daily'[Enabled] == 1)",
        'Visible planned units':'VAR quantity=[Eligible planned units] RETURN IF(ISBLANK(quantity),BLANK(),quantity)',
        'Planned daily rate':'DIVIDE([Visible planned units], [Calendar workdays])',
        'Closing stock units':("VAR lastDay=MAX('Production Daily'[Day]) RETURN "
                               "CALCULATE(SUM('Production Daily'[ClosingUnits]), 'Production Daily'[Day] == lastDay)"),
        'Actual stock coverage':'DIVIDE([Closing stock units], [Actual daily rate])',
        'Planned stock coverage':'DIVIDE([Closing stock units], [Planned daily rate])',
        'Unit target variance':('VAR actual=[Visible actual units] VAR planned=[Visible planned units] '
                                'RETURN IF(ISBLANK(actual) && ISBLANK(planned),BLANK(),actual-planned)'),
        'Remaining target variance':'VAR planned=[Visible planned units] RETURN IF(ISBLANK(planned),BLANK(),0-planned)',
    }
    for name,actual,planned in [(ROOTS[0],'Actual daily rate','Planned daily rate'),
                                (ROOTS[1],'Actual stock coverage','Planned stock coverage'),
                                (ROOTS[2],'Unit target variance','Remaining target variance')]:
        measures[name]=('VAR clock=TODAY()\nVAR periodRows=VALUES(\'Periods\'[PeriodKey])\n'
            'VAR scored=ADDCOLUMNS(periodRows,"@value",\n'
            '    VAR periodEnd=CALCULATE(MAX(\'Periods\'[EndDay]))\n'
            '    VAR useActual=IF(ISBLANK(periodEnd),FALSE(),periodEnd < clock)\n'
            '    RETURN IF(useActual,['+actual+'],['+planned+']))\n'
            'VAR keptRows=FILTER(scored,NOT ISBLANK([@value]))\n'
            'RETURN IF(ISEMPTY(keptRows),BLANK(),SUMX(keptRows,[@value]))')
    schemas={
        'Periods':{'PeriodKey':'int64','EndDay':'dateTime'},
        'Calendar':{'Day':'dateTime','PeriodKey':'int64','Workday':'int64'},
        'Site':{'SiteKey':'int64','Region':'string'},
        'Product':{'ProductKey':'int64','Family':'string'},
        'Budget':{'BudgetKey':'int64','Version':'string'},
        'Production Daily':{'Day':'dateTime','SiteKey':'int64','ProductKey':'int64',
                            'ProducedUnits':'int64','RejectedUnits':'int64','ClosingUnits':'int64','Enabled':'int64'},
        'Plan Daily':{'Day':'dateTime','SiteKey':'int64','ProductKey':'int64','BudgetKey':'int64',
                      'PlannedUnits':'int64','RejectedUnits':'int64','Enabled':'int64'},
        'Metrics':{'Placeholder':'int64'}}
    tables=[{'name':name,'columns':[{'name':col,'dataType':typ,'sourceColumn':col} for col,typ in fields.items()],
             'partitions':[{'name':name,'mode':'import'}]} for name,fields in schemas.items()]
    tables[-1]['measures']=[{'name':name,'expression':expr,'formatString':'0.0000'} for name,expr in measures.items()]
    links=[('Calendar','PeriodKey','Periods','PeriodKey')]
    for fact in ['Production Daily','Plan Daily']:
        links.extend([(fact,'Day','Calendar','Day'),(fact,'SiteKey','Site','SiteKey'),(fact,'ProductKey','Product','ProductKey')])
    links.append(('Plan Daily','BudgetKey','Budget','BudgetKey'))
    return {'name':'Fictional daily production lab','compatibilityLevel':1601,'model':{
        'culture':'en-US','defaultPowerBIDataSourceVersion':'powerBI_V3','tables':tables,
        'relationships':[{'name':'Lab relation '+str(i),'fromTable':a,'fromColumn':b,'toTable':c,'toColumn':d,
            'fromCardinality':'many','toCardinality':'one','crossFilteringBehavior':'oneDirection','isActive':True}
            for i,(a,b,c,d) in enumerate(links,1)]}}


def production_data(days=112,sites=3,products=4):
    """Daily site/product grain with missing days, corrections, BLANKs and zeros."""
    for value,low,high,label in [(days,28,1096,'days'),(sites,2,12,'sites'),(products,2,12,'products')]:
        if type(value) is not int or not low<=value<=high:raise ModelError(f'{label} must be between {low} and {high}')
    start=date(2024,1,1)
    data={'Periods':[],'Calendar':[],'Site':[[i,'Region '+str((i-1)%2+1)] for i in range(1,sites+1)],
          'Product':[[i,'Empty' if i==products else 'Family '+str((i-1)%2+1)] for i in range(1,products+1)],
          'Budget':[[1,'Original'],[2,'Revision']], 'Metrics':[[1]],'Production Daily':[],'Plan Daily':[]}
    for key in range((days+13)//14):data['Periods'].append([key+1,(start+timedelta(days=min(days-1,key*14+13))).isoformat()])
    for offset in range(days):
        current=start+timedelta(days=offset);day=current.isoformat();period=offset//14+1
        data['Calendar'].append([day,period,int(current.weekday()<5)])
        for site in range(1,sites+1):
            for product in range(1,products+1):
                empty=product==products;zero=(offset+site+product)%29==0
                units=None if empty else 0 if zero else -40 if (offset+site+product)%37==0 else 80+(offset*7+site*11+product*3)%90
                rejected=None if empty else 0 if zero else (offset+product)%6
                stock=None if empty or (offset+site)%13==0 else 0 if zero else 700+site*90+product*25+offset
                # Unequal last observation dates make stock deliberately
                # nonadditive across sites and periods.
                if not (site==sites and offset%14>=11):
                    data['Production Daily'].append([day,site,product,units,rejected,stock,int(offset%19!=0)])
                for budget in [1,2]:
                    planned=None if empty else 0 if zero else 100+site*7+product*4+budget*9
                    data['Plan Daily'].append([day,site,product,budget,planned,None if empty else budget+product,int(offset%23!=0)])
    data['Production Daily'].append([None,1,1,11,0,55,1])
    return data


def _embedded_partition(table,rows):
    stream=io.StringIO(newline='');writer=csv.writer(stream);writer.writerow([c['name'] for c in table['columns']]);writer.writerows(rows)
    payload=base64.b64encode(gzip.compress(stream.getvalue().encode('utf-8'),mtime=0)).decode('ascii')
    types={'int64':'Int64.Type','dateTime':'type datetime','string':'type text'}
    fields=', '.join('{"'+c['name']+'", '+types[c['dataType']]+'}' for c in table['columns'])
    return {'type':'m','expression':('let Source=Csv.Document(Binary.Decompress(Binary.FromText("'+payload+'", BinaryEncoding.Base64), Compression.GZip),'
        '[Delimiter=",", Encoding=65001, QuoteStyle=QuoteStyle.Csv]), Headers=Table.PromoteHeaders(Source,[PromoteAllScalars=true]), '
        'Typed=Table.TransformColumnTypes(Headers,{'+fields+'},"en-US") in Typed')}


def create_production_lab(directory,*,days=112,sites=3,products=4):
    """Export mock sources, three optimized roots, one PBIP matrix and DAGs."""
    root=Path(directory).resolve()
    if root.exists() and (not root.is_dir() or any(root.iterdir())):raise ModelError('Output directory must be absent or empty')
    root.mkdir(parents=True,exist_ok=True)
    document=production_model();data=production_data(days,sites,products)
    for table in document['model']['tables']:table['partitions'][0]['source']=_embedded_partition(table,data[table['name']])
    _json(root/'daily-production.bim',document);_json(root/'daily-data.json',data)
    dates=['2024-01-01','2024-02-15','2024-02-29','2025-01-01']
    model=parse_model(document);changes=[];cases=[]
    for index,name in enumerate(ROOTS,1):
        visual={'id':'production-matrix-'+str(index),'title':name,'rows':[['Site','Region'],['Product','Family']],
                'columns':[['Periods','PeriodKey']],'values':[['Metrics',name]],'context_complete':True}
        created=create_measure(model,name,table='Metrics',visuals=[visual],validation_dates=dates)
        created.write(root/'measures'/str(index));obj=copy.deepcopy(created.data['measure_object'])
        changes.append({'table':'Metrics','original_name':name,'candidate':obj})
        cases.append({'name':name,'candidate':obj['name'],'directory':'measures/'+str(index),
                      'chain_specialization':created.data['chain_specialization'],
                      'before_operators':created.data['dag_comparison']['difference']['query_operators_original'],
                      'after_operators':created.data['dag_comparison']['difference']['query_operators_proposed']})
    document['model']['tables'][-1]['measures'].extend(c['candidate'] for c in changes)
    _json(root/'Showcase.SemanticModel/model.bim',document)
    _report(root,{'rows':[['Site','Region'],['Product','Family']],'columns':[['Periods','PeriodKey']],
                  'title':'Daily production: original and proposed measures'},changes)
    visual_path=root/'Showcase.Report/definition/pages/Showcase/visuals/Comparison/visual.json'
    visual=json.loads(visual_path.read_text());visual['visual']['visualContainerObjects']['subTitle'][0]['properties']['text']['expr']['Literal']['Value']="'Daily-grain sources; guarded DAX proposals; validate before interpreting timings'";_json(visual_path,visual)
    manifest={'kind':'daily-production-lab','days':days,'sites':sites,'products':products,
              'dates':dates,'row_counts':{k:len(v) for k,v in data.items()},'cases':cases,
              'scope':'Independent fictional business rules and mock sources; not reconstructed user data. No engine execution or speedup is implied.'}
    _json(root/'production-lab.json',manifest)
    links=''.join('<li>'+name+' · <a href="measures/'+str(i)+'/report.html">New measure & DAGs</a></li>' for i,name in enumerate(ROOTS,1))
    (root/'report.html').write_text('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Daily production lab</title><style>body{font:16px/1.6 system-ui;max-width:900px;margin:40px auto;padding:20px}</style><h1>Daily production → planning periods</h1><p>Eight tables and 24 measures with shared paths for rejects, eligibility, workdays, plan revisions, stock snapshots and TODAY-based actual/plan selection. Original helpers are unchanged; each proposed root is one new measure.</p><ul>'+links+'</ul><p>Open Showcase.pbip and refresh in Desktop. One matrix compares three original/candidate pairs. The integer source units are exact counts; stock and ratios remain nonadditive. Queries include controlled dates, independent visual contexts and explicit BLANK comparisons. Correctness and timing have not been tested by this export.</p>',encoding='utf-8')
    return root
