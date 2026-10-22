"""Independent source-row arithmetic for the fictional six-stage business rules.

This oracle never parses DAX and is not a semantic-engine emulator.
"""
from datetime import date, timedelta


def total(values):
    present = [v for v in values if v is not None]
    return sum(present) if present else None


def expected_chain(data, context=None):
    context = context or {}
    sites = {r[0] for r in data['Site'] if 'regions' not in context or r[1] in context['regions']}
    if 'sites' in context: sites.intersection_update(context['sites'])
    products = {r[0] for r in data['Product'] if 'families' not in context or r[1] in context['families']}
    if 'products' in context: products.intersection_update(context['products'])
    periods = {r[0] for r in data['Periods']}
    if 'periods' in context: periods.intersection_update(context['periods'])
    calendar = {r[0]: r[1] for r in data['Calendar']}
    days = {day for day, period in calendar.items() if period in periods}
    # The fixture deliberately includes an unmatched fact date. VALUES(Calendar)
    # exposes that unknown member even though COUNTROWS(Calendar) counts stored rows.
    unknown_visible = 'periods' not in context or None in context['periods']
    if unknown_visible: days.add(None)
    if 'days' in context: days.intersection_update(context['days'])
    rows = [r for r in data['Production Daily'] if r[1] in sites and r[2] in products]
    by_day = {}
    for row in rows: by_day.setdefault(row[0], []).append(row[3])
    def production(day): return total(by_day.get(day, []))
    def change(day):
        if day is None: return None
        prior = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
        current, previous = production(day), production(prior)
        return None if current is None or previous is None else current - previous
    daily = total(row[3] for row in rows if row[0] in days)
    difference = change(next(iter(days))) if len(days) == 1 else None
    period = total(change(day) for day in days)
    cutoff = max(periods) if periods else None
    cumulative_days = {day for day, key in calendar.items() if cutoff is not None and key <= cutoff}
    if 'days' in context: cumulative_days.intersection_update(context['days'])
    cumulative = total(change(day) for day in cumulative_days) if cutoff is not None else None
    average = cumulative / len(cumulative_days) if cumulative is not None and cumulative_days else None
    projection = average * 14 if average is not None else None
    return [daily, difference, period, cumulative, average, projection]
