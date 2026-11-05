# AUTO-GENERATED from a-stock-data 02aa8b4d1316 — do not edit by hand.
# source file: SKILL.md
# source file sha256: a774151626a5389b1eaf45d5d38f83314e96826bdf20fd25057fe4ca5bb6ccb8
# blocks: [1, 2, 3, 4, 62, 64]
# block sha256s: ['43a05af128a6', '72a4d20b1fca', '9a045253336e', 'cf4508a5d608', '0002f46911b6', '560b1cbecbda']
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

import re
_TICKER_RE = re.compile('^(?:(sh|sz|bj)(\\d{6})|(\\d{6})(?:\\.(sh|sz|bj|xshg|xshe))?)$', re.IGNORECASE)
_JQ_SUFFIX = {'xshg': 'sh', 'xshe': 'sz'}

def _natural_market(digits: str) -> str:
    """6 位码的自然归属市场。仅用于校验显式前缀是否自相矛盾。
    注意 000xxx 是沪指数/深个股共用的歧义段，由调用处单独处理，不走这里。"""
    if digits.startswith(('4', '8', '92')):
        return 'bj'
    if digits[0] in ('5', '6', '9'):
        return 'sh'
    return 'sz'

def norm_ticker(code: str, stock_only: bool=False) -> str:
    """任意受支持写法 → 纯 6 位数字代码。

    支持 600519 / SH600519 / sh600519 / 600519.SH / BJ920982 等。
    stock_only=True：个股专用接口（研报、一致预期等）传这个，会拒绝显式指数写法。
    ⚠️ 不匹配时**抛 ValueError，绝不静默返回空串或猜一个代码**——
    否则调用方会把「代码格式写错」误读成「这只票没有数据」，
    或者更糟：拿到另一只股票的数据还以为是对的。
    """
    raw = str(code).strip()
    m = _TICKER_RE.match(raw)
    if not m:
        raise ValueError(f'无法把 {code!r} 解析为 6 位股票代码；支持格式：600519 / SH600519 / sh600519 / 600519.SH / 600519.XSHG（聚宽）（前缀与后缀二选一，不能同时写）')
    digits = m.group(2) or m.group(3)
    market = (m.group(1) or m.group(4) or '').lower()
    market = _JQ_SUFFIX.get(market, market)
    if market:
        if digits.startswith('000'):
            if market == 'bj':
                raise ValueError(f'{code!r} 市场标识与号段矛盾：000xxx 不属北交所。')
            if stock_only and market == 'sh':
                raise ValueError(f'{code!r} 指向沪市指数而非个股（沪市无 000xxx 个股），本接口只服务个股。要查同号段的深市个股请显式传 sz{digits}。')
        else:
            nat = _natural_market(digits)
            if market != nat:
                raise ValueError(f'{code!r} 的市场标识与号段矛盾：{digits} 属 {nat} 市，而不是 {market} 市。（改用 {nat}{digits} 或去掉市场标识）')
    return digits

def em_market_code(code: str) -> int:
    """东财 secid 的市场号：**沪=1，深/北=0**（V3.7.0 新增 · #46）。

    ⚠️ 绝不要用 `code.startswith("6")` 判市场 —— 那会把**沪市 ETF（51x）**、
    **科创板 ETF（588x）**、**沪 B 股（900x）** 全部错判成深市，接口返回 `data: null`。
    2026-08-19 实测：510300 / 588000 / 600519 / 688112 / 900901 → m=1；
    300750 / 159915 / 920982 / 832982 → m=0（北交所与深市共用 m=0）。
    """
    return 1 if get_prefix(code) == 'sh' else 0

def em_secid(code: str) -> str:
    """东财 push2/push2his 的 secid，如 `1.600519` / `0.300750`。"""
    return f'{em_market_code(code)}.{norm_ticker(code)}'

def to_joinquant(code: str) -> str:
    """任意受支持写法 → 聚宽代码，如 `600519.XSHG` / `000001.XSHE`（#55）。

    000 段歧义码按 get_prefix() 的规则走：`to_joinquant("sh000001")` → `000001.XSHG`（上证指数），
    `to_joinquant("000001")` → `000001.XSHE`（平安银行）。反方向（聚宽 → 本 Skill）用
    `get_prefix(c) + norm_ticker(c)` 得到 `sh000001` 这类显式前缀写法，再传给各端点。
    北交所：聚宽公开文档与 jqdatasdk 源码（2026-09-20 查）只有 .XSHG / .XSHE，没查到北交所后缀，抛 ValueError 不猜。
    """
    digits, market = (norm_ticker(code), get_prefix(code))
    if market == 'bj':
        raise ValueError(f'{code!r} 是北交所证券；聚宽公开文档未给出北交所后缀，不做转换')
    return f"{digits}.{('XSHG' if market == 'sh' else 'XSHE')}"

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

import functools
import math
import re
from datetime import date as _date_cls, datetime, timezone

import pandas as pd
import requests

V39_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# 一次要翻几十页（§1.4 腾讯逐笔）或偶尔握手卡住（§13.7 新浪期货日 K）的请求走这个会话：复用连接，
# 连接失败、握手或等响应头超时、429 / 5xx 指数退避重试（写法同 EM_SESSION）；读响应体中途超时不重试，
# 也不按服务端给的 Retry-After 等待。
V39_RETRY_SESSION = requests.Session()
try:
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    _v39_retry_adapter = HTTPAdapter(max_retries=Retry(
        total=3, connect=3, backoff_factor=0.6, respect_retry_after_header=False,
        status_forcelist=[429, 500, 502, 503, 504], allowed_methods=["GET"]))
    V39_RETRY_SESSION.mount("https://", _v39_retry_adapter)
    V39_RETRY_SESSION.mount("http://", _v39_retry_adapter)
except Exception:
    # 老版本 urllib3（< 1.26）缺 allowed_methods：不重试，也不复用连接（复用的连接被服务端断开时没有重试兜底），同 v3.10.0
    V39_RETRY_SESSION = None


def _v39_http(url, params=None, data=None, headers=None, method="GET", timeout=(10, 40),
              allow_status=(), allow_redirects=True, session=None):
    """非东财的 HTTP 请求：带浏览器 UA。网络错误、非 2xx 一律抛 RuntimeError（不把错误页当数据）；
    allow_status 里的状态码（源用 404 表示「当天没发布」时）原样返回，由调用方判断。
    session：传 V39_RETRY_SESSION 复用连接并自动重试（哪些情况重试见它的定义）；不传每次新建连接、不重试。"""
    merged = {"User-Agent": V39_UA}
    merged.update(headers or {})
    try:
        response = (session or requests).request(method, url, params=params, data=data, headers=merged,
                                                 timeout=timeout, allow_redirects=allow_redirects)
        if response.status_code not in allow_status:
            response.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(f"请求 {url} 失败: {type(exc).__name__}: {exc}") from exc
    return response


def _v39_json(response):
    """解析 JSON；不是 JSON 抛 RuntimeError。json 的解析错误是 ValueError 的子类，
    不转换会被调用方当成「确实没有数据」。"""
    try:
        return response.json()
    except ValueError as exc:
        raise RuntimeError(f"{getattr(response, 'url', '')} 返回的不是 JSON，可能是错误页") from exc


def _v39_date(value):
    """'2026-09-18' / '20260918' / date 对象 → '2026-09-18'；其他写法抛 ValueError。"""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, _date_cls):
        return value.isoformat()
    text = str(value).strip()
    fmt = "%Y%m%d" if re.fullmatch(r"[0-9]{8}", text) else "%Y-%m-%d"
    return datetime.strptime(text, fmt).date().isoformat()


def _v39_src_date(value):
    """来源返回的日期 → 'YYYY-MM-DD'；认不出抛 RuntimeError（源格式变了，不是参数写错）。"""
    try:
        return _v39_date(value)
    except ValueError as exc:
        raise RuntimeError(f"来源返回了无法识别的日期 {value!r}") from exc


def _v39_num(value):
    """'1,234.50' → 1234.5；空串 / '-' / '--' / None → None；其他非数字抛 RuntimeError
    （来源给了认不出的值是「源的格式变了」，不能和参数错误的 ValueError 混在一起）。
    JSON 布尔值同样抛错：float(True)=1.0 会把格式错误静默写成价格 / 成交量。"""
    if value is None:
        return None
    if isinstance(value, bool):
        raise RuntimeError(f"来源在数值字段给了布尔值 {value!r}")
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return float(value)
    text = str(value).replace(",", "").strip()
    if text in ("", "-", "--", "None", "null"):
        return None
    try:
        number = float(text)
    except ValueError as exc:
        raise RuntimeError(f"来源返回了无法识别的数值 {value!r}") from exc
    return number if math.isfinite(number) else None


def _v39_rows(value, what):
    """来源里可能整段缺失的行列表：字段没有（None）按空处理，其余必须是对象列表。
    写成 `value or []` 会把 {} / '' / 0 这类结构改变也当成空表，静默丢掉整段数据。"""
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        raise RuntimeError(f"{what} 应为对象列表，实际是 {type(value).__name__}: {str(value)[:120]}")
    return value


def _v39_labels(value, what):
    """来源的标签数组（频道名之类）：None 按空处理，其余必须是字符串列表。
    直接 `value or []` 再 join，来源把数组改成字符串时会被拆成单字（'ab' → 'a,b'）。"""
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise RuntimeError(f"{what} 应为字符串列表，实际是 {type(value).__name__}: {str(value)[:120]}")
    return value


def _v39_req_num(value, what):
    """必填数值（价格、成交量）：在 _v39_num 之上，空值 / NaN / inf 也抛 RuntimeError，不能当缺失放过。"""
    number = _v39_num(value)
    if number is None:
        raise RuntimeError(f"来源的 {what} 为空或不是有限数值: {value!r}")
    return number


def _v39_contract(func):
    """统一异常契约：来源行缺字段时 row["X"] 会漏出 KeyError，调用方按「参数错 / 没数据」处理就会
    把「来源格式变了」当成正常情况。这里把它转成带函数名和字段名的 RuntimeError。
    （函数内所有按用户参数取字典的地方都先校验过参数，不会走到这里。）"""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except KeyError as exc:
            raise RuntimeError(f"{func.__name__}: 来源数据缺少字段 {exc}，格式可能已变") from exc
    return wrapper


def _v39_count(value, what):
    """来源自报的页数 / 条数 → 非负 int。只认 int 或纯数字串；bool 抛错（int(True)=1 会让
    「只返回 1 条」通过完整性核对），其他写法也抛 RuntimeError。"""
    text = str(value).strip() if isinstance(value, (int, str)) and not isinstance(value, bool) else ""
    if not re.fullmatch(r"[0-9]+", text):
        raise RuntimeError(f"{what} 不是非负整数: {value!r}")
    return int(text)


def _v39_frame(rows, source, url, columns=None):
    """统一出表：附 source / source_url / fetched_at。rows 为空时返回带列名的空表，
    是否允许为空由调用方判断（「确实没有」与「接口坏了」要分开处理）。"""
    frame = pd.DataFrame(rows, columns=columns)
    frame["source"] = source
    frame["source_url"] = url
    frame["fetched_at"] = datetime.now(timezone.utc).isoformat()
    return frame


def _em_datacenter_strict(report_name, filter_str="", sort_columns="", sort_types="",
                          page_size=500, max_rows=5000, columns="ALL", extra=None):
    """东财 datacenter 严格版：code=0 取数据；第 1 页就 9201(返回数据为空) → []；其他错误码直接抛。

    与旧 eastmoney_datacenter() 的区别：后者把任何失败都变成 []，调用方分不清
    「这只票确实没有」和「参数写错/被风控」。sortTypes 个数必须与 sortColumns 一致，
    否则东财返回 9501「排序字段和顺序数量不一致」。
    翻页中途失败（第 2 页起 9201、空页、非末页不满页、缺 pages / count、总页数或总条数变了、
    最终条数与 count 不符）抛 RuntimeError，不把部分结果当完整结果返回。
    payload / result 不是对象、data 不是由对象组成的列表，同样抛 RuntimeError。
    只有「第 1 页、pages=1、data 为空」才算确实没有数据。
    max_rows 只在来源自报总数 count > max_rows 时提前截断；count 不超过上限的，一律走完分页并核对总数。
    """
    n_cols = len([c for c in sort_columns.split(",") if c]) if sort_columns else 0
    n_types = len([t for t in sort_types.split(",") if t]) if sort_types else 0
    if n_cols != n_types:
        raise ValueError(f"sortColumns({n_cols}) 与 sortTypes({n_types}) 个数不一致")
    rows, page, first = [], 1, None
    while True:
        params = {"reportName": report_name, "columns": columns, "filter": filter_str,
                  "pageNumber": str(page), "pageSize": str(page_size),
                  "sortColumns": sort_columns, "sortTypes": sort_types,
                  "source": "WEB", "client": "WEB"}
        params.update(extra or {})
        try:
            response = em_get(DATACENTER_URL, params=params, timeout=20)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise RuntimeError(f"东财 {report_name} 请求失败: {type(exc).__name__}: {exc}") from exc
        payload = _v39_json(response)
        if not isinstance(payload, dict):
            raise RuntimeError(f"东财 {report_name} 返回的不是 JSON 对象: {str(payload)[:100]}")
        if payload.get("code") == 9201:
            if page == 1:
                return []
            raise RuntimeError(f"东财 {report_name} 第 {page} 页返回「数据为空」，"
                               f"前面已取 {len(rows)} 条，结果不完整")
        if payload.get("code") != 0 or not payload.get("result"):
            raise RuntimeError(f"东财 {report_name} 返回错误: "
                               f"{payload.get('code')} {payload.get('message')}")
        result = payload["result"]
        if not isinstance(result, dict):
            raise RuntimeError(f"东财 {report_name} 的 result 不是对象: {str(result)[:100]}")
        pages, count, data = result.get("pages"), result.get("count"), result.get("data")
        if data is None:
            data = []
        if not isinstance(data, list) or not all(isinstance(r, dict) for r in data):
            raise RuntimeError(f"东财 {report_name} 第 {page} 页的 data 不是由对象组成的列表，格式可能已变")
        if (any(isinstance(v, bool) or not isinstance(v, int) for v in (pages, count))
                or pages < 1 or count < 0):
            raise RuntimeError(f"东财 {report_name} 缺少分页信息（pages={pages!r}, count={count!r}）")
        if first is None:
            first = (pages, count)
        elif (pages, count) != first:
            raise RuntimeError(f"东财 {report_name} 翻页时总页数 / 总条数从 {first} 变成 {(pages, count)}，"
                               "结果可能错位，请重试")
        if not data and (page > 1 or pages > 1):
            raise RuntimeError(f"东财 {report_name} 第 {page}/{pages} 页是空的，结果不完整")
        if page < pages and len(data) != int(page_size):
            raise RuntimeError(f"东财 {report_name} 第 {page}/{pages} 页只有 {len(data)} 条"
                               f"（非末页应为 {page_size} 条），结果不完整")
        rows.extend(data)
        # 只有来源自报的总数确实超过上限才提前截断；否则（count <= max_rows 却已拿到更多行）
        # 必须走完分页并核对总数，不然「data 比 count 还多」这种格式异常会被当成正常截断放过
        if len(rows) >= max_rows and count > max_rows:
            return rows[:max_rows]
        if page >= pages:
            if len(rows) != count:
                raise RuntimeError(f"东财 {report_name} 翻页后 {len(rows)} 条，与总数 {count} 不符")
            return rows
        page += 1


def _em_day(value):
    """东财日期串 '2026-09-18 00:00:00' → '2026-09-18'；空值 → None；认不出的写法抛 RuntimeError
    （只截前 10 个字符会把 '2026/09/18' 原样放行，再拿去和日期串比较就会比错）。"""
    return _v39_src_date(str(value)[:10]) if value else None

import requests
from datetime import datetime

def cninfo_irm(code: str, page_size: int=30, page_num: int=1) -> list[dict]:
    """互动易问答（巨潮，深市公司）。code: 6位代码。沪市公司实测返回 0 条，请用 §10.3 sse_e_interaction()。
    返回每条: code/company/question(投资者提问)/answer(公司回复,None=未回复)/
    answerer(回答方)/ask_time。"""
    try:
        r1 = requests.post('https://irm.cninfo.com.cn/newircs/index/queryKeyboardInfo', data={'keyWord': code}, headers={'User-Agent': UA}, timeout=10)
        d1 = r1.json().get('data') or []
        if not d1:
            return []
        org_id = d1[0].get('secid')
        params = {'_t': 1, 'stockcode': code, 'orgId': org_id, 'pageSize': page_size, 'pageNum': page_num, 'keyWord': '', 'startDay': '', 'endDay': ''}
        r2 = requests.post('https://irm.cninfo.com.cn/newircs/company/question', params=params, headers={'User-Agent': UA}, timeout=10)
        rows = r2.json().get('rows') or []
    except Exception as e:
        print(f'[WARN] 互动易请求失败: {e}')
        return []
    out = []
    for it in rows:
        pd = it.get('pubDate')
        out.append({'code': it.get('stockCode'), 'company': it.get('companyShortName'), 'question': it.get('mainContent'), 'answer': it.get('attachedContent'), 'answerer': it.get('attachedAuthor'), 'ask_time': datetime.fromtimestamp(pd / 1000).strftime('%Y-%m-%d %H:%M') if pd else ''})
    return out

import datetime as _dt
import html as _html
import re

SSE_E_BASE = "https://sns.sseinfo.com"
_SSE_CN_TZ = _dt.timezone(_dt.timedelta(hours=8))   # 「昨天 18:22」按北京时间换算，海外服务器也不跑偏
_sse_uid_cache = {}                 # 证券代码 → 上证e互动公司 uid
_sse_company_pages = {}             # 页码 → [(code, uid)]，二分查找时复用
_SSE_COMPANY_END = "没有任何上市公司的信息"
# 没有问答时的提示：公司维度「近1个月暂无回复 / 暂无提问」，全市场翻过末页「暂时没有问答内容」（2026-09-20 实测）
_SSE_EMPTY_NOTE = re.compile(r'class="m_feed_note"[^>]*>[^<]*(暂无|暂时没有)[^<]*<')


def _sse_company_page(page):
    if page not in _sse_company_pages:
        response = _v39_http(SSE_E_BASE + "/allcompany.do", method="POST",
                             data={"code": "0", "order": "2", "areaId": "0", "page": page},
                             headers={"Referer": SSE_E_BASE + "/"})
        payload = _v39_json(response)
        content = payload.get("content") if isinstance(payload, dict) else None
        if not isinstance(content, str):    # 末页之后也返回字符串（2026-09-20 实测），缺 content 是格式变了
            raise RuntimeError(f"上证e互动公司列表第 {page} 页的返回结构变了（没有 content 字符串）")
        pairs = [(code, uid) for uid, code in
                 re.findall(r"uid=['\"]?(\d+)['\"]?[^>]*>\s*<img[^>]*company/(\d{6})\.png", content)]
        # 末页之后固定返回「没有任何上市公司的信息」（2026-09-20 实测第 74 页起）。
        # 第 1 页为空、或者别的页既解析不出公司又没有这句话，只能是页面格式变了，不能说成「公司不存在」
        if not pairs and (page == 1 or _SSE_COMPANY_END not in content):
            raise RuntimeError(f"上证e互动公司列表第 {page} 页解析出 0 家公司，页面格式可能已变")
        _sse_company_pages[page] = pairs
        for code, uid in pairs:
            _sse_uid_cache[code] = uid
    return _sse_company_pages[page]


def _sse_company_uid(code):
    """上证e互动按公司 uid 查询；公司列表按代码升序分页（每页 32 家），倍增 + 二分定位。"""
    if code in _sse_uid_cache:
        return _sse_uid_cache[code]
    low, high = 1, 1
    while _sse_company_page(high):      # 先倍增找到末页之后的空页
        if _sse_company_page(high)[-1][0] >= code:
            break
        low, high = high, high * 2
    while low <= high:
        mid = (low + high) // 2
        pairs = _sse_company_page(mid)
        if not pairs or code < pairs[0][0]:
            high = mid - 1
        elif code > pairs[-1][0]:
            low = mid + 1
        else:
            break
    if code not in _sse_uid_cache:
        raise ValueError(f"上证e互动没有 {code}（公司不在上交所，或已退市）")
    return _sse_uid_cache[code]


def _sse_text(fragment):
    return _html.unescape(re.sub(r"<[^>]+>", "", fragment)).strip()


# 站点时间两种写法并存（2026-10-23 实测）：提问是绝对时间「2026年10月21日 17:47」，
# 1 天内的回复是相对时间「昨天 18:22」。只认绝对写法会让整页解析直接抛错。
_SSE_ABS_TIME = re.compile(r"(\d{4})\s*[年\-/]\s*(\d{1,2})\s*[月\-/]\s*(\d{1,2})\s*日?\s*(\d{1,2}):(\d{2})")
_SSE_DAY_WORD_TIME = re.compile(r"^(今天|昨天|前天)\s*(\d{1,2}):(\d{2})$")
_SSE_MONTH_DAY_TIME = re.compile(r"^(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})$")
_SSE_AGO_TIME = re.compile(r"^(\d+)\s*(秒|分钟|小时|天)前$")
_SSE_CLOCK_TIME = re.compile(r"^(\d{1,2}):(\d{2})$")
_SSE_DAY_WORD = {"今天": 0, "昨天": 1, "前天": 2}
_SSE_AGO_UNIT = {"秒": "seconds", "分钟": "minutes", "小时": "hours", "天": "days"}


def _sse_time(text, now=None):
    """把站点的时间文案换算成「YYYY-MM-DD HH:MM」；认不出返回 None，由调用方决定是否报错。

    相对写法按北京时间算；now 可注入，便于离线测试固定日期。
    """
    text = (text or "").strip()
    now = (now or _dt.datetime.now(_SSE_CN_TZ)).replace(tzinfo=None)
    match = _SSE_ABS_TIME.search(text)
    if match:
        year, month, day, hour, minute = (int(group) for group in match.groups())
        try:
            return _dt.datetime(year, month, day, hour, minute).strftime("%Y-%m-%d %H:%M")
        except ValueError:      # 2 月 30 日这类不存在的日期算认不出，不抛 ValueError 给上层
            return None
    match = _SSE_DAY_WORD_TIME.match(text)
    if match:
        when = now - _dt.timedelta(days=_SSE_DAY_WORD[match.group(1)])
        try:
            return when.replace(hour=int(match.group(2)), minute=int(match.group(3)),
                                second=0, microsecond=0).strftime("%Y-%m-%d %H:%M")
        except ValueError:      # 非法时分算认不出
            return None
    match = _SSE_MONTH_DAY_TIME.match(text)
    if match:
        month, day, hour, minute = (int(group) for group in match.groups())
        try:
            when = _dt.datetime(now.year, month, day, hour, minute)
        except ValueError:
            return None
        if when - now > _dt.timedelta(days=1):   # 跨年：1 月里看到「12-31」是去年的
            try:
                when = when.replace(year=now.year - 1)
            except ValueError:  # 去年可能没有 2 月 29 日
                return None
        return when.strftime("%Y-%m-%d %H:%M")
    match = _SSE_AGO_TIME.match(text)
    if match:
        try:
            delta = _dt.timedelta(**{_SSE_AGO_UNIT[match.group(2)]: int(match.group(1))})
            return (now - delta).strftime("%Y-%m-%d %H:%M")
        except (OverflowError, ValueError):  # N 过大算认不出
            return None
    if text in ("刚刚", "刚才"):
        return now.strftime("%Y-%m-%d %H:%M")
    match = _SSE_CLOCK_TIME.match(text)    # 只有时分 = 今天
    if match:
        try:
            return now.replace(hour=int(match.group(1)), minute=int(match.group(2)),
                               second=0, microsecond=0).strftime("%Y-%m-%d %H:%M")
        except ValueError:      # 非法时分算认不出
            return None
    return None


def _sse_required_time(item_id, text):
    """问答时间必须解析出来：认不出还照常返回，就是把「时间格式变了」变成了一列 None。"""
    when = _sse_time(text)
    if when is None:
        raise RuntimeError(f"上证e互动第 {item_id} 条的提问时间认不出: {text!r}")
    return when


def _sse_parse_feed(text):
    """「最新回复」与「最新提问」两种列表的标记不同（问题框有没有 id），
    所以不靠 id 区分问答，而是以回复块 class="m_feed_detail m_qa" 为界切成问题段和回复段。"""
    rows = []
    for chunk in re.split(r'<div class="m_feed_item[^"]*" id="item-', text)[1:]:
        numbered = re.match(r"(\d+)", chunk)
        if not numbered:
            raise RuntimeError(f"上证e互动条目 id 不是数字，页面结构可能已变: {chunk[:60]}")
        item_id = numbered.group(1)
        # 注意全市场列表的问题块 class 是 "m_feed_detail m_qa_detail"，只能按完整 class 值切
        ask_part, _, answer_part = chunk.partition('class="m_feed_detail m_qa"')
        question = re.search(r'<div class="m_feed_txt"[^>]*>\s*<a[^>]*>:(.*?)\((\d{6})\)</a>(.*?)</div>',
                             ask_part, re.S)
        asker = re.search(r'rel="face"[^>]*?title="([^"]*)"', ask_part, re.S)
        ask_time = re.search(r'<div class="m_feed_from"[^>]*>\s*<span>([^<]+)</span>', ask_part)
        if not question or not ask_time:
            raise RuntimeError(f"上证e互动第 {item_id} 条结构改变，无法解析问题或时间")
        answer = answer_time = None
        if answer_part:
            body = re.search(r'<div class="m_feed_txt"[^>]*>(.*?)</div>', answer_part, re.S)
            when = re.search(r'<div class="m_feed_from"[^>]*>\s*<span>([^<]+)</span>', answer_part)
            if not body or not when:
                raise RuntimeError(f"上证e互动第 {item_id} 条有回复块但解析不出回复内容或回复时间")
            answer, answer_time = _sse_text(body.group(1)), _sse_time(when.group(1))
            if answer_time is None:     # 实测 500 条问答（含 250 条回复）时间字段无一缺失
                raise RuntimeError(f"上证e互动第 {item_id} 条的回复时间认不出: {when.group(1)!r}")
        rows.append({"id": item_id, "code": question.group(2), "name": _sse_text(question.group(1)),
                     "asker": asker.group(1) if asker else None,
                     "question": _sse_text(question.group(3)),
                     "question_time": _sse_required_time(item_id, ask_time.group(1)),
                     "answer": answer, "answer_time": answer_time})
    return rows


_SSE_KIND = {"answered": 11, "questions": 10}


@_v39_contract
def sse_e_interaction(code=None, kind="answered", page=1, page_size=10):
    """上证e互动 — 投资者提问与沪市上市公司回复（上交所官方平台）。

    code=None 看全市场，给沪市代码（60/68/900 开头）只看该公司。
    kind='answered'：最新已回复问答；kind='questions'：最新提问（含未回复，answer 为 None）。
    平台只开放近期问答：公司维度实测约近 1 个月，更早的翻页为空。
    §10.1 巨潮互动易实测对沪市返回 0 条（2026-09-20，600519/600000/688981），沪市问答只能走本函数。
    首次查某家公司要先在公司列表里定位 uid（倍增 + 二分，约 10–13 次请求），之后走缓存。
    """
    if kind not in _SSE_KIND:
        raise ValueError("kind 只能是 'answered' 或 'questions'")
    if int(page) < 1 or not 1 <= int(page_size) <= 50:
        raise ValueError("page 从 1 开始，page_size 范围 1–50")
    if code is None:
        response = _v39_http(SSE_E_BASE + "/ajax/feeds.do",
                             params={"type": _SSE_KIND[kind], "pageSize": int(page_size), "lastid": -1,
                                     "show": 1, "page": int(page)},
                             headers={"Referer": SSE_E_BASE + "/"})
    else:
        digits = norm_ticker(code, stock_only=True)
        if get_prefix(code) != "sh":
            raise ValueError(f"{code} 不是沪市证券；深市互动问答请用 §10.1 cninfo_irm")  # 北交所两处都没有
        response = _v39_http(SSE_E_BASE + "/ajax/userfeeds.do", method="POST",
                             data={"typeCode": "company", "type": _SSE_KIND[kind], "pageSize": int(page_size),
                                   "uid": _sse_company_uid(digits), "page": int(page)},
                             headers={"Referer": SSE_E_BASE + "/"})
    text = response.content.decode("utf-8", "replace")
    rows = _sse_parse_feed(text)
    # 只有带明确的「暂无 / 暂时没有」提示才算真没有问答；解析出 0 条又没有这句提示，是页面结构变了
    if not rows and not _SSE_EMPTY_NOTE.search(text):
        raise RuntimeError("上证e互动返回的页面既没有问答也没有「暂无」提示，结构可能已变")
    if code is not None and any(r["code"] != digits for r in rows):
        raise RuntimeError("上证e互动返回了其他公司的问答，uid 映射可能已变")
    return _v39_frame(rows, "sse_e", response.url,
                      ["id", "code", "name", "asker", "question", "question_time",
                       "answer", "answer_time"])
