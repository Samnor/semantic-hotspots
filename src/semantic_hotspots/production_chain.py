"""Six sequential fictional measures, starting at daily production grain."""
from __future__ import annotations

import copy
import html
from pathlib import Path

from .creation import create_measure
from .model import Entity, ModelError
from .parser import parse_model
from .production_lab import production_model, production_data, _embedded_partition
from .project import _json, _report

CHAIN_MEASURES = [
    'Daily production',
    'Daily production difference',
    'Period production difference',
    'Cumulative production difference',
    'Average daily production difference',
    'Projected next-period production difference',
]


def production_chain_model():
    """Exactly six measures; the final root has five transitive helpers.

    Difference means current production minus the previous calendar day's
    production. Missing either observation is BLANK, not zero or a change from
    the previous observed day. Calendar filters are removed only for the prior
    lookup; site/product filters stay in force.
    """
    document = production_model()
    document['name'] = 'Fictional six-stage daily production chain'
    # Reuse the coherent source schemas and keys; no unrelated measures or plan
    # branches are retained in this smaller fixture.
    keep = {'Periods', 'Calendar', 'Site', 'Product', 'Production Daily', 'Metrics'}
    document['model']['tables'] = [t for t in document['model']['tables'] if t['name'] in keep]
    document['model']['relationships'] = [r for r in document['model']['relationships']
                                        if r['fromTable'] in keep and r['toTable'] in keep]
    expressions = [
        "SUMX(VALUES('Production Daily'[Day]), CALCULATE(SUM('Production Daily'[ProducedUnits])))",
        """VAR dayCount = COUNTROWS(VALUES('Calendar'[Day]))
VAR currentDay = MAX('Calendar'[Day])
VAR currentUnits = [Daily production]
VAR priorUnits = CALCULATE(
    [Daily production],
    REMOVEFILTERS('Calendar'),
    REMOVEFILTERS('Periods'),
    'Calendar'[Day] == currentDay - 1
)
RETURN IF(dayCount <> 1 || ISBLANK(currentDay) || ISBLANK(currentUnits) || ISBLANK(priorUnits),
    BLANK(), currentUnits - priorUnits)""",
        "SUMX(VALUES('Calendar'[Day]), [Daily production difference])",
        """VAR lastPeriod = MAX('Periods'[PeriodKey])
RETURN IF(ISBLANK(lastPeriod), BLANK(),
    CALCULATE([Period production difference],
        FILTER(ALL('Periods'), 'Periods'[PeriodKey] <= lastPeriod)))""",
        """VAR lastPeriod = MAX('Periods'[PeriodKey])
VAR calendarDays = CALCULATE(COUNTROWS('Calendar'),
    FILTER(ALL('Periods'), 'Periods'[PeriodKey] <= lastPeriod))
RETURN DIVIDE([Cumulative production difference], calendarDays)""",
        """VAR dailyChange = [Average daily production difference]
RETURN IF(ISBLANK(dailyChange), BLANK(), dailyChange * 14)""",
    ]
    for table in document['model']['tables']:
        table.pop('measures', None)
        if table['name'] == 'Metrics':
            table['measures'] = [{'name': name, 'expression': expression,
                                  'formatString': '0.0000' if i >= 4 else '0'}
                                 for i, (name, expression) in enumerate(zip(CHAIN_MEASURES, expressions))]
    return document


def create_production_chain_lab(directory, *, days=112, sites=3, products=4):
    """Export six original measures, one generated root, a matrix and both DAGs.

    This creates sources and validation queries; it does not execute an engine.
    A separate native run is required to establish correctness or compute use.
    """
    root = Path(directory).resolve()
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise ModelError('Output directory must be absent or empty')
    document = production_chain_model()
    all_data = production_data(days, sites, products)
    data = {t['name']: all_data[t['name']] for t in document['model']['tables']}
    for table in document['model']['tables']:
        table['partitions'][0]['source'] = _embedded_partition(table, data[table['name']])
    model = parse_model(document)
    visual = {'id': 'six-stage-production-chain', 'title': 'Daily differences through five helpers',
              'rows': [['Site', 'Region'], ['Product', 'Family']],
              'columns': [['Periods', 'PeriodKey']],
              'values': [['Metrics', CHAIN_MEASURES[-1]]], 'context_complete': True}
    created = create_measure(model, CHAIN_MEASURES[-1], table='Metrics', visuals=[visual])
    if created.data['outcome'] != 'rewritten' or not created.data['validation_available']:
        raise ModelError('Six-stage fixture did not produce a validatable root proposal')
    root.mkdir(parents=True, exist_ok=True)
    _json(root/'original-model.bim', document)
    _json(root/'daily-data.json', data)
    created.write(root/'new-measure')
    candidate = copy.deepcopy(created.data['measure_object'])
    showcase = copy.deepcopy(document)
    next(t for t in showcase['model']['tables'] if t['name'] == 'Metrics')['measures'].append(candidate)
    _json(root/'Showcase.SemanticModel/model.bim', showcase)
    originals = next(t for t in document['model']['tables'] if t['name'] == 'Metrics')['measures']
    _json(root/'measure-definitions.json', {'originals': originals, 'generated': candidate})
    declarations = []
    for measure in [*originals, candidate]:
        target = Entity('measure', 'Metrics', measure['name']).qualified_name
        declarations.append('MEASURE '+target+' =\n'+measure['expression'])
    original = Entity('measure', 'Metrics', CHAIN_MEASURES[-1]).qualified_name
    proposed = Entity('measure', 'Metrics', candidate['name']).qualified_name
    fields = ('"Original", '+original+', "Candidate", '+proposed+',\n'
              '"Equal", IF(ISBLANK('+original+') == ISBLANK('+proposed+') && '+original+' == '+proposed+', 1, 0),\n'
              '"OriginalBlank", ISBLANK('+original+'), "CandidateBlank", ISBLANK('+proposed+')')
    (root/'validate-chain.dax').write_text(
        '// Query-scoped definitions of all six originals and the generated candidate. No model edits.\n'
        'DEFINE\n'+'\n\n'.join(declarations)+'\nEVALUATE ROW('+fields+')\n', encoding='utf-8')
    _report(root, dict(visual, title='Six-stage production chain: original and candidate'),
            [{'table': 'Metrics', 'original_name': CHAIN_MEASURES[-1], 'candidate': candidate}])
    manifest = {'kind': 'six-stage-production-chain', 'measures': CHAIN_MEASURES,
                'root': CHAIN_MEASURES[-1], 'transitive_helper_count': 5,
                'difference': 'Current day minus previous calendar day; BLANK if either observation is missing',
                'cumulative_scope': 'All periods through the current period, retaining site/product and explicit Calendar filters',
                'average_denominator': 'All calendar days in the cumulative context, including days without a difference',
                'forecast_horizon_calendar_days': 14, 'row_counts': {k: len(v) for k, v in data.items()},
                'candidate': candidate['name'], 'correctness': 'not_run', 'compute': 'unknown'}
    _json(root/'chain-lab.json', manifest)
    steps = ''.join('<li>'+html.escape(name)+'</li>' for name in CHAIN_MEASURES)
    (root/'report.html').write_text(
        '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>Six-stage production chain</title><style>body{font:16px/1.6 system-ui;max-width:900px;margin:40px auto;padding:20px}</style>'
        '<h1>One measure, five helpers</h1><ol>'+steps+'</ol>'
        '<p>Daily difference uses the previous calendar day. Missing observations remain BLANK. Period differences sum daily changes; cumulative totals evaluate the selected period range. The average divides by all calendar days in that cumulative context. The last step projects that average over 14 days, a fictional test rule.</p>'
        '<p>The generated candidate changes only the final root. The five helper definitions remain unchanged. '
        '<a href="new-measure/report.html">Inspect the new measure</a> · '
        '<a href="new-measure/dags/report.html">Original and proposed DAGs</a> · '
        '<a href="chain-lab.json">Fixture rules and source counts</a> · '
        '<a href="measure-definitions.json">All six original definitions</a> · '
        '<a href="validate-chain.dax">Runnable full-chain validation query</a></p>'
        '<p>Open Showcase.pbip and refresh in Power BI Desktop. One matrix compares both paths. Exporting this fixture does not execute DAX or establish correctness or CU savings.</p>', encoding='utf-8')
    return root
