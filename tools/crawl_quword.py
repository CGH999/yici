"""从 quword.com 抓取词表，输出「译词」可直接导入的 JSON。

用法：
    python tools/crawl_quword.py                          # 默认抓 CET6 -> cet6.json
    python tools/crawl_quword.py --book CET4 --out cet4.json
    python tools/crawl_quword.py --delay 2.5 --max-pages 5   # 先抓 5 页试水

输出格式（正好是仪表盘「新建词书（粘贴 JSON）」要的）::

    {"data": [{"word": "abandon", "translation": "n. 放任；抛弃\\nvt. 放弃…"}, ...]}

说明：
* 每页 50 词，从 0 开始翻，翻到第一页没有词为止；
* 带本地缓存（默认 _cache/），中断后重跑不会重新下载已抓过的页；
* 词条按 word 精确去重（大小写敏感），重复的只保留第一次出现；
* 请把 --delay 保持在 1.5 秒以上，别给对方服务器添麻烦。
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

import requests
from lxml import html as lxml_html

BASE_URL = "https://www.quword.com/study/{book}/{page}"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def fetch_page(session: requests.Session, book: str, page: int, timeout: int = 20, retries: int = 3) -> str:
    url = BASE_URL.format(book=book, page=page)
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = session.get(url, timeout=timeout, headers=HEADERS)
            response.raise_for_status()
            response.encoding = response.encoding or "utf-8"
            return response.text
        except Exception as exc:  # noqa: BLE001 - 网络问题统一重试
            last_error = exc
            wait = 2 ** attempt
            print(f"  第 {page} 页第 {attempt} 次失败（{exc}），{wait} 秒后重试", flush=True)
            time.sleep(wait)
    raise SystemExit(f"抓取第 {page} 页失败：{last_error}")


def parse_page(content: str) -> list[dict]:
    """从一页 HTML 里取出 [{word, translation}, ...]。"""
    tree = lxml_html.fromstring(content)
    items: list[dict] = []
    slides = tree.xpath('//div[@class="swiper-slide"]')
    for slide in slides:
        links = slide.xpath(".//h3/a")
        paragraphs = slide.xpath(".//p")
        if not links or not paragraphs:
            continue
        word = (links[0].text_content() or "").strip()
        if not word:
            continue
        translation = "\n".join(
            line.strip() for line in (paragraphs[0].text_content() or "").splitlines() if line.strip()
        )
        items.append({"word": word, "translation": translation})
    return items


def crawl(book: str, start: int, max_pages: int | None, delay: float, cache_dir: Path) -> list[dict]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    collected: list[dict] = []
    seen: set[str] = set()
    duplicates = 0
    page = start
    fetched = 0

    while True:
        if max_pages is not None and fetched >= max_pages:
            print(f"到达 --max-pages={max_pages}，停止")
            break

        cache_file = cache_dir / f"{book}-{page}.html"
        if cache_file.exists():
            content = cache_file.read_text(encoding="utf-8")
            source = "缓存"
        else:
            content = fetch_page(session, book, page)
            cache_file.write_text(content, encoding="utf-8")
            source = "下载"
            time.sleep(delay + random.uniform(0, 0.4))

        items = parse_page(content)
        fetched += 1
        if not items:
            print(f"第 {page} 页没有词条（{source}），抓取结束", flush=True)
            break

        for item in items:
            if item["word"] in seen:
                duplicates += 1
                continue
            seen.add(item["word"])
            collected.append(item)

        print(
            f"第 {page:3d} 页（{source}）：{len(items):2d} 词，累计 {len(collected)} 词",
            flush=True,
        )
        page += 1

    if duplicates:
        print(f"跳过重复词 {duplicates} 个（按 word 精确去重）")
    return collected


def main() -> int:
    parser = argparse.ArgumentParser(description="抓取 quword 词表并输出导入 JSON")
    parser.add_argument("--book", default="CET6", help="词书代号，如 CET6 / CET4 / KAOYAN")
    parser.add_argument("--out", default="", help="输出文件，默认 <book 小写>.json")
    parser.add_argument("--start", type=int, default=0, help="起始页（默认 0）")
    parser.add_argument("--max-pages", type=int, default=None, help="最多抓多少页（调试用）")
    parser.add_argument("--delay", type=float, default=1.5, help="每页之间的间隔秒数（默认 1.5）")
    parser.add_argument("--cache-dir", default="_cache", help="页面缓存目录（默认 _cache）")
    args = parser.parse_args()

    output = Path(args.out) if args.out else Path(f"{args.book.lower()}.json")
    if not output.is_absolute():
        output = PROJECT_ROOT / output

    print(f"开始抓取 {args.book}，缓存目录 {args.cache_dir}，间隔 {args.delay}s")
    items = crawl(args.book, args.start, args.max_pages, args.delay, Path(args.cache_dir))

    output.parent.mkdir(parents=True, exist_ok=True)
    payload = {"data": items}
    # 显式指定 newline="\n"：Windows 下 write_text 默认会把 \n 转成 \r\n，白白变大
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")

    size_kb = output.stat().st_size / 1024
    empty = sum(1 for item in items if not item["translation"])
    print(f"\n完成：{len(items)} 个词 -> {output}（{size_kb:.1f} KB）")
    if empty:
        print(f"其中 {empty} 个词没有抓到释义")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
