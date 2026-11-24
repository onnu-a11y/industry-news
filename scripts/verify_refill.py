"""M4-1.5 追加検証：本文が取れなかった話題を、話題の見出しでGoogleニュースを検索し直し、別の無料記事で補えるかを調べる。

前提：先に scripts/verify.py を実行して data/verify/result.json があること。AIは使わない。
公開リポジトリなので本文そのものは保存しない（文字数・媒体・候補の見出しだけ残す）。

使い方:
  python scripts/verify_refill.py
出力:
  data/verify/refill.json / data/verify/refill.md
"""
import json
import pathlib
import re
import sys
import time
from urllib.parse import quote_plus, urlparse

import feedparser

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import verify  # noqa: E402
from googlenewsdecoder import GoogleDecoder  # noqa: E402

ROOT = verify.ROOT
OUT = ROOT / "data" / "verify"
PRIORITY_TAGS = {"制度・法令", "税・価格", "競合"}
# 前回の検証で本文が取れなかった媒体（会員制・拒否）。候補から外す
BLOCKED = {"zeiken.co.jp", "netkeizai.com", "news.nicovideo.jp", "bloomberg.com", "nikkei.com", "business.nikkei.com",
           "excite.co.jp", "ryukyushimpo.jp", "finance.yahoo.co.jp", "abema.tv", "nna.jp"}
MAX_CAND = 4


def clean(t: str) -> str:
    t = re.sub(r"（[^）]{1,20}）$|\([^)]{1,20}\)$", "", t)          # 末尾の（媒体名）
    t = re.sub(r"[【\[].*?[】\]]", " ", t)                          # 【速報】など
    t = re.sub(r"[「」『』“”\"!！?？、。・:：|｜]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def bigrams(t: str):
    t = re.sub(r"\s", "", t)
    return {t[i:i + 2] for i in range(len(t) - 1)}


def similar(a: str, b: str) -> float:
    A, B = bigrams(a), bigrams(b)
    return round(len(A & B) / max(1, min(len(A), len(B))), 2)


def search(query: str):
    url = f"https://news.google.com/rss/search?q={quote_plus(query + ' when:7d')}&hl=ja&gl=JP&ceid=JP:ja"
    p = feedparser.parse(url, agent=verify.UA)
    out = []
    for e in p.entries[:15]:
        title = re.sub(r"\s+-\s+[^-]+$", "", e.get("title", ""))
        src = (e.get("source") or {}).get("href", "") if isinstance(e.get("source"), dict) else ""
        out.append({"title": title, "url": e.get("link", ""), "src_domain": urlparse(src).netloc.replace("www.", "")})
    return out


def main():
    res = json.loads((OUT / "result.json").read_text(encoding="utf-8"))
    day = res["summary"]["day"]
    doc = json.loads((ROOT / "data" / "days" / f"{day}.json").read_text(encoding="utf-8"))
    arts = {a["id"]: a for a in res["articles"]}
    targets = [s for s in doc["stories"] if s["level"] in ("S", "A")
               and not any(arts.get(x["id"], {}).get("body_ok") for x in s["articles"])]
    verify.log(f"本文が取れなかったS・Aの話題 {len(targets)}件を再検索します")
    rows = []
    with GoogleDecoder(timeout=15, user_agent=verify.UA) as dec:
        for s in targets:
            lead = s["articles"][0]
            own = arts.get(lead["id"], {})
            row = {"level": s["level"], "tags": s.get("tags", []), "title": lead["title"],
                   "priority": s["level"] == "S" or bool(PRIORITY_TAGS & set(s.get("tags", []))),
                   "orig_domain": own.get("domain", ""), "orig_chars": own.get("body_chars", 0),
                   "queries": [], "candidates": [], "refilled": False, "secs": 0}
            t0 = time.time()
            seen = {x["url"] for x in s["articles"]}
            queries = [clean(lead["title"])[:60]]
            if s.get("summary") and clean(s["summary"]) != queries[0]:
                queries.append(clean(s["summary"])[:40])
            tried = 0
            for q in queries:
                row["queries"].append(q)
                try:
                    found = search(q)
                except Exception as e:  # noqa: BLE001
                    verify.log(f"search error {type(e).__name__}")
                    found = []
                time.sleep(1.5)
                for c in found:
                    if row["refilled"] or tried >= MAX_CAND:
                        break
                    if c["url"] in seen or c["src_domain"] in BLOCKED or (c["src_domain"] and c["src_domain"] == row["orig_domain"]):
                        continue
                    sim = similar(clean(lead["title"]), clean(c["title"]))
                    if sim < 0.35:  # 別の出来事の記事は使わない
                        continue
                    seen.add(c["url"])
                    tried += 1
                    r = verify.probe_article({"id": "x", "url": c["url"], "level": s["level"]}, dec)
                    r.pop("_body", None)
                    cand = {"title": c["title"], "domain": r["domain"], "sim": sim, "chars": r["body_chars"],
                            "ok": r["body_ok"], "image": bool(r["image"]), "error": r["error"]}
                    row["candidates"].append(cand)
                    if r["body_ok"] and r["domain"] not in BLOCKED:
                        row["refilled"] = True
                        row["refill"] = cand
                    time.sleep(0.5)
                if row["refilled"]:
                    break
            row["secs"] = round(time.time() - t0, 1)
            rows.append(row)
            verify.log(f"{s['level']} {'★' if row['priority'] else ' '} {lead['title'][:30]} → "
                       f"{'補えた ' + row['refill']['domain'] + ' ' + str(row['refill']['chars']) + '字' if row['refilled'] else '補えず'}"
                       f"（候補{len(row['candidates'])}件 {row['secs']}s）")

    pr = [r for r in rows if r["priority"]]
    sm = {"day": day, "targets": len(rows), "refilled": sum(r["refilled"] for r in rows),
          "priority": len(pr), "priority_refilled": sum(r["refilled"] for r in pr),
          "partial_100_399": sum(1 for r in rows if 100 <= r["orig_chars"] < 400),
          "secs_total": round(sum(r["secs"] for r in rows), 1)}
    (OUT / "refill.json").write_text(json.dumps({"summary": sm, "rows": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
    L = [f"# 追加検証：別の無料記事で本文を補えるか（{day}分）", "",
         f"- 対象（本文が取れなかったS・A）：{sm['targets']}話題 → 補えた {sm['refilled']}話題",
         f"- うち重点（S、または制度・法令／税・価格／競合）：{sm['priority']}話題 → 補えた {sm['priority_refilled']}話題",
         f"- 元の記事で冒頭100〜399字は取れていた話題：{sm['partial_100_399']}",
         f"- 再検索にかかった時間：合計 {sm['secs_total']}秒", "",
         "| 重 | 話題（元の見出し） | 元の媒体・字数 | 結果 | 補った記事（類似度） |", "|---|---|---|---|---|"]
    for r in rows:
        got = f"{r['refill']['domain']} {r['refill']['chars']}字：{r['refill']['title'][:40]}（{r['refill']['sim']}）" if r["refilled"] else \
            " ／ ".join(f"{c['domain']} {c['chars']}字 {c['error']}".strip() for c in r["candidates"]) or "候補なし"
        L.append(f"| {'★' if r['priority'] else ''} | [{r['level']}] {r['title'][:40]} | {r['orig_domain']} {r['orig_chars']}字 | "
                 f"{'補えた' if r['refilled'] else '補えず'} | {got} |")
    (OUT / "refill.md").write_text("\n".join(L), encoding="utf-8")
    verify.log(json.dumps(sm, ensure_ascii=False))


if __name__ == "__main__":
    main()
