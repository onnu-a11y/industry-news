"""全取得元からRSSを集め、重複をまとめて保存する。

使い方:
  python scripts/collect.py --probe   # 検証モード: 取得元ごとの件数レポートを data/probe/ に出力
  python scripts/collect.py           # 通常モード: data/raw/YYYY-MM-DD.json に保存
"""
import argparse
import datetime as dt
import difflib
import hashlib
import json
import pathlib
import re
import time
from zoneinfo import ZoneInfo

import feedparser

from feeds import FEEDS, LANES

JST = ZoneInfo("Asia/Tokyo")
ROOT = pathlib.Path(__file__).resolve().parent.parent
UA = "Mozilla/5.0 (compatible; industry-news-bot/1.0; +https://onnu-a11y.github.io/industry-news/)"


def clean_title(title: str, kind: str) -> str:
    t = re.sub(r"\s+", " ", title or "").strip()
    if kind == "gnews":
        # Googleニュースは「見出し - 媒体名」形式なので媒体名を外す
        t = re.sub(r"\s+-\s+[^-]+$", "", t)
    # 東洋経済の「| カテゴリ | 東洋経済オンライン」を外す
    t = re.sub(r"\s*\|[^|]*\|\s*東洋経済オンライン$", "", t)
    return t


def source_of(entry, feed) -> str:
    if feed["kind"] == "gnews":
        src = entry.get("source") or {}
        if isinstance(src, dict) and src.get("title"):
            return src["title"]
        m = re.search(r"\s+-\s+([^-]+)$", entry.get("title", ""))
        if m:
            return m.group(1).strip()
    return feed["name"]


def published(entry) -> dt.datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        st = entry.get(key)
        if st:
            return dt.datetime(*st[:6], tzinfo=dt.timezone.utc).astimezone(JST)
    return None


def norm(t: str) -> str:
    return re.sub(r"[\s　「」『』【】（）()［］\[\]、。・:：！!？?\"'“”]", "", t).lower()


def fetch_feed(feed):
    started = time.time()
    try:
        parsed = feedparser.parse(feed["url"], agent=UA, request_headers={"Accept-Language": "ja"})
        status = getattr(parsed, "status", None)
        err = None
        if parsed.bozo and not parsed.entries:
            err = str(parsed.get("bozo_exception", "parse error"))[:200]
    except Exception as e:  # noqa: BLE001
        return {"status": None, "error": str(e)[:200], "entries": [], "secs": round(time.time() - started, 1)}
    return {"status": status, "error": err, "entries": parsed.entries, "secs": round(time.time() - started, 1)}


def snippet(entry, kind) -> str:
    if kind == "gnews":
        return ""  # Googleニュースの説明欄はリンクの羅列なので使わない
    raw = entry.get("summary") or entry.get("description") or ""
    txt = re.sub(r"<[^>]+>", " ", raw)
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt[:140]


def collect(hours: int):
    now = dt.datetime.now(JST)
    report, items = [], []
    for feed in FEEDS:
        res = fetch_feed(feed)
        kept = 0
        since = now - dt.timedelta(hours=max(hours, feed.get("hours", 0)))
        for e in res["entries"]:
            pub = published(e)
            if pub and pub < since:
                continue
            title = clean_title(e.get("title", ""), feed["kind"])
            if not title:
                continue
            link = e.get("link", "")
            items.append({
                "id": hashlib.sha1((norm(title) + link).encode()).hexdigest()[:12],
                "title": title,
                "url": link,
                "source": source_of(e, feed),
                "snippet": snippet(e, feed["kind"]),
                "lane": feed["lane"],
                "feed": feed["id"],
                "published": pub.isoformat() if pub else None,
            })
            kept += 1
        report.append({
            "feed": feed["id"], "name": feed["name"], "lane": feed["lane"], "kind": feed["kind"],
            "http": res["status"], "error": res["error"], "total": len(res["entries"]),
            f"within_{hours}h": kept, "secs": res["secs"],
        })
        time.sleep(1.0)  # 取得元に負荷をかけない
    return now, report, dedupe(items)


def dedupe(items):
    """同じ話題（URL一致 or 見出しがほぼ同じ）を1件にまとめ、媒体数を数える。"""
    clusters = []
    for it in sorted(items, key=lambda x: x["published"] or "", reverse=True):
        key = norm(it["title"])
        hit = None
        for c in clusters:
            if it["url"] and it["url"] == c["url"]:
                hit = c
                break
            if difflib.SequenceMatcher(None, key, c["_key"]).ratio() >= 0.75:
                hit = c
                break
        if hit:
            if it["source"] not in hit["sources"]:
                hit["sources"].append(it["source"])
            if it["lane"] not in hit["lanes"]:
                hit["lanes"].append(it["lane"])
        else:
            clusters.append({**it, "_key": key, "sources": [it["source"]], "lanes": [it["lane"]]})
    for c in clusters:
        c.pop("_key")
    return clusters


def write_probe(now, report, clusters, hours):
    out = ROOT / "data" / "probe"
    out.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y-%m-%d_%H%M")
    payload = json.dumps({"generated": now.isoformat(), "report": report, "items": clusters}, ensure_ascii=False, indent=1)
    (out / f"{stamp}.json").write_text(payload, encoding="utf-8")
    (out / "latest.json").write_text(payload, encoding="utf-8")

    lines = [f"# 取得元の検証レポート（{now:%Y-%m-%d %H:%M} JST・直近{hours}時間）", ""]
    lines += ["| レーン | 取得元 | 種類 | HTTP | 全件 | 期間内 | エラー |", "|---|---|---|---|---|---|---|"]
    for r in report:
        lines.append(f"| {LANES[r['lane']]} | {r['name']} | {r['kind']} | {r['http']} | {r['total']} | {r[f'within_{hours}h']} | {r['error'] or ''} |")
    raw = sum(r[f"within_{hours}h"] for r in report)
    lines += ["", f"- 期間内の合計: {raw}件 → 重複まとめ後: {len(clusters)}件", ""]
    for lane, label in LANES.items():
        lane_items = [c for c in clusters if c["lane"] == lane]
        lines += [f"## {label}（{len(lane_items)}件・先頭15件）", ""]
        for c in lane_items[:15]:
            multi = f"［{len(c['sources'])}媒体］" if len(c["sources"]) > 1 else ""
            lines.append(f"- {c['title']} — {c['sources'][0]}{multi}")
        lines.append("")
    (out / "latest.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines[:len(report) + 6]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--hours", type=int, default=72)
    a = ap.parse_args()
    now, report, clusters = collect(a.hours)
    if a.probe:
        write_probe(now, report, clusters, a.hours)
    else:
        out = ROOT / "data" / "raw"
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{now:%Y-%m-%d}.json").write_text(json.dumps({"generated": now.isoformat(), "items": clusters}, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"saved {len(clusters)} items")


if __name__ == "__main__":
    main()
