# AUTO-GENERATED from a-stock-data 02aa8b4d1316 — do not edit by hand.
# source file: SKILL.md
# source file sha256: a774151626a5389b1eaf45d5d38f83314e96826bdf20fd25057fe4ca5bb6ccb8
# blocks: [3, 39, 41]
# block sha256s: ['9a045253336e', 'acfa8cacdc37', 'c2ab4d0dc099']
# patch: strip-demo-calls(top-expr/loops/business-call-assigns)
# Upstream licensed Apache-2.0 (see vendor LICENSE); this generated
# file records origin and is rebuilt deterministically by astock.build.

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
import re
import json

def eastmoney_stock_news(code: str, page_size: int=20) -> list[dict]:
    """
    东财个股新闻（JSONP 接口）。
    返回: [{title, content, time, source, url}]
    """
    cb = 'jQuery_news'
    url = 'https://search-api-web.eastmoney.com/search/jsonp'
    inner_params = json.dumps({'uid': '', 'keyword': code, 'type': ['cmsArticleWebOld'], 'client': 'web', 'clientType': 'web', 'clientVersion': 'curr', 'param': {'cmsArticleWebOld': {'searchScope': 'default', 'sort': 'default', 'pageIndex': 1, 'pageSize': page_size, 'preTag': '', 'postTag': ''}}}, separators=(',', ':'))
    params = {'cb': cb, 'param': inner_params}
    headers = {'User-Agent': UA, 'Referer': 'https://so.eastmoney.com/'}
    r = em_get(url, params=params, headers=headers, timeout=15)
    text = r.text
    json_str = text[text.index('(') + 1:text.rindex(')')]
    d = json.loads(json_str)
    rows = []
    articles = d.get('result', {}).get('cmsArticleWebOld', []) or []
    for a in articles:
        rows.append({'title': re.sub('<[^>]+>', '', a.get('title', '')), 'content': re.sub('<[^>]+>', '', a.get('content', ''))[:200], 'time': a.get('date', ''), 'source': a.get('mediaName', ''), 'url': a.get('url', '')})
    return rows

import requests
import uuid

def eastmoney_global_news(page_size: int=50) -> list[dict]:
    """
    东方财富全球财经资讯（7x24 滚动）。
    返回: [{title, summary, time}]
    """
    url = 'https://np-weblist.eastmoney.com/comm/web/getFastNewsList'
    params = {'client': 'web', 'biz': 'web_724', 'fastColumn': '102', 'sortEnd': '', 'pageSize': str(page_size), 'req_trace': str(uuid.uuid4())}
    headers = {'User-Agent': UA, 'Referer': 'https://kuaixun.eastmoney.com/'}
    r = em_get(url, params=params, headers=headers, timeout=10)
    d = r.json()
    rows = []
    for item in (d.get('data') or {}).get('fastNewsList') or []:
        rows.append({'title': item.get('title', ''), 'summary': item.get('summary', '')[:200], 'time': item.get('showTime', '')})
    return rows
