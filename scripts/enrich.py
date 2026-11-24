"""M4-3①：S・Aの話題に「本文・写真」を付ける（毎朝の処理 daily.py から呼ぶ）。

流れ（M4-1.5 の検証結果で決めた仕様）:
  1) 各記事の元URLを復元（Googleニュース経由のみ）→ 本文抽出（trafilatura, favor_precision）→ og:image
     1話題で本文は最大2本。400字以上＝ok、100〜399字＝partial（30秒の材料だけに使う）
  2) 本文が ok にならなかった話題は、話題の見出し（無ければAI要約）でGoogleニュースを再検索（when:7d）し、
     除外媒体でも元と同じ媒体でもなく、見出しの一致度0.45以上の記事だけを本文に使う（候補は最大4件。キーワード検索はしない）
  3) 補った記事が別の話題の記事と同じなら、2つの話題を1つにまとめる
  4) 失敗しても毎朝の更新は止めない。必要なライブラリが無い・元URLの復元が全滅した時は、見出しモード（今までの動き）で続行する

公開リポジトリなので、本文そのものは保存しない。本文は戻り値（メモリ上）だけで要約に渡す。
話題に付ける項目：image（写真のURL）、body（ok／partial／none）、refill（補った記事の媒体・見出し・URL）
"""
import re
import time
from urllib.parse import quote_plus, urlparse

UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1")
BODY_OK = 400
BODY_PARTIAL = 100
MAX_BODIES = 2          # 1話題で使う本文の数
MAX_TRY_ARTICLES = 4    # 1話題で本文を取りに行く記事の数
REFILL_SIM = 0.45       # 補完に使う見出しの一致度
REFILL_MAX_CAND = 4
TIME_BUDGET = 15 * 60   # 本文取得全体の時間上限（秒）。超えたら残りは見出しモード
BLOCKED = {"zeiken.co.jp", "netkeizai.com", "news.nicovideo.jp", "bloomberg.com", "bloomberg.co.jp", "nikkei.com",
           "business.nikkei.com", "excite.co.jp", "ryukyushimpo.jp", "finance.yahoo.co.jp", "abema.tv", "nna.jp"}


# ---------------------------------------------------------------- 文字の処理（verify_refill.py と同じ）
def clean(t: str) -> str:
    t = re.sub(r"（[^）]{1,20}）$|\([^)]{1,20}\)$", "", t or "")
    t = re.sub(r"[【\[].*?[】\]]", " ", t)
    t = re.sub(r"[「」『』“”\"!！?？、。・:：|｜]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def bigrams(t: str):
    t = re.sub(r"\s", "", t)
    return {t[i:i + 2] for i in range(len(t) - 1)}


def similar(a: str, b: str) -> float:
    A, B = bigrams(a), bigrams(b)
    return round(len(A & B) / max(1, min(len(A), len(B))), 2)


def og_image(html: str):
    for pat in (r'<meta[^>]+property=["\']og:image(?::url)?["\'][^>]+content=["\']([^"\']+)',
                r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image',
                r'<meta[^>]+name=["\']twitter:image(?::src)?["\'][^>]+content=["\']([^"\']+)'):
        m = re.search(pat, html, re.I)
        if m and m.group(1).startswith("https://"):
            return m.group(1).replace("&amp;", "&")
    return ""


def domain(url: str) -> str:
    return urlparse(url).netloc.replace("www.", "")


# ---------------------------------------------------------------- 1記事の取得
class Fetcher:
    def __init__(self, log):
        import requests  # noqa: F401  （無ければ ImportError → 見出しモード）
        import trafilatura  # noqa: F401
        from googlenewsdecoder import GoogleDecoder
        self.log = log
        self.dec = GoogleDecoder(timeout=15, user_agent=UA)
        self.decode_ok = 0
        self.decode_ng = 0
        self.dead = False  # 復元が続けて全滅したら True（Googleの仕組み変更とみなす）

    def close(self):
        try:
            self.dec.close()
        except Exception:  # noqa: BLE001
            pass

    def resolve(self, url):
        if "news.google.com" not in url:
            return url
        if self.dead:
            return ""
        try:
            r = self.dec.decode_google_news_url(url, interval=1)
            if r.get("success"):
                self.decode_ok += 1
                return r["decoded_url"]
        except Exception:  # noqa: BLE001
            pass
        self.decode_ng += 1
        if self.decode_ok == 0 and self.decode_ng >= 6:
            self.dead = True
            self.log("enrich: Googleニュースの元URL復元が6回続けて失敗。以降は見出しモードで続行します")
        return ""

    def fetch(self, url):
        """戻り値 {url, domain, chars, body, image, error}。本文は保存しない。"""
        import requests
        import trafilatura
        rec = {"url": "", "domain": "", "chars": 0, "body": "", "image": "", "error": ""}
        try:
            real = self.resolve(url)
            if not real:
                rec["error"] = "復元失敗"
                return rec
            rec["url"], rec["domain"] = real, domain(real)
            if rec["domain"] in BLOCKED:
                rec["error"] = "除外媒体"
            resp = requests.get(real, headers={"User-Agent": UA, "Accept-Language": "ja"}, timeout=15)
            if resp.status_code >= 400:
                rec["error"] = f"HTTP {resp.status_code}"
                return rec
            resp.encoding = resp.apparent_encoding if resp.encoding in (None, "ISO-8859-1") else resp.encoding
            html = resp.text
            rec["image"] = og_image(html)
            if rec["error"]:  # 除外媒体：写真だけ使い、本文（冒頭のみ等）は使わない
                return rec
            body = trafilatura.extract(html, url=real, favor_precision=True, include_comments=False,
                                       include_tables=False) or ""
            rec["body"], rec["chars"] = body, len(body)
        except Exception as e:  # noqa: BLE001
            rec["error"] = type(e).__name__
        return rec

    def search(self, query):
        import feedparser
        url = f"https://news.google.com/rss/search?q={quote_plus(query + ' when:7d')}&hl=ja&gl=JP&ceid=JP:ja"
        p = feedparser.parse(url, agent=UA)
        out = []
        for e in p.entries[:15]:
            title = re.sub(r"\s+-\s+[^-]+$", "", e.get("title", ""))
            src = (e.get("source") or {}).get("href", "") if isinstance(e.get("source"), dict) else ""
            out.append({"title": title, "url": e.get("link", ""), "src_domain": domain(src) if src else ""})
        return out


# ---------------------------------------------------------------- 話題ごとの処理
def _enrich_story(s, f, log):
    texts = []   # [(見出し, 媒体, 本文)]
    partial = None
    tried = 0
    for a in s["articles"]:
        if len(texts) >= MAX_BODIES or tried >= MAX_TRY_ARTICLES or f.dead:
            break
        tried += 1
        r = f.fetch(a["url"])
        if r["url"] and r["url"] != a["url"]:
            a["src_url"] = r["url"]
        if r["image"] and not s.get("image"):
            s["image"] = r["image"]
        if r["chars"] >= BODY_OK:
            texts.append((a["title"], a.get("source", ""), r["body"]))
        elif r["chars"] >= BODY_PARTIAL and (partial is None or r["chars"] > len(partial[2])):
            partial = (a["title"], a.get("source", ""), r["body"])
        time.sleep(0.3)
    s["body"] = "ok" if texts else ("partial" if partial else "none")
    return texts, partial


def _refill(s, f, log, own_domains):
    lead = s["articles"][0]
    base = clean(lead["title"])
    queries = [base[:60]]
    if s.get("summary") and clean(s["summary"]) != queries[0]:
        queries.append(clean(s["summary"])[:40])
    seen = {a["url"] for a in s["articles"]}
    tried = 0
    for q in queries:
        try:
            found = f.search(q)
        except Exception as e:  # noqa: BLE001
            log(f"enrich: search {type(e).__name__}")
            found = []
        time.sleep(1.5)
        for c in found:
            if tried >= REFILL_MAX_CAND:
                return None
            if c["url"] in seen or c["src_domain"] in BLOCKED or (c["src_domain"] and c["src_domain"] in own_domains):
                continue
            sim = similar(base, clean(c["title"]))
            if sim < REFILL_SIM:
                continue
            seen.add(c["url"])
            tried += 1
            r = f.fetch(c["url"])
            if r["chars"] >= BODY_OK and r["domain"] not in BLOCKED and r["domain"] not in own_domains:
                if r["image"] and not s.get("image"):
                    s["image"] = r["image"]
                return {"source": r["domain"], "title": c["title"][:120], "url": r["url"], "sim": sim, "body": r["body"]}
            time.sleep(0.3)
    return None


def _merge(stories, bodies, log):
    """補った記事のURLが別の話題の記事（または別の話題の補完）と同じなら、1つの話題にまとめる。"""
    order = {"S": 0, "A": 1, "B": 2, "C": 3}
    owner = {}
    drop = set()
    for i, s in enumerate(stories):
        urls = [a.get("src_url") or a["url"] for a in s["articles"]]
        if s.get("refill"):
            urls.append(s["refill"]["url"])
        for u in urls:
            if i in drop:
                break
            j = owner.get(u)
            if j is None:
                owner[u] = i
                continue
            if j == i or j in drop:
                continue
            keep, gone = (j, i) if order[stories[j]["level"]] <= order[s["level"]] else (i, j)
            K, G = stories[keep], stories[gone]
            have = {a["id"] for a in K["articles"]}
            K["articles"] += [a for a in G["articles"] if a["id"] not in have]
            K["sources"] = K.get("sources", []) + [x for x in G.get("sources", []) if x not in K.get("sources", [])]
            K["lanes"] = sorted(set(K.get("lanes", [])) | set(G.get("lanes", [])))
            if not K.get("story") and G.get("story"):
                K["story"] = G["story"]
            if not K.get("image") and G.get("image"):
                K["image"] = G["image"]
            rank = {"ok": 0, "partial": 1, "none": 2}
            if rank.get(G.get("body"), 2) < rank.get(K.get("body"), 2):
                K["body"] = G["body"]
            bk, bg = bodies.get(K["key"], {}), bodies.pop(G["key"], {})
            if bg:
                bk["texts"] = (bk.get("texts") or []) + [t for t in bg.get("texts") or []
                                                         if t not in (bk.get("texts") or [])][:MAX_BODIES]
                bk["partial"] = bk.get("partial") or bg.get("partial")
                bodies[K["key"]] = bk
            drop.add(gone)
            log(f"enrich: 同じ記事を補ったため話題をまとめました「{K.get('story') or K['summary'][:20]}」←「{G.get('story') or G['summary'][:20]}」")
            for u2 in urls:
                owner[u2] = keep
    return [s for i, s in enumerate(stories) if i not in drop]


def enrich(stories, log):
    """S・Aの話題に本文・写真を付ける。戻り値 (stories, bodies, stats)。bodies は {key: {"texts":[...], "partial":...}}。"""
    stats = {"mode": "headline", "stories": 0, "ok": 0, "partial": 0, "none": 0, "refilled": 0, "image": 0,
             "merged": 0, "secs": 0}
    bodies = {}
    t0 = time.time()
    try:
        f = Fetcher(log)
    except Exception as e:  # noqa: BLE001  （ライブラリが無い等）
        log(f"enrich: 本文取得を使えないため見出しモードで続行します（{type(e).__name__}）")
        return stories, bodies, stats
    stats["mode"] = "body"
    targets = [s for s in stories if s["level"] in ("S", "A")]
    targets.sort(key=lambda s: (s["level"] != "S", -len(s["articles"])))  # 時間切れになってもSを先に済ませる
    try:
        for s in targets:
            if time.time() - t0 > TIME_BUDGET or f.dead:
                s.setdefault("body", "none")
                continue
            try:
                texts, partial = _enrich_story(s, f, log)
                if s["body"] != "ok" and not f.dead and time.time() - t0 < TIME_BUDGET:
                    own = {domain(a.get("src_url") or a["url"]) for a in s["articles"]}
                    r = _refill(s, f, log, own)
                    if r:
                        texts = [(r["title"], r["source"], r.pop("body"))]
                        s["refill"] = r
                        s["body"] = "ok"
                bodies[s["key"]] = {"texts": texts, "partial": partial}
            except Exception as e:  # noqa: BLE001  （1話題の失敗で全体を止めない）
                log(f"enrich: {type(e).__name__} 「{s['summary'][:20]}」")
                s.setdefault("body", "none")
        if f.dead and f.decode_ok == 0:
            stats["mode"] = "headline"
        before = len(stories)
        stories = _merge(stories, bodies, log)
        stats["merged"] = before - len(stories)
    finally:
        f.close()
    sa = [s for s in stories if s["level"] in ("S", "A")]
    stats.update({"stories": len(sa), "ok": sum(s.get("body") == "ok" for s in sa),
                  "partial": sum(s.get("body") == "partial" for s in sa), "none": sum(s.get("body", "none") == "none" for s in sa),
                  "refilled": sum(bool(s.get("refill")) for s in sa), "image": sum(bool(s.get("image")) for s in sa),
                  "decode_ok": f.decode_ok, "decode_ng": f.decode_ng, "secs": round(time.time() - t0)})
    log(f"enrich: S・A {stats['stories']}話題 本文ok {stats['ok']}／冒頭のみ {stats['partial']}／なし {stats['none']}"
        f"（補完 {stats['refilled']}・まとめ {stats['merged']}）写真 {stats['image']} {stats['secs']}秒")
    return stories, bodies, stats
