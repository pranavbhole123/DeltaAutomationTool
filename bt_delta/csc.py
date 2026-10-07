"""Discover regional carrier JSON and add missing keys without replacing values."""
import copy
import fnmatch
import json

from .perforce import PerforceError


CARRIER_FILE_PATTERNS = ["custom_carrier_feature_plan.json"]


def model_root(path, model):
    """Accept an old region path, but search from its selected model directory."""
    parts = path.rstrip("/")[2:].split("/")
    matches = [index for index, part in enumerate(parts[1:], 1) if part.lower() == model.lower()]
    if len(matches) != 1:
        raise ValueError(f"CSC path must contain one {model} model directory: {path}")
    return "//" + "/".join(parts[:matches[0] + 1])


def carrier_files(p4, root, rule):
    root = root.rstrip("/")
    result = {}
    patterns = rule.get("file_patterns", CARRIER_FILE_PATTERNS)
    for record in p4.files(root + "/..."):
        path = record["depotFile"]
        if not path.startswith(root + "/"):
            raise PerforceError(f"Carrier discovery returned a file outside the configured CSC model root: {path}")
        filename = path.rsplit("/", 1)[-1].lower()
        if any(fnmatch.fnmatchcase(filename, pattern.lower()) for pattern in patterns):
            relative = path[len(root) + 1:]
            result[relative] = path
    return dict(sorted(result.items()))


def carrier_json(text):
    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate carrier JSON key: {key}")
            result[key] = value
        return result
    value = json.loads(text, object_pairs_hook=unique_keys)
    if not isinstance(value, dict):
        raise ValueError("Carrier JSON must contain an object")
    return value


def add_missing_features(reference, current):
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
