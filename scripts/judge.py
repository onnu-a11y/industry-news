"""収集した記事をAIで判定する（重要度S/A/B/C・1行要約・関係する理由・タグ）。

Gemini API の無料枠を GEMINI_API_KEY（GitHub Secrets）で呼ぶ。
キーがない／上限に達した／障害のときは、キーワードのルール判定に自動で切り替える（更新は止めない）。
前提メモは環境変数 PROFILE（GitHub Secrets）があればそれを使い、なければ DEFAULT_PROFILE を使う。

使い方:
  python scripts/judge.py --input data/probe/latest.json --report   # 検証レポート出力
  python scripts/judge.py --input data/raw/2026-11-25.json --out data/judged/2026-11-25.json
"""
import argparse
import json
import os
import pathlib
import re
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
MODELS = ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]  # 先頭から試し、使えなければ次へ
BATCH = 20
PER_RUN_CAP = 60      # 1回の実行で送る最大リクエスト数（20件×60＝1,200件まで）
DAILY_CAP = 400       # 1日の上限（無料枠500回に対して余裕を残す）。超えた分はルール判定
QUOTA_FILE = ROOT / "data" / "state" / "quota.json"
LEVELS = ["S", "A", "B", "C"]
TAGS = ["制度・法令", "税・価格", "セキュリティ", "障害・規約", "競合", "新商品・サービス", "出店・閉店",
        "決算・業績", "EC・販促", "消費・市況", "為替・金利", "人・採用", "トレンド", "その他"]

DEFAULT_PROFILE = """読み手：日本のメンズスーツ専門店（オーダー・既製）のEC・販促担当者。
担当業務：自社ECサイトの運営、HTMLメルマガ制作・配信、LINE公式アカウント・自社アプリでの情報発信、DM・はがき等の発送物やポスター等の販促物の制作、ポイント・クーポン施策、競合調査、在庫連携。
直接競合：青山商事（洋服の青山）、AOKI、コナカ・フタタ、はるやま、SUIT SELECT、ORIHICA、FABRIC TOKYO、DIFFERENCE、麻布テーラー、KASHIYAMA など。"""

RUBRIC = """重要度の基準:
S＝最重要。「読み手の会社が今すぐ確認・対応すべきこと」に限る。次のいずれか。
 ・制度・法令・税・規制が「決まった／成立した／施行される／具体的な中身や日程が変わった」もの（消費税・減税、特商法、景表法、個人情報保護法、最低賃金、社会保険、下請法/取適法、インボイス、公益通報、メール配信規制など）。国内のみ。
 ・郵便料金・DM/はがき・宅配便の運賃・送料・印刷/用紙価格の改定や制度変更（発送物と販促物のコストに直結）
 ・ECサイトやWebの基盤（PHP、WordPress、カートシステム、決済、メール配信、LINE等）の重大な脆弱性・大規模障害・規約変更、公的機関（IPA・JPCERT等）の注意喚起
 ・直接競合の経営の大きな動き（統合・買収・撤退・大規模閉店・全面的な価格改定）
 ※次はSにしない（Aにする）：他社の情報漏えい・事故の報道、制度をめぐる国会の駆け引き・審議日程・賛否の論評・世論調査。
A＝重要。直接競合の新商品・新サービス・販促・出店閉店・決算、他社ECの情報漏えい事故（再発防止の教訓）、制度の審議の動き、EC・CRM・販促の実務に使える事例、消費・物価・為替の大きな変化、アパレル大手の戦略的な動き。
B＝参考。ファッション・アパレル全般の話題、小売一般、経済一般、テクノロジー一般で、読み手の業界と少しでも接点があるもの。
C＝対象外。芸能・エンタメ、スーツ以外の意味の「スーツ」、海外政治の詳細、読み手の仕事と接点がないもの。"""

SYSTEM = """あなたは日本のアパレル小売企業向けのニュース編集者です。与えられた記事一覧を、読み手の仕事にとっての重要度で判定し、JSONだけを返します。
{profile}

{rubric}

出力ルール:
- 形式: {{"items":[{{"id":"…","level":"S|A|B|C","summary":"…","why":"…","tags":["…"],"company":"…","story":"…"}}]}}
- summary: 何が起きたかを主語・数字を残して40字以内の日本語で。見出しの言い換えで構わないが、煽り表現は削る。
- why: S/Aのみ。読み手の仕事にどう関係するかを30字以内で。B/Cは空文字。
- tags: 次から1〜2個 {tags}
- company: 主役の企業名（なければ空文字）
- story: その記事が扱う「話題」の名前を15字以内で（例：「タワレコ情報流出」「消費減税法案」「佐川急便運賃改定」）。同じ出来事・同じ制度の記事には必ず同じ名前を付ける。
  既存の話題一覧に当てはまるものがあれば、その名前をそのまま使う。新しい話題のときだけ新しい名前を作る。
- 入力の全idについて必ず1件ずつ返す。"""


RULES = [
    ("S", "制度・法令", r"法案|改正|施行|成立|閣議決定|消費税|減税|増税|インボイス|最低賃金|社会保険|特定商取引|特商法|景品表示|景表法|個人情報保護法|下請法|取適法|ステマ規制|義務化"),
    ("S", "税・価格", r"郵便料金|はがき.*(値上げ|料金)|ゆうメール|ゆうパケット|日本郵便.*(改定|値上げ|廃止|終了)|運賃.*(改定|値上げ)|送料.*値上げ|印刷.*値上げ|用紙.*値上げ"),
    ("S", "セキュリティ", r"注意喚起|脆弱性対策|緊急メンテ|PHP|EC-CUBE|WordPress|MakeShop|Shopify|決済.*(障害|停止)|LINE.*(障害|仕様変更|終了)"),
    ("A", "競合", r"青山商事|洋服の青山|AOKI|コナカ|フタタ|はるやま|SUIT SELECT|スーツセレクト|ORIHICA|FABRIC TOKYO|DIFFERENCE|麻布テーラー|KASHIYAMA|オーダースーツ"),
    ("A", "セキュリティ", r"不正アクセス|情報漏え?い|流出|ランサム"),
    ("A", "消費・市況", r"円安|円高|為替|物価|値上げ|日銀|利上げ|個人消費|消費動向"),
    ("B", "トレンド", r"スーツ|紳士服|メンズ|アパレル|ファッション|ユニクロ|ZOZO|EC|通販|小売|百貨店"),
]


def rule_judge(it):
    text = it["title"] + " " + it.get("snippet", "")
    for level, tag, pat in RULES:
        if re.search(pat, text, re.I):
            return {"level": level, "summary": it["title"], "why": "", "tags": [tag], "company": "", "unjudged": True}
    return {"level": "C", "summary": it["title"], "why": "", "tags": [], "company": "", "unjudged": True}


def call_model(messages, model, token):
    system = messages[0]["content"]
    user = messages[1]["content"]
    body = json.dumps({
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 6000, "responseMimeType": "application/json"},
    }).encode()
    req = urllib.request.Request(ENDPOINT.format(model=model), data=body, method="POST", headers={
        "x-goog-api-key": token,
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.loads(r.read())
    parts = data["candidates"][0]["content"]["parts"]
    text = "".join(p.get("text", "") for p in parts)
    meta = data.get("usageMetadata", {})
    return text, {"prompt_tokens": meta.get("promptTokenCount", 0), "completion_tokens": meta.get("candidatesTokenCount", 0)}


def parse(text):
    try:
        return json.loads(text).get("items", [])
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        return json.loads(m.group(0)).get("items", []) if m else []


def quota_left():
    """無料枠の1日の回数は米国太平洋時間の0時にリセットされるので、その日付で数える。"""
    import datetime as dt
    from zoneinfo import ZoneInfo
    today = dt.datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")
    used = 0
    if QUOTA_FILE.exists():
        q = json.loads(QUOTA_FILE.read_text())
        used = q.get("used", 0) if q.get("date") == today else 0
    return today, used, max(0, min(PER_RUN_CAP, DAILY_CAP - used))


def save_quota(today, used):
    QUOTA_FILE.parent.mkdir(parents=True, exist_ok=True)
    QUOTA_FILE.write_text(json.dumps({"date": today, "used": used}))


def judge(items, token, profile, log, known_stories=None):
    # ルールでS/Aになりそうな記事から先にAIへ回す（上限に達しても重要な記事は確実にAI判定される）
    prio = {"S": 0, "A": 1, "B": 2, "C": 3}
    items = sorted(items, key=lambda x: prio[rule_judge(x)["level"]])
    today, used, budget = quota_left()
    log(f"quota: used={used} budget_this_run={budget}")
    system = SYSTEM.format(profile=profile, rubric=RUBRIC, tags="、".join(TAGS))
    models = list(MODELS)
    stories = dict(known_stories or {})
    results, model_idx, stats = {}, 0, {"requests": 0, "failed_batches": 0, "model": None, "tokens_in": 0, "tokens_out": 0, "requests_sent": 0, "skipped_by_quota": 0}
    for i in range(0, len(items), BATCH):
        batch = items[i:i + BATCH]
        lines = []
        for it in batch:
            line = f'id={it["id"]} | {it["title"]} | {"・".join(it["sources"][:3])}'
            if it.get("snippet"):
                line += f' | {it["snippet"][:100]}'
            lines.append(line)
        known = "、".join(list(stories)[-80:]) or "（なし）"
        user = f"既存の話題一覧：{known}\n\n記事一覧：\n" + "\n".join(lines)
        msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        if stats["requests_sent"] >= budget:
            stats["skipped_by_quota"] += len(batch)
            continue
        for attempt in range(5):
            if stats["requests_sent"] >= budget or not models:
                break
            model = models[model_idx % len(models)]
            stats["requests_sent"] += 1
            try:
                text, usage = call_model(msgs, model, token)
                stats["requests"] += 1
                stats["model"] = model
                stats["tokens_in"] += usage.get("prompt_tokens", 0)
                stats["tokens_out"] += usage.get("completion_tokens", 0)
                for r in parse(text):
                    if r.get("id") and r.get("level") in LEVELS:
                        results[r["id"]] = r
                        st = (r.get("story") or "").strip()[:20]
                        if st:
                            stories[st] = stories.get(st, 0) + 1
                break
            except urllib.error.HTTPError as e:
                body = e.read().decode(errors="ignore")
                m = re.search(r'"message":\s*"([^"]{0,120})', body)
                log(f"batch {i // BATCH}: HTTP {e.code} on {model}: {m.group(1) if m else ''}")
                if e.code in (400, 403, 404):
                    models.remove(model)  # このモデルは使えない → 候補から外す
                    continue
                if e.code == 429:
                    time.sleep(min(int(e.headers.get("retry-after", "30") or 30), 60))
                    model_idx += 1
                    continue
                # 500/503など（混雑・一時的な障害）：別のモデルに切り替え、待ち時間を延ばして再試行
                model_idx += 1
                time.sleep(min(10 * (2 ** attempt), 60))
            except Exception as e:  # noqa: BLE001
                log(f"batch {i // BATCH}: {type(e).__name__}")  # 例外の本文にはヘッダー値が含まれうるので出さない
                model_idx += 1
                time.sleep(10)
        else:
            stats["failed_batches"] += 1
        time.sleep(7)  # 無料枠の1分あたりの上限（10回）に配慮

    save_quota(today, used + stats["requests_sent"])
    out = []
    for it in items:
        r = results.get(it["id"])
        merged = dict(it)
        if r:
            merged.update({
                "level": r["level"],
                "summary": (r.get("summary") or it["title"])[:60],
                "why": (r.get("why") or "")[:50] if r["level"] in ("S", "A") else "",
                "tags": [t for t in r.get("tags", []) if t in TAGS][:2],
                "company": (r.get("company") or "")[:30],
                "story": (r.get("story") or "").strip()[:20],
            })
        else:  # AIで判定できなかった記事はキーワードのルールで判定する
            merged.update(rule_judge(it))
        out.append(merged)
    return out, stats


def group_stories(items):
    """同じ話題名の記事を1つの話題（カード）にまとめる。話題名がない記事はそれだけで1話題。"""
    order = {l: i for i, l in enumerate(LEVELS)}
    groups = {}
    for it in items:
        key = it.get("story") or f"_{it['id']}"
        groups.setdefault(key, []).append(it)
    stories = []
    for key, arts in groups.items():
        arts.sort(key=lambda x: (order[x["level"]], -(len(x.get("sources") or [])), x.get("published") or ""))
        lead = arts[0]
        srcs = []
        for a in arts:
            for s_ in a.get("sources") or []:
                if s_ not in srcs:
                    srcs.append(s_)
        stories.append({
            "story": "" if key.startswith("_") else key,
            "level": lead["level"],
            "summary": lead.get("summary") or lead["title"],
            "why": next((a["why"] for a in arts if a.get("why")), ""),
            "tags": lead.get("tags") or [],
            "company": lead.get("company") or "",
            "lanes": sorted({l for a in arts for l in (a.get("lanes") or [a["lane"]])}),
            "sources": srcs,
            "latest": max((a.get("published") or "" for a in arts), default=""),
            "articles": [{"id": a["id"], "title": a["title"], "url": a["url"], "source": (a.get("sources") or [""])[0],
                          "published": a.get("published"), "level": a["level"]} for a in
                         sorted(arts, key=lambda x: x.get("published") or "", reverse=True)],
        })
    stories.sort(key=lambda x: (order[x["level"]], -len(x["articles"]), x["latest"]), reverse=False)
    return stories


def write_report(items, stats, path):
    order = {l: i for i, l in enumerate(LEVELS)}
    items = sorted(items, key=lambda x: (order[x["level"]], x.get("published") or ""), reverse=False)
    cnt = {l: sum(1 for x in items if x["level"] == l) for l in LEVELS}
    lane_name = {"industry": "業界", "retail_ec": "小売・EC", "economy": "経済", "it_sec": "IT・セキュリティ"}
    L = ["# AI判定の検証レポート", "",
         f"- モデル: {stats['model']} / リクエスト: {stats['requests']}回 / 失敗バッチ: {stats['failed_batches']}",
         f"- トークン: 入力 {stats['tokens_in']:,} / 出力 {stats['tokens_out']:,}",
         f"- 件数: S {cnt['S']} / A {cnt['A']} / B {cnt['B']} / C {cnt['C']}（合計 {len(items)}・未判定 {sum(1 for x in items if x.get('unjudged'))}）", ""]
    st = group_stories(items)
    scnt = {l: sum(1 for x in st if x["level"] == l) for l in LEVELS}
    L += [f"- 話題数: S {scnt['S']} / A {scnt['A']} / B {scnt['B']} / C {scnt['C']}（記事{len(items)}件 → 話題{len(st)}件）", ""]
    for lvl, label in (("S", "【話題】S 最重要"), ("A", "【話題】A 重要")):
        L += [f"## {label}", ""]
        for x in [g for g in st if g["level"] == lvl]:
            name = f"《{x['story']}》" if x["story"] else ""
            why = f" ／ {x['why']}" if x.get("why") else ""
            L.append(f"- {name}{x['summary']}（{len(x['articles'])}記事・{len(x['sources'])}媒体）{why}")
        L.append("")
    for lvl, label, limit in (("B", "B 参考（先頭40件）", 40), ("C", "C 対象外（先頭40件）", 40)):
        L += [f"## {label}", ""]
        for x in [i for i in items if i["level"] == lvl][:limit]:
            tag = "・".join(x.get("tags") or [])
            why = f" ／ {x['why']}" if x.get("why") else ""
            L.append(f"- [{lane_name.get(x['lane'], x['lane'])}｜{tag}] {x['summary']}{why}  ＜元: {x['title'][:50]}＞")
        L.append("")
    path.write_text("\n".join(L), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--out")
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()
    token = os.environ.get("GEMINI_API_KEY", "").strip()
    if token and not re.fullmatch(r"[A-Za-z0-9_.\-]{20,120}", token):
        # 値はどこにも出力しない（公開リポジトリに残さないため）
        print("GEMINI_API_KEY の形式が正しくありません（英数字のキー1行だけを登録してください）。ルール判定で続行します。", flush=True)
        token = ""
    profile = os.environ.get("PROFILE", "").strip() or DEFAULT_PROFILE
    src = json.loads((ROOT / a.input).read_text(encoding="utf-8"))
    logs = []
    def log(m):
        m = str(m)
        if token:
            m = m.replace(token, "***")  # 念のため、キーが文字列に混ざっていたら伏せる
        m = m[:300]
        print(m, flush=True)
        logs.append(m)
    if token:
        items, stats = judge(src["items"], token, profile, log)
    else:
        log("GEMINI_API_KEY が未設定のため、ルール判定のみで処理します")
        items = [dict(it, **rule_judge(it)) for it in src["items"]]
        stats = {"requests": 0, "failed_batches": 0, "model": "rules-only", "tokens_in": 0, "tokens_out": 0}
    stats["profile"] = "secret" if os.environ.get("PROFILE", "").strip() else "default"
    if a.out:
        p = ROOT / a.out
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"generated": src.get("generated"), "stats": stats, "items": items, "stories": group_stories(items)}, ensure_ascii=False, indent=1), encoding="utf-8")
    if a.report:
        out = ROOT / "data" / "probe"
        write_report(items, stats, out / "judge.md")
        (out / "judged.json").write_text(json.dumps({"stats": stats, "logs": logs, "items": items, "stories": group_stories(items)}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(stats, ensure_ascii=False))


if __name__ == "__main__":
    main()
