"""Optional current-build identifiers from a local build log."""
from pathlib import Path
import re


LOG_FIELDS = {
    "chipset": ("ro.product.board", re.compile(r"^\s*ro\.product\.board\s*=\s*(.*?)\s*$", re.I)),
    "model": ("TARGET", re.compile(r"^\s*TARGET\s*:\s*(.*?)\s*$", re.I)),
    "ap": ("MODEL", re.compile(r"^\s*MODEL\s*:\s*(.*?)\s*$", re.I)),
}


def read_build_log(path, report):
    found = {key: set() for key in LOG_FIELDS}
    try:
        with Path(path).expanduser().open(encoding="utf-8-sig", errors="replace") as log:
            for line in log:
                for key, (label, pattern) in LOG_FIELDS.items():
                    match = pattern.fullmatch(line.rstrip("\r\n"))
                    if match:
                        value = match[1].strip()
                        if re.fullmatch(r"[A-Za-z0-9_.-]+", value):
                            found[key].add(value.lower())
                        else:
                            report(f"Current build log: ignored invalid {label} value {value!r}.")
    except OSError as exc:
        report(f"Optional current build log could not be read: {exc}. Using existing inputs/inference.")
        return {}
    values = {}
    for key, (label, _) in LOG_FIELDS.items():
        candidates = found[key]
        if len(candidates) == 1:
            values[key] = next(iter(candidates))
            report(f"Current build log: {label}={values[key]} → current {key}.")
        elif candidates:
            report(f"Current build log: conflicting {label} values ({', '.join(sorted(candidates))}); using existing input/inference for {key}.")
        else:
            report(f"Current build log: {label} not found; using existing input/inference for {key}.")
    return values
