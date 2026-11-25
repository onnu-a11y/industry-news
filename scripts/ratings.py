"""M4-4：画面で付けた評価（S/A/B/C）を履歴に取り込み、AI判定の手本を作る。

流れ：画面 → Cloudflare（industry-news-cron の受け口＋KV、4日で自動消去）
      → 毎朝の起動時に直近の評価を inputs.ratings として渡す → ここで data/state/ratings.json に保存
      → judge.py が「AIの判定と違った評価」を手本として使う（AIの呼び出し回数は増えない）

inputs.ratings の形式：[["日付:記事ID または 日付:話題名", "S|A|B|C|''(取り消し)", 受け付けた時刻(ミリ秒)], ...]
ratings.json は公開リポジトリに置かれる（見出し・評価だけで、見られて困る内容ではない）。
"""
import datetime as dt
import json
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
PATH = ROOT / "data" / "state" / "ratings.json"
KEY_RE = re.compile(r"^(\d{4}-\d\d-\d\d):([^\s<>\"]{1,60})$")
LEVELS = ("S", "A", "B", "C")
KEEP_DAYS = 120


def _load(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def _find(key, cache):
    """key に当たる話題の見出しとAIの判定を、その日のデータから探す。"""
    date = key.split(":", 1)[0]
    if date not in cache:
        cache[date] = _load(ROOT / "data" / "days" / f"{date}.json") or {}
    for s in cache[date].get("stories", []):
        if s.get("key") == key:
            a = (s.get("articles") or [{}])[0]
            return {"title": (a.get("title") or s.get("summary") or "")[:100], "ai": s.get("level", "")}
    return None


def merge(raw, log=print, today=None):
    """inputs.ratings を ratings.json に取り込む。返り値は (取り込んだ件数, 取り消した件数)。"""
    doc = _load(PATH) or {"items": {}}
    items = doc.setdefault("items", {})
    added = removed = 0
    try:
        rows = json.loads(raw) if raw and raw.strip() else []
    except Exception:  # noqa: BLE001
        log("ratings: 形式が読めないため取り込みを省きます")
        rows = []
    if not isinstance(rows, list):
        rows = []
    cache = {}
    for r in rows:
        if not (isinstance(r, list) and len(r) >= 3 and isinstance(r[0], str) and KEY_RE.match(r[0])
                and r[1] in LEVELS + ("",) and isinstance(r[2], (int, float))):
            continue
        key, level, at = r[0], r[1], int(r[2])
        old = items.get(key)
        if old and old.get("at", 0) >= at:
            continue  # 同じか古い受け付け（前の朝に取り込み済み）
        if level == "":
            if old:
                del items[key]
                removed += 1
            continue
        found = _find(key, cache)
        if not found:
            continue  # 該当する話題がない（形式は正しいが存在しない key）
        items[key] = {"level": level, "ai": found["ai"], "title": found["title"], "at": at}
        added += 1
    # 古い履歴を整理（手本に使うのは直近30日、保存は120日）
    today = today or dt.date.today()
    cut = str(today - dt.timedelta(days=KEEP_DAYS))
    for k in [k for k in items if k[:10] < cut]:
        del items[k]
    if added or removed or not PATH.exists():
        doc["updated"] = dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).isoformat(timespec="minutes")
        PATH.parent.mkdir(parents=True, exist_ok=True)
        PATH.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"ratings: 受け取り {len(rows)}件 → 取り込み {added}件・取り消し {removed}件（履歴 {len(items)}件）")
    return added, removed


def examples(today, days=30, limit=40):
    """AIの判定と違った評価（直近 days 日・新しい順に最大 limit 件）を、判定の手本の文にする。"""
    doc = _load(PATH) or {}
    cut = str(today - dt.timedelta(days=days))
    rows = [v for k, v in doc.get("items", {}).items()
            if k[:10] >= cut and v.get("level") in LEVELS and v.get("ai") and v["level"] != v["ai"]]
    rows.sort(key=lambda v: -v.get("at", 0))
    rows = rows[:limit]
    if not rows:
        return ""
    lines = [f'- {v["title"]}｜AI:{v["ai"]} → 読み手:{v["level"]}' for v in rows]
    return ("読み手が過去に付け直した評価（AIの判定と違ったもの。新しい順）。似た記事はこの傾向に合わせる：\n"
            + "\n".join(lines))
