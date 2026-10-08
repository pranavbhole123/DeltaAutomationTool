"""Resolve file roles with editable routes and bounded searches inside Views."""
from __future__ import annotations

import re
import time
from .perforce import MappingError, PerforceSearchLimit, parse_view, translate_path, validate_depot_path
from .catalog import PATH_RULES
from .diagnostics import emit
from .discovery import anchor_matches, match_record, search_queries


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
                for match in anchor_matches(anchor, static):
                    candidate = static[:match.start()] + match.group() + relative
                    if match.group() != anchor:
                        emit(self.p4, f"Anchor variant accepted: configured={anchor}; mapped={match.group()}; template mapping={mapping.depot}")
                    probe = candidate + "/__route_probe__" if directory else candidate
                    if pattern_regex(mapping.depot).fullmatch(probe):
                        candidates.add(candidate)
                        emit(self.p4, f"Route candidate covered by View: {candidate}; mapping={mapping.depot}")
                    else:
                        emit(self.p4, f"Route candidate rejected before querying Perforce: {candidate}; not covered by mapping={mapping.depot}")
        return sorted(candidates)

    def discover(self, role, scope, target, *, optional=False):
        key, directory = f"{role}.{scope}", target in ("bluetooth_folder", "hcf")
        view = self.views[key]
        label = f"{key}.{target} (template {self.config[role][scope + '_template']})"
        emit(self.p4, f"Discovery started: {label}; optional={optional}; strategy=numeric-version anchors + bounded mapped keyword search.")
        budget = self.config.get('discovery', {})
        seconds, max_queries, max_records = budget.get('timeout_seconds', 30), budget.get('max_queries', 24), budget.get('max_records', 2000)
        started = time.monotonic()
        deadline = started + seconds
        explicit = self.override(role, scope, target)
        route_error = ''
        try:
            candidates = ([validate_depot_path(explicit.rstrip("/"))] if explicit
                          else self._route_candidates(scope, target, view, directory))
        except MappingError as exc:
            if not str(exc).startswith("Set "):
                raise
            candidates = []
            route_error = str(exc)
            emit(self.p4, f"Configured route could not be expanded for {label}: {exc}. Trying available model/AP/chipset hints in mapped subtrees.")
        records_by_path = {}
        queries = []
        self.attempts[f"{key}.{target}"] = queries

        def query_files(query, mode):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or len(queries) >= max_queries:
                message = f"{label}: discovery budget exhausted after {time.monotonic() - started:.1f}s / {len(queries)} queries (limits {seconds}s / {max_queries} queries). Last paths: " + '; '.join(queries) + ". Partial matches cannot establish a unique file; narrow the path with an override."
                emit(self.p4, message)
                raise PerforceSearchLimit(message)
            queries.append(query)
            if query not in self.cache:
                emit(self.p4, f"{mode} discovery query for {label}: {query}; remaining={remaining:.1f}s; result cap={max_records}.")
                if hasattr(self.p4, 'bounded_files'):
                    records = self.p4.bounded_files(query, timeout_seconds=remaining, max_records=max_records)
                else:
                    records = self.p4.files(query)
                if len(records) > max_records or time.monotonic() > deadline:
                    raise PerforceSearchLimit(f"{label}: discovery time/result limit exceeded for {query}; partial results were not used.")
                self.cache[query] = records
            else:
                emit(self.p4, f"Discovery cache reused for {label}: {query}")
            emit(self.p4, f"Discovery returned {len(self.cache[query])} live file(s): {query}")
            accepted = []
            for record in self.cache[query]:
                if time.monotonic() >= deadline:
                    raise PerforceSearchLimit(f"{label}: discovery time budget exhausted while filtering {query}; partial results were not used.")
                path = record["depotFile"]
                try:
                    relative_for(view, path)
                except MappingError as exc:
                    emit(self.p4, f"Discovery file rejected: {path}; {exc}")
                    continue
                accepted.append(record)
            return accepted

        for candidate in candidates:
            query = candidate.rstrip("/") + "/..." if directory else candidate
            for record in query_files(query, 'Configured route'):
                records_by_path[record['depotFile']] = record

        if not explicit and (not records_by_path or (target == 'hcf' and not any(path.lower().endswith('.hcf') for path in records_by_path))):
            emit(self.p4, f"Configured routes did not locate eligible {target} files for {label}; starting bounded keyword fallback from relevant included View prefixes.")
            fallback = search_queries(view, scope, target, self.config)
            expected_names = []
            routes = self.path_rules.get(scope, {}).get(target, [])
            for route in ([routes] if isinstance(routes, dict) else routes):
                leaf = route.get('relative', '').rsplit('/', 1)[-1]
                for name, value in self.config.items():
                    if isinstance(value, str):
                        leaf = leaf.replace('{' + name + '}', value)
                expected_names.append(leaf)
            ranked = {}
            for query in fallback:
                if query in queries:
                    continue
                for record in query_files(query, 'Fallback'):
                    if time.monotonic() >= deadline:
                        raise PerforceSearchLimit(f"{label}: discovery time budget exhausted while ranking {query}; partial matches were not used.")
                    path = record['depotFile']
                    match = match_record(path, scope, target, self.config, expected_names)
                    if match is None:
                        emit(self.p4, f"Fallback rejected file-role/model/AP/chipset mismatch for {label}: {path}")
                        continue
                    score, group = match
                    ranked[path] = (score, group, record)
                    emit(self.p4, f"Fallback candidate for {label}: {path}#{record.get('rev', '?')}; evidence score={score}; group={group}.")
            if ranked:
                highest = max(value[0] for value in ranked.values())
                best = [value for value in ranked.values() if value[0] == highest]
                groups = {value[1] for value in best} if directory else {value[2]['depotFile'] for value in best}
                if len(groups) != 1:
                    choices = '; '.join(sorted(value[2]['depotFile'] for value in best))
                    raise MappingError(f"{label}: bounded fallback found ambiguous equally relevant matches: {choices}. Set paths['{key}.{target}'] explicitly; no file was selected.")
                chosen = next(iter(groups))
                records_by_path = {path: value[2] for path, value in ranked.items()
                                   if (value[1] if directory else path) == chosen}
                emit(self.p4, f"Fallback selected {label}: {chosen}; unique strongest file-role/model/AP/chipset evidence, within the effective View.")
            emit(self.p4, f"Fallback completed for {label}: {len(fallback)} planned query(s), {len(ranked)} eligible candidate file(s), elapsed={time.monotonic() - started:.1f}s.")
        if time.monotonic() >= deadline:
            raise PerforceSearchLimit(f"{label}: discovery time budget exhausted before completing lookup; partial matches were not used.")
        for path, record in records_by_path.items():
            emit(self.p4, f"Discovery matched {label}: {path}#{record.get('rev', '?')} ({record.get('type', 'unknown type')})")
        records = list(records_by_path.values())
        if not records:
            routes = self.path_rules.get(scope, {}).get(target, [])
            requested = []
            for route in ([routes] if isinstance(routes, dict) else routes):
                hint = route.get('anchor', '') + route.get('relative', '')
                for name, value in self.config.items():
                    if isinstance(value, str):
                        hint = hint.replace('{' + name + '}', value)
                requested.append(hint)
            message = (f"{label}: couldn't find an eligible live file after configured-route and bounded keyword discovery. Exact path(s) searched: " + ("; ".join(queries) or 'none') +
                       ". Configured hints: " + '; '.join(requested) + ('. Route input error: ' + route_error if route_error else '') +
                       (". No p4 files query was made: no relevant included View prefix was reachable; server-side absence is not established." if not queries else
                        ". No queried live file matched the required role/model/AP/chipset within the effective View; this is not a depot-wide absence claim.") +
                       f" Search limits: {seconds}s, {max_queries} queries, {max_records} records/query. Set paths['{key}.{target}'] to an exact mapped path if the naming keywords differ.")
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

    def directory_root(self, role, scope, target, records):
        """Recover the selected physical folder without another Perforce call."""
        explicit = self.override(role, scope, target)
        if explicit:
            return explicit.rstrip('/')
        view = self.views[f'{role}.{scope}']
        candidates = self._route_candidates(scope, target, view, True)
        covered = {root for root in candidates if all(record['depotFile'].startswith(root + '/') for record in records)}
        if len(covered) == 1:
            return covered.pop()
        matched = [match_record(record['depotFile'], scope, target, self.config) for record in records]
        groups = {value[1] for value in matched if value}
        if len(groups) != 1 or any(value is None for value in matched):
            raise MappingError(f'{role}.{scope}.{target}: cannot identify one folder containing the discovered files; set an exact path override')
        return groups.pop()

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
