# TVBox 多仓 -> 单配置聚合器
# 数据全部来自远程不可信 JSON：仅做解析/合并，不执行其中任何内容。
import sys, json, gzip, re, encodings.idna, urllib.request, urllib.error, ssl
from urllib.parse import urljoin, urlsplit, urlunsplit
from concurrent.futures import ThreadPoolExecutor
from collections import OrderedDict

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SEED = sys.argv[1] if len(sys.argv) > 1 else "seed.json"
OUT  = sys.argv[2] if len(sys.argv) > 2 else "aggregate.json"
STAT = sys.argv[3] if len(sys.argv) > 3 else "stats.txt"

UA = "Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36 (KHTML, like Gecko) TVBox"
ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE

def ascii_url(u):
    """非 ASCII 域名走 IDNA，非 ASCII 路径/查询 percent-encode"""
    if "://" not in u: return u
    scheme, rest = u.split("://", 1)
    hostpart = rest.split("/", 1)[0]
    pathq = rest[len(hostpart):]
    try:
        hostpart.encode("ascii")
    except UnicodeEncodeError:
        if ":" in hostpart and not hostpart.endswith("]"):
            host, port = hostpart.rsplit(":", 1)
        else:
            host, port = hostpart, ""
        host = ".".join(encodings.idna.ToASCII(x).decode() for x in host.split("."))
        hostpart = host + ((":" + port) if port else "")
    sp = urlsplit(pathq)
    pathq = urlunsplit((
        "", "",
        urllib.request.quote(sp.path, safe="/%"),
        urllib.request.quote(sp.query, safe="=&?/%+:;,@!$'()*[],~-._"),
        urllib.request.quote(sp.fragment, safe=""),
    ))
    return scheme + "://" + hostpart + pathq

PROXY_PREFIXES = (
    "https://gh-proxy.com/", "https://gh-proxy.org/", "https://cdn.gh-proxy.org/",
    "https://ghproxy.net/", "https://ghproxy.cn/", "https://github.moeyy.xyz/",
    "https://gh.jasonzeng.dev/",
)

def candidates(u):
    """代理 URL 同时回退直连（GitHub Actions 在海外直连更稳）"""
    out = [u]
    for p in PROXY_PREFIXES:
        if u.startswith(p):
            inner = u[len(p):]
            if inner.startswith("http://") or inner.startswith("https://"):
                inner = re.sub(r"^(https?)/+", r"\1://", inner)
            elif inner.startswith(("raw.githubusercontent.com/", "github.com/", "gitlab.com/")):
                inner = "https://" + inner
            if inner != u and inner not in out:
                out.append(inner)
            break
    return out

def fetch(u, timeout=18):
    req = urllib.request.Request(ascii_url(u), headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        raw = r.read()
        final = r.geturl()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return raw.decode("utf-8-sig", "replace"), final

def sanitize(s):
    """去掉 JSON 外/内部的 // 整行注释，修尾逗号"""
    s = re.sub(r'(?m)^\s*//.*$', '', s)
    s = re.sub(r',\s*([}\]])', r'\1', s)
    return s

def parse_doc(txt, base_url):
    """返回 ('leaf', dict) | ('multi', [urls]) | ('html', [候选url]) | None"""
    s = sanitize(txt.lstrip("﻿ \t\r\n"))
    i = s.find("{")
    if i < 0:
        cands = html_candidates(txt)
        return ("html", cands) if cands else None
    try:
        d, _ = json.JSONDecoder().raw_decode(s[i:])
    except Exception:
        cands = html_candidates(txt)
        return ("html", cands) if cands else None
    if not isinstance(d, dict):
        return None
    if isinstance(d.get("sites"), list):
        return ("leaf", d)
    urls = []
    if isinstance(d.get("urls"), list):
        for it in d["urls"]:
            if isinstance(it, dict) and it.get("url"):
                urls.append((str(it.get("name") or ""), str(it["url"])))
    sh = d.get("storeHouse")
    if isinstance(sh, list):
        for grp in sh:
            if isinstance(grp, dict) and isinstance(grp.get("sites"), list):
                for it in grp["sites"]:
                    if isinstance(it, dict) and it.get("url"):
                        urls.append((str(it.get("name") or ""), str(it["url"])))
            elif isinstance(grp, dict) and grp.get("url"):
                urls.append((str(grp.get("name") or ""), str(grp["url"])))
    if urls:
        return ("multi", urls)
    if isinstance(d.get("sites"), list) is False and any(k in d for k in ("spider", "lives", "parses")):
        return ("leaf", d)
    return None

def html_candidates(html):
    import re
    out = []
    for m in re.finditer(r'https?://[^\s"\'<>\\]+', html):
        v = m.group(0)
        low = v.lower()
        if low.endswith((".json", ".bmp", ".txt")) or "/raw/" in low or "provide/tvbox" in low:
            if "imagehub" not in low and v not in out:
                out.append(v)
    return out[:6]

def norm_ext(e):
    if isinstance(e, (dict, list)):
        return json.dumps(e, sort_keys=True, ensure_ascii=False)
    if isinstance(e, str):
        t = e.strip()
        if t[:1] in "{[":
            try: return json.dumps(json.loads(t), sort_keys=True, ensure_ascii=False)
            except Exception: return t
        return t
    return "" if e is None else str(e)

def site_id(site, jar):
    return json.dumps([
        str(site.get("type", "")), str(site.get("api", "")), norm_ext(site.get("ext")),
        (jar or "").split(";md5")[0], str(site.get("playUrl", "")),
        str(site.get("searchable", "")), str(site.get("quickSearch", "")),
        str(site.get("filterable", "")),
    ], ensure_ascii=False)

def merge():
    seed = json.load(open(SEED, encoding="utf-8"))
    def is_http(u):
        return isinstance(u, str) and re.match(r"^https?://\S+\.\S+", u.strip())
    frontier = [(str(x.get("name") or "").strip(), str(x["url"]).strip())
                for x in seed["urls"] if is_http(str(x.get("url", "")))]
    seen = set()
    leaves = []   # (leaf_dict, final_url, repo_name)
    failures = []
    html_hits = []

    def work(item):
        name, u = item
        last_err = "unparseable"
        tried = set()
        for cu in candidates(u):
            if cu in tried:
                continue
            tried.add(cu)
            try:
                txt, final = fetch(cu)
                doc = parse_doc(txt, final)
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
                continue
            if doc is None:
                continue
            kind, val = doc
            if kind == "leaf":
                return ("leaf", (val, final, name))
            if kind == "multi":
                return ("multi", [(n, uu if uu.startswith("http") else urljoin(final, uu)) for n, uu in val])
            if kind == "html":
                for hc in val:
                    if hc in (
                        "https://gh-proxy.net/https://raw.githubusercontent.com/yoursmile66/TVBox/refs/heads/main/XC.json",
                    ):
                        continue
                    for hc2 in candidates(hc):
                        try:
                            t2, f2 = fetch(hc2)
                            d2 = parse_doc(t2, f2)
                        except Exception:
                            continue
                        if d2 and d2[0] in ("leaf", "multi"):
                            html_hits.append((name, hc2))
                            if d2[0] == "leaf":
                                return ("leaf", (d2[1], f2, name + "[html]"))
                            return ("multi", [(n, uu if uu.startswith("http") else urljoin(f2, uu))
                                              for n, uu in d2[1]])
        return ("fail", (name, u, last_err))

    rounds = 0
    while frontier and rounds < 8:
        rounds += 1
        todo = []
        for n, u in frontier:
            cu = u.rstrip("/")
            if cu in seen:
                continue
            seen.add(cu)
            todo.append((n, u))
        frontier = []
        with ThreadPoolExecutor(max_workers=8) as ex:
            for status, payload in ex.map(work, todo):
                if status == "leaf":
                    leaves.append(payload)
                elif status == "multi":
                    for n, u in payload:
                        if is_http(u) and u.rstrip("/") not in seen:
                            frontier.append((n, u))
                else:
                    failures.append(payload)

    sites = OrderedDict()
    parses, parses_ids = [], set()
    lives, lives_ids = [], set()
    first_extras = {}
    leaf_stats = []

    for leaf, base, repo in leaves:
        spider = leaf.get("spider")
        if isinstance(spider, str) and spider and not spider.startswith("http"):
            spider = urljoin(base, spider)
        added = 0
        for s in leaf.get("sites") or []:
            if not isinstance(s, dict) or not s.get("api") or s.get("type") in (None, ""):
                continue
            s = dict(s)
            jar = s.get("jar") or spider or ""
            if jar and not str(jar).startswith("http"):
                jar = urljoin(base, str(jar))
            s["jar"] = jar
            api = s["api"]
            if not str(api).startswith("http") and str(api).startswith("/"):
                s["api"] = urljoin(base, str(api))
            sid = site_id(s, jar)
            if sid in sites:
                continue
            sites[sid] = s
            added += 1
        leaf_stats.append((repo, len(leaf.get("sites") or []), added))
        for p in leaf.get("parses") or []:
            if isinstance(p, dict):
                pid = json.dumps([p.get("name"), p.get("url"), p.get("type")], ensure_ascii=False)
                if pid not in parses_ids:
                    parses_ids.add(pid); parses.append(p)
        for lv in leaf.get("lives") or []:
            if isinstance(lv, dict):
                lid = json.dumps([lv.get("name"), lv.get("type"), str(lv.get("url", ""))[:200]], ensure_ascii=False)
                if lid not in lives_ids:
                    lives_ids.add(lid); lives.append(lv)
        for k in ("wallpaper", "logo", "flags", "rules", "danmaku"):
            if k not in first_extras and leaf.get(k):
                first_extras[k] = leaf[k]

    # key 唯一化
    used, out_sites = set(), []
    auto = 0
    for s in sites.values():
        k = str(s.get("key") or "").strip()
        if not k:
            auto += 1; k = f"agg{auto}"
        if k in used:
            n = 2
            while f"{k}__{n}" in used:
                n += 1
            k = f"{k}__{n}"
        used.add(k)
        s["key"] = k
        out_sites.append(s)

    config = {"sites": out_sites}
    if parses:
        config["parses"] = parses
    if lives:
        config["lives"] = lives
    for k, v in first_extras.items():
        config[k] = v

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, separators=(",", ":"))

    lines = []
    lines.append(f"leaves={len(leaves)} sites={len(out_sites)} parses={len(parses)} lives={len(lives)} failures={len(failures)}")
    lines.append("--- per leaf (repo, raw_sites, new_added) ---")
    for r, raw, add in leaf_stats:
        lines.append(f"{add:5d} / {raw:5d}  {r}")
    lines.append("--- html fallback hits ---")
    for n, c in html_hits:
        lines.append(f"{n} -> {c}")
    lines.append("--- failures ---")
    for n, u, e in failures:
        lines.append(f"{n} | {u} | {e}")
    import os
    lines.append(f"--- size {os.path.getsize(OUT)} bytes ---")
    open(STAT, "w", encoding="utf-8").write("\n".join(lines))

if __name__ == "__main__":
    merge()
