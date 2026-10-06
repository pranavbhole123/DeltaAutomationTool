"""Small, auditable Perforce CLI adapter and conservative client-view resolver.

Every P4CLI instance starts read-only. The application may call enable_writes()
only after validating an approved plan. This module never submits, reverts,
deletes depot files, or creates/changes client specifications. All CLI calls use
argument arrays and the marshal protocol; P4Python is not required.

``fstat`` returns an empty dict and ``files`` an empty list only for Perforce's
EV_EMPTY (17). Authentication, connectivity and permission failures are errors.
``read_file`` always prints an explicit positive revision: when omitted, the
revision is first resolved with ``files``. Plans should supply their saved rev.
"""

from __future__ import annotations

import io
import marshal
import os
from pathlib import Path, PureWindowsPath
import re
import shlex
import subprocess
import logging
import time
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence


EV_EMPTY = 17


class PerforceError(RuntimeError):
    """A Perforce command failed or returned an unsafe/unexpected result."""


class MappingError(PerforceError):
    """A client view cannot resolve one unambiguous depot path."""


class PerforceTimeout(PerforceError):
    """Stop planning when the server does not answer within the request limit."""


class WriteDisabledError(PerforceError):
    """An operation attempted to mutate a read-only adapter."""


# Short alias retained for callers that prefer conventional P4 terminology.
P4Error = PerforceError


def _text(value: Any) -> str:
    return value.decode("utf-8", "surrogateescape") if isinstance(value, bytes) else str(value)


def _safe_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value):
        raise ValueError(f"{label} must be nonempty text without control characters")
    return value


def _name(value: Any, label: str) -> str:
    value = _safe_text(value, label)
    if value.startswith("-") or any(c in value for c in "/\\@#*%") or "..." in value:
        raise ValueError(f"Invalid {label}: {value!r}")
    return value


def validate_depot_path(path: str, *, allow_wildcards: bool = False) -> str:
    """Validate an absolute depot path without revision selectors or switches."""
    path = _safe_text(path, "Perforce path")
    if not path.startswith("//") or path.startswith("///") or "\\" in path:
        raise ValueError("Expected a //depot/path using forward slashes")
    if "@" in path or "#" in path:
        raise ValueError("Pass revision numbers separately; @ and # are not allowed in paths")
    if not allow_wildcards and ("*" in path or "..." in path):
        raise ValueError("Wildcards are only allowed in discovery patterns")
    parts = path[2:].split("/")
    if len(parts) < 2 or any(part in ("", ".", "..") for part in parts):
        raise ValueError("Expected a depot and file path without empty/traversal components")
    return path


def _revision(value: Any) -> str:
    # bool is an int subclass, but never a meaningful revision or changelist.
    if isinstance(value, bool) or not re.fullmatch(r"[1-9][0-9]*", str(value)):
        raise ValueError("Revision/changelist must be a positive integer")
    return str(value)


class P4CLI:
    """Perforce adapter configured by port, user, client and optional executable.

    Optional ``timeout_seconds`` defaults to 30. Calls are noninteractive and
    inherit the user's existing ticket/charset environment. No credentials are
    accepted or persisted. Public read methods return decoded string-keyed dicts.
    """

    def __init__(self, config: Mapping[str, Any], progress=None):
        self.port = _safe_text(config.get("port"), "port")
        self.user = _name(config.get("user"), "user")
        self.client = _name(config.get("client"), "client")
        self.executable = _safe_text(config.get("executable", "p4"), "executable")
        self.timeout_seconds = float(config.get("timeout_seconds", 30))
        self.progress = progress
        if not 0 < self.timeout_seconds <= 3600:
            raise ValueError("timeout_seconds must be between 0 and 3600")
        self._writes_enabled = False

    def _report(self, message):
        logging.getLogger(__name__).info(message)
        if self.progress:
            self.progress(message)

    @property
    def writes_enabled(self) -> bool:
        return self._writes_enabled

    def enable_writes(self) -> None:
        """Explicitly enable pending-change operations on this instance only."""
        self._writes_enabled = True

    def _require_writes(self) -> None:
        if not self._writes_enabled:
            raise WriteDisabledError("Perforce writes require an approved plan and enable_writes()")

    def _run(
        self,
        command: str,
        *arguments: str,
        allow_empty: bool = False,
        input_record: Mapping[str, str] | None = None,
        binary_data: bool = False,
    ) -> list[dict[str, Any]]:
        argv = [self.executable, "-G", "-p", self.port, "-u", self.user, "-c", self.client,
                command, *arguments]
        kwargs: dict[str, Any] = {
            "stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
            "timeout": self.timeout_seconds, "check": False, "shell": False,
        }
        if input_record is None:
            kwargs["stdin"] = subprocess.DEVNULL
        else:
            encoded = {key.encode("utf-8"): value.encode("utf-8")
                       for key, value in input_record.items()}
            kwargs["input"] = marshal.dumps(encoded, 0)
        label = "p4 " + " ".join([command, *arguments])
        self._report(f"Running ({self.timeout_seconds:g}s timeout): {label}")
        started = time.monotonic()
        try:
            completed = subprocess.run(argv, **kwargs)
        except subprocess.TimeoutExpired as exc:
            self._report(f"TIMEOUT after {self.timeout_seconds:g}s: {label}")
            raise PerforceTimeout(f"Timed out after {self.timeout_seconds:g}s: {label}. Check server/VPN connectivity or narrow the path using an override.") from exc
        except OSError as exc:
            raise PerforceError(f"p4 {command} could not complete: {exc}") from exc
        self._report(f"Returned in {time.monotonic() - started:.1f}s (exit {completed.returncode}): {label}")

        stream = io.BytesIO(completed.stdout)
        records: list[dict[str, Any]] = []
        try:
            while stream.tell() < len(completed.stdout):
                raw = marshal.load(stream)
                if not isinstance(raw, dict):
                    raise ValueError("Expected a marshal dictionary")
                record = {}
                for key, value in raw.items():
                    key = _text(key)
                    record[key] = value if binary_data and key == "data" else (
                        _text(value) if isinstance(value, bytes) else value)
                records.append(record)
        except (EOFError, ValueError, TypeError) as exc:
            raise PerforceError(f"p4 {command} returned invalid marshal output") from exc

        errors = [record for record in records if record.get("code") == "error"]
        failures = [record for record in errors
                    if not (allow_empty and str(record.get("generic")) == str(EV_EMPTY))]
        if failures:
            detail = "; ".join(_text(record.get("data", "Perforce error")).strip()
                               for record in failures)
            raise PerforceError(f"p4 {command} failed: {detail}")
        if completed.returncode and not (allow_empty and errors and not failures):
            detail = _text(completed.stderr).strip() or "no error details returned"
            raise PerforceError(f"p4 {command} exited {completed.returncode}: {detail}")
        # Keep explicitly allowed EV_EMPTY records so callers can distinguish
        # confirmed absence from a malformed/empty successful response.
        return records

    @staticmethod
    def _stats(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        return [record for record in records if record.get("code") == "stat"]

    def client_spec(self, name: str) -> dict[str, Any]:
        """Read an existing client/template; reject p4's generated new-client spec."""
        name = _name(name, "client/template name")
        records = self._stats(self._run("client", "-o", name))
        if len(records) != 1 or not records[0].get("Client"):
            raise PerforceError(f"Expected one client specification for {name}")
        spec = records[0]
        if not (spec.get("Update") or spec.get("Access")):
            raise PerforceError(f"Client/template {name!r} does not exist (generated specification)")
        return spec

    def workspace_spec(self) -> dict[str, Any]:
        return self.client_spec(self.client)

    def identity(self) -> dict[str, Any]:
        records = self._stats(self._run("info"))
        if len(records) != 1:
            raise PerforceError("Expected one Perforce server identity record")
        return records[0]

    def files(self, pattern: str) -> list[dict[str, Any]]:
        """Discover head file records; exclude delete and move/delete revisions."""
        pattern = validate_depot_path(pattern, allow_wildcards=True)
        return self._file_records(pattern)

    def _file_records(self, pattern):
        response = self._run("files", pattern, allow_empty=True)
        records = self._stats(response)
        if not records and not any(str(record.get("generic")) == str(EV_EMPTY) for record in response):
            raise PerforceError(f"p4 files returned no file or EV_EMPTY record for {pattern}")
        return [record for record in records
                if record.get("action") not in ("delete", "move/delete")]

    def read_file(self, path: str, revision: int | str | None = None) -> bytes:
        path = validate_depot_path(path)
        if revision is None:
            files = self.files(path)
            if len(files) != 1 or not files[0].get("rev"):
                raise PerforceError(f"Cannot resolve a unique live revision for {path}")
            revision = files[0]["rev"]
        target = f"{path}#{_revision(revision)}"
        return self._print_bytes(target)

    def read_shelved_file(self, path: str, change: int | str) -> bytes:
        return self._print_bytes(f"{validate_depot_path(path)}@={_revision(change)}")

    def _print_bytes(self, target):
        records = self._run("print", "-q", target, binary_data=True)
        chunks: list[bytes] = []
        for record in records:
            if record.get("code") in ("text", "binary"):
                data = record.get("data", b"")
                chunks.append(data if isinstance(data, bytes) else str(data).encode("utf-8"))
        return b"".join(chunks)

    def describe_change(self, change: int | str, *, shelved=False) -> dict[str, Any]:
        """Read every listed file; never request a truncated describe response."""
        number = _revision(change)
        options = ["-s", "-S"] if shelved else ["-s"]
        records = self._stats(self._run("describe", *options, number))
        if len(records) != 1 or str(records[0].get("change")) != number:
            raise PerforceError(f"Expected a complete description of changelist {number}")
        record = records[0]
        if record.get("status") not in ("pending", "submitted"):
            raise PerforceError("Changelist status is missing or unsupported")
        indices = sorted(int(key[9:]) for key in record if re.fullmatch(r"depotFile[0-9]+", key))
        if indices != list(range(len(indices))):
            raise PerforceError("Changelist file list is incomplete")
        files = []
        for index in indices:
            path = validate_depot_path(record[f"depotFile{index}"])
            action = record.get(f"action{index}")
            revision = record.get(f"rev{index}")
            if not action or revision is None or not re.fullmatch(r"[0-9]+", str(revision)):
                raise PerforceError(f"Missing action/revision for changelist file {path}")
            files.append({"path": path, "action": action, "revision": int(revision),
                          "type": record.get(f"type{index}", "")})
        if len({item["path"] for item in files}) != len(files):
            raise PerforceError("Duplicate changelist file paths")
        return {"number": number, "status": record["status"], "user": record.get("user", ""),
                "client": record.get("client", ""), "description": record.get("desc", ""),
                "files": files}

    def fstat(self, path: str) -> dict[str, Any]:
        path = validate_depot_path(path)
        response = self._run("fstat", path, allow_empty=True)
        records = self._stats(response)
        if not records and not any(str(record.get("generic")) == str(EV_EMPTY) for record in response):
            raise PerforceError(f"p4 fstat returned no file or EV_EMPTY record for {path}")
        if len(records) > 1:
            raise PerforceError(f"Expected at most one fstat result for {path}")
        return records[0] if records else {}

    def where(self, path: str) -> str:
        path = validate_depot_path(path)
        records = self._stats(self._run("where", path))
        if any("unmap" in record or str(record.get("depotFile", "")).startswith("-")
               for record in records):
            raise MappingError(f"Workspace mapping excludes {path}")
        local_paths = {record["path"] for record in records if record.get("path")}
        if len(local_paths) != 1:
            raise MappingError(f"Expected one workspace path for {path}; found {len(local_paths)}")
        result = local_paths.pop()
        if not (Path(result).is_absolute() or PureWindowsPath(result).is_absolute()):
            raise MappingError(f"Perforce returned a nonabsolute workspace path for {path}")
        return result

    def create_change(self, description: str) -> str:
        self._require_writes()
        if not isinstance(description, str) or not description.strip() or "\x00" in description:
            raise ValueError("A nonempty changelist description is required")
        records = self._run("change", "-i", input_record={
            "Change": "new", "Client": self.client, "User": self.user,
            "Status": "pending", "Description": description,
        })
        for record in records:
            if record.get("change"):
                return _revision(record["change"])
            match = re.search(r"\bChange ([1-9][0-9]*) created\.", _text(record.get("data", "")))
            if match:
                return match.group(1)
        raise PerforceError("Perforce created a change but returned no recognizable change number")

    def sync(self, path: str, revision: int | str) -> None:
        self._require_writes()
        records = self._run("sync", f"{validate_depot_path(path)}#{_revision(revision)}", allow_empty=True)
        for record in records:
            if record.get("code") == "error" and "up-to-date" not in _text(record.get("data", "")):
                raise PerforceError(f"p4 sync failed: {_text(record.get('data', 'Missing revision'))}")

    def edit(self, path: str, change: int | str) -> None:
        self._require_writes()
        self._run("edit", "-c", _revision(change), validate_depot_path(path))

    def add(self, localpath: str, change: int | str, file_type: str | None = None) -> None:
        self._require_writes()
        localpath = _safe_text(os.fspath(localpath), "local path")
        if not (Path(localpath).is_absolute() or PureWindowsPath(localpath).is_absolute()):
            raise ValueError("Add requires an absolute workspace file path")
        if any(c in localpath for c in "@#*") or "..." in localpath:
            raise ValueError("Add requires a literal file path without Perforce selectors/wildcards")
        options = []
        if file_type:
            if not re.fullmatch(r"(?:text|binary)(?:\+[A-Za-z0-9]+)?", file_type):
                raise ValueError("Unsupported add file type")
            options = ["-t", file_type]
        self._run("add", "-c", _revision(change), *options, localpath)

    def shelve(self, change: int | str) -> None:
        self._require_writes()
        self._run("shelve", "-c", _revision(change))


@dataclass(frozen=True)
class ViewMapping:
    """One client-view line; modifier is '', '+', or '-'."""

    depot: str
    client: str
    modifier: str = ""

    @property
    def relative_pattern(self) -> str:
        return self.client[2:].partition("/")[2]


def _wildcards(pattern: str) -> list[str]:
    return re.findall(r"\.\.\.|\*", pattern)


def _matcher(pattern: str) -> re.Pattern[str]:
    parts = re.split(r"(\.\.\.|\*)", pattern)
    return re.compile("^" + "".join("(.*)" if part == "..." else "([^/]*)" if part == "*"
                                    else re.escape(part) for part in parts) + "$")


def _substitute(pattern: str, groups: Sequence[str]) -> str:
    values = iter(groups)
    return re.sub(r"\.\.\.|\*", lambda _match: next(values), pattern)


def parse_view(spec: Mapping[str, Any] | Sequence[str]) -> tuple[ViewMapping, ...]:
    """Parse marshaled View0/View1 fields, a View list/string, or view lines.

    Quoted paths, ``...`` and ``*`` mappings, exclusions and overlays are parsed.
    Positional %% mappings and ditto (&) mappings are rejected explicitly.
    Translation follows ordered overrides and rejects competing overlays.
    """
    if isinstance(spec, Mapping):
        if "View" in spec:
            raw = spec["View"]
            lines = raw.splitlines() if isinstance(raw, str) else list(raw)
        else:
            keys = sorted((key for key in spec if re.fullmatch(r"View[0-9]+", str(key))),
                          key=lambda key: int(str(key)[4:]))
            lines = [spec[key] for key in keys]
    elif isinstance(spec, str):
        lines = spec.splitlines()
    else:
        lines = list(spec)
    mappings = []
    for number, line in enumerate(lines, 1):
        line = _text(line).strip()
        if not line:
            continue
        try:
            fields = shlex.split(line, posix=True)
        except ValueError as exc:
            raise MappingError(f"Invalid quotes in view line {number}") from exc
        if len(fields) != 2:
            raise MappingError(f"View line {number} must contain depot and client paths")
        depot, client = fields
        modifier = depot[0] if depot[0] in "+-" else ""
        if modifier:
            depot = depot[1:]
        if depot.startswith("&") or "%%" in depot or "%%" in client:
            raise MappingError(f"View line {number} uses unsupported ditto/positional mapping")
        try:
            validate_depot_path(depot, allow_wildcards=True)
            validate_depot_path(client, allow_wildcards=True)
        except ValueError as exc:
            raise MappingError(f"Invalid view line {number}: {exc}") from exc
        if _wildcards(depot) != _wildcards(client):
            raise MappingError(f"View line {number} has incompatible wildcard pairs")
        mappings.append(ViewMapping(depot, client, modifier))
    if not mappings:
        raise MappingError("Client/template has no supported View mappings")
    return tuple(mappings)


def translate_path(view: Sequence[ViewMapping], relative_path: str) -> str:
    """Map a client-relative build path to exactly one depot file path.

    Later ordinary mappings override earlier ones; exclusions remove matching
    candidates. Competing overlays fail with a useful diagnostic.
    No live client is created or changed during template resolution.
    """
    relative_path = _safe_text(relative_path, "client-relative path").replace("\\", "/")
    if relative_path.startswith("/") or ":" in relative_path:
        raise MappingError("Expected a client-relative build path")
    try:
        validate_depot_path("//placeholder/" + relative_path)
    except ValueError as exc:
        raise MappingError(str(exc)) from exc
    candidates: list[str] = []
    for mapping in view:
        match = _matcher(mapping.relative_pattern).fullmatch(relative_path)
        source_matcher = _matcher(mapping.depot)
        if mapping.modifier == "-":
            candidates = [path for path in candidates if not source_matcher.fullmatch(path)]
            continue
        if mapping.modifier != "+":
            # A later normal mapping also overrides a depot path moved to a
            # different client location, even when its RHS does not match.
            candidates = [] if match else [path for path in candidates
                                           if not source_matcher.fullmatch(path)]
        if match is None:
            continue
        candidate = _substitute(mapping.depot, match.groups())
        if candidate not in candidates:
            candidates.append(candidate)
    if not candidates:
        raise MappingError(f"Template does not map (or excludes) {relative_path}")
    if len(candidates) != 1:
        raise MappingError(f"Ambiguous template mapping for {relative_path}: " + ", ".join(candidates))
    return validate_depot_path(candidates[0])


def depot_roots(view: Sequence[ViewMapping]) -> list[str]:
    """List unique static directory prefixes of included depot mappings.

    These are discovery candidates, not authorization to edit every child.
    Resolve each actual file with translate_path and workspace where/fstat.
    """
    roots: list[str] = []
    for mapping in view:
        if mapping.modifier == "-":
            continue
        prefix = re.split(r"\.\.\.|\*", mapping.depot, maxsplit=1)[0]
        root = prefix if prefix.endswith("/") else prefix.rsplit("/", 1)[0] + "/"
        if root not in roots:
            roots.append(root)
    return roots
