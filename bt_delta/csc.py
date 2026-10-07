"""Discover regional carrier JSON and add missing keys without replacing values."""
import copy
import json
import logging

from .perforce import PerforceError


CARRIER_FILENAME = "customer_carrier_feature_plan.json"


def model_root(path, model):
    """Accept an old region path, but search from its selected model directory."""
    parts = path.rstrip("/")[2:].split("/")
    matches = [index for index, part in enumerate(parts[1:], 1) if part.lower() == model.lower()]
    if len(matches) != 1:
        raise ValueError(f"CSC path must contain one {model} model directory: {path}")
    return "//" + "/".join(parts[:matches[0] + 1])


def carrier_files(p4, root, *, details=None):
    root = root.rstrip("/")
    result = {}
    query = root + "/..."
    records = p4.files(query)
    directories = {}
    for record in records:
        path = record["depotFile"]
        if not path.startswith(root + "/"):
            raise PerforceError(f"Carrier discovery returned a file outside the configured CSC model root: {path}")
        directory, filename = path.rsplit("/", 1)
        directories.setdefault(directory, []).append({"path": path, "filename": filename,
                                                     "type": record.get("type", "unknown")})
        # Filename spelling is exact. In particular, customer_carrier_feature.json
        # is a different, binary file and must never be read or copied here.
        if filename == CARRIER_FILENAME:
            relative = path[len(root) + 1:]
            result[relative] = path
    skipped = [{"path": directory, "files": sorted(files, key=lambda file: file["path"])}
               for directory, files in sorted(directories.items())
               if not any(file["filename"] == CARRIER_FILENAME for file in files)]
    information = {"query": query, "returned_files": len(records), "matched_files": len(result),
                   "filename": CARRIER_FILENAME, "skipped_directories": skipped}
    if details is not None:
        details.update(information)
    logging.getLogger(__name__).info("CSC discovery: %s; %d files returned, %d matched %s; examples: %s",
                                    query, len(records), len(result), CARRIER_FILENAME,
                                    ", ".join(sorted(result.values())[:5]))
    return dict(sorted(result.items()))


def missing_carrier_message(details):
    query = details["query"]
    if not details["returned_files"]:
        return (f"Perforce returned no files for {query}. Check the exact depot/directory casing "
                "and that the configured Perforce server contains this path.")
    return (f"Perforce returned {details['returned_files']} files for {query}, but none matched "
            f"the exact filename {CARRIER_FILENAME}. Directories were left untouched; "
            "their file paths and types are listed in the skipped-directory checks.")


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
