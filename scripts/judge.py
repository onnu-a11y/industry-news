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
 ・制度・法令・税・規制が「決まった／成立した／施行される／具体的な中身や日程が変わった」もの（消費税・減税、特商法、景表法、個人情報保護法、最低賃金、社会保険、下請法/取適法、インボイス、公益通報、メール配信規制など）。国内のみ。読み手の業務（EC・販促・人事・税）に関わる制度に限る。
   貿易・海運の書類、金融・証券、医療・介護、建設、農林水産など他業界に固有の制度はSにしない（接点があればA、なければB）。
 ・郵便料金・DM/はがき・宅配便の運賃・送料・印刷/用紙価格の改定や制度変更（発送物と販促物のコストに直結）
 ・ECサイトやWebの基盤（PHP、WordPress、カートシステム、決済、メール配信、LINE等）の重大な脆弱性・大規模障害・規約変更、公的機関（IPA・JPCERT等）の注意喚起
 ・直接競合の経営の大きな動き（統合・買収・撤退・大規模閉店・全面的な価格改定）
 ※次はSにしない（Aにする）：他社の情報漏えい・事故の報道、制度をめぐる国会の駆け引き・審議日程・賛否の論評・世論調査。
 ※1つの道県・市町村だけの話（関東1都6県・関西2府4県を除く）はSにしない。仕事に関わるならA、そうでなければB。
A＝重要。次のいずれか。
 ・直接競合（上の前提メモの社名に限る）の新商品・新サービス・販促・出店閉店・決算
 ・ユニクロ・ワークマン・しまむら・無印良品などスーツ専門店以外の大手は、決算・経営戦略・価格改定・スーツやビジネスウェアへの参入だけ
 ・他社の情報漏えい・不正アクセスのうち、EC・通販・決済・メール配信が関わるもの、または100万件以上のもの
 ・制度の審議の山場（成立の見通し・施行日・税率・対象が動いた）
 ・EC・CRM・販促の実務にそのまま使える具体的な事例
 ・日経平均・TOPIXが前日比2%以上動いた、または節目を越えた日の市況
 ・日銀の金融政策の決定、利上げ時期の見方の大きな変化
 ・消費者物価・個人消費・賃金・月例経済報告・GDPなど主要統計の公表
 ・為替が節目（155円・160円など）を越えた、または1日に1円以上動いた
 ・アパレル大手の戦略的な動き
B＝参考。次はAではなくBにする（ただし大きく報道されている・業界で前例のない取り組みならA）。
 ・商品のレビュー・おすすめ・口コミ・ランキング・セール紹介の記事（どの会社でも）
 ・セミナー・ウェビナーの告知、資料の無料公開、サービスの宣伝
 ・アナリスト評価・株価予想の小幅な変動
 ・上の条件に当たらない他社の情報漏えい
 ・毎日の為替・株式の見通しや市況コメント（節目を越えていない日）、制度をめぐる政局（問責・辞任・党の駆け引き）
 ・論評・コラム・研究所のレポート・インタビュー（統計や決定そのものの公表はA）
 ・そのほか、ファッション・アパレル全般、小売一般、経済一般、テクノロジー一般で、読み手の業界と少しでも接点があるもの。
C＝対象外。芸能・エンタメ、スーツ以外の意味の「スーツ」、海外政治の詳細、読み手の仕事と接点がないもの。"""

SYSTEM = """あなたは日本のアパレル小売企業向けのニュース編集者です。与えられた記事一覧を、読み手の仕事にとっての重要度で判定し、JSONだけを返します。
{profile}

{rubric}

出力ルール:
- 形式: {{"items":[{{"id":"…","level":"S|A|B|C","summary":"…","why":"…","tags":["…"],"company":"…","theme":"…","scope":"全国|地域|海外"}}]}}
- summary: 何が起きたかを主語・数字を残して40字以内の日本語で。見出しの言い換えで構わないが、煽り表現は削る。
- why: S/Aのみ。読み手の仕事にどう関係するかを30字以内で。B/Cは空文字。
- tags: 次から1〜2個 {tags}
- company: 主役の企業名（なければ空文字）
- theme: 同じ政策・同じ出来事の流れを表す短い名前（例：「消費税減税法案」「楽天証券の情報漏えい」）。同じ流れの記事には必ず同じ名前を付ける。「不正アクセス全般」「ユニクロの新商品」のような分野・会社のくくりは不可で、その場合は空文字
- scope: 記事の範囲。全国＝日本全体に関わる話、地域＝1つの都道府県・市区町村だけの話（東京・大阪など関東・関西も含む）、海外＝日本国外の話
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


WEB_BASE = r"PHP|WordPress|EC-CUBE|MakeShop|Apache HTTP|NGINX|nginx|OpenSSL|Shopify|決済|LINE"


def cap_weekly(it):
    """JPCERTの週次レポートの個別製品の脆弱性は、Web/ECの基盤に関係するものを除きB止まりにする。"""
    if it["title"].startswith("Weekly Report") and not re.search(WEB_BASE, it["title"]):
        if it.get("level") in ("S", "A"):
            it["level"] = "B"
            it["why"] = ""


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


def judge(items, token, profile, log, prev_names=None, examples=""):
    # ルールでS/Aになりそうな記事から先にAIへ回す（上限に達しても重要な記事は確実にAI判定される）
    prio = {"S": 0, "A": 1, "B": 2, "C": 3}
    items = sorted(items, key=lambda x: prio[rule_judge(x)["level"]])
    today, used, budget = quota_left()
    log(f"quota: used={used} budget_this_run={budget}")
    system = SYSTEM.format(profile=profile, rubric=RUBRIC, tags="、".join(TAGS))
    if examples:  # M4-4：読み手が付け直した評価（AIの呼び出し回数は増えない）
        system += "\n\n" + examples
    models = list(MODELS)
    results, model_idx, stats = {}, 0, {"requests": 0, "failed_batches": 0, "model": None, "tokens_in": 0, "tokens_out": 0, "requests_sent": 0, "skipped_by_quota": 0}
    for i in range(0, len(items), BATCH):
        batch = items[i:i + BATCH]
        lines = []
        for it in batch:
            line = f'id={it["id"]} | {it["title"]} | {"・".join(it["sources"][:3])}'
            if it.get("snippet"):
                line += f' | {it["snippet"][:100]}'
            lines.append(line)
        user = "記事一覧：\n" + "\n".join(lines)
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
                    if isinstance(r, dict) and r.get("id") and r.get("level") in LEVELS:
                        results[r["id"]] = r
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
                "theme": (r.get("theme") or "").strip()[:30] if isinstance(r.get("theme"), str) else "",
                "scope": r.get("scope") if r.get("scope") in ("全国", "地域", "海外") else "",
            })
        else:  # AIで判定できなかった記事はキーワードのルールで判定する
            merged.update(rule_judge(it))
        out.append(merged)
    for it in out:
        cap_weekly(it)
    if models:
        n = cluster(out, token, models, log, budget - stats["requests_sent"], prev_names=prev_names)
        stats["requests_sent"] += n
        stats["cluster_requests"] = n
    save_quota(today, used + stats["requests_sent"])
    return out, stats


CLUSTER_SYSTEM = """あなたはニュース編集者です。記事一覧のうち「同じ出来事」を扱う記事をグループにまとめ、JSONだけを返します。
同じ出来事とは：同じ企業の同じ件（例：タワーレコードの情報流出）、同じ制度の同じ改正・法案（例：消費税減税法案）、同じ会社の同じ料金改定（例：佐川急便の運賃改定）。
禁止：テーマや分野でまとめること（「情報漏えい」「為替」「販促」「その他」などの総称でまとめない）。別の企業の別々の出来事は必ず別グループ。
グループ名（name）は固有名詞を含めて15字以内（例：「タワレコ情報流出」「消費税減税法案」「佐川急便運賃改定」）。
記事が1件だけの出来事はグループにしない。
{continuity}
形式：{{"groups":[{{"name":"…","ids":["…","…"]}}]}}"""


def cluster(items, token, models, log, budget_left, prev_names=None):
    """S/Aの記事を出来事ごとにまとめ、各記事に story を付ける。戻り値は使ったリクエスト数。"""
    targets = [it for it in items if it.get("level") in ("S", "A")]
    for it in items:
        it["story"] = ""
    if not targets or not token or budget_left <= 0:
        return 0
    cont = ""
    if prev_names:
        cont = "過去の話題名（同じ出来事の続報なら、この名前をそのまま使う）：" + "、".join(prev_names[:60])
    system = CLUSTER_SYSTEM.format(continuity=cont)
    used = 0
    for i in range(0, len(targets), 120):
        if used >= budget_left:
            break
        chunk = targets[i:i + 120]
        lines = [f'id={it["id"]} | {it["title"][:60]}' for it in chunk]
        msgs = [{"role": "system", "content": system}, {"role": "user", "content": "\n".join(lines)}]
        for model in models:
            used += 1
            try:
                text, _ = call_model(msgs, model, token)
                data = json.loads(text) if text.strip().startswith("{") else json.loads(re.search(r"\{.*\}", text, re.S).group(0))
                ids_in_chunk = {it["id"] for it in chunk}
                by_id = {it["id"]: it for it in chunk}
                for g in data.get("groups", []):
                    name = (g.get("name") or "").strip()[:20]
                    ids = [x for x in g.get("ids", []) if x in ids_in_chunk]
                    if name and len(ids) >= 2 and name not in BAD_STORY_NAMES:
                        for x in ids:
                            by_id[x]["story"] = name
                break
            except Exception as e:  # noqa: BLE001
                log(f"cluster {i // 120}: {type(e).__name__} on {model}")
                time.sleep(10)
        time.sleep(7)
    return used


BAD_STORY_NAMES = {"その他", "情報漏えい", "情報漏えい事故", "為替", "為替・金利", "販促", "競合販促", "トレンド", "消費・市況",
                   "出店・閉店", "EC・販促", "セキュリティ", "ECサイト運用", "商品開発トレンド", "制度・法令"}


def _bigrams(t):
    t = re.sub(r"\s|【.*?】", "", t or "")
    return {t[i:i + 2] for i in range(len(t) - 1)}


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
        if len(arts) >= 3:
            # 複数の出来事をまとめて伝える記事（速報まとめ等）が代表にならないよう、
            # 同じ重要度の中で「ほかの記事と見出しが最も近い記事」を代表にする
            top = [a for a in arts if a["level"] == lead["level"]]
            if len(top) >= 2:
                def centrality(a):
                    A = _bigrams(a["title"])
                    return sum(len(A & _bigrams(b["title"])) / max(1, min(len(A), len(_bigrams(b["title"]))))
                               for b in arts if b is not a) / (len(arts) - 1)
                lead = max(top, key=centrality)
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


# ---------------------------------------------------------------- M4-6：判定の上限と「大きく報道」の引き上げ
# AIの判断がぶれても確実に効くよう、話題（カード）単位でかける。
NON_KK = ("北海道|青森|岩手|宮城|秋田|山形|福島|新潟|富山|石川|福井|山梨|長野|岐阜|静岡|愛知|三重|鳥取|島根|岡山|広島|山口"
          "|徳島|香川|愛媛|高知|福岡|佐賀|長崎|熊本|大分|宮崎|鹿児島|沖縄")
RE_REGION = re.compile(rf"(?:{NON_KK})(?:県|道|市|労働局)?|県内|市内|町内|村内|地元")
RE_NATIONAL = re.compile(r"全国|政府|国会|閣議|全店|国内全|全都道府県")
RE_REVIEW = re.compile(r"レビュー|試着|購入レポ|口コミ|絶賛|好評|狙い目|即買い|おすすめ|オススメ|\d+選|ランキング|第\d+位|使い心地|買ってよかった"
                       r"|色違い|写真特集|＜画像|<画像|忖度なし|コスパ|買い逃|\d+アイテム|即完|お値段以上|驚いた")
RE_PROMO = re.compile(r"セミナー|ウェビナー|オンライン開催|資料.{0,6}(?:公開|ダウンロード|プレゼント)|無料公開|アナリスト(?:評価|予想)|目標株価|レーティング|株予報")
# スーツ専門店以外の大手は、経営の動き（決算・戦略・価格改定・出店閉店・参入）以外はBまで
RE_MASS = re.compile(r"ユニクロ|ワークマン|しまむら|無印良品|ジーユー|\bGU\b|アベイル")
RE_MASS_BIZ = re.compile(r"決算|業績|売上|営業利益|社長|戦略|値上げ|価格改定|出店|閉店|オープン|参入|買収|提携|上場|株価|不正アクセス|漏え|流出|障害")
# 毎日の相場コメントはBまで（節目越え・急変・「○年ぶり」などの大きな動きはAのまま）
RE_MARKET = re.compile(r"見通し|もみ合い|上値重|下値堅|底堅|注文情報|市況概況|マーケットダイジェスト|外為：|為替：|NY円|東京外為|東京為替|欧州為替|NY為替|予想レンジ|今日の外為|相場展望|を挟んだ取り引き|小幅な値動き")
RE_BIGMOVE = re.compile(r"突破|割れ|超え|ぶり(?:の)?(?:高|安|水準)|急騰|急落|大幅|[0-9０-９,，]{4,}円(?:高|安|値上がり|値下がり)")
# 大ヒット・記録・革新の言葉がある記事は上限をかけない（AIの判定を残す）
RE_HIT = re.compile(r"大ヒット|ヒット商品|完売|売り切れ|品薄|過去最高|最高益|記録的|販売数.{0,6}(?:突破|超)|[0-9０-９]+万(?:枚|着|個|本|足)"
                    r"|社会現象|業界初|世界初|日本初|国内初|新業態|新サービス|生成AI|\bAI\b|ＡＩ|DX")
BIG_MIN_ARTICLES, BIG_MIN_OUTLETS = 8, 4
# M4-6.1：「大きく報道」の本数に数えない記事（1つの都道府県・市区町村だけの話、海外の話）
# AI の scope（地域・海外）と、見出しの言葉の2つで見分け、どちらかに当たれば数えない（見落としを減らすため二重にする）
PREFS = ("北海道|青森|岩手|宮城|秋田|山形|福島|茨城|栃木|群馬|埼玉|千葉|東京|神奈川|新潟|富山|石川|福井|山梨|長野|岐阜|静岡|愛知|三重"
         "|滋賀|京都|大阪|兵庫|奈良|和歌山|鳥取|島根|岡山|広島|山口|徳島|香川|愛媛|高知|福岡|佐賀|長崎|熊本|大分|宮崎|鹿児島|沖縄")
RE_LOCAL = re.compile(rf"(?:{PREFS})(?:県|府|都|労働局)|北海道|(?:{NON_KK})(?![゠-ヿ])|県内|府内|都内|道内|市内|町内|村内|区内|地元"
                      r"|[一-鿿]{1,4}(?:市|町|村)(?:長|議会|議|内|役所)|[一-鿿]{1,4}市[北南東西中]区")
KANA = r"\u30a0-\u30ff"
RE_FOREIGN = re.compile(r"米国|米政府|中国政府|韓国|台湾|香港|英国|欧州|EU|豪州|中東|北朝鮮|"
                        rf"(?<![{KANA}])(?:アメリカ|トランプ|インド|ベトナム|タイ|インドネシア|フィリピン|マレーシア|シンガポール|バングラデシュ"
                        rf"|カンボジア|ミャンマー|パキスタン|イギリス|フランス|ドイツ|イタリア|スペイン|ロシア|ウクライナ|ブラジル|メキシコ|カナダ"
                        rf"|オーストラリア)(?![{KANA}])")
RE_JAPAN = re.compile(r"日本|国内|政府|日銀|円(?:安|高)|全国|訪日|インバウンド|政府・与党|国会")
RE_OPINION = re.compile(r"なぜ|？|\?|！|!|疑う|みんなの広場|論説|社説|コラム|考える|識者|インタビュー|寄稿|オピニオン|意見|視点|【注目】|【PR】")


def counts_as_national(it):
    """「大きく報道」の本数に数える記事か。地域・海外の記事は数えない。"""
    if it.get("scope") in ("地域", "海外"):
        return False
    title = it.get("title") or ""
    if RE_LOCAL.search(title) and not RE_NATIONAL.search(title):
        return False
    if RE_FOREIGN.search(title) and not RE_JAPAN.search(title):
        return False
    return True


def finalize(stories, items, log=print):
    """上限（地域はSにしない／レビュー・告知・株価予想はBまで）をかけ、同じテーマが大きく報道された日は代表1件をSにする。"""
    order = {l: i for i, l in enumerate(LEVELS)}
    st = {"region": 0, "review": 0, "promo": 0, "big": [], "not_counted": {}}
    by_id = {it["id"]: it for it in items}
    # テーマごとの記事数・媒体数（C以外、かつ全国の記事だけを数える）
    by_theme = {}
    for it in items:
        t = (it.get("theme") or "").strip()
        if not t or it.get("level") == "C":
            continue
        d = by_theme.setdefault(t, {"n": 0, "outlets": set(), "raw": 0})
        d["raw"] += 1
        if counts_as_national(it):
            d["n"] += 1
            d["outlets"].add((it.get("sources") or [""])[0])
    for t, d in by_theme.items():
        if d["raw"] >= BIG_MIN_ARTICLES and d["raw"] != d["n"]:
            st["not_counted"][t] = d["raw"] - d["n"]
    id_theme = {it["id"]: (it.get("theme") or "").strip() for it in items}
    for s in stories:
        ts = [id_theme.get(a["id"], "") for a in s["articles"]]
        ts = [t for t in ts if t]
        s["theme"] = max(set(ts), key=ts.count) if ts else ""

    def is_big(d):
        return d and d["n"] >= BIG_MIN_ARTICLES and len(d["outlets"]) >= BIG_MIN_OUTLETS

    for s in stories:
        if is_big(by_theme.get(s.get("theme") or "")):
            s["big"] = by_theme[s["theme"]]["n"]
    for s in stories:
        if s["level"] not in ("S", "A"):
            continue
        heads = " ".join(a["title"] for a in s["articles"][:3]) + " " + (s.get("summary") or "")
        lead = s["articles"][0]["title"] if s["articles"] else ""
        exempt = bool(s.get("big") or RE_HIT.search(lead))  # 大きく報道・大ヒット・革新の話題はBへの上限をかけない
        if exempt:
            pass
        elif RE_REVIEW.search(lead):
            s["level"], s["capped"] = "B", "review"; st["review"] += 1
        elif RE_MASS.search(lead) and not RE_MASS_BIZ.search(lead):
            s["level"], s["capped"] = "B", "review"; st["review"] += 1
        elif RE_MARKET.search(lead) and not RE_BIGMOVE.search(lead):
            s["level"], s["capped"] = "B", "market"; st["market"] = st.get("market", 0) + 1
        elif RE_PROMO.search(lead):
            s["level"], s["capped"] = "B", "promo"; st["promo"] += 1
        if s["level"] == "S" and RE_REGION.search(heads) and not RE_NATIONAL.search(heads):
            s["level"], s["capped"] = "A", "region"; st["region"] += 1
        if s.get("capped"):
            s["why"] = s["why"] if s["level"] == "A" else ""

    def lead_item(s):
        a = s["articles"][0] if s["articles"] else {}
        return by_id.get(a.get("id"), a)

    for t, d in by_theme.items():
        if not is_big(d):
            continue
        # 代表の候補：S・A・B のうち、上限で下げた話題・地域や海外の話題を除く（M4-6.1 で B も対象に）
        cand = [s for s in stories if s.get("theme") == t and s["level"] in ("S", "A", "B")
                and s.get("capped") not in ("review", "promo", "market", "region") and counts_as_national(lead_item(s))]
        if not cand:
            continue
        if not any(s["level"] == "S" for s in cand):
            def rank(s):
                lead = s["articles"][0]["title"] if s["articles"] else ""
                law = bool({"制度・法令", "税・価格"} & set(s.get("tags") or []))
                near = len(_bigrams(t) & _bigrams(lead)) / max(1, len(_bigrams(t)))  # 見出しがテーマ名に近いほど代表向き
                return (order[s["level"]], bool(RE_OPINION.search(lead)), -round(near, 1), not law,
                        -len(s["articles"]), -len(s.get("sources") or []))
            rep = sorted(cand, key=rank)[0]
            if rep["level"] == "B" and not rep.get("why"):
                rep["why"] = f"大きく報道（{d['n']}本）"
            rep["level_before_boost"] = rep["level"]
            rep["level"], rep["boosted"] = "S", True
        st["big"].append(f"{t}:{d['n']}本/{len(d['outlets'])}媒体")
    stories.sort(key=lambda x: (order[x["level"]], -len(x["articles"]), x.get("latest") or ""))
    nc = "、".join(f"{k}:{v}本" for k, v in st["not_counted"].items()) or "なし"
    log(f"上限：地域{st['region']}件・レビュー{st['review']}件・告知等{st['promo']}件・相場コメント{st.get('market', 0)}件"
        f"／大きく報道：{'、'.join(st['big']) or 'なし'}／地域・海外で数えなかった記事：{nc}")
    return st


# ---------------------------------------------------------------- M4-3②：S・Aの話題の要約（30秒・要点・図解）
SUMMARY_BATCH = 5
NA = "記事に記載なし"
SUM_SYSTEM = """あなたは日本のメンズスーツ専門店のEC・販促担当者向けのニュース編集者です。与えられた複数の話題それぞれを、スマホで30秒で把握できる形にまとめ、JSONだけを返します。
{profile}

厳守：入力に書かれていない数字・金額・割合・日付・社名は絶対に書かない。分からない項目は「記事に記載なし」と書く。推測で埋めない。
話題ごとの材料の種類：
 ・［本文あり］見出しと本文 → 30秒・要点・図解を作る
 ・［冒頭のみ］見出しと本文の冒頭 → 30秒だけ作る。points は空配列、diagram の type は none
 ・［見出しのみ］→ 30秒だけ作る。points は空配列、diagram の type は none
形式：{{"items":[{{"key":"入力のkeyをそのまま",
 "quick":{{"what":"何が起きたか（40字以内）","when":"いつから・いつまで（入力にある日付だけ。無ければ「記事に記載なし」）","me":"読み手の仕事への影響と、やること（40字以内）"}},
 "points":["要点（各40字以内、3〜5個。入力に無い内容は書かない）"],
 "diagram":{{"type":"change|steps|impact|none","title":"図の見出し（15字以内）",
   "rows":[{{"label":"項目","before":"変更前","after":"変更後"}}],
   "steps":[{{"label":"段階名","state":"done|now|todo"}}],
   "impact":["送料設定|DM・はがき|EC基盤|メルマガ|ポイント|競合調査|価格設定|採用・人件費 から該当するもの"]}},
 "level_recheck":"S|A|B|C"}}]}}
図の選び方：料金・価格・条件が変わる話→change（rowsを使う）、法律・制度の進み具合→steps、それ以外で自社業務に関係→impact、図にする材料が無い→none。使わない欄は空配列。
level_recheck：重要度Sの話題だけ、本文を読んだうえで重要度を付け直す。海外の話・読み手の仕事と無関係・読み手の業務（EC・販促・人事・税）に関わらない制度・Sの基準（国内の制度の決定や施行、運賃・郵便料金の改定、EC基盤の重大な脆弱性、直接競合の大きな動き）に当たらない場合は A/B/C に下げる。S以外の話題は空文字。
入力の全keyについて必ず1件ずつ返す。"""

QUESTION_OF = {"when": "いつから適用？", "what": "何が決まった？", "me": "自社への影響は？"}
TODO_TAGS = {"制度・法令", "税・価格"}


def norm_num(s: str) -> str:
    import unicodedata
    return unicodedata.normalize("NFKC", s or "").replace(",", "").replace("，", "")


NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def unsupported_numbers(output, source_text: str):
    """AIの出力に含まれる数字のうち、入力に無いものを返す（verify.py と同じ判定）。"""
    src_nums = set(NUM_RE.findall(norm_num(source_text)))
    out_text = norm_num(output if isinstance(output, str) else json.dumps(output, ensure_ascii=False))
    bad = []
    for n in NUM_RE.findall(out_text):
        if n in src_nums or any(s.endswith(n) or s.lstrip("0") == n.lstrip("0") for s in src_nums):
            continue
        bad.append(n)
    return sorted(set(bad))


def _story_input(s, b):
    head = [f"key={s['key']}", f"重要度：{s['level']}", f"話題：{s.get('story') or s['summary']}"]
    for a in s["articles"][:6]:
        head.append(f"・{a['title']}（{a.get('source', '')}）")
    texts = (b or {}).get("texts") or []
    partial = (b or {}).get("partial")
    if texts:
        kind = "本文あり"
        for t, src, body in texts[:2]:
            head.append(f"―― 本文：{t}（{src}）\n{body[:2500]}")
    elif partial:
        kind = "冒頭のみ"
        head.append(f"―― 本文の冒頭：{partial[0]}（{partial[1]}）\n{partial[2][:1200]}")
    else:
        kind = "見出しのみ"
    return f"【{kind}】" + "\n".join(head), kind


def _loads(text):
    """AIの応答からJSONを取り出す。前後の説明文・コードの囲み・制御文字・末尾のカンマに耐える。"""
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?|```$", "", t, flags=re.M).strip()
    m = re.search(r"\{.*\}", t, re.S)
    if not m:
        return None
    t = m.group(0)
    for cand in (t, re.sub(r",\s*([}\]])", r"\1", t)):
        try:
            d = json.loads(cand, strict=False)
            return d if isinstance(d, dict) else None
        except json.JSONDecodeError:
            continue
    return None


def _clean_summary(o, src, kind):
    """出力を整え、入力に無い数字を含む欄は捨てる。"""
    def ok(text):
        return not unsupported_numbers(str(text), src)

    q = o.get("quick") or {}
    quick = {}
    for k, lim in (("what", 60), ("when", 60), ("me", 60)):
        v = str(q.get(k) or "").strip()[:lim]
        quick[k] = v if v and ok(v) else NA
    out = {"quick": quick, "points": [], "diagram": {"type": "none"}}
    if kind == "本文あり":
        out["points"] = [str(p)[:60] for p in (o.get("points") or []) if p and NA not in str(p) and ok(p)][:5]
        d = o.get("diagram") or {}
        t = d.get("type") if d.get("type") in ("change", "steps", "impact") else "none"
        dd = {"type": t, "title": str(d.get("title") or "")[:20] if ok(d.get("title") or "") else ""}
        if t == "change":
            rows = [{"label": str(r.get("label", ""))[:20], "before": str(r.get("before", ""))[:30], "after": str(r.get("after", ""))[:30]}
                    for r in d.get("rows") or [] if isinstance(r, dict) and ok(json.dumps(r, ensure_ascii=False))]
            rows = [r for r in rows if r["label"] and (r["before"] or r["after"])][:6]
            dd["rows"] = rows
            if not rows:
                dd = {"type": "none"}
        elif t == "steps":
            steps = [{"label": str(x.get("label", ""))[:20], "state": x.get("state") if x.get("state") in ("done", "now", "todo") else "todo"}
                     for x in d.get("steps") or [] if isinstance(x, dict) and x.get("label") and ok(x.get("label"))][:6]
            dd["steps"] = steps
            if len(steps) < 2:
                dd = {"type": "none"}
        elif t == "impact":
            imp = [str(x)[:12] for x in d.get("impact") or [] if x][:6]
            dd["impact"] = imp
            if not imp:
                dd = {"type": "none"}
        out["diagram"] = dd
    lr = o.get("level_recheck")
    out["level_recheck"] = lr if lr in LEVELS else ""
    return out


def summarize(stories, bodies, token, log, profile):
    """S・Aの話題に quick / points / diagram を付け、Sの再判定・要追加調査・質問を付ける。戻り値は統計。"""
    stats = {"requests": 0, "done": 0, "failed": 0, "dropped_fields": 0, "demoted": 0}
    targets = [s for s in stories if s["level"] in ("S", "A")]
    targets.sort(key=lambda s: (s["level"] != "S", s.get("body") != "ok"))
    today, used, budget = quota_left()
    system = SUM_SYSTEM.format(profile=profile)
    models = list(MODELS)
    if token and budget > 0:
        for i in range(0, len(targets), SUMMARY_BATCH):
            if stats["requests"] >= budget:
                log(f"summary: 無料枠の上限のため残り{len(targets) - i}話題は要約なし")
                break
            chunk = targets[i:i + SUMMARY_BATCH]
            inputs = {s["key"]: _story_input(s, bodies.get(s["key"])) for s in chunk}

            def ask(keys, tries):
                user = "\n\n".join(inputs[k][0] for k in keys)
                for attempt in range(tries):
                    if stats["requests"] >= budget:
                        return None
                    model = models[attempt % len(models)]  # 再試行のたびに別のモデルへ（11/26 は同じモデルの503が続いた）
                    stats["requests"] += 1
                    try:
                        text, _ = call_model([{"role": "system", "content": system}, {"role": "user", "content": user}], model, token)
                        d = _loads(text)
                        if d:
                            return d
                        log(f"summary {i // SUMMARY_BATCH}: 読めない応答 on {model}")
                    except Exception as e:  # noqa: BLE001
                        log(f"summary {i // SUMMARY_BATCH}: {type(e).__name__} {getattr(e, 'code', '')} on {model}")
                        time.sleep(min(15 * (attempt + 1), 45))
                return None

            data = ask(list(inputs), 3)
            items = list((data or {}).get("items", []))
            got_keys = {str(o.get("key")) for o in items if isinstance(o, dict)}
            missing = [k for k in inputs if k not in got_keys]
            if missing and len(inputs) > 1:  # 束で失敗した話題は1話題ずつ（応答が短くなり読み取りやすい）
                for k in missing:
                    one = [o for o in (ask([k], 2) or {}).get("items", []) if isinstance(o, dict)]
                    if len(one) == 1:
                        one[0]["key"] = k  # 1話題だけ聞いたので、keyの書き間違いがあっても対応づける
                    items += one
                    time.sleep(4)
            data = {"items": items}
            got = {str(o.get("key")): o for o in (data or {}).get("items", []) if isinstance(o, dict)}
            for s in chunk:
                o = got.get(s["key"])
                if not o:
                    stats["failed"] += 1
                    continue
                src, kind = inputs[s["key"]]
                before = json.dumps(o, ensure_ascii=False)
                c = _clean_summary(o, src, kind)
                if unsupported_numbers(before, src):
                    stats["dropped_fields"] += 1
                s["quick"], s["points"], s["diagram"] = c["quick"], c["points"], c["diagram"]
                # Sの再判定：本文を読めた時だけ下げる（見出しだけで下げると取りこぼしが増えるため）
                if s["level"] == "S" and not s.get("boosted") and s.get("body") == "ok" and c["level_recheck"] in ("A", "B", "C"):
                    s["level_orig"] = "S"
                    s["level"] = c["level_recheck"]
                    stats["demoted"] += 1
                    log(f"summary: S→{s['level']}「{(s.get('story') or s['summary'])[:24]}」")
                stats["done"] += 1
            time.sleep(7)
        save_quota(today, used + stats["requests"])
    # 要追加調査と質問（AIが使えなかった日も付ける）
    for s in targets:
        q = s.get("quick") or {}
        s["questions"] = [QUESTION_OF[k] for k in ("when", "what", "me") if q and q.get(k) == NA]
        s["todo"] = s.get("body", "none") == "none" and (
            s.get("level_orig", s["level"]) == "S" or (s["level"] == "A" and bool(TODO_TAGS & set(s.get("tags") or []))))
        if s["todo"] and not s["questions"]:
            s["questions"] = ["いつから適用？", "自社への影響は？"]
    log(f"summary: {stats['done']}話題（失敗 {stats['failed']}・数字の不一致で一部削除 {stats['dropped_fields']}・Sから下げ {stats['demoted']}）"
        f" AI {stats['requests']}回")
    return stats


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
        for it in items:
            cap_weekly(it)
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
