#!/usr/bin/env python3
"""Offline shapes/invariants only. No network, storage writes or market-truth claims."""
from copy import deepcopy
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
import importlib.util
import json
from pathlib import Path
import argparse
import hashlib

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parent
BASE = ROOT / 'baseline' if (ROOT / 'baseline').is_dir() else ROOT.parent / 'stockdb-market-v3'
spec = importlib.util.spec_from_file_location('stockdb_frozen_offline_validator', BASE / 'validate_contract.py')
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)
SCHEMA = json.loads((ROOT / 'contract.schema.json').read_text())
require = core.require
digest = core.digest
FIELDS = ('open', 'high', 'low', 'close')


def shape(payload, name):
    Draft202012Validator({**SCHEMA, '$ref': '#/$defs/' + name},
                         format_checker=core.FORMAT_CHECKER).validate(payload)


def factor_request(payload):
    shape(payload, 'FactorRequest')
    start, end = core.date8(payload['start']), core.date8(payload['end'])
    require(start <= end, 'factor start exceeds end')
    core.date8(payload['adjustment_anchor'])
    for key in payload.get('codes', [payload.get('code')]):
        exchange, asset, namespace, _ = key.split('/')
        require(exchange in {'XSHG', 'XSHE'} and asset in {'equity', 'etf'} and namespace == 'exchange',
                'unsupported factor asset/namespace')


def research_metadata(payload, *, factor=False):
    value = payload.get('extensions', {}).get('research_v1')
    shape(value, 'ResearchMetadata')
    require(set(value['series_catalog']) == set(payload['query']['codes']), 'series catalog scope differs')
    adjusted = not factor and payload['query']['fq'] != 'none'
    require(bool(value['raw_snapshot_id']) == adjusted, 'raw snapshot reference inappropriate')
    if adjusted:
        require(value['raw_snapshot_id'] != payload['snapshot_id'], 'raw and adjusted snapshot alias')
    for key, rows in payload['data'].items():
        item = value['series_catalog'][key]
        if not rows:
            continue
        require(item is not None and item['instrument_id'] == key, 'missing series identity')
        require(all(item[k] for k in ('series_id', 'provider', 'history_start', 'history_end',
                                      'calendar_exchange', 'calendar_id', 'origin_reference')),
                'usable series lacks provenance, calendar or historical bounds')
        require(item['history_start'] <= item['history_end'], 'invalid declared history bounds')
        require(all(item['history_start'] <= row['date'] <= item['history_end'] for row in rows),
                'returned rows outside declared history')
        if item['volume_multiplier_to_wire'] is not None:
            require(Decimal(item['volume_multiplier_to_wire']) > 0, 'invalid volume conversion')
    return value


def factors(payload):
    shape(payload, 'FactorResponse')
    core.validate_header(payload)
    query, data, meta, catalog = (payload[k] for k in ('query', 'data', 'meta', 'source_catalog'))
    args = {'contract_version': 'stockdb-market-v3', **{k: v for k, v in query.items() if v is not None}}
    factor_request(args)
    require(payload['request_hash'] == digest(query) and payload['data_hash'] == digest(data), 'factor hash mismatch')
    require(set(data) == set(meta) == set(query['codes']), 'factor data/meta scope differs')
    require(all(e['instrument_id'] is None or e['instrument_id'] in data for e in payload['errors']),
            'factor error references unrequested security')
    start, end = core.date8(query['start']).isoformat(), core.date8(query['end']).isoformat()
    anchor = core.date8(query['adjustment_anchor']).isoformat()
    all_complete, usable = True, False
    for key, rows in data.items():
        item = meta[key]
        core.instrument(item['instrument'], catalog)
        require(item['instrument']['instrument_id'] == key, 'factor identity differs')
        dates = [row['date'] for row in rows]
        require(dates == sorted(set(dates)) and all(start <= day <= end for day in dates), 'factor date range/order invalid')
        for row in rows:
            require(row['source_id'] in catalog, 'factor source missing')
            require(Decimal(row['multiplier']) > 0, 'factor multiplier must be positive')
            if row['date'] == anchor:
                require(Decimal(row['multiplier']) == 1 and Decimal(row['offset']) == 0, 'anchor is not identity')
        if rows or item['coverage']['state'] == 'complete':
            adjustment = item['adjustment']
            require(adjustment is not None and adjustment == {
                'mode': query['fq'], 'anchor_date': query['adjustment_anchor'],
                'factor_snapshot_id': payload['snapshot_id'], 'algorithm': 'affine-v1'},
                'factor mode/anchor/snapshot binding differs')
            derivation = item['derivation']
            require(derivation is not None and derivation['source_id'] in catalog, 'factor derivation missing')
        coverage = item['coverage']
        expected, available, gaps = (coverage[k] for k in ('expected_rows', 'available_rows', 'gaps'))
        require(coverage['returned_rows'] == len(rows), 'factor returned row count differs')
        require(available is None or available >= len(rows), 'factor available count too small')
        if expected is not None and available is not None:
            require(expected == available + len(gaps), 'factor expected count differs from rows plus gaps')
        if not item['truncated'] and available is not None:
            require(available == len(rows), 'factor unreported truncation')
        require(item['truncation_reason'] != 'caller_limit', 'factor tool has no caller limit')
        gap_dates = [gap['date'] for gap in gaps]
        require(len(gap_dates) == len(set(gap_dates)) and not set(dates) & set(gap_dates), 'factor duplicate/overlapping gaps')
        for gap in gaps:
            require(start <= gap['date'] <= end and gap['time'] is None, 'factor gap range invalid')
            require(all(e['source_id'] in catalog for e in gap['evidence']), 'factor gap evidence source missing')
        has_error = any(e['instrument_id'] in (None, key) for e in payload['errors'])
        if coverage['state'] == 'complete':
            require(expected is not None and available is not None and not item['truncated'] and not has_error,
                    'factor falsely complete')
            require(all(g['reason'] in core.LEGAL_GAPS and g['evidence'] for g in gaps), 'unexplained factor gap')
            require(item['instrument']['identity_verified'], 'complete factor identity unverified')
            require(item['status'] == ('ok' if rows else 'empty'), 'factor complete status mismatch')
        else:
            require(item['status'] == ('partial' if rows else 'error'), 'factor incomplete status mismatch')
            require(item['truncated'] or has_error, 'factor incomplete lacks error')
        all_complete &= coverage['state'] == 'complete'
        usable |= bool(rows) or (coverage['state'] == 'complete' and item['status'] == 'empty')
    require(payload['truncated'] == any(m['truncated'] for m in meta.values()), 'factor truncation flags differ')
    status = 'complete' if all_complete and not payload['errors'] and not payload['truncated'] else ('partial' if usable else 'failed')
    require(payload['status'] == status, 'factor overall status differs')
    research_metadata(payload, factor=True)


def validate(payload, kind):
    if kind == 'FactorRequest':
        factor_request(payload)
    elif kind == 'FactorResponse':
        factors(payload)
    elif kind == 'FactorToolResponse':
        shape(payload, kind)
        validate(payload, 'ToolFailureResponse' if payload.get('query') is None else 'FactorResponse')
    elif kind in {'ResearchMetadata', 'ResearchStatusMetadata'}:
        shape(payload, kind)
    else:
        core.validate(payload, kind)
        if kind == 'StatusResponse':
            shape(payload.get('extensions', {}).get('research_v1'), 'ResearchStatusMetadata')
        if kind == 'KlineResponse' and payload['query'] is not None:
            research_metadata(payload)
            for key, rows in payload['data'].items():
                if payload['meta'][key]['instrument']['asset_type'] in {'index', 'board'}:
                    require(payload['query']['fq'] == 'none' and payload['meta'][key]['units']['price'] == 'points',
                            'index/board adjustment or price units invalid')
                    require(all(Decimal(row[f]) > 0 for row in rows for f in FIELDS if f in row),
                            'index/board level not positive')


def reconstruct(raw, factor, adjusted):
    """Internal consistency only: independent economic/market validation remains mandatory."""
    validate(raw, 'KlineResponse')
    validate(factor, 'FactorResponse')
    validate(adjusted, 'KlineResponse')
    require(all(p['status'] == 'complete' for p in (raw, factor, adjusted)), 'reconstruction requires complete examples')
    require(raw['query']['fq'] == 'none', 'raw input is adjusted')
    require(adjusted['extensions']['research_v1']['raw_snapshot_id'] == raw['snapshot_id'], 'wrong raw snapshot')
    require(set(raw['data']) == set(factor['data']) == set(adjusted['data']), 'reconstruction scope differs')
    require(adjusted['query']['factor_snapshot_id'] == factor['snapshot_id'], 'wrong factor snapshot')
    for name in ('start', 'end', 'fq', 'adjustment_anchor'):
        require(adjusted['query'][name] == factor['query'][name], 'adjustment query differs from factor scope')
    for key, bars in raw['data'].items():
        result = adjusted['data'][key]
        coefficients = factor['data'][key]
        require([r['date'] for r in bars] == [r['date'] for r in result] == [r['date'] for r in coefficients], 'factor/raw dates differ')
        raw_series = raw['extensions']['research_v1']['series_catalog'][key]
        adjusted_series = adjusted['extensions']['research_v1']['series_catalog'][key]
        require(all(raw_series[k] == adjusted_series[k] for k in ('series_id', 'provider', 'methodology_id', 'methodology_version')),
                'raw and adjusted series/provider/method differ')
        quantum = Decimal(1).scaleb(-adjusted['meta'][key]['units']['price_decimals'])
        for original, coeff, actual in zip(bars, coefficients, result, strict=True):
            for field in FIELDS:
                value = (Decimal(original[field]) * Decimal(coeff['multiplier']) + Decimal(coeff['offset'])).quantize(quantum, rounding=ROUND_HALF_UP)
                require(value == Decimal(actual[field]), 'adjusted price does not reproduce')
            require(all(original[f] == actual[f] for f in ('volume', 'amount')), 'volume/amount were adjusted')


def self_test():
    Draft202012Validator.check_schema(SCHEMA)
    require(all(SCHEMA['$defs'].get(k) == v for k, v in core.SCHEMA['$defs'].items()), 'frozen baseline definition modified')
    base_positive, base_negative = core.self_check()
    examples = json.loads((ROOT / 'examples.json').read_text())
    count = 0
    for group in ('requests', 'responses'):
        for example in examples[group]:
            validate(example['payload'], example['schema'])
            count += 1
    responses = {e['name']: e['payload'] for e in examples['responses']}
    for mode in ('qfq', 'hfq'):
        reconstruct(responses['raw_stock_etf'], responses['factor_' + mode], responses['adjusted_' + mode])
    factor = responses['factor_qfq']
    key = factor['query']['codes'][0]
    rejected = 0

    def reject(payload, kind, mutation, *, rehash=False):
        nonlocal rejected
        value = deepcopy(payload)
        mutation(value)
        if rehash:
            value['data_hash'], value['request_hash'] = digest(value['data']), digest(value['query'])
        try:
            validate(value, kind)
        except Exception:
            rejected += 1
        else:
            raise ValueError('negative control accepted: ' + kind)

    reject(factor, 'FactorResponse', lambda p: p['data'][key][0].update(multiplier='0'), rehash=True)
    reject(factor, 'FactorResponse', lambda p: p['data'][key][-1].update(offset='1'), rehash=True)
    reject(factor, 'FactorResponse', lambda p: p.update(data_hash='0' * 64))
    reject(factor, 'FactorResponse', lambda p: p['meta'][key]['coverage'].update(returned_rows=99))
    reject(factor, 'FactorResponse', lambda p: p['data'][key][0].update(source_id='absent'), rehash=True)
    reject(factor, 'FactorResponse', lambda p: p['meta'][key].update(derivation=None))
    reject(factor, 'FactorResponse', lambda p: p['meta'][key]['adjustment'].update(factor_snapshot_id='other'))
    reject(factor, 'FactorResponse', lambda p: p['data'][key].append(deepcopy(p['data'][key][0])), rehash=True)
    reject(factor, 'FactorResponse', lambda p: p['extensions']['research_v1'].update(raw_snapshot_id='not-a-raw-response'))
    reject(factor, 'FactorResponse', lambda p: p['extensions']['research_v1']['series_catalog'].pop(key))
    reject(factor, 'FactorResponse', lambda p: p['data'][key][0].update(multiplier='NaN'), rehash=True)
    request = next(e['payload'] for e in examples['requests'] if e['schema'] == 'FactorRequest')
    for mutation in (
        lambda p: p.update(code=key), lambda p: p.update(fq='none'), lambda p: p.update(start='20260230'),
        lambda p: p.update(start='20261022'), lambda p: p.update(date_pattern='*'),
        lambda p: p.update(codes=['XSHG/index/exchange/000001']), lambda p: p.pop('adjustment_anchor'),
    ):
        reject(request, 'FactorRequest', mutation)
    for field in ('close', 'volume'):
        damaged = deepcopy(responses['adjusted_qfq'])
        damaged['data'][key][0][field] = '-0.80' if field == 'close' else 999
        damaged['data_hash'] = digest(damaged['data'])
        try:
            reconstruct(responses['raw_stock_etf'], factor, damaged)
        except Exception:
            rejected += 1
        else:
            raise ValueError('tampered reconstruction accepted')
    cases = json.loads((ROOT / 'acceptance-cases.json').read_text())['cases']
    require(len(cases) == 56 and len({c['id'] for c in cases}) == 56, 'case count/id mismatch')
    report = json.loads((ROOT / 'report.template.json').read_text())
    require([r['case_id'] for r in report['results']] == [c['id'] for c in cases], 'report cases differ')
    manifest_count = verify_manifest() if (ROOT / 'MANIFEST.sha256.json').is_file() else 0
    return {'status': 'PASS', 'baseline_positive': base_positive, 'baseline_negative_rejected': base_negative,
            'extension_examples': count, 'extension_negative_rejected': rejected,
            'reconstruction_modes': ['qfq', 'hfq'], 'acceptance_cases': len(cases),
            'manifest_files_verified': manifest_count,
            'scope': 'OFFLINE ONLY; no service, market truth or deployment acceptance'}


def verify_manifest():
    manifest = json.loads((ROOT / 'MANIFEST.sha256.json').read_text())
    require(manifest.get('algorithm') == 'sha256', 'manifest algorithm differs')
    for name, expected in manifest['files'].items():
        relative = Path(name)
        target = (ROOT / relative).resolve()
        require(not relative.is_absolute() and '..' not in relative.parts and target.is_relative_to(ROOT),
                'unsafe manifest path')
        require(target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == expected,
                'manifest file/hash differs: ' + name)
    return len(manifest['files'])


def validate_report(path, evidence_root):
    """Check report completeness and evidence hashes, never infer evidence authenticity."""
    report = json.loads(path.read_text())
    cases = json.loads((ROOT / 'acceptance-cases.json').read_text())['cases']
    require(report.get('extension_contract') == 'stockdb-market-v3-research-extension'
            and report.get('extension_revision') == '1.0.0', 'report contract differs')
    phase = report.get('phase')
    require(phase in {'offline', 'service', 'client', 'joint'}, 'invalid report phase')
    require(bool(report.get('server_build_id')), 'report build identity missing')
    require(datetime.fromisoformat(report['generated_at']).tzinfo is not None, 'report timestamp lacks timezone')
    results = report.get('results', [])
    require(len(results) == len(cases) and {r['case_id'] for r in results} == {c['id'] for c in cases},
            'report cases missing, duplicated or unknown')
    root = evidence_root.resolve()
    by_id = {r['case_id']: r for r in results}
    statuses = {'PASS', 'FAIL', 'BLOCKED', 'NOT_RUN'}
    missing = []
    checked = 0
    for case in cases:
        result = by_id[case['id']]
        require(result['status'] in statuses, 'invalid case status')
        assertions = result.get('assertion_results', [])
        require([a['assertion'] for a in assertions] == case['assertions'], 'assertion list changed or incomplete')
        if result['status'] != 'PASS':
            require(bool(result.get('reason') or result.get('limitations')), 'non-pass needs reason')
        for assertion in assertions:
            require(assertion['status'] in statuses, 'invalid assertion status')
            evidence = assertion.get('evidence', [])
            if assertion['status'] == 'PASS':
                require(bool(evidence), 'PASS assertion lacks evidence')
            for entry in evidence:
                relative = Path(entry['path'])
                require(not relative.is_absolute() and '..' not in relative.parts, 'unsafe evidence path')
                target = (root / relative).resolve()
                require(target.is_relative_to(root) and target.is_file(), 'missing/out-of-root evidence')
                require(hashlib.sha256(target.read_bytes()).hexdigest() == entry['sha256'], 'evidence hash differs')
                require(entry.get('command_or_request') and entry.get('server_build_id'), 'evidence request/build absent')
                timestamp = datetime.fromisoformat(entry['captured_at'])
                require(timestamp.tzinfo is not None, 'evidence timestamp lacks timezone')
                require(entry.get('evidence_kind') in {'live', 'injected', 'offline', 'source'}, 'invalid evidence kind')
                if entry['evidence_kind'] == 'live':
                    require(entry['server_build_id'] == report['server_build_id'], 'live evidence mixes service builds')
                checked += 1
        require((result['status'] == 'PASS') == all(a['status'] == 'PASS' for a in assertions),
                'case PASS differs from assertion results')
        project_pending = phase == 'service' and case['owner'] == 'project'
        if project_pending:
            require(result['status'] == 'NOT_RUN' and result.get('reason') == 'consumer_workspace_required',
                    'service report cannot assert project acceptance')
        elif case['required'] and result['status'] != 'PASS':
            missing.append(case['id'])
    return {'status': 'PASS' if not missing else 'INCOMPLETE', 'phase': phase,
            'mandatory_not_passed': missing, 'evidence_references_verified': checked,
            'scope': 'Report structure and hashes only; independent content review required'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--self-test', action='store_true')
    parser.add_argument('--response', type=Path)
    parser.add_argument('--report', type=Path)
    parser.add_argument('--evidence-root', type=Path)
    parser.add_argument('--kind', default='FactorResponse', choices=('FactorRequest', 'FactorResponse',
        'FactorToolResponse', 'KlineRequest', 'KlineResponse', 'ToolFailureResponse', 'StatusResponse',
        'ResearchMetadata', 'ResearchStatusMetadata'))
    args = parser.parse_args()
    if args.self_test or (args.response is None and args.report is None):
        print(json.dumps(self_test(), ensure_ascii=False, indent=2))
    if args.response:
        validate(json.loads(args.response.read_text()), args.kind)
        print(json.dumps({'status': 'PASS', 'kind': args.kind, 'file': str(args.response), 'scope': 'OFFLINE ONLY'}))
    if args.report:
        result = validate_report(args.report, args.evidence_root or args.report.parent)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result['status'] != 'PASS':
            raise SystemExit(2)


if __name__ == '__main__':
    main()
