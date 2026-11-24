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

RUBRIC = """重要度の基準（迷ったら低い方ではなく、読み手が「知らなかったら困る」かで判断）:
S＝最重要。次のいずれか。
 ・制度・法令・税・規制の新設/改正/施行/成立、またはその審議が大きく動いたもの（消費税・減税、特商法、景表法、個人情報保護法、最低賃金、社会保険、下請法/取適法、インボイス、メール配信規制など）
 ・郵便料金・DM/はがき・宅配便の運賃・送料・印刷/用紙価格の改定や制度変更（発送物と販促物のコストに直結）
 ・ECサイトやWebの基盤（PHP、WordPress、カートシステム、決済、メール配信、LINE等）の重大な脆弱性・大規模障害・規約変更、公的機関（IPA・JPCERT等）の注意喚起
 ・直接競合の経営の大きな動き（統合・買収・撤退・大規模閉店・全面的な価格改定）
A＝重要。直接競合の新商品・新サービス・販促・出店閉店・決算、他社ECの情報漏えい事故（再発防止の教訓）、EC・CRM・販促の実務に使える事例、消費・物価・為替の大きな変化、アパレル大手の戦略的な動き。
B＝参考。ファッション・アパレル全般の話題、小売一般、経済一般、テクノロジー一般で、読み手の業界と少しでも接点があるもの。
C＝対象外。芸能・エンタメ、スーツ以外の意味の「スーツ」、海外政治の詳細、読み手の仕事と接点がないもの。"""

SYSTEM = """あなたは日本のアパレル小売企業向けのニュース編集者です。与えられた記事一覧を、読み手の仕事にとっての重要度で判定し、JSONだけを返します。
{profile}

{rubric}

出力ルール:
- 形式: {{"items":[{{"id":"…","level":"S|A|B|C","summary":"…","why":"…","tags":["…"],"company":"…"}}]}}
- summary: 何が起きたかを主語・数字を残して40字以内の日本語で。見出しの言い換えで構わないが、煽り表現は削る。
- why: S/Aのみ。読み手の仕事にどう関係するかを30字以内で。B/Cは空文字。
- tags: 次から1〜2個 {tags}
- company: 主役の企業名（なければ空文字）
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


def judge(items, token, profile, log):
    # ルールでS/Aになりそうな記事から先にAIへ回す（上限に達しても重要な記事は確実にAI判定される）
    prio = {"S": 0, "A": 1, "B": 2, "C": 3}
    items = sorted(items, key=lambda x: prio[rule_judge(x)["level"]])
    today, used, budget = quota_left()
    log(f"quota: used={used} budget_this_run={budget}")
    system = SYSTEM.format(profile=profile, rubric=RUBRIC, tags="、".join(TAGS))
    results, model_idx, stats = {}, 0, {"requests": 0, "failed_batches": 0, "model": None, "tokens_in": 0, "tokens_out": 0, "requests_sent": 0, "skipped_by_quota": 0}
    for i in range(0, len(items), BATCH):
        batch = items[i:i + BATCH]
        lines = []
        for it in batch:
            line = f'id={it["id"]} | {it["title"]} | {"・".join(it["sources"][:3])}'
            if it.get("snippet"):
                line += f' | {it["snippet"][:100]}'
            lines.append(line)
        msgs = [{"role": "system", "content": system}, {"role": "user", "content": "\n".join(lines)}]
        if stats["requests_sent"] >= budget:
            stats["skipped_by_quota"] += len(batch)
            continue
        for attempt in range(4):
            if stats["requests_sent"] >= budget:
                break
            model = MODELS[model_idx]
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
                break
            except urllib.error.HTTPError as e:
                msg = e.read().decode(errors="ignore")[:200]
                log(f"batch {i // BATCH}: HTTP {e.code} on {model}: {msg}")
                if e.code in (400, 403, 404) and model_idx + 1 < len(MODELS):
                    model_idx += 1  # モデルが使えない → 次のモデルへ
                    continue
                if e.code == 429:
                    time.sleep(int(e.headers.get("retry-after", "30") or 30))
                    continue
                time.sleep(5)
            except Exception as e:  # noqa: BLE001
                log(f"batch {i // BATCH}: {type(e).__name__}")  # 例外の本文にはヘッダー値が含まれうるので出さない
                time.sleep(5)
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
            })
        else:  # AIで判定できなかった記事はキーワードのルールで判定する
            merged.update(rule_judge(it))
        out.append(merged)
    return out, stats


def write_report(items, stats, path):
    order = {l: i for i, l in enumerate(LEVELS)}
    items = sorted(items, key=lambda x: (order[x["level"]], x.get("published") or ""), reverse=False)
    cnt = {l: sum(1 for x in items if x["level"] == l) for l in LEVELS}
    lane_name = {"industry": "業界", "retail_ec": "小売・EC", "economy": "経済", "it_sec": "IT・セキュリティ"}
    L = ["# AI判定の検証レポート", "",
         f"- モデル: {stats['model']} / リクエスト: {stats['requests']}回 / 失敗バッチ: {stats['failed_batches']}",
         f"- トークン: 入力 {stats['tokens_in']:,} / 出力 {stats['tokens_out']:,}",
         f"- 件数: S {cnt['S']} / A {cnt['A']} / B {cnt['B']} / C {cnt['C']}（合計 {len(items)}・未判定 {sum(1 for x in items if x.get('unjudged'))}）", ""]
    for lvl, label, limit in (("S", "S 最重要", 999), ("A", "A 重要", 999), ("B", "B 参考（先頭40件）", 40), ("C", "C 対象外（先頭40件）", 40)):
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
        p.write_text(json.dumps({"generated": src.get("generated"), "stats": stats, "items": items}, ensure_ascii=False, indent=1), encoding="utf-8")
    if a.report:
        out = ROOT / "data" / "probe"
        write_report(items, stats, out / "judge.md")
        (out / "judged.json").write_text(json.dumps({"stats": stats, "logs": logs, "items": items}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(stats, ensure_ascii=False))


if __name__ == "__main__":
    main()
