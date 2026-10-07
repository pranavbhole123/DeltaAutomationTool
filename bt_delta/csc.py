"""Discover regional carrier JSON and add missing keys without replacing values."""
import copy
import fnmatch
import json
import logging

from .perforce import PerforceError


CARRIER_FILE_PATTERNS = ["*carrier_feature*.json"]


def model_root(path, model):
    """Accept an old region path, but search from its selected model directory."""
    parts = path.rstrip("/")[2:].split("/")
    matches = [index for index, part in enumerate(parts[1:], 1) if part.lower() == model.lower()]
    if len(matches) != 1:
        raise ValueError(f"CSC path must contain one {model} model directory: {path}")
    return "//" + "/".join(parts[:matches[0] + 1])


def carrier_files(p4, root, rule, *, details=None):
    root = root.rstrip("/")
    result = {}
    patterns = rule.get("file_patterns", CARRIER_FILE_PATTERNS)
    query = root + "/..."
    records = p4.files(query)
    candidates = []
    for record in records:
        path = record["depotFile"]
        if not path.startswith(root + "/"):
            raise PerforceError(f"Carrier discovery returned a file outside the configured CSC model root: {path}")
        filename = path.rsplit("/", 1)[-1].lower()
        if "carrier_" in filename and filename.endswith(".json"):
            candidates.append(path)
        if any(fnmatch.fnmatchcase(filename, pattern.lower()) for pattern in patterns):
            relative = path[len(root) + 1:]
            result[relative] = path
    information = {"query": query, "returned_files": len(records), "matched_files": len(result),
                   "patterns": list(patterns), "carrier_candidates": sorted(candidates)[:8]}
    if details is not None:
        details.update(information)
    logging.getLogger(__name__).info("CSC discovery: %s; %d files returned, %d matched %s; examples: %s",
                                    query, len(records), len(result), ", ".join(patterns),
                                    ", ".join(sorted(result.values())[:5] or information["carrier_candidates"]))
    return dict(sorted(result.items()))


def missing_carrier_message(details):
    query = details["query"]
    if not details["returned_files"]:
        return (f"Perforce returned no files for {query}. Check the exact depot/directory casing "
                "and that the configured Perforce server contains this path.")
    candidates = "; ".join(details["carrier_candidates"]) or "none"
    return (f"Perforce returned {details['returned_files']} files for {query}, but none matched "
            f"{', '.join(details['patterns'])}. Carrier JSON candidates returned: {candidates}.")


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
