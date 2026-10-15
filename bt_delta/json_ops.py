"""Generic JSON object parsing and missing-key merging."""

import copy
import json


def json_object(text):
    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result

    value = json.loads(text, object_pairs_hook=unique_keys)
    if not isinstance(value, dict):
        raise ValueError("JSON merge requires an object")
    return value


def merge_missing(reference, current):
    """Merge dictionaries only; existing values and arrays remain untouched."""
    merged = copy.deepcopy(current)
    added = []

    def visit(source, target, trail=""):
        for key, value in source.items():
            location = trail + "/" + key
            if key not in target:
                target[key] = copy.deepcopy(value)
                added.append(location)
            elif isinstance(value, dict) and isinstance(target[key], dict):
                visit(value, target[key], location)

    visit(reference, merged)
    return merged, added
