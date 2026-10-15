"""Compatibility import for callers; the JSON file is the only rule catalogue."""
from .spec import load_spec


def default_catalog():
    return load_spec()


def __getattr__(name):
    if name == 'CHIPSETS':
        return load_spec()['lookups']['firmware']
    raise AttributeError(name)
