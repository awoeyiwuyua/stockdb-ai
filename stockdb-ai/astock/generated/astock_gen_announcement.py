# AUTO-GENERATED from a-stock-data 02aa8b4d1316 — do not edit by hand.
# source file: SKILL.md
# source file sha256: a774151626a5389b1eaf45d5d38f83314e96826bdf20fd25057fe4ca5bb6ccb8
# blocks: [1, 3, 54]
# block sha256s: ['43a05af128a6', '9a045253336e', '46784647b7bf']
# patch: strip-demo-calls(top-expr/loops/business-call-assigns)
# Upstream licensed Apache-2.0 (see vendor LICENSE); this generated
# file records origin and is rebuilt deterministically by astock.build.

# 沪市指数白名单：与深市 000xxx 个股同段，需白名单区分（沪深300/上证50/中证500/科创50/中证1000/上证180）
SH_INDEX = {"000300", "000905", "000016", "000688", "000852", "000010"}

def get_prefix(code: str) -> str:
    """6位代码 → 市场前缀（sh/sz/bj）。支持显式前缀/后缀（sh000016 / 000016.SH）透传以解决歧义。"""
    c = code.lower().strip()
    if c.endswith((".sh", ".sz", ".bj")):    # 后缀写法与前缀等价：000016.SH ≡ sh000016。
        return c[-2:]                        # 不认后缀会让 000016.SH 落到默认深市 → 静默查成深康佳A
    if c.endswith((".xshg", ".xshe")):       # 聚宽写法（#55）：.XSHG=上交所 / .XSHE=深交所，000001.XSHG=上证指数
        return "sh" if c.endswith(".xshg") else "sz"
    if c.startswith(("sh", "sz", "bj")):     # 显式前缀透传（如 sh000001=上证指数 vs sz000001=平安银行）
        return c[:2]
    if c.startswith("92"):                   # 北交所 2024-10 起的新股号段，必须先于下面的 9x 判断
        return "bj"
    if c.startswith(("5", "6", "9")):        # 5x=沪 ETF/LOF，6/9=沪个股（900xxx=沪 B 股）
        return "sh"
    if c.startswith(("4", "8")):             # 4x/8x=北交所【老号段，多数已迁 920，见下方警告】
        return "bj"
    if c in SH_INDEX:                         # 沪深300/上证50 等沪指数（000xxx）
        return "sh"
    return "sz"                              # 深市个股/ETF（00/30/15x/16x/159 等），深指数 399xxx 亦走 sz

import time
import random
import requests
UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
DATACENTER_URL = 'https://datacenter-web.eastmoney.com/api/data/v1/get'
EM_SESSION = requests.Session()
try:
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    _em_adapter = HTTPAdapter(max_retries=Retry(total=3, connect=3, backoff_factor=0.6, status_forcelist=[429, 500, 502, 503, 504], allowed_methods=['GET']))
    EM_SESSION.mount('https://', _em_adapter)
    EM_SESSION.mount('http://', _em_adapter)
except Exception:
    pass
from typing import Optional
EM_MIN_INTERVAL = 1.0
_em_last_call = [0.0]

def em_get(url: str, params: Optional[dict]=None, headers: Optional[dict]=None, timeout: int=15, **kwargs):
    """东财统一请求入口：自动节流 + 复用 session + 默认 UA。
    所有 eastmoney.com 接口都应通过它请求，避免高频被封 IP。"""
    wait = EM_MIN_INTERVAL - (time.time() - _em_last_call[0])
    if wait > 0:
        time.sleep(wait + random.uniform(0.1, 0.5))
    try:
        return EM_SESSION.get(url, params=params, headers=headers, timeout=timeout, **kwargs)
    finally:
        _em_last_call[0] = time.time()

def eastmoney_datacenter(report_name: str, columns: str='ALL', filter_str: str='', page_size: int=50, sort_columns: str='', sort_types: str='-1') -> list[dict]:
    """东财数据中心统一查询 — 龙虎榜/解禁/融资融券/大宗交易/股东户数/分红 共用（已内置限流）"""
    params = {'reportName': report_name, 'columns': columns, 'filter': filter_str, 'pageNumber': '1', 'pageSize': str(page_size), 'sortColumns': sort_columns, 'sortTypes': sort_types, 'source': 'WEB', 'client': 'WEB'}
    r = em_get(DATACENTER_URL, params=params, timeout=15)
    d = r.json()
    if d.get('result') and d['result'].get('data'):
        return d['result']['data']
    return []

import requests
from datetime import datetime

def _cninfo_ts_to_date(ts):
    """巨潮 announcementTime 返回 Unix 毫秒整数，需转换为日期字符串。"""
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts / 1000).strftime('%Y-%m-%d')
    return str(ts)[:10] if ts else ''
_CNINFO_ORGID_MAP = {}

def _cninfo_orgid(code: str) -> str:
    """查股票真实 orgId。巨潮 orgId 并非统一 `gssx0{code}` 格式（如 601318→9900002221、
    601398→jjxt0000019、688017→9900041602），硬编码会导致大量股票（尤其 601xxx 段）
    返回 totalAnnouncement=0、查不到公告（#19）。优先动态查官方映射表，查不到再回退硬编码。"""
    global _CNINFO_ORGID_MAP
    if not _CNINFO_ORGID_MAP:
        try:
            r = requests.get('http://www.cninfo.com.cn/new/data/szse_stock.json', headers={'User-Agent': UA}, timeout=15)
            _CNINFO_ORGID_MAP = {s['code']: s['orgId'] for s in r.json().get('stockList', [])}
        except Exception as e:
            print(f'[WARN] 巨潮 orgId 映射表拉取失败，回退硬编码规则: {e}')
    org = _CNINFO_ORGID_MAP.get(code)
    if org:
        return org
    return f'gs{get_prefix(code)}0{code}'

def cninfo_announcements(code: str, page_size: int=30) -> list[dict]:
    """
    巨潮公告全文检索。
    返回: [{title, type, date, url}]
    """
    url = 'https://www.cninfo.com.cn/new/hisAnnouncement/query'
    org_id = _cninfo_orgid(code)
    payload = {'stock': f'{code},{org_id}', 'tabName': 'fulltext', 'pageSize': str(page_size), 'pageNum': '1', 'column': '', 'category': '', 'plate': '', 'seDate': '', 'searchkey': '', 'secid': '', 'sortName': '', 'sortType': '', 'isHLtitle': 'true'}
    headers = {'User-Agent': UA, 'Content-Type': 'application/x-www-form-urlencoded', 'Referer': 'https://www.cninfo.com.cn/new/disclosure', 'Origin': 'https://www.cninfo.com.cn'}
    r = requests.post(url, data=payload, headers=headers, timeout=15)
    d = r.json()
    rows = []
    for item in d.get('announcements', []) or []:
        rows.append({'title': item.get('announcementTitle', ''), 'type': item.get('announcementTypeName', ''), 'date': _cninfo_ts_to_date(item.get('announcementTime')), 'url': f"https://www.cninfo.com.cn/new/disclosure/detail?annoId={item.get('announcementId', '')}"})
    return rows
