"""M4-6 検証用：同じ記事を「今の基準（old）」と「新しい基準（new）」で判定し、件数と中身を比べる。
本番の data/days には書かない。結果は data/probe/compare.md と compare.json に保存する。
使い方: python scripts/compare.py --old /tmp/judge_old.py --hours 30
"""
import argparse
import collections
import importlib.util
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import collect  # noqa: E402
import judge  # noqa: E402

ROOT = collect.ROOT
LN = {"industry": "業界", "retail_ec": "小売EC", "economy": "経済", "it_sec": "IT"}


def load_old(path):
    spec = importlib.util.spec_from_file_location("judge_old", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def table(stories):
    c = collections.Counter((s["level"], (s.get("lanes") or ["?"])[0]) for s in stories)
    rows = ["| 重要度 | 業界 | 小売EC | 経済 | IT | 計 |", "| --- | --- | --- | --- | --- | --- |"]
    for l in "SABC":
        n = [c[(l, k)] for k in LN]
        rows.append(f"| {l} | " + " | ".join(map(str, n)) + f" | {sum(n)} |")
    return "\n".join(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--old", required=True)
    ap.add_argument("--hours", type=int, default=30)
    a = ap.parse_args()
    token = os.environ.get("GEMINI_API_KEY", "").strip()
    log = lambda m: print(str(m)[:300], flush=True)  # noqa: E731
    profile = os.environ.get("PROFILE", "").strip() or judge.DEFAULT_PROFILE
    _, _, items = collect.collect(a.hours)
    log(f"記事 {len(items)}件")
    old = load_old(a.old)
    oj, ost_ = old.judge([dict(i) for i in items], token, profile, log)
    ost = old.group_stories(oj)
    nj, nst_ = judge.judge([dict(i) for i in items], token, profile, log)
    nst = judge.group_stories(nj)
    fin = judge.finalize(nst, nj, log)
    olv = {it["id"]: it["level"] for it in oj}
    out = ["# M4-6 検証：今の基準（old）と新しい基準（new）", "",
           f"記事 {len(items)}件（直近{a.hours}時間）。AIの使用：old {ost_.get('requests_sent')}回・new {nst_.get('requests_sent')}回", "",
           "## old（今の基準）", table(ost), "", "## new（新しい基準＋上限）", table(nst), "",
           f"上限：{json.dumps({k: v for k, v in fin.items()}, ensure_ascii=False)}", "",
           "## new の S と A（old での重要度）", ""]
    for s in nst:
        if s["level"] in ("S", "A"):
            lead = s["articles"][0]
            mark = "【大きく報道 %d本】" % s["big"] if s.get("big") else ""
            out.append(f"- {s['level']}（old {olv.get(lead['id'], '?')}）{mark}{LN.get((s.get('lanes') or ['?'])[0], '')}｜{lead['title'][:70]}｜{lead.get('source', '')}")
    out += ["", "## old で A 以上 → new で B 以下", ""]
    nlv = {a_["id"]: s["level"] for s in nst for a_ in s["articles"]}
    for s in ost:
        if s["level"] in ("S", "A"):
            lead = s["articles"][0]
            if nlv.get(lead["id"]) in ("B", "C"):
                out.append(f"- {s['level']}→{nlv[lead['id']]}｜{lead['title'][:70]}")
    themes = collections.Counter(it.get("theme") for it in nj if it.get("theme"))
    out += ["", "## テーマ別の記事数（上位15）", ""] + [f"- {t}：{n}本" for t, n in themes.most_common(15)]
    p = ROOT / "data" / "probe"
    p.mkdir(parents=True, exist_ok=True)
    (p / "compare.md").write_text("\n".join(out), encoding="utf-8")
    print("\n".join(out[:40]))


if __name__ == "__main__":
    main()
