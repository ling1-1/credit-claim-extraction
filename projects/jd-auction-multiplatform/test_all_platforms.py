"""测试三个新平台的 API 可访问性"""
import sys
import os
import json
sys.path.insert(0, r'f:\codex_project\jd')
os.environ.setdefault('PYTHONIOENCODING', 'utf-8')

import requests

HEADERS_COMMON = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

def safe_print(msg):
    msg = str(msg).replace('\u200c', '').replace('\u200d', '')
    print(msg)

def test_url(name, url, headers=None, params=None, is_json=False):
    h = {**HEADERS_COMMON, **(headers or {})}
    try:
        if params:
            r = requests.get(url, params=params, headers=h, timeout=15)
        else:
            r = requests.get(url, headers=h, timeout=15)
        ct = r.headers.get('content-type', '')
        safe_print(f"\n[{name}] Status={r.status_code} CT={ct[:60]}")
        
        if r.status_code == 200:
            if 'json' in ct or is_json:
                d = r.json()
                safe_print(f"  JSON keys: {list(d.keys())[:20]}")
                for key in ['data', 'result', 'records', 'list', 'items']:
                    if key in d:
                        val = d[key]
                        if isinstance(val, dict):
                            safe_print(f"  data.{key} keys: {list(val.keys())[:20]}")
                            for sk in ['total', 'count', 'records', 'list', 'items']:
                                if sk in val:
                                    sv = val[sk]
                                    safe_print(f"  .{sk}: {sv if not isinstance(sv, list) else f'len={len(sv)}'}")
                        elif isinstance(val, list):
                            safe_print(f"  data.{key} len={len(val)}")
                return True, d
            else:
                text_len = len(r.text)
                safe_print(f"  HTML len={text_len}")
                if text_len < 5000:
                    safe_print(f"  Content: {r.text[:400]}")
                else:
                    import re
                    tables = re.findall(r'<table', r.text, re.IGNORECASE)
                    safe_print(f"  Tables: {len(tables)}, tr: {len(re.findall(r'<tr', r.text, re.IGNORECASE))}")
                return True, r.text
        else:
            safe_print(f"  Body: {r.text[:200]}")
            return False, None
    except Exception as e:
        safe_print(f"  Error: {e}")
        return False, None

# ===== TPRE List API =====
safe_print("\n" + "="*60 + "\n1. TPRE (天津) - List API\n" + "="*60)
tpre_h = {
    "accept": "application/json, text/plain, */*",
    "systemcode": "PROPERTY_RIGHT_TRANSFER_WEB",
    "uniflowsystemcode": "INFORMATIONIZE",
}
ok, data = test_url("TPRE-List", "https://trade.tpre.cn/up/biz/project/anmuas/equity-trading/page",
         headers=tpre_h, params={"current": 1, "size": 2}, is_json=True)

# ===== PreChina URLs =====
safe_print("\n" + "="*60 + "\n2. PreChina (贵州)\n" + "="*60)
prechina_tests = [
    ("首页", "https://www.prechina.net/"),
    ("ejygg首页", "https://www.prechina.net/ejygg/index.jhtml"),
]
for name, url in prechina_tests:
    test_url(name, url)

# ===== GXCQ URLs =====
safe_print("\n" + "="*60 + "\n3. GXCQ (广西)\n" + "="*60)
gxcq_tests = [
    ("首页", "https://www.gxcq.com.cn/"),
]
for name, url in gxcq_tests:
    test_url(name, url)

# ===== More API guesses =====
safe_print("\n" + "="*60 + "\n4. API Guesses\n" + "="*60)
api_tests = [
    ("GXCQ-api-list", "https://www.gxcq.com.cn/api/project/list", {"pageIndex": 1, "pageSize": 5}),
    ("PreChina-ejygg-List", "https://www.prechina.net/ejygg/zczr/list.jhtml", {"pageNo": 1}),
]
for name, url, params in api_tests:
    test_url(name, url, params=params)

safe_print("\nDone!")
