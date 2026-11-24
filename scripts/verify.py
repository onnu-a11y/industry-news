"""M4-1.5 検証：本文・写真が取れるか／見出しだけと本文ありで要約がどう変わるか を実データで調べる。

調べること:
  1) Googleニュース経由のURLを元記事のURLに戻せるか（復元率）
  2) 元記事から本文を抜き出せるか（400字以上を「取れた」とする）
  3) 代表画像（og:image / twitter:image）があるか
  4) 1記事あたりの処理時間（毎朝の処理がどれだけ延びるか）
  5) 同じ話題を「見出しだけ」と「本文あり」でAIに要約させ、30秒・要点・図解の中身を比べる
     あわせて、AIが出した数字・日付が入力に本当にあるか（記事に無い数字を作っていないか）を数える

公開リポジトリなので、記事の本文そのものは保存しない（文字数と判定結果だけ残す）。

使い方:
  python scripts/verify.py                         # data/index.json の最新日を使う
  python scripts/verify.py --day 2026-11-24 --b-sample 30 --n-summary 10
出力:
  data/verify/result.json  … 全件の結果
  data/verify/report.md    … 集計レポート
"""
import argparse
import collections
import datetime as dt
import json
import os
import pathlib
import re
import sys
import time
import unicodedata
from urllib.parse import urlparse

import requests
import trafilatura
from googlenewsdecoder import GoogleDecoder

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import judge  # noqa: E402

ROOT = judge.ROOT
OUT = ROOT / "data" / "verify"
UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1")
BODY_OK = 400  # 本文として使える最低文字数


def log(m):
    print(str(m)[:300], flush=True)


# ---------------------------------------------------------------- 1〜4) 本文・写真
def og_image(html: str):
    for pat in (r'<meta[^>]+property=["\']og:image(?::url)?["\'][^>]+content=["\']([^"\']+)',
                r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image',
                r'<meta[^>]+name=["\']twitter:image(?::src)?["\'][^>]+content=["\']([^"\']+)'):
        m = re.search(pat, html, re.I)
        if m and m.group(1).startswith("http"):
            return m.group(1)
    return ""


def probe_article(art, decoder):
    url = art["url"]
    rec = {"id": art["id"], "level": art["level"], "lane": art.get("lane", ""), "source": art.get("source", ""),
           "gnews": "news.google.com" in url, "resolved": False, "final_url": "", "domain": "",
           "http": None, "body_chars": 0, "body_ok": False, "image": "", "secs": 0.0, "error": ""}
    t0 = time.time()
    try:
        if rec["gnews"]:
            r = decoder.decode_google_news_url(url, interval=1)
            if not r.get("success"):
                rec["error"] = "復元失敗: " + str(r.get("message", ""))[:80]
                return rec
            url = r["decoded_url"]
        rec["resolved"] = True
        rec["final_url"] = url
        rec["domain"] = urlparse(url).netloc.replace("www.", "")
        resp = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "ja"}, timeout=15)
        rec["http"] = resp.status_code
        if resp.status_code >= 400:
            rec["error"] = f"HTTP {resp.status_code}"
            return rec
        resp.encoding = resp.apparent_encoding if resp.encoding in (None, "ISO-8859-1") else resp.encoding
        html = resp.text
        rec["image"] = og_image(html)
        body = trafilatura.extract(html, url=url, favor_precision=True, include_comments=False,
                                   include_tables=False) or ""
        rec["body_chars"] = len(body)
        rec["body_ok"] = len(body) >= BODY_OK
        rec["_body"] = body  # メモリ上だけで使う（保存しない）
    except Exception as e:  # noqa: BLE001
        rec["error"] = f"{type(e).__name__}"
    finally:
        rec["secs"] = round(time.time() - t0, 1)
    return rec


# ---------------------------------------------------------------- 5) 要約の比較
SUM_SYSTEM = """あなたは日本のメンズスーツ専門店のEC・販促担当者向けのニュース編集者です。与えられた話題を、スマホで30秒で把握できる形にまとめ、JSONだけを返します。
{profile}

厳守：入力に書かれていない数字・金額・割合・日付・社名は絶対に書かない。分からない項目は「記事に記載なし」と書く。推測で埋めない。
形式：
{{"what":"何が起きたか（40字以内）",
 "when":"いつから・いつまで（入力にある日付だけ。無ければ「記事に記載なし」）",
 "me":"読み手の仕事への影響と、やること（40字以内）",
 "points":["要点（各40字以内、3〜5個。入力に無い内容は書かない。材料が足りなければ少なくてよい）"],
 "diagram":{{"type":"change|steps|impact|none",
   "title":"図の見出し（15字以内）",
   "rows":[{{"label":"項目","before":"変更前","after":"変更後"}}],
   "steps":[{{"label":"段階名","state":"done|now|todo"}}],
   "impact":["送料設定|DM・はがき|EC基盤|メルマガ|ポイント|競合調査|価格設定|採用・人件費 から該当するもの"]}}}}
図の選び方：料金・価格・条件が変わる話→change（rowsを使う）、法律・制度の進み具合→steps、それ以外で自社業務に関係→impact、図にする材料が無い→none。使わない欄は空配列。"""


def norm_num(s: str) -> str:
    return unicodedata.normalize("NFKC", s).replace(",", "").replace("，", "")


NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def unsupported_numbers(output: dict, source_text: str):
    """AIの出力に含まれる数字のうち、入力に無いものを返す（記事に無い数字を作っていないかの確認）。"""
    src = norm_num(source_text)
    src_nums = set(NUM_RE.findall(src))
    out_text = norm_num(json.dumps(output, ensure_ascii=False))
    bad = []
    for n in NUM_RE.findall(out_text):
        if n in src_nums:
            continue
        # 年の省略（2027年→27）や先頭ゼロの違いは許容
        if any(s.endswith(n) or s.lstrip("0") == n.lstrip("0") for s in src_nums):
            continue
        bad.append(n)
    return sorted(set(bad))


def ask(system, user, token, models):
    for attempt in range(4):
        model = models[attempt % len(models)]
        try:
            text, _ = judge.call_model([{"role": "system", "content": system}, {"role": "user", "content": user}], model, token)
            m = re.search(r"\{.*\}", text, re.S)
            return (json.loads(m.group(0)) if m else None), model, attempt + 1
        except Exception as e:  # noqa: BLE001
            code = getattr(e, "code", "")
            log(f"summary: {type(e).__name__} {code} on {model}")
            time.sleep(min(15 * (attempt + 1), 45))
    return None, None, 4


def headline_input(story):
    rows = [f"話題：{story.get('story') or story['summary']}", f"重要度：{story['level']}"]
    for a in story["articles"][:6]:
        rows.append(f"・{a['title']}（{a.get('source', '')}）")
    return "\n".join(rows)


def body_input(story, bodies):
    rows = [headline_input(story), "", "本文（抜粋）："]
    n = 0
    for a in story["articles"]:
        b = bodies.get(a["id"])
        if b and n < 2:
            rows.append(f"―― {a['title']}（{a.get('source', '')}）\n{b[:2500]}")
            n += 1
    return "\n".join(rows)


def count_filled(o):
    if not o:
        return 0
    na = "記事に記載なし"
    n = sum(1 for k in ("what", "when", "me") if o.get(k) and na not in str(o.get(k)))
    n += len([p for p in o.get("points") or [] if p and na not in p])
    d = o.get("diagram") or {}
    n += len(d.get("rows") or []) + len(d.get("steps") or [])
    return n


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--day")
    ap.add_argument("--b-sample", type=int, default=30)
    ap.add_argument("--n-summary", type=int, default=10)
    a = ap.parse_args()
    day = a.day or json.loads((ROOT / "data" / "index.json").read_text())["latest"]
    doc = json.loads((ROOT / "data" / "days" / f"{day}.json").read_text(encoding="utf-8"))
    stories = doc["stories"]
    started = time.time()

    # 対象：S・Aの全記事＋Bの話題の先頭記事（写真の取得率を見るため）
    targets, seen = [], set()
    for s in stories:
        arts = s["articles"] if s["level"] in ("S", "A") else s["articles"][:1]
        if s["level"] == "B" and sum(1 for t in targets if t["level"] == "B") >= a.b_sample:
            continue
        for art in arts:
            if art["url"] in seen:
                continue
            seen.add(art["url"])
            targets.append(dict(art, level=s["level"], lane=(s.get("lanes") or [""])[0]))
    log(f"{day}: 対象 {len(targets)}記事（S/A全件＋B {a.b_sample}件）")

    recs, bodies = [], {}
    with GoogleDecoder(timeout=15, user_agent=UA) as dec:
        for i, art in enumerate(targets):
            r = probe_article(art, dec)
            if r.get("_body") and r["body_ok"]:
                bodies[r["id"]] = r["_body"]
            r.pop("_body", None)
            recs.append(r)
            log(f"[{i + 1}/{len(targets)}] {r['level']} {'G' if r['gnews'] else 'D'} {r['domain'] or r['source'][:20]} "
                f"本文{r['body_chars']}字 画像{'有' if r['image'] else '無'} {r['secs']}s {r['error']}")
            time.sleep(0.5)
    fetch_secs = round(time.time() - started)

    # 要約の比較（S優先・記事が多い順・本文が取れた話題を優先）
    token = os.environ.get("GEMINI_API_KEY", "").strip()
    comps = []
    if token and a.n_summary > 0:
        today, used, budget = judge.quota_left()
        budget = min(budget, a.n_summary * 2)
        cand = [s for s in stories if s["level"] in ("S", "A")]
        cand.sort(key=lambda s: (s["level"] != "S", not any(x["id"] in bodies for x in s["articles"]), -len(s["articles"])))
        system = SUM_SYSTEM.format(profile=os.environ.get("PROFILE", "").strip() or judge.DEFAULT_PROFILE)
        sent = 0
        for s in cand[:a.n_summary]:
            if sent + 2 > budget:
                log("無料枠の上限に近いため、要約の比較をここで止めます")
                break
            has_body = any(x["id"] in bodies for x in s["articles"])
            h_in = headline_input(s)
            h_out, h_model, n1 = ask(system, h_in, token, judge.MODELS)
            sent += n1
            time.sleep(7)
            b_out, b_model, n2 = (None, None, 0)
            b_in = ""
            if has_body:
                b_in = body_input(s, bodies)
                b_out, b_model, n2 = ask(system, b_in, token, judge.MODELS)
                sent += n2
                time.sleep(7)
            comps.append({
                "key": s.get("key"), "level": s["level"], "story": s.get("story") or s["summary"],
                "articles": len(s["articles"]), "has_body": has_body,
                "headline": {"out": h_out, "filled": count_filled(h_out),
                             "unsupported_numbers": unsupported_numbers(h_out or {}, h_in), "model": h_model},
                "body": {"out": b_out, "filled": count_filled(b_out),
                         "unsupported_numbers": unsupported_numbers(b_out or {}, b_in) if b_out else [],
                         "model": b_model} if has_body else None,
                "titles": [x["title"] for x in s["articles"][:4]],
            })
            log(f"要約比較 {len(comps)}: {s['level']} {comps[-1]['story'][:20]} 見出し{comps[-1]['headline']['filled']}項目"
                + (f" / 本文{comps[-1]['body']['filled']}項目" if has_body else " / 本文なし"))
        judge.save_quota(today, used + sent)

    # 集計
    def rate(rows, key):
        return f"{sum(1 for r in rows if r[key])}/{len(rows)}（{(100 * sum(1 for r in rows if r[key]) // max(1, len(rows)))}%）"

    groups = {
        "S・A（Googleニュース経由）": [r for r in recs if r["level"] in "SA" and r["gnews"]],
        "S・A（媒体の直接RSS）": [r for r in recs if r["level"] in "SA" and not r["gnews"]],
        "B（見本）": [r for r in recs if r["level"] == "B"],
        "全体": recs,
    }
    secs = [r["secs"] for r in recs]
    by_dom = collections.defaultdict(list)
    for r in recs:
        by_dom[r["domain"] or "（復元失敗）"].append(r)
    errors = collections.Counter(r["error"].split(":")[0] for r in recs if r["error"])
    summary = {
        "day": day, "generated": dt.datetime.now(judge_tz()).isoformat(timespec="minutes"),
        "articles": len(recs), "fetch_secs": fetch_secs,
        "avg_secs": round(sum(secs) / max(1, len(secs)), 1),
        "groups": {k: {"n": len(v), "resolved": rate(v, "resolved"), "body": rate(v, "body_ok"), "image": rate(v, "image")}
                   for k, v in groups.items()},
        "errors": dict(errors),
        "domains": sorted(({"domain": d, "n": len(v), "body": sum(1 for r in v if r["body_ok"]),
                            "image": sum(1 for r in v if r["image"])} for d, v in by_dom.items()), key=lambda x: -x["n"]),
        "stories_SA": sum(1 for s in stories if s["level"] in "SA"),
        "stories_SA_with_body": sum(1 for s in stories if s["level"] in "SA" and any(x["id"] in bodies for x in s["articles"])),
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "result.json").write_text(json.dumps({"summary": summary, "articles": recs, "comparisons": comps},
                                                ensure_ascii=False, indent=1), encoding="utf-8")
    write_report(summary, comps)
    log(json.dumps(summary["groups"], ensure_ascii=False))


def judge_tz():
    from zoneinfo import ZoneInfo
    return ZoneInfo("Asia/Tokyo")


def write_report(sm, comps):
    L = [f"# M4-1.5 検証レポート（{sm['day']}分・{sm['generated']}実行）", "",
         f"- 対象 {sm['articles']}記事 / 取得にかかった時間 {sm['fetch_secs']}秒（1記事平均 {sm['avg_secs']}秒）",
         f"- S・Aの話題 {sm['stories_SA']}件のうち、本文が1本でも取れた話題 {sm['stories_SA_with_body']}件", "",
         "| 区分 | 件数 | 元URL復元 | 本文（400字以上） | 代表画像 |", "|---|---|---|---|---|"]
    for k, g in sm["groups"].items():
        L.append(f"| {k} | {g['n']} | {g['resolved']} | {g['body']} | {g['image']} |")
    L += ["", "## 失敗の内訳", ""] + [f"- {k}：{v}件" for k, v in sm["errors"].items()]
    L += ["", "## 媒体別（上位30）", "", "| 媒体 | 件数 | 本文 | 画像 |", "|---|---|---|---|"]
    for d in sm["domains"][:30]:
        L.append(f"| {d['domain']} | {d['n']} | {d['body']} | {d['image']} |")
    L += ["", "## 要約の比較（見出しだけ ／ 本文あり）", ""]
    for c in comps:
        L.append(f"### [{c['level']}] {c['story']}（{c['articles']}記事）")
        for mode, label in (("headline", "見出しだけ"), ("body", "本文あり")):
            r = c.get(mode)
            if not r:
                L.append(f"- {label}：本文が取れず比較なし")
                continue
            o = r["out"] or {}
            L.append(f"- **{label}**（埋まった項目 {r['filled']}／入力に無い数字 {', '.join(r['unsupported_numbers']) or 'なし'}）")
            L.append(f"  - 何が：{o.get('what', '')}／いつ：{o.get('when', '')}／自分に：{o.get('me', '')}")
            for p in o.get("points") or []:
                L.append(f"  - ・{p}")
            d = o.get("diagram") or {}
            L.append(f"  - 図解：{d.get('type', '')} {d.get('title', '')} "
                     f"{json.dumps(d.get('rows') or d.get('steps') or d.get('impact') or [], ensure_ascii=False)[:200]}")
        L.append("")
    (OUT / "report.md").write_text("\n".join(L), encoding="utf-8")


if __name__ == "__main__":
    main()
