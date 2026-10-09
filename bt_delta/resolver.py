"""Resolve checklist targets from explicit, editable template-view routes."""
from __future__ import annotations

import re
from .perforce import MappingError, parse_view, translate_path, validate_depot_path
from .catalog import PATH_RULES
from .diagnostics import emit


class DiscoveryMiss(MappingError):
    """A configured lookup cannot locate a file; not a server/read failure."""

    def __init__(self, message, role, paths=()):
        super().__init__(message)
        self.role, self.paths = role, list(paths)


def pattern_regex(pattern: str):
    return re.compile("^" + "".join("(.*)" if p == "..." else "([^/]*)" if p == "*" else re.escape(p)
                                   for p in re.split(r"(\.\.\.|\*)", pattern)) + "$")


def relative_for(view, depot: str) -> str:
    candidates = set()
    for mapping in view:
        if mapping.modifier == "-":
            continue
        match = pattern_regex(mapping.depot).fullmatch(depot)
        if match:
            groups = iter(match.groups())
            relative = re.sub(r"\.\.\.|\*", lambda _: next(groups), mapping.relative_pattern)
            try:
                if translate_path(view, relative) == depot:
                    candidates.add(relative)
            except MappingError:
                continue
    if len(candidates) != 1:
        raise MappingError(f"Path is excluded, ambiguous or outside template: {depot}")
    return candidates.pop()


class Resolver:
    def __init__(self, p4, config, path_rules=None):
        self.p4, self.config = p4, config
        self.specs, self.views, self.cache, self.attempts = {}, {}, {}, {}
        self.path_rules = path_rules or PATH_RULES
        for role in ("current", "reference"):
            for scope in ("system", "vendor"):
                key = f"{role}.{scope}"
                spec = p4.client_spec(config[role][scope + "_template"])
                self.specs[key] = spec
                self.views[key] = parse_view(spec)
                emit(p4, f"Template loaded: {key} = {config[role][scope + '_template']}; {len(self.views[key])} View mappings.")

    def override(self, role, scope, target):
        return self.config["paths"].get(f"{role}.{scope}.{target}")

    def _expand(self, value):
        values = {k: str(self.config.get(k) or "") for k in
                  ("model", "common_device", "ap", "chipset", "firmware", "hcf_variant")}
        required = re.findall(r"{(\w+)}", value)
        missing = [name for name in required if not values.get(name)]
        if missing:
            raise MappingError(f"Set {', '.join(missing)} to resolve {value}")
        try:
            result = value.format_map(values)
        except KeyError as exc:
            raise MappingError(f"Unknown path variable {exc} in {value}") from exc
        if "{" in result or "}" in result or not result.strip("/"):
            raise MappingError(f"Unresolved or empty path route: {value}")
        return result.strip("/")

    def _route_candidates(self, scope, target, view, directory):
        routes = self.path_rules.get(scope, {}).get(target, [])
        if isinstance(routes, dict):
            routes = [routes]
        if not routes:
            raise MappingError(f"No path_rules.{scope}.{target} route; add it to checklist/slsi.json")
        candidates = set()
        for route in routes:
            anchor = route.get("anchor", "")
            relative = self._expand(route.get("relative", ""))
            emit(self.p4, f"Route {scope}.{target}: anchor={anchor}; relative={relative}")
            if not anchor.startswith("/") or not anchor.endswith("/") or "..." in anchor or "*" in anchor:
                raise MappingError(f"Invalid stable anchor for {scope}.{target}: {anchor}")
            for mapping in view:
                if mapping.modifier == "-":
                    continue
                static = re.split(r"\.\.\.|\*", mapping.depot, maxsplit=1)[0]
                # Only EXYNOS gains an optional numeric suffix. Preserve the
                # actual mapped spelling; never send this regex to Perforce.
                anchor_pattern = re.escape(anchor).replace("/EXYNOS/", r"/EXYNOS[0-9]*/")
                match = re.search(anchor_pattern, static)
                if not match:
                    continue
                candidate = static[:match.end()] + relative
                probe = candidate + "/__route_probe__" if directory else candidate
                if pattern_regex(mapping.depot).fullmatch(probe):
                    candidates.add(candidate)
                    emit(self.p4, f"Route candidate covered by View: {candidate}; mapping={mapping.depot}")
                else:
                    emit(self.p4, f"Route candidate rejected before querying Perforce: {candidate}; not covered by mapping={mapping.depot}")
            # A View may end above the shared hardware suffix. Translate its
            # build path through the View instead of assuming Common/Cinnamon.
            if target in ("hcf", "hcf_makefile") and anchor == "/vendor/samsung/hardware/vendor/":
                try:
                    candidate = translate_path(view, "android" + anchor + relative)
                    relative_for(view, candidate + "/__route_probe__" if directory else candidate)
                except MappingError:
                    pass
                else:
                    candidates.add(candidate)
                    emit(self.p4, f"Hardware route from template View: {candidate}")
        return sorted(candidates)

    def discover(self, role, scope, target, *, optional=False):
        key, directory = f"{role}.{scope}", target in ("bluetooth_folder", "hcf")
        view = self.views[key]
        label = f"{key}.{target} (template {self.config[role][scope + '_template']})"
        emit(self.p4, f"Discovery started: {label}; optional={optional}.")
        explicit = self.override(role, scope, target)
        try:
            candidates = ([validate_depot_path(explicit.rstrip("/"))] if explicit
                          else self._route_candidates(scope, target, view, directory))
        except MappingError as exc:
            if not str(exc).startswith("Set "):
                raise
            message = f"{label}: couldn't construct a search path: {exc}. No Perforce query was made. Resolve this input or provide an exact path override."
            emit(self.p4, message)
            raise DiscoveryMiss(message, role) from exc
        if not candidates:
            routes = self.path_rules.get(scope, {}).get(target, [])
            routes = [routes] if isinstance(routes, dict) else routes
            requested = [route['anchor'] + self._expand(route['relative']) for route in routes]
            message = (f"{label}: couldn't find a mapped search path; no configured anchor route exists in this template View. "
                       "No p4 files query was made, so server-side absence is not established. "
                       "Attempted route(s), relative to a matching depot prefix: " + "; ".join(requested) +
                       ". Check the resolved AP/chipset, template View, or set paths['" + key + "." + target + "']. "
                       "Included View paths: " + "; ".join(m.depot for m in view if m.modifier != '-'))
            emit(self.p4, message)
            raise DiscoveryMiss(message, role, requested)
        records_by_path = {}
        queries = []
        self.attempts[f"{key}.{target}"] = queries
        for candidate in candidates:
            query = candidate.rstrip("/") + "/..." if directory else candidate
            queries.append(query)
            if query not in self.cache:
                emit(self.p4, f"Discovery query for {label}: {query}")
                self.cache[query] = self.p4.files(query)
            else:
                emit(self.p4, f"Discovery cache reused for {label}: {query}")
            emit(self.p4, f"Discovery returned {len(self.cache[query])} live file(s): {query}")
            for record in self.cache[query]:
                path = record["depotFile"]
                if target == "hcf" and not explicit:
                    # Search just the literal chipset directory; match model
                    # folder names locally (e.g. m34x -> m34xnsxx).
                    suffix = path.removeprefix(candidate.rstrip("/") + "/")
                    folder, separator, _ = suffix.partition("/")
                    model = re.escape(self.config["model"])
                    if not separator or not re.fullmatch(model + r"[a-zA-Z0-9_-]*", folder):
                        emit(self.p4, f"HCF file ignored: {path}; folder does not match model {self.config['model']}.")
                        continue
                try:
                    relative_for(view, path)
                except MappingError as exc:
                    emit(self.p4, f"Discovery file rejected: {path}; {exc}")
                    continue
                records_by_path[path] = record
                emit(self.p4, f"Discovery matched {label}: {path}#{record.get('rev', '?')} ({record.get('type', 'unknown type')})")
        records = list(records_by_path.values())
        if not records:
            message = (f"{label}: couldn't find an eligible live file. Exact path(s) searched: " + "; ".join(queries) +
                       ". No returned live file was accepted by the effective template View. "
                       "Check spelling, filename, head deletion, and View exclusions; the tool does not guess another branch.")
            emit(self.p4, message)
            if not optional:
                raise DiscoveryMiss(message, role, queries)
        if directory:
            return sorted(records, key=lambda r: r["depotFile"])
        if not records and optional:
            return None
        if len(records) != 1:
            choices = ", ".join(r["depotFile"] for r in records) or "none"
            raise MappingError(f"{key}.{target}: expected one file, found {choices}. Edit path_rules or set paths['{key}.{target}'] explicitly.")
        return records[0]["depotFile"]

    def counterpart(self, source, scope):
        relative = relative_for(self.views[f"reference.{scope}"], source)
        return translate_path(self.views[f"current.{scope}"], relative)

    def blank_target(self, scope, target):
        """Resolve a file destination from View routes without reading current files."""
        existing = self.discover("current", scope, target, optional=True)
        if existing:
            return existing
        view = self.views[f"current.{scope}"]
        explicit = self.override("current", scope, target)
        candidates = ([validate_depot_path(explicit)] if explicit else
                      self._route_candidates(scope, target, view, False))
        mapped = []
        for path in candidates:
            try:
                relative_for(view, path)
                mapped.append(path)
            except MappingError:
                continue
        if len(mapped) > 1 and not explicit:
            reference = self.discover("reference", scope, target, optional=True)
            if reference:
                translated = self.counterpart(reference, scope)
                if translated in mapped:
                    return translated
        if len(mapped) != 1:
            raise MappingError(f"current.{scope}.{target}: expected one blank-file destination, found "
                               f"{', '.join(mapped) or 'none'}; set an exact path override")
        return mapped[0]
