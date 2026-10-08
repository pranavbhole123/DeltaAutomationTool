"""Bounded search hints: mapped roots and file roles, never depot-wide guessing."""
import re


MODEL_TARGETS = {'board_config', 'device_common', 'model_init', 'sec_product', 'bluetooth_header', 'bluetooth_folder'}
VERSION_SUFFIX = r'(?:[._-]?\d+(?:[._-]\d+)*)?'
GLOBS = {
    'board_config': ['*board*config*.mk'],
    'device_common': ['*device*.mk'],
    'model_init': ['init*.rc'],
    'sec_product': ['*product*feature*'],
    'bluetooth_header': ['*.h'],
    'bluetooth_folder': ['...'],
    'root_init': ['init*.rc'],
    'manifest': ['*manifest*.xml', '*vintf*.xml'],
    'hcf_makefile': ['*bluetooth*.mk', '*bt*.mk'],
    'hcf': ['*.hcf'],
    'firmware': ['*.bin', '*.fw'],
}


def anchor_matches(anchor, path):
    """Accept arbitrary numeric platform versions, e.g. EXYNOS8825 or EXYNOS12_3."""
    components = anchor.strip('/').split('/')
    pattern = '/' + '/'.join(re.escape(part) + (VERSION_SUFFIX if part.isalpha() else '') for part in components) + '/'
    return list(re.finditer(pattern, path, re.I))


def _model(parts, config):
    model = config['model'].lower()
    return any(re.search(r'(?:^|[_.-])' + re.escape(model) + r'(?:$|[_.-])', part) for part in parts)


def _ap(config):
    explicit = config.get('ap', '').lower()
    digits = re.fullmatch(r's5e(\d+)', config.get('chipset', '').lower())
    return explicit or ('erd' + digits[1] if digits else '')


def _same_ap(actual, expected):
    """Platform folders may rename erd8825 to universal8825 between releases."""
    if actual == expected:
        return True
    actual_number = re.fullmatch(r'(?:erd|universal)(\d+)', actual)
    expected_number = re.fullmatch(r'(?:erd|universal)(\d+)', expected)
    return bool(actual_number and expected_number and actual_number[1] == expected_number[1])


def _context(path, scope, target, config, *, root=False):
    """Reject other models/APs/chips and unrelated partitions before ranking."""
    path = path.lower()
    parts = path.strip('/').split('/')
    model = config['model'].lower()
    if target in MODEL_TARGETS:
        broad_model_root = root and bool(re.search(r'/exynos' + VERSION_SUFFIX + r'$', path))
        if not _model(parts, config) and not broad_model_root:
            return False
        opposite = model + ('_sssi' if scope == 'vendor' else '_vendor')
        if any(part.startswith(opposite) for part in parts):
            return False
        marker = '/vendor/' if target == 'sec_product' else '/device/'
        return marker in path or (root and not any(other in path for other in ('/device/', '/vendor/')) and
                                  (broad_model_root or any(part.startswith(model + '_') for part in parts)))
    if target == 'root_init':
        return ('rootdir' in path or '/system/core/' in path or
                (root and bool(re.search(r'/essi' + VERSION_SUFFIX + r'/android(?:/|$)', path))))
    if target == 'manifest':
        ap = _ap(config)
        actual = re.search(r'/device/samsung/([^/]+)', path)
        if actual and ap and not _same_ap(actual[1], ap):
            return False
        if root:
            return '/device/samsung/' in path or bool(re.search(r'/exynos' + VERSION_SUFFIX + r'/android(?:/|$)', path))
        return bool(ap and actual and _same_ap(actual[1], ap))
    if target in ('hcf', 'hcf_makefile'):
        chip = config.get('chipset', '').lower()
        other = re.findall(r'(?:^|/)(s5e\d+)(?=/|$)', path)
        if other and chip not in other:
            return False
        if root:
            return ('bluetooth' in path or '/bt/' in path or
                    # The release tree may be Cinnamon, Common or another name.
                    # Restrict broader roots to vendor hardware, not frameworks/apps.
                    bool(re.search(r'/vendor/[^/]+/vendor(?:/samsung(?:/hardware(?:/vendor)?)?)?$', path)))
        if not (chip in parts and ('bluetooth' in path or '/bt/' in path)):
            return False
        return target != 'hcf' or any(part.startswith(model) for part in parts)
    if target == 'firmware':
        family = config.get('firmware', '').lower()
        # Known firmware families from the supplied checklist distinguish chips.
        families = {'rice_s620', 'quartz_s621p', 'papaya_s620', 'rose_s621p'}
        if any(part in families and part != family for part in parts):
            return False
        if root:
            return ('firmware' in path or any(term in path for term in ('mx140', 'scsc', 'wlbt')) or
                    bool(re.search(r'/exynos' + VERSION_SUFFIX + r'/android(?:/|$)', path)))
        name = parts[-1]
        identity = any(term and term in path for term in ('mx140', 'wlbt', 'bluetooth', family, config.get('chipset', '').lower()))
        return bool(identity and ('firmware' in path or 'firmware' in name))
    return False


def search_queries(view, scope, target, config):
    """Search each relevant mapped subtree; narrower mappings run first."""
    roots = {}
    for mapping in view:
        if mapping.modifier == '-':
            continue
        static = re.split(r'\.\.\.|\*', mapping.depot, maxsplit=1)[0]
        wildcard = static != mapping.depot
        root = static.rstrip('/') if wildcard and static.endswith('/') else static.rsplit('/', 1)[0]
        if not _context(root, scope, target, config, root=True):
            continue
        # Never climb above this included mapping's fixed prefix.
        query_root = static.rstrip('/') if wildcard and static.endswith('/') else None
        queries = [mapping.depot] if query_root is None else []
        if query_root is not None:
            for glob in GLOBS.get(target, []):
                queries.append(query_root + '/' + glob)
                if glob != '...':
                    queries.append(query_root + '/.../' + glob)
        for query in queries:
            roots[query] = max(roots.get(query, 0), len(root.split('/')))
    return [query for query, _ in sorted(roots.items(), key=lambda item: (-item[1], item[0]))]


def match_record(path, scope, target, config, expected_names=()):
    """Return (evidence score, directory group) for a plausible file role."""
    lower = path.lower()
    parts = lower.strip('/').split('/')
    name = parts[-1]
    if not _context(path, scope, target, config):
        return None
    role = {
        'board_config': 'board' in name and 'config' in name and name.endswith('.mk'),
        'device_common': 'device' in name and name.endswith('.mk'),
        'model_init': name.startswith('init') and name.endswith('.rc'),
        'sec_product': 'product' in name and 'feature' in name,
        'bluetooth_header': name.endswith('.h') and any(term in name for term in ('bdroid', 'bluetooth', 'bt')) and any(term in name for term in ('cfg', 'config', 'build')),
        'bluetooth_folder': any(re.fullmatch(r'(?:bluetooth|bt)(?:\d+|[_.-].*)?', part) for part in parts[:-1]),
        'root_init': name.startswith('init') and name.endswith('.rc'),
        'manifest': name.endswith('.xml') and any(term in name for term in ('manifest', 'vintf')),
        'hcf_makefile': name.endswith('.mk') and any(term in name for term in ('bluetooth', 'bt')),
        'hcf': name.endswith('.hcf'),
        'firmware': name.endswith(('.bin', '.fw')),
    }.get(target, False)
    if not role:
        return None
    score = 100
    if name in {leaf.lower() for leaf in expected_names}:
        score += 40
    if config.get('common_device', '').lower() in parts:
        score += 30
    if target in ('board_config', 'device_common') and 'common' in name:
        score += 20
    if config.get('firmware', '').lower() and config['firmware'].lower() in parts:
        score += 30
    group = path.rsplit('/', 1)[0]
    if target == 'bluetooth_folder':
        index = next(i for i, part in enumerate(parts) if re.fullmatch(r'(?:bluetooth|bt)(?:\d+|[_.-].*)?', part))
        group = '//' + '/'.join(path[2:].split('/')[:index + 1])
        # Files in the same folder constitute one candidate, not competing files.
        score = 100
    if target == 'hcf':
        variant = config.get('hcf_variant', '').lower()
        indexes = [i for i, part in enumerate(parts[:-1]) if part.startswith(config['model'].lower())]
        index = indexes[-1]
        group = '//' + '/'.join(path[2:].split('/')[:index + 1])
        score = 140 if variant and parts[index] == variant else 100
    return score, group
