"""Explain tab-4 suggestions and developer edits without changing comparison rules."""


def signature(unit):
    return unit.get('identity'), unit.get('value')


def origins(unit):
    parts = []
    if unit.get('changelists'):
        parts.append('CL ' + ', '.join(unit['changelists']))
    if unit.get('line'):
        parts.append('line ' + str(unit['line']))
    if unit.get('sources'):
        parts.append('rules ' + ', '.join(dict.fromkeys(source['rule'] for source in unit['sources'])))
    return '; '.join(parts)


def explain_file(item):
    """Add reasons while keeping existing missing/extra/matched lists intact."""
    findings = item['findings'] = []
    if item['errors']:
        for error in item['errors']:
            findings.append({'status': 'unreadable', 'text': item['filename'], 'reason': error,
                             'suggested': None, 'actual': []})
        return
    for wanted in item['missing']:
        actual = [unit for unit in item['combined_additions']
                  if wanted.get('identity') and unit['identity'] == wanted['identity'] and unit['value'] != wanted['value']]
        status = 'different_value' if actual else 'missing'
        if actual:
            reason = 'The selected changelists introduce a different value for the same setting/key and context. The suggested value is missing and the developer value is also reported as extra.'
        elif not item['in_changelists']:
            reason = 'This file does not appear in any selected changelist, so none of its preview suggestions were introduced by those changes.'
        elif any(signature(wanted) == signature(unit) for unit in item['developer_removals']):
            actual = [unit for unit in item['developer_removals'] if signature(wanted) == signature(unit)]
            reason = 'The selected changelists remove this suggested content instead of introducing it.'
        else:
            cancelled = [event for event in item['cancelled_edits']
                         if event['earlier']['operation'] == 'add' and signature(event['earlier']) == signature(wanted)]
            unchanged = [(delta['changelist'], unit) for delta in item['developer_changes']
                         for unit in delta.get('unchanged_suggestions', []) if signature(unit) == signature(wanted)]
            if cancelled:
                reason = 'An earlier selected changelist added this content, but a later selected changelist removed it; it does not survive in the combined additions.'
                actual = [unit for event in cancelled for unit in (event['earlier'], event['later'])]
            elif unchanged:
                reason = 'This content was already present and unchanged in CL ' + ', '.join(dict.fromkeys(number for number, unit in unchanged)) + '. It was not introduced by that edit. This is a comparison of selected edits, not a claim that the current file lacks the content.'
                actual = [dict(unit, operation='unchanged', changelists=[number]) for number, unit in unchanged]
            else:
                reason = 'No surviving addition from the selected changelists matches this preview statement/value in the same file and context.'
        findings.append({'status': status, 'text': wanted['text'], 'reason': reason,
                         'suggested': wanted, 'actual': actual})
    for unit in item['matched']:
        findings.append({'status': 'matched', 'text': unit['text'],
                         'reason': 'A surviving addition from ' + origins(unit) + ' matches the preview statement/value in the same file and context.',
                         'suggested': unit, 'actual': [unit]})
    for unit in item['extra']:
        if unit.get('operation') == 'action':
            reason = 'This file operation is not represented by an add/edit suggestion in the tab-4 preview.'
        elif unit.get('operation') == 'remove':
            reason = 'The developer removed this content; the removal is not covered by a matching preview replacement.'
        elif item['extra_file']:
            reason = 'This file has no tab-4 content suggestion. Its developer additions are outside the generated preview baseline. Inspect related skipped/manual rule results before deciding whether to extend a rule.'
        else:
            reason = 'The developer introduced this statement/value, but no tab-4 suggestion matches it in this file and context.'
        findings.append({'status': 'extra', 'text': unit['text'], 'reason': reason,
                         'suggested': None, 'actual': [unit]})
    if not findings:
        findings.append({'status': 'no_content_difference', 'text': item['filename'],
                         'reason': 'No comparable additions/removals differ from the preview baseline. Original file operations and raw diffs are shown above.',
                         'suggested': None, 'actual': []})


def render_comparison(report):
    counts = report['counts']
    lines = ['Tab 4 Empty-file preview versus developer changelists: ' + ', '.join(change['number'] for change in report['changelists']),
             report['comparison_basis'], 'Comparison run: ' + report.get('run_id', 'saved report'),
             'Model: ' + report['plan']['config']['model'],
             'Read-only. Suggestions below are the content to add to blank files in tab 4, including files needing no edit in the regular plan.',
             'Only edits introduced by selected changelists count as matches. Existing unchanged content is identified explicitly.',
             'Missing items describe coverage by these changelists; they do not establish that the current template is incomplete.',
             'Submitted CLs are combined in file-revision order; pending CLs follow in input order. Unselected intermediate edits are excluded.',
             'Formatting normalization retains Make/init context and XML/JSON key paths. Binary comparisons use byte hashes.',
             'INCOMPLETE: inspect file errors and blocked preview-source rules.' if report['incomplete'] else 'Comparison completed.',
             f"Files: {counts['blank_plan_files']} suggested by tool; {counts['developer_files']} in compared CLs; {counts['extra_files']} extra; {counts['missing_from_changelists']} with uncovered suggestions; {counts['unreadable_files']} with errors.",
             f"Items: {counts.get('suggested_items', 0)} suggested; {counts['matched_items']} matched; {counts.get('missing_items', 0)} missing from selected edits; {counts.get('extra_items', 0)} extra; {counts.get('value_differences', 0)} different values."]
    for role in ('current', 'reference'):
        config = report['plan']['config'][role]
        lines.append(f"{role.capitalize()} templates: system={config['system_template']}; vendor={config['vendor_template']}")
    lines.extend(['', 'SELECTED CHANGELISTS'])
    for change in report['changelists']:
        lines.extend([f"CL {change['number']}: {change['status']}; source={change['content_source']}; developer={change['user']}@{change['client']}; {len(change['files'])} described file(s).",
                      'Description: ' + change['description'].strip()])
    lines.extend('NOTE: ' + warning for warning in report['warnings'])
    lines.extend(['', 'RESULT INDEX (a file can appear in more than one category)'])
    for label, selected in (
        ('EXTRA FILES', lambda item: item['extra_file']),
        ('EXTRA CHANGES', lambda item: bool(item['extra']) and not item['extra_file']),
        ('MISSING FROM CHANGELISTS', lambda item: bool(item['missing'])),
        ('MATCHED', lambda item: bool(item['matched'])),
        ('UNREADABLE', lambda item: bool(item['errors']))):
        matches = [item['path'] for item in report['files'] if selected(item)]
        lines.append(label + ': ' + str(len(matches)) + ' file(s)')
        lines.extend('  ' + path for path in matches)
        if not matches:
            lines.append('  None.')
    lines.extend(['', '=' * 80, 'TAB 4 EMPTY-FILE PREVIEW BASELINE AND FILE-BY-FILE COMPARISON'])
    if not report['plan']['previews']:
        lines.append('No preview entries were generated. Inspect the rule results below.')

    def units(title, entries, prefix):
        lines.append(title + f' ({len(entries)} item(s))')
        if not entries:
            lines.append('  None.')
        for unit in entries:
            source = origins(unit)
            lines.append(prefix + ' ' + unit['text'] + (' [' + source + ']' if source else ''))

    for index, item in enumerate(report['files'], 1):
        lines.extend(['', '=' * 80, f"FILE {index}/{len(report['files'])}: {item['path']}",
                      'Filename: ' + item['filename'],
                      'Preview rules: ' + (', '.join(item['rules']) or 'none'),
                      'Developer CLs: ' + (', '.join(item['changelists']) or 'file absent from selected changelists'),
                      'Template locations:'])
        lines.extend('  ' + path for path in item['template_paths'])
        if item['outside_templates']:
            lines.append('  Outside configured current templates/CSC root; included for completeness.')
        if item['extra_file']:
            lines.append('FILE RESULT: extra file — this path has no content suggestion in tab 4.')
        lines.extend(['', '1. WHAT THE TOOL SUGGESTS ADDING'])
        if not item['preview_sources']:
            lines.append('No content preview exists for this file. The tool makes no content suggestion here.')
        for preview in item['preview_sources']:
            lines.extend([f"Rule: {preview['rule']} — {preview['title']} ({preview['source']})",
                          'Target: ' + preview['target_path'], 'Reference: ' + (preview.get('reference_path') or 'no reference path recorded'),
                          'Reason / scope: ' + (preview.get('note') or 'Selected reference content for an empty destination.'),
                          'Exact tab-4 preview content:', preview['content']])
        units('Comparable suggestions', item['suggested'], 'SUGGEST ADD:')
        for unit in item['suggested']:
            for source in unit.get('sources', []):
                reference = source.get('reference_path') or 'not recorded'
                if source.get('reference_revision') is not None:
                    reference += '#' + str(source['reference_revision'])
                lines.append(f"  Source for {unit['text']}: rule={source['rule']}; worksheet={source['worksheet']}; reference={reference}; preview line={source.get('preview_line') or 'n/a'}")
        lines.extend(['', '2. WHAT EACH CHANGELIST ACTUALLY CHANGED'])
        if not item['developer_changes']:
            lines.append('None: this file is absent from all selected changelists.')
        for delta in item['developer_changes']:
            lines.extend(['', f"CL {delta['changelist']}: action={delta['action']}; source={delta['content_source']}; type={delta.get('comparison_type', delta['type'])}",
                          'Before: ' + delta.get('before_source', 'unavailable — see error'),
                          'After: ' + delta.get('after_source', 'unavailable — see error')])
            if 'before_sha256' in delta:
                lines.append(f"Before bytes/hash: {delta['before_bytes']} / {delta['before_sha256']}; after bytes/hash: {delta['after_bytes']} / {delta['after_sha256']}")
            if delta['error']:
                lines.append('ERROR: ' + delta['error'])
            units('Added content / new values (line numbers refer to after)', delta['additions'], '+ ADD:')
            units('Removed content / old values (line numbers refer to before)', delta['removals'], '- REMOVE:')
            units('Suggested content already present and unchanged in this edit', delta.get('unchanged_suggestions', []), 'UNCHANGED:')
            lines.extend(['Original before/after diff:', delta['diff'] or '(No text diff, or content could not be read; see metadata/errors above.)'])
        lines.extend(['', '3. COMBINED EFFECT OF SELECTED CHANGELISTS'])
        units('Surviving additions / new values', item['combined_additions'], '+ NET ADD:')
        units('Surviving removals / old values', item['developer_removals'], '- NET REMOVE:')
        lines.append('Edits cancelled by later selected changelists:')
        if not item['cancelled_edits']:
            lines.append('  None.')
        for event in item['cancelled_edits']:
            earlier, later = event['earlier'], event['later']
            lines.append(f"  {earlier['text']}: {earlier['operation'].upper()} [{origins(earlier)}] cancelled by {later['operation'].upper()} [{origins(later)}].")
        lines.extend(['', '4. SUGGESTION VERSUS DEVELOPER RESULT, WITH REASONS'])
        for finding in item['findings']:
            lines.append(f"[{finding['status'].upper()}] {finding['text']}")
            if finding['suggested']:
                lines.append('  Tool suggests: ' + finding['suggested']['text'])
            if finding['actual']:
                for actual in finding['actual']:
                    lines.append('  Developer ' + actual.get('operation', 'change') + ': ' + actual['text'] + ' [' + origins(actual) + ']')
            else:
                lines.append('  Matching developer addition: none, or unavailable — see reason.')
            lines.append('  Reason: ' + finding['reason'])
        if item['errors']:
            lines.append('Classification is incomplete; successful reads above are evidence only, not complete match/missing/extra findings.')
        units('MATCHED suggestions', item['matched'], 'MATCH:')
        units('MISSING FROM CHANGELISTS suggestions', item['missing'], 'MISSING:')
        units('EXTRA developer changes', item['extra'], 'EXTRA:')
    lines.extend(['', '=' * 80, 'PREVIEW SOURCE RULE RESULTS — INCLUDING SKIPPED, REVIEW AND BLOCKED REASONS'])
    for check in report['plan']['checks']:
        lines.extend([f"[{check['status'].upper()}] {check['rule']}: {check['message']}", *check['paths']])
    return '\n'.join(lines).rstrip() + '\n'
