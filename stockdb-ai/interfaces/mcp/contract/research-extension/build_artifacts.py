#!/usr/bin/env python3
"""Rebuild bounded, synthetic contract artifacts; never access NAS or business data."""
from copy import deepcopy
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BASE = ROOT / 'baseline' if (ROOT / 'baseline').is_dir() else ROOT.parent / 'stockdb-market-v3'
EXTENSION = 'stockdb-market-v3-research-extension'


def write(name, value):
    (ROOT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def ref(name):
    return {'$ref': '#/$defs/' + name}


def obj(properties, required=None):
    return {'type': 'object', 'additionalProperties': False,
            'required': list(properties) if required is None else required, 'properties': properties}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                   separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def rehash(value):
    value['request_hash'] = digest(value['query'])
    value['data_hash'] = digest(value['data'])


def build_schema():
    baseline = json.loads((BASE / 'contract.schema.json').read_text())
    schema = deepcopy(baseline)
    schema.update({'$id': 'urn:stockdb:market:v3:research-extension:1.0.0',
                   'title': 'StockDB research extension 1.0.0, additive to market v3/3.0.0',
                   'description': 'All baseline definitions unchanged. New factor and extensions definitions only.',
                   '$ref': '#/$defs/FactorResponse'})
    defs = schema['$defs']
    both = deepcopy(defs['KlineRequest']['oneOf'])
    defs['FactorRequest'] = obj({
        'contract_version': ref('ContractVersion'), 'code': ref('InstrumentId'), 'codes': ref('Codes'),
        'start': ref('Date8'), 'end': ref('Date8'), 'fq': {'enum': ['qfq', 'hfq']},
        'adjustment_anchor': ref('Date8'), 'snapshot_id': {'type': 'string', 'minLength': 1},
    }, ['contract_version', 'start', 'end', 'fq', 'adjustment_anchor'])
    defs['FactorRequest']['oneOf'] = both
    defs['FactorQuery'] = obj({
        'codes': ref('Codes'), 'start': ref('Date8'), 'end': ref('Date8'),
        'fq': {'enum': ['qfq', 'hfq']}, 'adjustment_anchor': ref('Date8'),
        'snapshot_id': ref('NullableString'),
    })
    defs['FactorRow'] = obj({'date': ref('Date'), 'multiplier': ref('Decimal'),
                             'offset': ref('Decimal'), 'source_id': {'type': 'string', 'minLength': 1}})
    defs['FactorDerivation'] = obj({
        'methodology_id': {'type': 'string', 'minLength': 1},
        'methodology_version': {'type': 'string', 'minLength': 1},
        'source_id': {'type': 'string', 'minLength': 1},
        'formula_reference': {'type': 'string', 'minLength': 1},
    })
    meta = deepcopy(defs['InstrumentMeta'])
    meta['required'].remove('units')
    del meta['properties']['units']
    meta['properties']['adjustment'] = {'oneOf': [ref('Adjustment'), {'type': 'null'}]}
    meta['required'].append('derivation')
    meta['properties']['derivation'] = {'oneOf': [ref('FactorDerivation'), {'type': 'null'}]}
    defs['FactorMeta'] = meta
    nullable_day = {'oneOf': [ref('Date'), {'type': 'null'}]}
    defs['SeriesDescriptor'] = obj({
        'instrument_id': ref('InstrumentId'), 'series_id': ref('NullableString'),
        'provider': ref('NullableString'), 'methodology_id': ref('NullableString'),
        'methodology_version': ref('NullableString'), 'history_start': nullable_day,
        'history_end': nullable_day,
        'calendar_exchange': {'enum': ['XSHG', 'XSHE', 'XBSE', None]},
        'calendar_id': ref('NullableString'), 'calendar_version': ref('NullableString'),
        'native_volume_unit': ref('NullableString'),
        'volume_multiplier_to_wire': {'oneOf': [ref('Decimal'), {'type': 'null'}]},
        'origin_reference': ref('NullableString'),
        'limitations': {'type': 'array', 'items': {'type': 'string', 'minLength': 1}},
    })
    defs['ResearchMetadata'] = obj({
        'extension_contract': {'const': EXTENSION}, 'extension_revision': {'const': '1.0.0'},
        'raw_snapshot_id': ref('NullableString'),
        'series_catalog': {'type': 'object', 'propertyNames': ref('InstrumentId'),
                           'additionalProperties': {'oneOf': [ref('SeriesDescriptor'), {'type': 'null'}]}},
    })
    defs['ResearchStatusMetadata'] = obj({
        'extension_contract': {'const': EXTENSION}, 'extension_revision': {'const': '1.0.0'},
        'history_targets': obj({'index_sessions': {'const': 500}, 'board_sessions': {'const': 250},
                                'stock_etf_sessions': {'const': 500}}),
        'known_limitations': {'type': 'array', 'items': {'type': 'string', 'minLength': 1}},
    })
    defs['FactorResponse'] = {
        'type': 'object', 'unevaluatedProperties': False,
        'allOf': [ref('ResponseHeader'), {
            'required': ['query', 'request_hash', 'data', 'meta', 'extensions'],
            'properties': {
                'query': ref('FactorQuery'), 'request_hash': ref('Hash'),
                'data': {'type': 'object', 'propertyNames': ref('InstrumentId'),
                         'additionalProperties': {'type': 'array', 'items': ref('FactorRow')}},
                'meta': {'type': 'object', 'propertyNames': ref('InstrumentId'),
                         'additionalProperties': ref('FactorMeta')},
                'extensions': {'type': 'object', 'required': ['research_v1'],
                               'properties': {'research_v1': ref('ResearchMetadata')}},
            },
        }],
    }
    defs['FactorToolResponse'] = {'type': 'object', 'oneOf': [ref('FactorResponse'), ref('ToolFailureResponse')]}
    assert all(defs[name] == definition for name, definition in baseline['$defs'].items())
    write('contract.schema.json', schema)


def research(payload, raw=None):
    catalog = {}
    for key in payload['query']['codes']:
        instrument = payload['meta'][key]['instrument']
        catalog[key] = {
            'instrument_id': key, 'series_id': 'synthetic-series:' + key,
            'provider': 'synthetic-provider', 'methodology_id': 'synthetic-method',
            'methodology_version': '1', 'history_start': '2026-10-20', 'history_end': '2026-10-21',
            'calendar_exchange': 'XSHG' if instrument['exchange'] == 'CN' else instrument['exchange'],
            'calendar_id': 'synthetic-calendar', 'calendar_version': 'fixture-1',
            'native_volume_unit': 'synthetic-verified-unit', 'volume_multiplier_to_wire': '1',
            'origin_reference': 'urn:fixture:series-identity', 'limitations': ['synthetic fixture only'],
        }
    payload['extensions'] = {'research_v1': {
        'extension_contract': EXTENSION, 'extension_revision': '1.0.0',
        'raw_snapshot_id': raw, 'series_catalog': catalog,
    }}


def build_examples():
    source = json.loads((BASE / 'examples.json').read_text())
    raw = deepcopy(source['responses'][0]['payload'])
    raw['snapshot_id'] = 'fixture-raw-R'
    research(raw)
    responses = [{'name': 'raw_stock_etf', 'schema': 'KlineResponse', 'payload': raw}]
    requests = []
    for mode, anchor in [('qfq', '20261021'), ('hfq', '20261020')]:
        factor = deepcopy(raw)
        factor['snapshot_id'] = 'fixture-factor-F-' + mode
        factor['request_id'] = 'fixture-factor-' + mode
        factor['query'] = {k: deepcopy(raw['query'][k]) for k in ('codes', 'start', 'end')}
        factor['query'].update(fq=mode, adjustment_anchor=anchor, snapshot_id=None)
        for key in factor['query']['codes']:
            days = [r['date'] for r in raw['data'][key]]
            coefficients = [('0.5', '-6'), ('1', '0')] if mode == 'qfq' else [('1', '0'), ('2', '12')]
            factor['data'][key] = [{'date': day, 'multiplier': m, 'offset': b, 'source_id': 's1'}
                                   for day, (m, b) in zip(days, coefficients, strict=True)]
            factor['meta'][key].pop('units')
            factor['meta'][key]['adjustment'] = {'mode': mode, 'anchor_date': anchor,
                'factor_snapshot_id': factor['snapshot_id'], 'algorithm': 'affine-v1'}
            factor['meta'][key]['derivation'] = {'methodology_id': 'synthetic-affine-example',
                'methodology_version': '1', 'source_id': 's1', 'formula_reference': 'urn:fixture:affine-not-market-facts'}
        research(factor)
        rehash(factor)
        request = {'contract_version': 'stockdb-market-v3', **{k: v for k, v in factor['query'].items() if v is not None}}
        requests.append({'name': 'factor_' + mode, 'tool': 'get_adjust_factors', 'schema': 'FactorRequest', 'payload': request})
        responses.append({'name': 'factor_' + mode, 'schema': 'FactorResponse', 'payload': factor})
        adjusted = deepcopy(raw)
        adjusted['snapshot_id'] = 'fixture-adjusted-A-' + mode
        adjusted['query'].update(fq=mode, adjustment_anchor=anchor, factor_snapshot_id=factor['snapshot_id'])
        for key, rows in adjusted['data'].items():
            adjusted['meta'][key]['adjustment'] = deepcopy(factor['meta'][key]['adjustment'])
            quantum = Decimal(1).scaleb(-adjusted['meta'][key]['units']['price_decimals'])
            for row, coeff in zip(rows, factor['data'][key], strict=True):
                for field in ('open', 'high', 'low', 'close'):
                    row[field] = format((Decimal(row[field]) * Decimal(coeff['multiplier']) + Decimal(coeff['offset']))
                                        .quantize(quantum, rounding=ROUND_HALF_UP), 'f')
        research(adjusted, raw['snapshot_id'])
        rehash(adjusted)
        responses.append({'name': 'adjusted_' + mode, 'schema': 'KlineResponse', 'payload': adjusted})
        request = {'contract_version': 'stockdb-market-v3', **{k: v for k, v in adjusted['query'].items() if v is not None}}
        requests.append({'name': 'adjusted_' + mode, 'tool': 'get_kline', 'schema': 'KlineRequest', 'payload': request})
    index = deepcopy(raw)
    old = next(iter(index['data']))
    index['query']['codes'] = ['XSHG/index/exchange/000001', 'CN/board/eastmoney/BK1138']
    index['data'], index['meta'] = {}, {}
    for key in index['query']['codes']:
        exchange, asset, namespace, code = key.split('/')
        index['data'][key] = deepcopy(raw['data'][old])
        index['meta'][key] = deepcopy(raw['meta'][old])
        index['meta'][key]['instrument'].update(instrument_id=key, exchange=exchange, asset_type=asset,
                                               namespace=namespace, code=code, name='synthetic-index-or-board')
        index['meta'][key]['units'].update(price='points', volume='unknown', amount='unknown',
                                          amount_decimals=None, null_reason='synthetic source has no defined volume/amount')
        for row in index['data'][key]:
            row['volume'], row['amount'] = None, None
    index['snapshot_id'] = 'fixture-index-board'
    research(index)
    rehash(index)
    responses.append({'name': 'index_board_price_only', 'schema': 'KlineResponse', 'payload': index})
    requests.append({'name': 'index_board', 'tool': 'get_kline', 'schema': 'KlineRequest',
                     'payload': {'contract_version': 'stockdb-market-v3', **{k: v for k,v in index['query'].items() if v is not None}}})
    failed = deepcopy(next(r['payload'] for r in responses if r['name'] == 'factor_qfq'))
    failed.update(status='failed', is_partial=True, snapshot_id=None, snapshot_expires_at=None)
    failed['source_catalog'] = {}
    failed['errors'] = [{'code': 'SOURCE_UNAVAILABLE', 'message': 'synthetic failure',
                         'instrument_id': None, 'retryable': True, 'details': {}}]
    for key in failed['query']['codes']:
        failed['data'][key] = []
        meta = failed['meta'][key]
        meta.update(status='error', adjustment=None, derivation=None)
        meta['instrument'].update(identity_verified=False, identity_source_id=None)
        meta['coverage'].update(state='incomplete', available_rows=0, returned_rows=0,
            gaps=[{'date': day, 'time': None, 'reason': 'SOURCE_ERROR', 'evidence': []}
                  for day in ('2026-10-20', '2026-10-21')])
        failed['extensions']['research_v1']['series_catalog'][key] = None
    rehash(failed)
    responses.append({'name': 'factor_all_failed', 'schema': 'FactorResponse', 'payload': failed})
    partial = deepcopy(next(r['payload'] for r in responses if r['name'] == 'factor_qfq'))
    bad = partial['query']['codes'][-1]
    partial.update(status='partial', is_partial=True)
    partial['data'][bad] = []
    partial['meta'][bad] = deepcopy(failed['meta'][bad])
    partial['errors'] = [{**failed['errors'][0], 'instrument_id': bad}]
    partial['extensions']['research_v1']['series_catalog'][bad] = None
    rehash(partial)
    responses.append({'name': 'factor_partial', 'schema': 'FactorResponse', 'payload': partial})
    responses.append(deepcopy(next(r for r in source['responses'] if r['schema'] == 'ToolFailureResponse')))
    status = deepcopy(next(r['payload'] for r in source['responses'] if r['schema'] == 'StatusResponse'))
    status['extensions'] = {'research_v1': {
        'extension_contract': EXTENSION, 'extension_revision': '1.0.0',
        'history_targets': {'index_sessions': 500, 'board_sessions': 250, 'stock_etf_sessions': 500},
        'known_limitations': ['synthetic status only; two-day fixtures prove no historical coverage'],
    }}
    responses.append({'name': 'synthetic_status', 'schema': 'StatusResponse', 'payload': status})
    write('examples.json', {'notice': 'ALL SYNTHETIC; no live prices, economic events or capability proof.',
                            'requests': requests, 'responses': responses})


CASE_GROUPS = {
 'S': [
 ('基线与包完整性', '原六文件及所有原$defs精确一致', '所有交付文件hash一致且无私人资料'),
 ('实际工具schema', '四核心工具及因子工具根type=object', '所有成功/部分/失败被实际outputSchema接受，无任意object兜底'),
 ('核心回归', '基线C01-C22适用服务断言通过并有逐断言证据', '既有股票ETF未复权主链路行为与shape不变'),
 ('能力发现', '六profile按用途精确声明，实际支持组合可逐项定位', '服务版本、线协议、扩展修订独立，未支持能力明确拒绝'),
 ('旧版兼容', '无contract_version保持旧版', '核心3.0.0请求无需新增参数；新分支不误匹配legacy'),
 ('期限与资源', '冷/热500日请求及并发测试均有界且总期限<=30秒', '慢负载至少20次status本地p95<=2秒，失败后槽位恢复'),
 ('传输及错误', 'JSON/SSE请求id、structuredContent/JSON文本一致', '单项失败partial，全部失败failed/isError=true，不丢机器错误'),
 ('公开数据边界', '请求与日志仅含公开查询参数', '无账户/持仓/成本/交易/研究判断、令牌和秘密路径'),
 ],
 'I': [
 ('四指数身份', '四个指定规范键逐项真实取数和公开身份映射', '中证1000编制者与发布市场不混淆'),
 ('同码不同资产', '上证000001与深圳股票000001不同identity/源请求', '缓存、批量、单票及重放不会串资产'),
 ('七BK板块', '七个指定BK逐项250日及末日真实验证', '失败序列逐项BLOCKED而非缩小范围后整批PASS'),
 ('提供者namespace隔离', 'BK请求绝不返回THS同主题序列', 'THS可选支持独立声明；成员列表成功不代表日K支持'),
 ('序列元数据及日历', 'series_catalog恰好覆盖请求键且真实边界/日历可核验', 'CN板块明确交易所日历；series_id与方法版本语义稳定'),
 ('量额与用途', 'points/volume/amount单位有证据；未知null有原因', 'price-only与full-OHLCV分开，不以假0适配消费者'),
 ('历史方法修订', '改名/方法版本变化保留独立证据', '不跨方法或提供者悄悄拼历史/沿用点位'),
 ('独立公开比价', '每序列末5日及规则抽样5日有公开参考和逐点差值', '身份口径可比后按固定精度阈值判定，未解释差异不得PASS'),
 ('缓存和混合批量', '指数/板块/股票/ETF混合批量逐项隔离', '单项错误保留其他项和全部请求键'),
 ('未支持与错误键', '错namespace/未知序列/不支持周期和复权明确失败', '不把错误身份正规化成另一个存在的代码'),
 ],
 'H': [
 ('长窗口矩阵', '4指数500、7BK250、4股票2ETF500日覆盖表逐项给出', '成熟样本以公开规则选择；合法上市不足另列证据'),
 ('长日历', '500/1000日需要的自然日区间日历完整', '跨年/春节/闰日无400自然日静默截断'),
 ('分页边界', '所有源页穷尽且同版本，边界无重复遗漏', '乱序、重复、缺日注入不能被静默清理后假完整'),
 ('合法gap', '停牌/上市前/摘牌gap逐日有有效公开证据', '有证据空与连续指标可用性分别判定，不造平盘K'),
 ('非法或未知gap', '源失败/历史不足/未发布不得冒充停牌', 'UNKNOWN或无证据原因使覆盖不完整并保留错误'),
 ('limit与截断', '0全取、正数每证券最新N且升序', '源/响应上限显式truncated/partial或RESULT_TOO_LARGE'),
 ('同快照分段', '完整捕获后按同snapshot拆证券/日期/字段', '合并值与整段一致；范围扩张不得实时补数据'),
 ('长历史资源边界', '大窗口不无限延长期限或响应上限', '超过可捕获范围明确失败，不把多个实时请求说成原子快照'),
 ('时间与研究复现', '源时间、捕获、服务时间分别留证', '事后长历史不伪装成过去当时可知数据'),
 ('1000日能力探测', '可选1000日/上市至今样本实际探测或明确限制', '不支持时声明而非静默截断；不强求所有证券相同长度'),
 ],
 'A': [
 ('因子工具schema', '原$defs不改，FactorRequest/Response/Failure分支完整', '根object、传递依赖及真实部署schema独立校验'),
 ('因子单批一致', '单票/单元素/多元素在同F规范化相同', 'data/meta恰好覆盖codes，source_id可解析'),
 ('逐日因子覆盖', '有真实原始bar的每一天有系数', '缺因子不默认1/0，合法gap与原始行情一致'),
 ('仿射公式', '源SDK字段真实含义和转换公式有代码/公开资料', 'R乘multiplier加offset逐OHLC复算，不能仅自洽'),
 ('锚点', 'qfq末日、hfq首日锚定必须支持，锚点系数1/0', '子范围重读保留原锚点；其他锚点限制明确拒绝'),
 ('真实现金除权', '至少一个真实现金事件前后各3日回算及独立对照', '源事件、锚点、系数和经济语义有证据'),
 ('真实送转或拆合', '至少一个真实送股/转增/拆合事件或ETF调整证据', '跨事件价格按可证明方法一致，不能套用假因子'),
 ('组合事件合成', '现金与送转组合合成计算的multiplier/offset可验证', '该用例只证明算法边界，不能代替真实事件'),
 ('合法非正复权价', '合成qfq出现0/负值仍正确返回且OHLC有序', '客户端不得沿用raw>0过滤删掉合法历史'),
 ('量额不复权', '原始R与A逐日volume/amount精确相同', '价格因子从不作用于成交股数/份数与金额'),
 ('RFA绑定', 'A引用可重读原始R和因子F，证券/日期/锚点一致', '完整六字段原始R及F重算A，来源时间分离'),
 ('错快照与范围', '错F/fq/anchor/范围及互传快照明确错误', '不能把none原始快照改fq当复权重放'),
 ('修订不改旧值', '上游修改后旧R/F/A值与来源时间不变', '新快照hash变化可追溯，不偷换旧F'),
 ('因子部分与全失败', '单项源超时partial且其余项保留', '全部失败failed、typed errors、无伪完整或假系数'),
 ('持久化与到期', '正常服务/容器重启后未到期扩展R/F/A可读', '至少7日保留及过期/丢失失败有存储/时钟测试'),
 ('来源和方法隔离', '真实provider、公式版本、因子来源明确', '不同provider/方法不拼系数或偷换指数股票因子'),
 ('精度与舍入', 'Decimal精确计算并ROUND_HALF_UP到声明精度', '舍入边界和12位系数精度测试，无float/NaN/科学计数法'),
 ('因子query及hash', 'FactorQuery仅含约定6字段，代码正规化与hash正确', '篡改query/系数/锚点/重复日期均被拒绝'),
 ('因子非法请求', 'code+codes/空/重复/超限/坏日期/倒序/未知字段拒绝', 'none、date_pattern、非法资产及未完成锚点不误接收'),
 ('失败schema与返回', '可正规化失败保留codes/meta，未正规化失败用通用失败', '两类失败都被实际工具outputSchema接受并保留错误'),
 ],
 'J': [
 ('消费方能力分派', '客户端按用途profile而非仅core匹配', '可选扩展故障不阻塞原股票ETF主链路'),
 ('指数板块隔离发布', '规范键/series/provider/version维度正确存储', 'NAS到公开ODS/派生产品再到data_core读回，不直读NAS物理文件'),
 ('因子消费', '新增因子工具解析和adjustment_anchor/factor_snapshot_id参数', '完整保留R/F/A、算法、单位和来源'),
 ('用途资格', 'null量额、合法非正复权价、合法gap分用途验收', '旧正价格/连续60行规则不误用于所有研究数据'),
 ('备用与版本一致', '故障整段回退并记录provider/series/adjustment及原因', '不逐日拼接BK/THS、原始/复权或不同因子版本'),
 ('保护当前主源', '现有股票ETF日K/收盘same-batch不变', '私有事实未发送/未改写，原兼容与错误回归通过'),
 ('本项目审计', '对应测试、层边界/数据源边界及数仓审计通过', '业务估值受影响时增加一致性/SQLite strict，存量问题逐项判定'),
 ('逐数据集放行', '实际端点经过隔离到默认读回的joint验证', '服务/客户端/联合/生产状态分别记录，不伪造历史观察'),
 ],
}


def build_cases_and_templates():
    cases = []
    for group, rows in CASE_GROUPS.items():
        for number, (name, *assertions) in enumerate(rows, 1):
            case_id = f'{group}{number:02}'
            cases.append({'id': case_id, 'name': name, 'owner': 'project' if group == 'J' else 'service+client',
                          'required': case_id != 'H10', 'assertions': assertions})
    write('acceptance-cases.json', {'extension_contract': EXTENSION, 'extension_revision': '1.0.0',
        'status_values': ['PASS', 'FAIL', 'BLOCKED', 'NOT_RUN'], 'cases': cases})
    indices = ['XSHG/index/exchange/000001', 'XSHE/index/exchange/399001',
               'XSHE/index/exchange/399006', 'XSHG/index/csi/000852']
    boards = ['BK1408','BK1134','BK1138','BK1101','BK1037','BK0728','BK1160']
    keys = indices + ['CN/board/eastmoney/' + c for c in boards]
    write('support-matrix.template.json', {
        'notice': 'Template only; replace null with measured facts, never invent evidence.',
        'server': {'version': None, 'build_id': None, 'source_commit': None, 'image_digest': None, 'checked_at': None},
        'required_series': [{'instrument_id': k, 'status': 'NOT_RUN', 'ohlcv_support': None,
            'history_start': None, 'history_end': None, 'tested_sessions': None,
            'provider': None, 'series_id': None, 'evidence': [], 'limitations': []} for k in keys],
        'scope_status_legend': {'ohlcv_support': 'price_only|full_ohlcv|unsupported; null means not checked'},
        'stock_etf_samples': [], 'optional_ths_series': [],
        'adjustment_modes': [{'fq': fq, 'status': 'NOT_RUN', 'supported_anchor_rules': [],
            'tested_events': [], 'evidence': [], 'limitations': []} for fq in ('qfq', 'hfq')],
    })
    write('report.template.json', {'extension_contract': EXTENSION, 'extension_revision': '1.0.0',
        'phase': 'service', 'server_build_id': None, 'generated_at': None,
        'results': [{'case_id': c['id'], 'status': 'NOT_RUN', 'reason': 'consumer_workspace_required' if c['owner']=='project' else 'not_executed',
                     'assertion_results': [{'assertion': a, 'status': 'NOT_RUN', 'evidence': []} for a in c['assertions']],
                     'limitations': []} for c in cases],
        'evidence_entry_format': {'path': 'relative/path.json', 'sha256': '64 lower-case hex',
                                  'command_or_request': {}, 'captured_at': None, 'server_build_id': None,
                                  'evidence_kind': 'live|injected|offline|source'},
    })


if __name__ == '__main__':
    build_schema()
    build_examples()
    build_cases_and_templates()
    print('Built additive schema, synthetic examples, 56 cases and two templates; no service accessed.')
