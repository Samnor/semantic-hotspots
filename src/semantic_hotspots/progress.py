"""Optional, synchronous progress observations; never estimates of remaining work."""


def emit(callback, stage, label, *, completed=None, total=None, unit=None, detail=None):
    if callback is not None:
        callback({'stage': stage, 'label': label, 'completed': completed,
                  'total': total, 'unit': unit, 'detail': detail})
