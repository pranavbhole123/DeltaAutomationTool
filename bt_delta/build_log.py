"""Compatibility helper for callers of the original SLSI build-log reader.

The engine uses Values directly; extraction declarations live only in JSON.
"""
from .spec import load_spec
from .expressions import Values


def read_build_log(path, report):
    spec = load_spec()
    values = Values(spec, {}, log=report)
    result = {}
    for declaration in spec['derive'].values():
        expression = declaration['value']
        if not isinstance(expression, dict) or 'log' not in expression:
            continue
        from .spec import set_value
        set_value(values.config, expression['log']['input'], str(path))
        value = values.eval(expression)
        if value is not None:
            result[declaration['export'].rsplit('.', 1)[-1]] = value
    return result
