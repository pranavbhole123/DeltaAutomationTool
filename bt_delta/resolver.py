"""Resolve checklist targets from explicit, editable template-view routes."""
from __future__ import annotations

import re
from .perforce import MappingError, parse_view, translate_path, validate_depot_path
from .catalog import PATH_RULES


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
        self.specs, self.views, self.cache = {}, {}, {}
        self.path_rules = path_rules or PATH_RULES
        for role in ("current", "reference"):
            for scope in ("system", "vendor"):
                key = f"{role}.{scope}"
                spec = p4.client_spec(config[role][scope + "_template"])
                self.specs[key] = spec
                self.views[key] = parse_view(spec)

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
            if not anchor.startswith("/") or not anchor.endswith("/") or "..." in anchor or "*" in anchor:
                raise MappingError(f"Invalid stable anchor for {scope}.{target}: {anchor}")
            for mapping in view:
                if mapping.modifier == "-":
                    continue
                static = re.split(r"\.\.\.|\*", mapping.depot, maxsplit=1)[0]
                pos = static.find(anchor)
                if pos < 0:
                    continue
                candidate = static[:pos] + anchor + relative
                probe = candidate + "/__route_probe__" if directory else candidate
                if pattern_regex(mapping.depot).fullmatch(probe):
                    candidates.add(candidate)
        return sorted(candidates)

    def discover(self, role, scope, target, *, optional=False):
        key, directory = f"{role}.{scope}", target in ("bluetooth_folder", "hcf")
        view = self.views[key]
        explicit = self.override(role, scope, target)
        candidates = ([validate_depot_path(explicit.rstrip("/"))] if explicit
                      else self._route_candidates(scope, target, view, directory))
        if not candidates:
            raise MappingError(f"{key}.{target}: no configured anchor route exists in this template View; edit path_rules or set an exact path override")
        records_by_path = {}
        for candidate in candidates:
            query = candidate.rstrip("/") + "/..." if directory else candidate
            if query not in self.cache:
                self.cache[query] = self.p4.files(query)
            for record in self.cache[query]:
                path = record["depotFile"]
                try:
                    relative_for(view, path)
                except MappingError:
                    continue
                records_by_path[path] = record
        records = list(records_by_path.values())
        if directory:
            if not records and not optional:
                raise MappingError(f"{key}.{target}: no files found in exact configured route(s): {', '.join(candidates) or 'none'}")
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
