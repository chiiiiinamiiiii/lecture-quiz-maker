"""抽出に渡す本文から、どのページにも出る決まり文句とページ番号を除く（#119）。

スライドの見出し・脚注に毎ページ出る講師名・授業名・大学名は、改行の除去で見出しや本文とつながり
（例: 'Application: OptimizationTomoo Matsumura0 / 12'）、抽出で講師が多くの関係の中心になった。

- 決まり文句: コースの全ページのうち半分以上（MIN_SHARE）に出てくる文字列。行の区切りには頼らず、
  文字の並び（MIN_LEN 字以上）で探す。左右に 1 字ずつ伸ばして、出るページ数が減らないあいだは伸ばす。
  そのうえで、出るページの多くで 1 回だけ・ページの頭か末尾に出るもの（見出し・脚注）だけを除く
- ページ番号: 「0 / 12」のような「数 / 数」で、隣のページに分母が同じで分子が 1 つ違うものがあるもの
- 除くのは抽出（と語彙の導出）に渡す本文（Lecture.prompt_pages）だけ。pages.json の本文・スライド画像・
  evidence の照合に使う本文（Lecture.pages）はそのまま
"""
from __future__ import annotations

import re
from collections import Counter

from quizmap.common import Lecture

MIN_SHARE = 0.5      # コースの全ページのうち、この割合以上のページに出る文字列を決まり文句とみなす
MIN_PAGES = 6        # ページがこれより少ないコースでは探さない（偶然の一致を避ける）
EDGE_CHARS = 80      # 見出し・脚注とみなす、ページの頭・末尾からの字数
HEADER_SHARE = 0.75  # 出るページのうち、この割合以上で 1 回だけ・頭か末尾に出るものを見出し・脚注とみなす
GROW_KEEP = 0.95     # 伸ばすとき、出るページ数がこの割合より減るなら伸ばさない
SEED_LEN = 8         # 探し始める文字の並びの長さ
MIN_PAGE_NUMBERS = 3  # 1 回のうち、ページ番号と見なせるページがこれより少なければ除かない
MIN_LEN = 10         # 決まり文句とみなす最短の長さ（前後の空白を除いて）。「 of the 」などの言い回しを避ける

# ページの末尾の「数 / 数」。分子の数字は前の字（数式の数字など）とつながっていることがある
_PAGE_NO_RE = re.compile(r"(\d+)\s*/\s*(\d{1,3})\s*$")
_HAS_WORD_RE = re.compile(r"[^\W\d_]")


def _df(s: str, pages: list[str]) -> int:
    return sum(1 for p in pages if s in p)


def _grow(seed: str, pages: list[str], need: int) -> str:
    """seed を左右に 1 字ずつ伸ばす（多い隣の字から試す）。

    伸ばしても出るページ数がほとんど減らない（GROW_KEEP 以上残る）あいだだけ伸ばす。
    講師名の前の「.」のように、多くのページで隣にあるだけの字まで含めないため。
    """
    s = seed
    n = _df(s, pages)
    for side in ("right", "left"):
        while True:
            nexts = Counter()
            for p in pages:
                start = p.find(s)
                while start >= 0:
                    j = start + len(s) if side == "right" else start - 1
                    if 0 <= j < len(p):
                        nexts[p[j]] += 1
                    start = p.find(s, start + 1)
            grown = None
            for c, _ in nexts.most_common(3):
                cand = s + c if side == "right" else c + s
                k = _df(cand, pages)
                if k >= need and k >= GROW_KEEP * n:
                    grown = cand
                    break
            if grown is None:
                break
            s, n = grown, k
    return s


def _looks_like_header(s: str, pages: list[str]) -> bool:
    """見出し・脚注らしいか。出るページの多く（HEADER_SHARE 以上）で、1 ページに 1 回だけ、
    ページの頭か末尾から EDGE_CHARS 字以内に出る。本文の語（統計の講義の 'distribution' など）は、
    半分以上のページに出ても、1 ページに何度も出たり、ページの途中に出たりするので落ちる。"""
    has = [p for p in pages if s in p]
    once = sum(1 for p in has if p.count(s) == 1)
    edge = sum(1 for p in has if p.find(s) < EDGE_CHARS or len(p) - p.rfind(s) - len(s) < EDGE_CHARS)
    return bool(has) and once >= HEADER_SHARE * len(has) and edge >= HEADER_SHARE * len(has)


def find_repeated(pages: list[str], min_share: float = MIN_SHARE, min_len: int = MIN_LEN) -> list[dict]:
    """半分以上のページに出る文字列。[{"text", "pages"}]（長い順）。前後の空白は落とす。"""
    pages = [p for p in pages]
    if len(pages) < MIN_PAGES:
        return []
    need = max(2, -(-int(len(pages) * min_share * 1000) // 1000))   # 切り上げ
    df = Counter()
    for p in pages:
        df.update({p[i:i + SEED_LEN] for i in range(len(p) - SEED_LEN + 1)})
    seeds = [g for g, n in df.items() if n >= need]
    found: dict[str, int] = {}
    covered: set[str] = set()
    for g in sorted(seeds, key=lambda g: -df[g]):
        if any(g in s for s in covered):
            continue
        s = _grow(g, pages, need).strip()
        covered.add(s)
        if len(s) < min_len or not _HAS_WORD_RE.search(s) or not _looks_like_header(s, pages):
            continue
        found[s] = _df(s, pages)
    # ほかの見つけた文字列の一部でしかないものは落とす
    keep = [s for s in found if not any(s != t and s in t for t in found)]
    return [{"text": s, "pages": found[s]} for s in sorted(keep, key=lambda s: (-len(s), s))]


def _page_number_options(page: str) -> list[tuple[int, int, int]]:
    """ページの末尾の「数 / 数」の読み方の候補 [(切る位置, 分子, 分母)]。

    分子の数字が前の数字とつながっていることがあるので（'…a21 / 12' は '…a2' と '1 / 12'）、
    数字の並びの後ろ 1〜3 字を分子の候補にする。分子が分母より大きい読み方は捨てる。
    """
    m = _PAGE_NO_RE.search(page)
    if not m:
        return []
    digits, den = m.group(1), int(m.group(2))
    out = []
    for k in range(1, min(3, len(digits)) + 1):
        num = int(digits[-k:])
        if num <= den:
            out.append((m.start(1) + len(digits) - k, num, den))
    return out


def find_page_numbers(pages: list[str]) -> list[int | None]:
    """1 回分のページについて、末尾のページ番号（「0 / 12」）が始まる位置をページごとに返す（無ければ None）。

    隣のページ（前か後ろ）の末尾に、分母が同じで分子の差が 1 以内の「数 / 数」があるものだけをページ番号とみなす
    （同じ番号が続くのは、1 枚のスライドを少しずつ見せるページ）。1 回の PDF に別々に番号を振った資料が
    続いていてもよい。そう見なせるページが MIN_PAGE_NUMBERS 未満の回は何も除かない。
    """
    opts = [_page_number_options(p) for p in pages]

    def near(j: int, num: int, den: int) -> bool:
        return 0 <= j < len(pages) and any(d == den and abs(n - num) <= 1 for _, n, d in opts[j])

    out: list[int | None] = [None] * len(pages)
    for i, cands in enumerate(opts):
        # 短い読み方（つながった数字を分子に含めない）を先に試す
        for cut, num, den in cands:
            if near(i - 1, num, den) or near(i + 1, num, den):
                out[i] = cut
                break
    if sum(1 for x in out if x is not None) < MIN_PAGE_NUMBERS:
        return [None] * len(pages)
    return out


def _remove(text: str, parts: list[str]) -> str:
    for s in parts:
        text = text.replace(s, " ")
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def strip_boilerplate(lectures: list[Lecture]) -> dict:
    """lectures の prompt_pages に、決まり文句とページ番号を除いた本文を入れる。何を除いたかを返す。

    返す形（graph.json の stats.boilerplate に入れる）:
    {"repeated": [{"text", "pages"}], "page_numbers": 除いたページ数, "page_number_examples": [...],
     "pages": 全ページ数, "chars_removed": 除いた文字数}
    """
    all_pages = [p for lec in lectures for p in lec.pages]
    repeated = find_repeated(all_pages)
    texts = [r["text"] for r in repeated]
    n_numbers, examples, removed = 0, [], 0
    for lec in lectures:
        new = []
        for page, cut in zip(lec.pages, find_page_numbers(lec.pages)):
            text = page
            if cut is not None:
                n_numbers += 1
                if len(examples) < 3:
                    examples.append(page[cut:].strip())
                text = page[:cut]
            if text != page or any(t in text for t in texts):
                text = _remove(text, texts)
            removed += len(page) - len(text)
            new.append(text)
        lec.prompt_pages = new
    return {"repeated": repeated, "page_numbers": n_numbers, "page_number_examples": examples,
            "pages": len(all_pages), "chars_removed": removed}


def report_lines(report: dict) -> list[str]:
    """取り込みの記録に出す行。"""
    lines = []
    if report["repeated"]:
        shown = " / ".join(f"「{r['text']}」({r['pages']} ページ)" for r in report["repeated"][:5])
        lines.append(f"抽出に渡す本文から、半分以上のページに出る文字列を除いた: {shown}")
    if report["page_numbers"]:
        lines.append(f"抽出に渡す本文から、ページ番号を除いた: {report['page_numbers']} ページ"
                     f"（例: {' / '.join(report['page_number_examples'])}）")
    if not lines:
        lines.append("抽出に渡す本文から除いた決まり文句・ページ番号は無い")
    return lines
