"""Resolve checklist targets from explicit, editable template-view routes."""

from __future__ import annotations

import re
from .perforce import MappingError, parse_view, translate_path, validate_depot_path
from .spec import load_spec, get_value
from .expressions import Values, Unresolved
from .diagnostics import emit


class DiscoveryMiss(MappingError):
    """A configured lookup cannot locate a file; not a server/read failure."""

    def __init__(self, message, role, paths=()):
        super().__init__(message)
        self.role, self.paths = role, list(paths)


def pattern_regex(pattern: str):
    return re.compile(
        "^"
        + "".join(
            "(.*)" if p == "..." else "([^/]*)" if p == "*" else re.escape(p)
            for p in re.split(r"(\.\.\.|\*)", pattern)
        )
        + "$"
    )


def relative_for(view, depot: str) -> str:
    candidates = set()
    for mapping in view:
        if mapping.modifier == "-":
            continue
        match = pattern_regex(mapping.depot).fullmatch(depot)
        if match:
            groups = iter(match.groups())
            relative = re.sub(
                r"\.\.\.|\*", lambda _: next(groups), mapping.relative_pattern
            )
            try:
                if translate_path(view, relative) == depot:
                    candidates.add(relative)
            except MappingError:
                continue
    if len(candidates) != 1:
        raise MappingError(f"Path is excluded, ambiguous or outside template: {depot}")
    return candidates.pop()


class Resolver:
    """Resolve only file declarations and source bindings from the selected JSON."""

    def __init__(self, p4, config, spec=None, *, values=None):
        self.p4, self.config = p4, config
        self.spec = spec or load_spec(config=config)
        self.values = values or Values(
            self.spec, config, read=self.read_optional, log=lambda m: emit(p4, m)
        )
        self.specs, self.views, self.roots, self.cache, self.attempts = (
            {},
            {},
            {},
            {},
            {},
        )
        self.excluded = set()
        for key, source in self.spec["sources"].items():
            if not self.values.condition(source.get("when", True)):
                continue
            if source["kind"] == "template":
                template = get_value(config, source["input"])
                spec_data = p4.client_spec(template)
                self.specs[key] = spec_data
                self.views[key] = parse_view(spec_data)
                emit(
                    p4,
                    f"Template loaded: {key} = {template}; {len(self.views[key])} View mappings.",
                )
            else:
                self.roots[key] = validate_depot_path(
                    self.values.eval(source["root"]).rstrip("/")
                )

    def read_optional(self, file_id):
        path = self.locate(file_id, optional=True)
        if not path:
            return None
        records = self.p4.files(path)
        return (
            self.p4.read_file(path, int(records[0]["rev"])).decode("utf-8-sig")
            if records
            else None
        )

    def override(self, role, scope, target):
        return self.config.get("paths", {}).get(f"{role}.{scope}.{target}")

    def candidates(self, file_id):
        file = self.spec["files"][file_id]
        if not self.values.condition(file.get("when", True)):
            self.excluded.add(file_id)
            return []
        directory = file["kind"] == "tree"
        explicit = self.config.get("paths", {}).get(file_id)
        source = file["source"]
        if explicit:
            result = [validate_depot_path(explicit.rstrip("/"))]
        else:
            result = set()
            for route in file["routes"]:
                if "depot" in route:
                    raw = (
                        self.values.eval(route["depot"])
                        if isinstance(route["depot"], dict)
                        else self.values.template(route["depot"])
                    )
                    result.add(validate_depot_path(raw.rstrip("/")))
                    continue
                view = self.views.get(source, [])
                if "client_relative" in route:
                    try:
                        result.add(
                            translate_path(
                                view, self.values.template(route["client_relative"])
                            )
                        )
                    except (MappingError, Unresolved):
                        pass
                    continue
                try:
                    relative = self.values.template(
                        route.get("relative", route.get("append", ""))
                    ).strip("/")
                except Unresolved:
                    # Another explicit route may resolve without this optional identifier.
                    continue
                for mapping in view:
                    if mapping.modifier == "-":
                        continue
                    static = re.split(r"\.\.\.|\*", mapping.depot, maxsplit=1)[0]
                    pattern = route.get("anchor_regex", route.get("mapped_regex"))
                    match = re.search(pattern, static)
                    if not match:
                        continue
                    if "mapped_regex" in route:
                        allowed = (
                            self.values.eval(route["allowed"])
                            if "allowed" in route
                            else None
                        )
                        if (
                            allowed is not None
                            and match.group(route["capture"]) not in allowed
                        ):
                            continue
                    candidate = static[: match.end()] + relative
                    probe = (
                        candidate.rstrip("/") + "/__route_probe__"
                        if directory
                        else candidate
                    )
                    if pattern_regex(mapping.depot).fullmatch(probe):
                        result.add(candidate.rstrip("/"))
                        emit(
                            self.p4,
                            f"Route candidate covered by View: {candidate}; mapping={mapping.depot}",
                        )
                    else:
                        emit(
                            self.p4,
                            f"Route candidate rejected before querying Perforce: {candidate}; not covered by mapping={mapping.depot}",
                        )
            result = sorted(result)
        accepted = []
        for path in result:
            try:
                self.covered(
                    file_id,
                    path.rstrip("/") + "/__route_probe__" if directory else path,
                )
            except MappingError:
                if explicit:
                    raise
                self.excluded.add(file_id)
                continue
            accepted.append(path)
        return accepted

    def covered(self, file_id, path):
        source = self.spec["files"][file_id]["source"]
        if source in self.views:
            return relative_for(self.views[source], path)
        root = self.roots.get(source)
        if root and path.startswith(root + "/"):
            return path[len(root) + 1 :]
        raise MappingError("Path is outside the declared source: " + path)

    def inventory(self, file_id):
        file = self.spec["files"][file_id]
        role = self.spec["sources"][file["source"]].get("role", "current")
        try:
            candidates = self.candidates(file_id)
        except Unresolved as exc:
            raise DiscoveryMiss(
                f"{file_id}: could not construct a search path: {exc}. No Perforce query was made.",
                role,
            ) from exc
        if not candidates and file_id in self.excluded:
            self.attempts[file_id] = []
            return [], []
        if not candidates:
            template = get_value(
                self.config, self.spec["sources"][file["source"]].get("input", "")
            )
            routes = []
            for route in file["routes"]:
                rendered = dict(route)
                for key in ("relative", "append", "client_relative"):
                    if key in rendered:
                        try:
                            rendered[key] = self.values.template(rendered[key])
                        except Unresolved:
                            pass
                routes.append(rendered)
            raise DiscoveryMiss(
                f"{file_id} (template {template}): couldn't find a mapped search path; no configured anchor route exists in this template View. No p4 files query was made, so server-side absence is not established. Attempted routes: {routes}",
                role,
            )
        directory = file["kind"] == "tree"
        records, all_records, queries = {}, {}, []
        self.attempts[file_id] = queries
        for candidate in candidates:
            query = candidate.rstrip("/") + "/..." if directory else candidate
            queries.append(query)
            if query not in self.cache:
                emit(self.p4, f"Discovery query for {file_id}: {query}")
                self.cache[query] = self.p4.files(query)
            else:
                emit(self.p4, f"Discovery cache reused for {file_id}: {query}")
            emit(
                self.p4,
                f"Discovery returned {len(self.cache[query])} live file(s): {query}",
            )
            for record in self.cache[query]:
                path = record["depotFile"]
                if not directory and path != candidate:
                    raise MappingError(
                        "Exact discovery returned an unexpected path: " + path
                    )
                if directory and not path.startswith(candidate + "/"):
                    raise MappingError(
                        "Tree discovery returned an out-of-root path: " + path
                    )
                try:
                    self.covered(file_id, path)
                except MappingError:
                    continue
                all_records[path] = record
                relative = (
                    path[len(candidate.rstrip("/")) + 1 :]
                    if directory
                    else path.rsplit("/", 1)[-1]
                )
                filt = (
                    {}
                    if self.config.get("paths", {}).get(file_id)
                    else file.get("filter", {})
                )
                if filt.get("basename") and path.rsplit("/", 1)[-1] != filt["basename"]:
                    continue
                if "relative_regex" in filt and not re.search(
                    self.values.template(filt["relative_regex"]), relative
                ):
                    continue
                records[path] = record
                emit(
                    self.p4,
                    f"Discovery matched {file_id}: {path}#{record.get('rev', '?')}",
                )
        return sorted(records.values(), key=lambda x: x["depotFile"]), sorted(
            all_records.values(), key=lambda x: x["depotFile"]
        )

    def locate(self, file_id, *, optional=False, allow_absent=False):
        file = self.spec["files"][file_id]
        role = self.spec["sources"][file["source"]].get("role", "current")
        records, _ = self.inventory(file_id)
        if file["kind"] == "tree":
            if not records and not optional:
                raise DiscoveryMiss(
                    f"{file_id}: no eligible live files. Exact paths searched: "
                    + "; ".join(self.attempts[file_id]),
                    role,
                    self.attempts[file_id],
                )
            return records
        if len(records) > 1:
            raise MappingError(
                f"{file_id}: expected one file, found "
                + ", ".join(r["depotFile"] for r in records)
                + ". Set an exact paths override."
            )
        if records:
            return records[0]["depotFile"]
        if allow_absent:
            candidates = self.candidates(file_id)
            if len(candidates) != 1:
                raise MappingError(
                    f"{file_id}: cannot uniquely choose an absent destination: {candidates}"
                )
            return candidates[0]
        if optional:
            return None
        raise DiscoveryMiss(
            f"{file_id}: couldn't find an eligible live file. Exact path(s) searched: "
            + "; ".join(self.attempts[file_id]),
            role,
            self.attempts[file_id],
        )

    def discover(self, role, scope, target, *, optional=False):
        return self.locate(f"{role}.{scope}.{target}", optional=optional)

    def blank_target(self, scope, target):
        return self.locate(f"current.{scope}.{target}", allow_absent=True)

    def paired(self, source_path, from_id, to_id, pairing=None):
        if not pairing:
            return self.locate(to_id, allow_absent=True)
        source, target = self.spec["files"][from_id], self.spec["files"][to_id]
        if pairing.get("method") == "view_relative":
            relative = self.covered(from_id, source_path)
            replacements = {
                str(self.values.eval(a)): str(self.values.eval(b))
                for a, b in pairing.get("replacements", [])
            }
            relative = "/".join(
                replacements.get(part, part) for part in relative.split("/")
            )
            destination = translate_path(self.views[target["source"]], relative)
        elif pairing.get("method") == "tree_relative":
            roots, targets = self.candidates(from_id), self.candidates(to_id)
            matches = [root for root in roots if source_path.startswith(root + "/")]
            if len(matches) != 1 or len(targets) != 1:
                raise MappingError("Ambiguous tree pairing")
            destination = targets[0] + source_path[len(matches[0]) :]
        else:
            raise MappingError("Unknown pairing method")
        self.covered(to_id, destination)
        return destination
