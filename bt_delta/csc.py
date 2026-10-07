"""Discover regional carrier JSON and add missing keys without replacing values."""
import copy
import json
import logging

from .perforce import PerforceError


CARRIER_FILENAME = "customer_carrier_feature_plain.json"


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
    # Region metadata identifies the subtree to report, rather than treating
    # etc/, icons/ and each parent directory as a separate missing region.
    # Discovery itself still searches all paths, including arbitrary layouts.
    region_roots = set()
    for directory, files in directories.items():
        if any(file["filename"] in ("customer.xml", "omc.info") for file in files):
            region_roots.add(directory)
        if any(file["filename"].startswith("customer_carrier_feature") for file in files):
            region_roots.add(directory.rsplit("/", 1)[0]
                             if directory.rsplit("/", 1)[-1].lower() == "system" else directory)
    inventory = sorted((file for files in directories.values() for file in files), key=lambda file: file["path"])
    if inventory and not region_roots:
        region_roots.add(root)
    skipped = []
    for region in sorted(region_roots):
        if any(path.startswith(region + "/") for path in result.values()):
            continue
        files = [file for file in inventory if file["path"].startswith(region + "/")]
        skipped.append({"path": region, "files": files})
    information = {"query": query, "returned_files": len(records), "matched_files": len(result),
                   "filename": CARRIER_FILENAME, "skipped_regions": skipped}
    if details is not None:
        details.update(information)
    def report(message):
        logging.getLogger(__name__).info(message)
        progress = getattr(p4, "progress", None)
        if progress:
            progress(message)

    report(f"CSC discovery: {query}; {len(records)} files found; "
           f"{len(result)} named exactly {CARRIER_FILENAME}.")
    file_types = {file["path"]: file["type"] for file in inventory}
    for path in sorted(result.values()):
        report(f"CSC carrier file found: {path} ({file_types[path]}).")
    for region in skipped:
        report(f"CSC region skipped: {region['path']}; searched the entire subtree, "
               f"including system/; {CARRIER_FILENAME} is absent.")
        for file in region["files"]:
            report(f"CSC file found and skipped: {file['path']} ({file['type']}); "
                   f"only {CARRIER_FILENAME} is eligible.")
    return dict(sorted(result.items()))


def missing_carrier_message(details):
    query = details["query"]
    if not details["returned_files"]:
        return (f"Perforce returned no files for {query}. Check the exact depot/directory casing "
                "and that the configured Perforce server contains this path.")
    return (f"Perforce returned {details['returned_files']} files for {query}, but none matched "
            f"the exact filename {CARRIER_FILENAME}. Other files were found and skipped. Directories were left untouched; "
            "their file paths and types are listed in the skipped-region checks.")


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
