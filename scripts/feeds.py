"""取得元の定義（4レーン）。

lane: industry / retail_ec / economy / it_sec
kind: direct = 媒体の公式RSS, gnews = Googleニュースのキーワード検索RSS
"""
from urllib.parse import quote_plus

GNEWS_TAIL = "&hl=ja&gl=JP&ceid=JP:ja"


def gnews(query: str, window: str = "2d") -> str:
    return f"https://news.google.com/rss/search?q={quote_plus(query + ' when:' + window)}{GNEWS_TAIL}"


FEEDS = [
    # ---- 業界（スーツ・メンズ・アパレル全般） ----
    {"id": "wwd", "lane": "industry", "kind": "direct", "name": "WWDJAPAN",
     "url": "https://www.wwdjapan.com/feed"},
    {"id": "fsnap", "lane": "industry", "kind": "direct", "name": "FASHIONSNAP",
     "url": "https://www.fashionsnap.com/rss.xml"},
    {"id": "g_suit_cos", "lane": "industry", "kind": "gnews", "name": "Google: 紳士服各社",
     "url": gnews('青山商事 OR 洋服の青山 OR AOKI OR コナカ OR はるやま OR "SUIT SELECT" OR "FABRIC TOKYO" OR "麻布テーラー"')},
    {"id": "g_suit_mkt", "lane": "industry", "kind": "gnews", "name": "Google: スーツ市場",
     "url": gnews("スーツ OR オーダースーツ OR 紳士服 OR ビジネスウェア OR セットアップ -スーツケース -ウェットスーツ -ドライスーツ -宇宙服 -パワードスーツ")},
    {"id": "g_apparel", "lane": "industry", "kind": "gnews", "name": "Google: アパレル各社",
     "url": gnews("ユニクロ OR ファーストリテイリング OR ワークマン OR しまむら OR ZOZO OR アダストリア OR ユナイテッドアローズ OR オンワード")},

    # ---- 小売・EC ----
    {"id": "g_ec", "lane": "retail_ec", "kind": "gnews", "name": "Google: EC・通販",
     "url": gnews("EC OR ネット通販 OR ネットショップ OR オンラインストア 小売")},
    {"id": "g_ec_platform", "lane": "retail_ec", "kind": "gnews", "name": "Google: ECプラットフォーム",
     "url": gnews('"EC-CUBE" OR MakeShop OR Shopify OR futureshop OR カラーミーショップ OR "BASE" EC OR 楽天市場 OR "Amazon"', "3d")},
    {"id": "g_crm", "lane": "retail_ec", "kind": "gnews", "name": "Google: 販促・CRM",
     "url": gnews('"LINE公式アカウント" OR "LINEヤフー" OR メルマガ OR ポイント還元 OR 販促 キャンペーン', "3d")},
    {"id": "g_consume", "lane": "retail_ec", "kind": "gnews", "name": "Google: 消費・百貨店",
     "url": gnews("百貨店 売上 OR 個人消費 OR 消費動向 OR 小売 既存店")},
    {"id": "itm_biz", "lane": "retail_ec", "kind": "direct", "name": "ITmedia ビジネス",
     "url": "https://rss.itmedia.co.jp/rss/2.0/business.xml"},

    # ---- 経済 ----
    {"id": "toyokeizai", "lane": "economy", "kind": "direct", "name": "東洋経済オンライン",
     "url": "https://toyokeizai.net/list/feed/rss"},
    {"id": "nhk_econ", "lane": "economy", "kind": "direct", "name": "NHK 経済",
     "url": "https://www.nhk.or.jp/rss/news/cat5.xml"},
    {"id": "g_macro", "lane": "economy", "kind": "gnews", "name": "Google: 為替・物価・金利",
     "url": gnews("円安 OR 円高 OR 為替 OR 消費者物価 OR 値上げ OR 日銀 金利")},
    {"id": "g_labor", "lane": "economy", "kind": "gnews", "name": "Google: 賃金・政策",
     "url": gnews("賃上げ OR 最低賃金 OR 物価高対策 OR 減税 OR 経済対策", "3d")},

    # ---- IT・セキュリティ・規制 ----
    {"id": "jpcert", "lane": "it_sec", "kind": "direct", "name": "JPCERT/CC",
     "url": "https://www.jpcert.or.jp/rss/jpcert.rdf", "hours": 168},
    {"id": "ipa", "lane": "it_sec", "kind": "direct", "name": "IPA 重要なセキュリティ情報",
     "url": "https://www.ipa.go.jp/security/alert-rss.rdf", "hours": 168},
    {"id": "itm_news", "lane": "it_sec", "kind": "direct", "name": "ITmedia NEWS",
     "url": "https://rss.itmedia.co.jp/rss/2.0/news_bursts.xml"},
    {"id": "g_sec", "lane": "it_sec", "kind": "gnews", "name": "Google: 脆弱性・漏えい",
     "url": gnews("脆弱性 OR 不正アクセス OR 情報漏えい OR クレジットカード情報 流出 OR 緊急メンテナンス")},
    {"id": "g_rule", "lane": "it_sec", "kind": "gnews", "name": "Google: 規制・ルール",
     "url": gnews("個人情報保護法 OR 特定商取引法 OR 景品表示法 OR ステマ規制 OR 送信ドメイン認証 OR Gmail ガイドライン", "7d"), "hours": 168},
    {"id": "g_policy", "lane": "economy", "kind": "gnews", "name": "Google: 制度・税",
     "url": gnews("消費税 OR 消費減税 OR インボイス OR 電子帳簿保存 OR 下請法 OR 取適法 OR 社会保険 適用拡大 OR 改正法 施行", "3d")},
]

LANES = {
    "industry": "業界",
    "retail_ec": "小売・EC",
    "economy": "経済",
    "it_sec": "IT・セキュリティ",
}
