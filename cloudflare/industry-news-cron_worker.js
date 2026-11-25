// industry-news-cron：①毎朝 GitHub Actions の daily を起動する ②画面で付けた評価（S/A/B/C）を受け付けて KV に置く
// 必要な設定：Secret「GH_TOKEN」（industry-news 専用の fine-grained トークン、Actions: Read and write）
//             KV のひも付け「RATINGS」（名前空間 industry-news-ratings）
// 定時（UTC）：17 20 * * * ＝ 5:17 JST ／ 47 20 * * * ＝ 5:47 JST（作成済みなら daily 側は評価の取り込みだけ行う）
// 受け口：POST /（本文は [["日付:記事ID or 日付:話題名","S|A|B|C|''"],...]、最大50件）
//   自分のページ（https://onnu-a11y.github.io）からだけ・形式検査・1日200件まで・4日で自動消去
// ※ 11/25 はダッシュボードの API（Chrome で開いた画面から）で反映。編集画面に手入力する時は1行版（industry-news-cron_worker.min.js）を使う
const G = "https://api.github.com/repos/onnu-a11y/industry-news/actions/workflows/daily.yml/dispatches";
const O = "https://onnu-a11y.github.io";
const K = /^\d{4}-\d\d-\d\d:[^\s<>"]{1,60}$/;

async function dispatch(env) {
  let r = [];
  try {
    // 評価は値を持たず、メタデータ {l: 評価, t: 受け付け時刻} に入れてあるので、一覧の取得1回で全部読める
    const l = await env.RATINGS.list({ prefix: "r:" });
    r = l.keys.map(k => [k.name.slice(2), (k.metadata || {}).l || "", (k.metadata || {}).t || 0]).sort((a, b) => b[2] - a[2]);
    while (r.length && new TextEncoder().encode(JSON.stringify(r)).length > 60000) r.pop(); // 入力は合計65,535文字まで（日本語でも超えないようバイト数で数える）
  } catch (e) {
    console.log("list: " + e);
  }
  const i = { force: "false" };
  if (r.length) i.ratings = JSON.stringify(r);
  const x = await fetch(G, {
    method: "POST",
    headers: { Authorization: "Bearer " + env.GH_TOKEN, Accept: "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "industry-news-cron", "Content-Type": "application/json" },
    body: JSON.stringify({ ref: "main", inputs: i }),
  });
  console.log("dispatch: HTTP " + x.status + " ratings=" + r.length); // 204 なら成功
  if (x.status !== 204) console.log((await x.text()).slice(0, 300));
}

async function rate(q, env) {
  const h = { "Access-Control-Allow-Origin": O, "Content-Type": "text/plain" };
  const res = (b, s) => new Response(b, { status: s, headers: h });
  if (q.method !== "POST" || q.headers.get("Origin") !== O) return res("forbidden", 403);
  let a;
  try { a = JSON.parse(await q.text()); } catch (e) { return res("bad", 400); }
  if (!Array.isArray(a) || a.length > 50) return res("bad", 400);
  const v = a.filter(x => Array.isArray(x) && typeof x[0] === "string" && K.test(x[0]) && ["S", "A", "B", "C", ""].includes(x[1]));
  const c = "n:" + new Date().toISOString().slice(0, 10);
  const n = +((await env.RATINGS.get(c)) || 0);
  if (n + v.length > 200) return res("limit", 429);
  try {
    for (const x of v) await env.RATINGS.put("r:" + x[0], "", { expirationTtl: 345600, metadata: { l: x[1], t: Date.now() } });
  } catch (e) {
    return res("retry", 503); // 同じ key への書き込みは1秒に1回まで。画面側が後で送り直す
  }
  try { await env.RATINGS.put(c, String(n + v.length), { expirationTtl: 172800 }); } catch (e) {}
  return res("ok " + v.length, 200);
}

export default {
  scheduled(e, env, ctx) { ctx.waitUntil(dispatch(env)); },
  fetch(q, env) { return rate(q, env); },
};
