"""Independent arithmetic oracle for the fictional production lab, not a DAX evaluator.

Works from source rows and stated business rules; it never reads generated DAX.
"""
from datetime import date


def total(values):
    present=[v for v in values if v is not None]
    return sum(present) if present else None


def difference(left,right):
    return None if left is None and right is None else (left or 0)-(right or 0)


def divide(left,right):
    return None if left is None or not right else left/right


def expected(data,as_of,context):
    """Three scalar roots, each summing eligible period values (not daily ratios)."""
    sites={r[0] for r in data['Site'] if not context.get('region') or r[1]==context['region']}
    if 'site' in context:sites&={context['site']}
    products={r[0] for r in data['Product'] if not context.get('family') or r[1]==context['family']}
    if 'product' in context:products&={context['product']}
    budgets={r[0] for r in data['Budget'] if not context.get('budget') or r[1]==context['budget']}
    dates=set(context['dates']) if 'dates' in context else None
    periods=[p for p in data['Periods'] if 'period' not in context or p[0]==context['period']]
    results=[[],[],[]]
    for key,end in periods:
        calendar=[r for r in data['Calendar'] if r[1]==key and (dates is None or r[0] in dates)]
        days={r[0] for r in calendar};workdays=total(r[2] for r in calendar)
        actual=[r for r in data['Production Daily'] if r[0] in days and r[1] in sites and r[2] in products]
        plan=[r for r in data['Plan Daily'] if r[0] in days and r[1] in sites and r[2] in products and r[3] in budgets]
        eligible=[r for r in actual if r[6]==1];planned=[r for r in plan if r[6]==1]
        # Eligibility CALCULATE replaces an incoming filter on that flag,
        # while the independent snapshot retains the incoming flag filter.
        if 'actual_enabled' in context:actual=[r for r in actual if r[6]==context['actual_enabled']]
        net=difference(total(r[3] for r in eligible),total(r[4] for r in eligible))
        target=difference(total(r[4] for r in planned),total(r[5] for r in planned))
        observed=[r[0] for r in actual if r[0] is not None]
        last=max(observed) if observed else None
        stock=total(r[5] for r in actual if r[0]==last)
        past=end<as_of
        rate=divide(net if past else target,workdays)
        coverage=divide(stock,rate)
        variance=difference(net,target) if past else None if target is None else -target
        for values,value in zip(results,[rate,coverage,variance]):values.append(value)
    return [total(values) for values in results]


def contexts(data):
    """Totals, leaf and subtotal slices, empty/BLANK, dates and budget revisions."""
    result=[
        {},{'region':'Region 1'},{'region':'Region 2'},{'site':len(data['Site'])},
        {'family':'Family 1'},{'family':'Family 2'},{'family':'Empty'},{'region':'absent'},
        {'period':1},{'period':2},{'period':3},{'period':999},
        {'budget':'Original'},{'budget':'Revision'},
        {'period':2,'family':'Family 1','region':'Region 2','budget':'Revision'},
        {'period':2,'site':len(data['Site']),'family':'Family 1'},
        {'dates':['2024-01-06','2024-01-07']},
        {'dates':['2024-01-03','2024-01-16','2024-02-29'],'budget':'Original'},
        {'dates':[]},{'actual_enabled':0},{'plan_enabled':0},
    ]
    workdays={r[0] for r in data['Calendar'] if r[2]==1}
    for predicate in [lambda v:v==0,lambda v:v<0]:
        row=next((r for r in data['Production Daily'] if r[3] is not None and predicate(r[3]) and r[6]==1 and r[0] in workdays),None)
        if row:result.append({'dates':[row[0]],'site':row[1],'product':row[2]})
    return result
