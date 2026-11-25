"""毎朝の処理：取得 → 前日までに出た記事を除外 → AI判定・話題まとめ → 日別に保存 → まとめ（今日の3行／休み明け／週次ベスト10）。

保存先:
  data/days/YYYY-MM-DD.json   その日の話題と記事
  data/digest/YYYY-MM-DD.json その日の「今日の3行」（休み明けは休み中のまとめ）
  data/weekly/YYYY-MM-DD.json 週次ベスト10（その週の最初の営業日に作成）
  data/index.json             画面が最初に読む目次

使い方:
  python scripts/daily.py            # 今日の分を作る（すでに作成済みなら何もしない）
  python scripts/daily.py --force    # 作成済みでも作り直す
"""
import argparse
import datetime as dt
import json
import os
import pathlib
import re
import sys
import time

import holidays

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import collect  # noqa: E402
import judge  # noqa: E402
import ratings  # noqa: E402

JST = collect.JST
ROOT = collect.ROOT
DATA = ROOT / "data"
JP_HOLIDAYS = holidays.Japan()
LEVEL_ORDER = {"S": 0, "A": 1, "B": 2, "C": 3}


def is_business_day(d: dt.date) -> bool:
    return d.weekday() < 5 and d not in JP_HOLIDAYS


def previous_business_day(d: dt.date) -> dt.date:
    p = d - dt.timedelta(days=1)
    while not is_business_day(p):
        p -= dt.timedelta(days=1)
    return p


def load(path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def save(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def recent_days(today: dt.date, n: int):
    out = []
    for i in range(1, n + 1):
        d = today - dt.timedelta(days=i)
        doc = load(DATA / "days" / f"{d}.json")
        if doc:
            out.append((d, doc))
    return out


def ask_json(system, user, token, log):
    """まとめ用の1回だけの呼び出し。混雑時は予備モデルに切り替える。"""
    for model in judge.MODELS * 2:
        try:
            text, _ = judge.call_model([{"role": "system", "content": system}, {"role": "user", "content": user}], model, token)
            m = re.search(r"\{.*\}", text, re.S)
            return json.loads(m.group(0)) if m else None
        except Exception as e:  # noqa: BLE001
            log(f"digest: {type(e).__name__} on {model}")
            time.sleep(15)
    return None


DIGEST_SYSTEM = """あなたはアパレル小売企業のEC・販促担当者向けの編集者です。与えられた話題一覧から、{span}の動きを「3行」にまとめます。
ルール：1行は40字以内。主語と数字を残す。S（最重要）を優先し、残りはA（重要）から選ぶ。煽り表現は使わない。
形式：{{"lines":["…","…","…"]}}のJSONだけを返す。"""

WEEKLY_SYSTEM = """あなたはアパレル小売企業のEC・販促担当者向けの編集者です。先週の話題一覧から「読んだ方がいい10選」を選びます。
選び方：仕事への影響が大きいもの（制度・料金改定・セキュリティ・直接競合の大きな動き）を優先し、同じ分野ばかりにならないよう分散させる。
reason は「なぜ読むべきか」を35字以内で。
形式：{{"picks":[{{"key":"…","reason":"…"}}]}}のJSONだけを返す（keyは入力のkeyをそのまま使う）。"""


def story_lines(stories, limit=60):
    rows = []
    for s in stories[:limit]:
        name = f"《{s['story']}》" if s.get("story") else ""
        rows.append(f"[{s['level']}] {name}{s['summary']}（{len(s['articles'])}記事）")
    return "\n".join(rows)


def build_digest(today, stories_today, token, log):
    prev_bd = previous_business_day(today)
    after_holiday = (today - prev_bd).days > 1
    pool = list(stories_today)
    span = "今日"
    if after_holiday:
        # 前の営業日の翌日〜昨日に作られた分も含めて「休み中のまとめ」にする
        d = prev_bd + dt.timedelta(days=1)
        while d < today:
            doc = load(DATA / "days" / f"{d}.json")
            if doc:
                pool += doc["stories"]
            d += dt.timedelta(days=1)
        span = f"休み中（{prev_bd + dt.timedelta(days=1):%-m/%-d}〜{today - dt.timedelta(days=1):%-m/%-d}）と今朝"
    pool = sorted([s for s in pool if s["level"] in ("S", "A")], key=lambda s: (LEVEL_ORDER[s["level"]], -len(s["articles"])))
    lines = []
    if token and pool:
        res = ask_json(DIGEST_SYSTEM.format(span=span), story_lines(pool), token, log)
        if res and isinstance(res.get("lines"), list):
            lines = [str(x)[:60] for x in res["lines"][:3]]
    if not lines:  # AIが使えないときは上位3話題の要約をそのまま使う
        lines = [s["summary"] for s in pool[:3]]
    return {"date": str(today), "after_holiday": after_holiday, "span": span, "lines": lines,
            "must": [s["key"] for s in pool if s["level"] == "S"][:10]}


def build_weekly(today, token, log):
    """直近7日の S/A 話題から10選。"""
    pool = []
    for d, doc in recent_days(today, 7):
        for s in doc["stories"]:
            if s["level"] in ("S", "A"):
                pool.append(dict(s, date=str(d)))
    pool.sort(key=lambda s: (LEVEL_ORDER[s["level"]], -len(s["articles"])))
    pool = pool[:120]
    picks = []
    if token and pool:
        rows = "\n".join(f"key={s['key']} | [{s['level']}] {s.get('story') or ''} {s['summary']}" for s in pool)
        res = ask_json(WEEKLY_SYSTEM, rows, token, log)
        if res and isinstance(res.get("picks"), list):
            by_key = {s["key"]: s for s in pool}
            for p in res["picks"]:
                if p.get("key") in by_key and len(picks) < 10:
                    picks.append({"key": p["key"], "date": by_key[p["key"]]["date"], "reason": str(p.get("reason", ""))[:50]})
    if not picks:
        picks = [{"key": s["key"], "date": s["date"], "reason": ""} for s in pool[:10]]
    start = today - dt.timedelta(days=7)
    return {"date": str(today), "range": f"{start:%-m/%-d}〜{today - dt.timedelta(days=1):%-m/%-d}", "picks": picks}


def story_key(s, date):
    base = s.get("story") or s["articles"][0]["id"]
    return f"{date}:{base}"


def update_index(today):
    days = sorted((p.stem for p in (DATA / "days").glob("*.json")), reverse=True)
    weekly = sorted((p.stem for p in (DATA / "weekly").glob("*.json")), reverse=True)
    save(DATA / "index.json", {"updated": dt.datetime.now(JST).isoformat(timespec="minutes"),
                               "latest": str(today), "days": days[:120], "weekly": weekly[:30]})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--date", help="テスト用：処理する日付（YYYY-MM-DD）")
    a = ap.parse_args()
    now = dt.datetime.now(JST)
    today = dt.date.fromisoformat(a.date) if a.date else now.date()
    day_path = DATA / "days" / f"{today}.json"
    # M4-4：画面で付けた評価を先に取り込む（作成済みで終える朝も取り込む。失敗しても本体は続行）
    try:
        ratings.merge(os.environ.get("RATINGS", ""), today=today)
    except Exception as e:  # noqa: BLE001
        print(f"ratings: {type(e).__name__}。取り込みを省いて続行します")
    if day_path.exists() and not a.force:
        print(f"{today} は作成済みのため終了します（予備実行）")
        return

    logs = []

    def log(m):
        m = str(m)[:300]
        print(m, flush=True)
        logs.append(m)

    # 1) 取得：前回の作成から今までの分（初回・欠けた日は最大96時間さかのぼる）
    prev = recent_days(today, 4)
    hours = 30
    if prev:
        last_gen = dt.datetime.fromisoformat(prev[0][1]["generated"])
        hours = int(min(96, max(26, (now - last_gen).total_seconds() / 3600 + 4)))
    else:
        hours = 48
    _, report, items = collect.collect(hours)

    # 2) 前日までに判定済みの記事（同じURL・同じ見出し）を除外
    seen_urls, seen_titles = set(), set()
    for _, doc in prev:
        for it in doc.get("items", []):
            seen_urls.add(it.get("url"))
            seen_titles.add(collect.norm(it["title"]))
    fresh = [it for it in items if it.get("url") not in seen_urls and collect.norm(it["title"]) not in seen_titles]
    log(f"取得 {len(items)}件 → 新着 {len(fresh)}件（直近{hours}時間）")

    # 3) AI判定・話題まとめ（直近7日の話題名を渡して、続報は同じ名前にそろえる）
    token = os.environ.get("GEMINI_API_KEY", "").strip()
    if token and not re.fullmatch(r"[A-Za-z0-9_.\-]{20,120}", token):
        log("GEMINI_API_KEY の形式が正しくありません。ルール判定で続行します。")
        token = ""
    prev_names = []
    for _, doc in recent_days(today, 7):
        for s in doc["stories"]:
            if s.get("story") and s["level"] in ("S", "A") and s["story"] not in prev_names:
                prev_names.append(s["story"])
    profile = os.environ.get("PROFILE", "").strip() or judge.DEFAULT_PROFILE
    if token:
        try:
            ex = ratings.examples(today)
        except Exception:  # noqa: BLE001
            ex = ""
        log(f"判定の手本：{ex.count(chr(10)) if ex else 0}件")
        judged, stats = judge.judge(fresh, token, profile, log, prev_names=prev_names, examples=ex)
    else:
        judged = [dict(it, **judge.rule_judge(it)) for it in fresh]
        for it in judged:
            judge.cap_weekly(it)
        stats = {"model": "rules-only"}
    stories = judge.group_stories(judged)
    for s in stories:
        s["key"] = story_key(s, today)
        # 過去にも出ていた話題なら、最初に出た日を記録（画面で「続報」と表示するため）
        if s.get("story"):
            for d, doc in sorted(recent_days(today, 30)):
                if any(x.get("story") == s["story"] for x in doc["stories"]):
                    s["first_seen"] = str(d)
                    break

    # 3.5) M4-3：S・Aの本文・写真 → 要約（30秒・要点・図解）。どちらも失敗したら見出しモードで続行
    bodies, enrich_stats, sum_stats = {}, {"mode": "headline"}, {}
    try:
        import enrich
        stories, bodies, enrich_stats = enrich.enrich(stories, log)
    except Exception as e:  # noqa: BLE001
        log(f"enrich: {type(e).__name__}。見出しモードで続行します")
    try:
        sum_stats = judge.summarize(stories, bodies, token, log, profile)
    except Exception as e:  # noqa: BLE001
        log(f"summary: {type(e).__name__}。要約なしで続行します")
    bodies = None  # 本文はここで捨てる（公開リポジトリに保存しない）
    stories.sort(key=lambda s: (LEVEL_ORDER[s["level"]], -len(s["articles"]), s.get("latest") or ""))

    # 4) 保存（C=対象外は見出し・URL・分野・日付だけ残す）
    keep = [s for s in stories if s["level"] != "C"]
    c_items = [{"t": s["articles"][0]["title"][:120], "u": s["articles"][0]["url"], "lane": (s.get("lanes") or [""])[0],
                "date": s.get("latest") or ""} for s in stories if s["level"] == "C" and s["articles"]]
    save(day_path, {
        "date": str(today), "generated": now.isoformat(timespec="minutes"), "hours": hours,
        "stats": dict({k: stats.get(k) for k in ("model", "requests_sent", "failed_batches", "cluster_requests")},
                      enrich=enrich_stats, summary=sum_stats),
        "counts": {l: sum(1 for s in stories if s["level"] == l) for l in "SABC"},
        "stories": keep,
        "c_items": c_items,
        "items": [{"id": it["id"], "title": it["title"], "url": it["url"]} for it in judged],
        "feeds": [{"feed": r["feed"], "http": r["http"], "n": r[f"within_{hours}h"]} for r in report],
        "logs": logs[-30:],
    })

    # 5) まとめ
    save(DATA / "digest" / f"{today}.json", build_digest(today, keep, token, log))
    week_start = today - dt.timedelta(days=today.weekday())
    first_bd_of_week = next((week_start + dt.timedelta(days=i) for i in range(7)
                             if is_business_day(week_start + dt.timedelta(days=i))), week_start)
    if today == first_bd_of_week:
        save(DATA / "weekly" / f"{today}.json", build_weekly(today, token, log))
    update_index(today)
    print(json.dumps({"date": str(today), "stories": len(keep), "counts": {l: sum(1 for s in stories if s["level"] == l) for l in "SABC"}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
