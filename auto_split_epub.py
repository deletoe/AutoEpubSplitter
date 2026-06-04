#!/usr/bin/env python3
"""Automatically split an EPUB collection into single-book EPUBs.

The script reuses EpubSplit for the fragile EPUB writing work.  Its own job is
to decide which EpubSplit "lines" are book starts, preferably via an
OpenAI-compatible local model and with a conservative heuristic fallback.
"""

from __future__ import annotations

import argparse
import html
import importlib.util
import json
import os
import re
import sys
import time
import warnings
from posixpath import normpath
from urllib.parse import unquote
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


ROOT = Path(__file__).resolve().parent
EPUBSPLIT_PATH = ROOT / "EpubSplit" / "epubsplit.py"
DEFAULT_VLLM_BASE_URL = os.environ.get("VLLM_BASE_URL", "http://10.130.92.107:8000")


def load_epubsplit():
    spec = importlib.util.spec_from_file_location("epubsplit_local", EPUBSPLIT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot import EpubSplit from {EPUBSPLIT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        from bs4 import XMLParsedAsHTMLWarning

        warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
        warnings.filterwarnings("ignore", category=DeprecationWarning, module="epubsplit_local")
    except Exception:
        pass
    return module


def clean_text(value: Any, limit: int = 240) -> str:
    text = " ".join(str(value or "").split())
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(" ".join(text.split()))
    return text[:limit]


def line_title(line: Dict[str, Any]) -> str:
    toc = line.get("toc") or []
    if toc:
        return clean_text(" / ".join(toc), 120)
    guide = line.get("guide")
    if guide:
        return clean_text(" / ".join(str(x) for x in guide), 120)
    return ""


def fast_split_lines(splitter: Any) -> List[Dict[str, Any]]:
    """Build EpubSplit-compatible split lines without expensive HTML samples."""
    metadom = splitter.get_content_dom()
    try:
        splitter.origtitle = metadom.getElementsByTagName("dc:title")[0].firstChild.data
    except Exception:
        splitter.origtitle = "(Title Missing)"

    splitter.origauthors = []
    for creator in metadom.getElementsByTagName("dc:creator"):
        try:
            role = creator.getAttribute("opf:role")
            if (role == "aut" or not creator.hasAttribute("opf:role")) and creator.firstChild is not None:
                if creator.firstChild.data not in splitter.origauthors:
                    splitter.origauthors.append(creator.firstChild.data)
        except Exception:
            pass
    if not splitter.origauthors:
        splitter.origauthors.append("(Authors Missing)")

    lines: List[Dict[str, Any]] = []
    count = 0
    manifest = splitter.get_manifest_items()
    guide = splitter.get_guide_items()
    toc_map = splitter.get_toc_map()

    for itemref in metadom.getElementsByTagName("itemref"):
        idref = itemref.getAttribute("idref")
        href, media_type = manifest["i:" + idref]
        current = {
            "href": href,
            "anchor": None,
            "toc": [],
            "id": idref,
            "type": media_type,
            "num": count,
            "sample": "",
        }
        if href in guide:
            current["guide"] = guide[href]
        lines.append(current)
        count += 1

        if href not in toc_map:
            continue
        for text, anchor in toc_map[href]:
            if anchor:
                current = {
                    "href": href,
                    "anchor": anchor,
                    "toc": [],
                    "id": idref,
                    "type": media_type,
                    "num": count,
                    "sample": "",
                }
                lines.append(current)
                count += 1
            current["toc"].append(str(text))

    splitter.split_lines = lines
    return lines


def build_toc_nodes(splitter: Any, lines: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    try:
        toc_dom = splitter.get_toc_dom()
        toc_relpath = splitter.get_toc_relpath()
        navmap = toc_dom.getElementsByTagName("navMap")[0]
    except Exception:
        return []

    lookup = split_line_lookup(lines)

    def walk(navpoint: Any, level: int, parent_titles: List[str]) -> List[Dict[str, Any]]:
        title = navpoint_text(navpoint)
        src = navpoint_src(navpoint, toc_relpath)
        line = lookup.get(src) or lookup.get(src.split("#", 1)[0])
        children = direct_child_elements(navpoint, "navPoint")
        node = {
            "level": level,
            "title": title,
            "line": line,
            "href": src.split("#", 1)[0],
            "anchor": src.split("#", 1)[1] if "#" in src else "",
            "parent_path": " > ".join(parent_titles),
            "children_count": len(children),
            "child_titles": [navpoint_text(child) for child in children[:18]],
        }
        out = [node]
        for child in children:
            out.extend(walk(child, level + 1, parent_titles + [title]))
        return out

    nodes: List[Dict[str, Any]] = []
    for top in direct_child_elements(navmap, "navPoint"):
        nodes.extend(walk(top, 1, []))
    return nodes


def build_prompt(
    epub_path: Path,
    lines: List[Dict[str, Any]],
    expected_count: Optional[int],
    toc_nodes: List[Dict[str, Any]],
) -> str:
    candidate_lines = []
    for line in lines:
        title = line_title(line)
        if not title:
            continue
        prev_toc = line_title(lines[line["num"] - 1]) if line["num"] > 0 else ""
        next_toc = line_title(lines[line["num"] + 1]) if line["num"] + 1 < len(lines) else ""
        context = []
        if prev_toc:
            context.append(f"prev={prev_toc}")
        if next_toc:
            context.append(f"next={next_toc}")
        suffix = f" ({'; '.join(context)})" if context else ""
        candidate_lines.append(f"[{line['num']}] {title}{suffix}")

    toc_outline = []
    for node in toc_nodes:
        if node.get("line") is None:
            continue
        indent = "  " * max(0, int(node["level"]) - 1)
        child_hint = ""
        if node.get("children_count"):
            child_titles = "、".join([x for x in node.get("child_titles", []) if x][:10])
            child_hint = f" -> 子项{node['children_count']}个: {child_titles}"
        toc_outline.append(f"{indent}- [{node['line']}] {node['title']}{child_hint}")

    expected = (
        f"用户额外提示：大约希望拆出 {expected_count} 个输出文件。这个数字只作为参考，不能为了凑数选择或删除不合理断点。"
        if expected_count
        else "用户没有提供预计数量；不要从文件名猜测数量，请完全根据目录结构和内容逻辑判断。"
    )
    return (
        "你是一个经验丰富的 EPUB 合集拆分助手。你的目标不是机械按目录层级拆分，"
        "而是模仿人工在 Calibre EpubSplit 中阅读目录后选择断点。\n"
        f"文件名：{epub_path.name}\n{expected}\n\n"
        "拆分原则：\n"
        "1. 输出粒度通常是“单册出版物/一本书”，而不是系列、辑、主题分组、总目录分组、章节、篇章或文章。\n"
        "2. 如果某个顶层目录项下面挂着多本独立书名，它很可能只是系列/分组标题，不要把它作为一本书；应选择它的子级真实书名。\n"
        "   例如父级像“陌生的中国”“日本现场观察”“自然与人”这类主题分组，子级才可能是《寻路中国》《江城》等书。\n"
        "3. 也存在例外：如果一部连续作品被分成上/下册、第一卷/第二卷、1/2/3/4册，而阅读上应作为整体保留，"
        "   请只返回这一组的第一个起始 line，用合适的整体标题命名，不要把各卷拆成多个输出。\n"
        "4. 不要选择封面、版权信息、Digital Lab 简介、总目录、目录、前言、序、后记、附录、章节标题、第一章/第二章、第一部/第二部等内部结构。\n"
        "5. 起点必须来自候选位置 JSON 中的 line 数字。只返回你认为应该成为输出 EPUB 的起始 line；下一个起始 line 前的内容会归入当前输出。\n"
        "6. title 必须是最终输出 EPUB 的干净书名：删除书名号、序号、套装/全集/文集前缀、文件名或作家名包装。"
        "例如“1. 悲惨世界”“雨果文集01.悲惨世界”“维克多·雨果作品集：悲惨世界（上中下）”都应返回“悲惨世界”。\n"
        "7. 如果不确定，在 reason 里说明，但仍给出最符合人工直觉的选择。\n\n"
        "只输出严格 JSON，不要 Markdown：\n"
        '{"books":[{"title":"输出书名","start_line":8,"confidence":0.92,"reason":"为什么这是单册起点，或为什么保留多卷为整体"}],"notes":[]}\n\n'
        "目录树大纲（缩进代表目录层级；方括号内是可拆分 line；箭头后是直接子项摘要）：\n"
        f"{chr(10).join(toc_outline)}\n\n"
        "候选断点（只列带标题信息的 line；start_line 必须从这些 line 中选择）：\n"
        f"{chr(10).join(candidate_lines)}"
    )


def http_json(url: str, payload: Optional[Dict[str, Any]] = None, timeout: int = 120) -> Dict[str, Any]:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def chat_completion_content(
    base_url: str,
    payload: Dict[str, Any],
    timeout: int,
    stream_callback: Optional[Any] = None,
    cancel_callback: Optional[Any] = None,
) -> str:
    if not stream_callback:
        data = http_json(base_url.rstrip("/") + "/v1/chat/completions", payload, timeout=timeout)
        return data["choices"][0]["message"]["content"]

    streamed = dict(payload)
    streamed["stream"] = True
    req = urllib.request.Request(
        base_url.rstrip("/") + "/v1/chat/completions",
        data=json.dumps(streamed, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    chunks: List[str] = []
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for raw_line in resp:
            if cancel_callback and cancel_callback():
                raise RuntimeError("Canceled by user")
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            if line.startswith("data:"):
                line = line[5:].strip()
            if line == "[DONE]":
                break
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            choices = data.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            piece = delta.get("content")
            if piece is None:
                piece = choices[0].get("text")
            if not piece:
                continue
            chunks.append(piece)
            stream_callback(piece)
    return "".join(chunks)


def get_vllm_model(base_url: str) -> str:
    data = http_json(base_url.rstrip("/") + "/v1/models", timeout=10)
    models = data.get("data") or []
    if not models:
        raise RuntimeError("vLLM /v1/models returned no models")
    return models[0]["id"]


def extract_json_object(text: str) -> Dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        return json.loads(text[start : end + 1])


def ask_llm(
    epub_path: Path,
    lines: List[Dict[str, Any]],
    expected_count: Optional[int],
    toc_nodes: List[Dict[str, Any]],
    base_url: str,
    model: Optional[str],
    timeout: int,
    stream_callback: Optional[Any] = None,
    cancel_callback: Optional[Any] = None,
) -> Dict[str, Any]:
    model = model or get_vllm_model(base_url)
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "你只输出可解析的 JSON。"},
            {"role": "user", "content": build_prompt(epub_path, lines, expected_count, toc_nodes)},
        ],
        "temperature": 0,
        "max_tokens": 2048,
        "response_format": {"type": "json_object"},
    }
    content = chat_completion_content(base_url, payload, timeout, stream_callback, cancel_callback)
    result = extract_json_object(content)
    result["_model"] = model
    return result


def clean_book_title(title: str) -> str:
    title = clean_text(title, 120)
    title = title.strip(" 《》「」『』“”\"'")
    title = re.sub(r"\s*[（(]\s*[上中下]+(?:[、,，和 ]*[上中下]+)*\s*[）)]\s*$", "", title)
    title = re.sub(r"\s*[（(]\s*(?:上|中|下)?\s*(?:册|卷)\s*[）)]\s*$", "", title)
    title = re.sub(r"^\s*(?:第\s*)?[0-9一二三四五六七八九十百零〇]{1,4}\s*[.．、:：\-]\s*", "", title)
    title = re.sub(r"^\s*[^:：.．、\-]{1,24}(?:全集|文集|作品集|套装|合集)\s*[0-9一二三四五六七八九十百零〇]{0,4}\s*[.．、:：\-]\s*", "", title)
    title = re.sub(r"^\s*[^:：.．、\-]{1,24}(?:全集|文集|作品集|套装|合集)\s*[：:]\s*", "", title)
    title = re.sub(r"\s*[（(]\s*[^）)]*(?:套装|全集|文集|推荐|新版|珍藏|插图|纪念|精装|修订)[^）)]*[）)]\s*$", "", title)
    return title.strip(" -_·《》「」『』“”\"'")


NOISE_PATTERNS = [
    r"版权",
    r"copyright",
    r"digital\s*lab",
    r"简介$",
    r"^总?目录$",
    r"^目\s*录$",
    r"^contents?$",
    r"^cover$",
    r"cover\s*/\s*cover",
    r"^封面$",
    r"^扉页$",
]

CHAPTER_PATTERNS = [
    r"^[一二三四五六七八九十百零〇0-9]+$",
    r"^第[一二三四五六七八九十百零〇0-9]+[章节节部卷篇回].*",
    r"^[上中下]编$",
    r"^序$",
    r"^前言$",
    r"^后记$",
    r"^附录",
]


def looks_noise(title: str) -> bool:
    low = title.strip().lower()
    return not low or any(re.search(p, low, re.I) for p in NOISE_PATTERNS)


def looks_chapter(title: str) -> bool:
    compact = re.sub(r"\s+", "", title)
    return any(re.search(p, compact, re.I) for p in CHAPTER_PATTERNS)


def direct_child_elements(node: Any, tag_name: str) -> List[Any]:
    return [child for child in node.childNodes if getattr(child, "tagName", None) == tag_name]


def navpoint_text(navpoint: Any) -> str:
    texts = navpoint.getElementsByTagName("text")
    if texts and texts[0].firstChild:
        return clean_text(texts[0].firstChild.data, 120)
    return ""


def navpoint_src(navpoint: Any, toc_relpath: str) -> str:
    contents = navpoint.getElementsByTagName("content")
    if not contents:
        return ""
    return normpath(unquote(toc_relpath + contents[0].getAttribute("src")))


def split_line_lookup(lines: List[Dict[str, Any]]) -> Dict[str, int]:
    lookup = {}
    for line in lines:
        href = line.get("href", "")
        anchor = line.get("anchor")
        if href:
            lookup.setdefault(href, line["num"])
        if href and anchor:
            lookup[f"{href}#{anchor}"] = line["num"]
    return lookup


def toc_top_level_books(splitter: Any, lines: List[Dict[str, Any]], expected_count: Optional[int]) -> List[Dict[str, Any]]:
    try:
        toc_dom = splitter.get_toc_dom()
        toc_relpath = splitter.get_toc_relpath()
        navmap = toc_dom.getElementsByTagName("navMap")[0]
    except Exception:
        return []

    lookup = split_line_lookup(lines)
    books = []
    for navpoint in direct_child_elements(navmap, "navPoint"):
        title = navpoint_text(navpoint)
        if looks_noise(title) or looks_chapter(title):
            continue
        src = navpoint_src(navpoint, toc_relpath)
        start = lookup.get(src) or lookup.get(src.split("#", 1)[0])
        if start is None:
            continue
        children = len(direct_child_elements(navpoint, "navPoint"))
        confidence = 0.95 if children else 0.82
        books.append({"title": title, "start_line": start, "confidence": confidence, "reason": "top-level toc"})

    seen = set()
    unique = []
    for book in sorted(books, key=lambda x: x["start_line"]):
        if book["start_line"] not in seen:
            unique.append(book)
            seen.add(book["start_line"])
    return unique


def toc_descendant_books(splitter: Any, lines: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    books = []
    for node in build_toc_nodes(splitter, lines):
        if node.get("level") <= 1 or node.get("line") is None:
            continue
        title = clean_text(node.get("title", ""), 80)
        if looks_noise(title) or looks_chapter(title):
            continue
        # For series/group structures, the true books often appear as direct
        # children that do not have their own child table of contents.
        if node.get("children_count", 0) > 2:
            continue
        books.append(
            {
                "title": title,
                "start_line": int(node["line"]),
                "confidence": 0.82,
                "reason": "descendant toc fallback",
            }
        )

    seen = set()
    unique = []
    for book in sorted(books, key=lambda x: x["start_line"]):
        if book["start_line"] not in seen:
            unique.append(book)
            seen.add(book["start_line"])
    return unique


def heuristic_books(splitter: Any, lines: List[Dict[str, Any]], expected_count: Optional[int]) -> Dict[str, Any]:
    toc_books = toc_top_level_books(splitter, lines, expected_count)
    if toc_books and (expected_count and len(toc_books) == expected_count or not expected_count and len(toc_books) >= 8):
        return {"books": toc_books, "notes": ["used top-level toc fallback"]}

    descendant_books = toc_descendant_books(splitter, lines)
    if toc_books and descendant_books and len(descendant_books) >= max(8, len(toc_books) * 2):
        notes = ["used descendant toc fallback because top-level toc looked like series/group headings"]
        if expected_count:
            notes.append(f"expected-count={expected_count} was treated as a hint only")
        return {"books": descendant_books, "notes": notes}

    parent_lines = set()
    if toc_books and len(toc_books) < 8:
        parent_lines = {book["start_line"] for book in toc_books}

    candidates = []
    for i, line in enumerate(lines):
        if line["num"] in parent_lines:
            continue
        title = line_title(line)
        if looks_noise(title) or looks_chapter(title):
            continue
        if not re.search(r"[\u4e00-\u9fffA-Za-z]", title):
            continue
        if len(title) > 40:
            continue

        nearby_prev = [line_title(x) for x in lines[max(0, i - 4) : i]]
        nearby_next = [line_title(x) for x in lines[i + 1 : i + 5]]
        score = 1.0
        if any(looks_noise(x) for x in nearby_prev + nearby_next):
            score += 0.8
        if 2 <= len(title) <= 16:
            score += 0.4
        candidates.append({"title": title, "start_line": line["num"], "confidence": min(score / 2.4, 0.88), "reason": "title heuristic"})

    if toc_books and len(toc_books) >= 8:
        by_start = {book["start_line"]: book for book in candidates}
        for book in toc_books:
            by_start[book["start_line"]] = book
        candidates = sorted(by_start.values(), key=lambda x: x["start_line"])

    notes = ["used heuristic fallback"]
    if expected_count:
        notes.append(f"expected-count={expected_count} was treated as a hint only")
    return {"books": candidates, "notes": notes}


def normalize_books(result: Dict[str, Any], lines: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    valid_lines = {line["num"] for line in lines}
    seen = set()
    books = []
    for book in result.get("books", []):
        try:
            start = int(book["start_line"])
        except (KeyError, TypeError, ValueError):
            continue
        if start not in valid_lines or start in seen:
            continue
        seen.add(start)
        title = clean_book_title(book.get("title") or line_title(lines[start]) or f"Book {len(books) + 1}")
        title = clean_text(title or f"Book {len(books) + 1}", 80)
        books.append(
            {
                "title": title,
                "start_line": start,
                "confidence": float(book.get("confidence", 0.0) or 0.0),
                "reason": clean_text(book.get("reason", ""), 120),
            }
        )
    return sorted(books, key=lambda x: x["start_line"])


def line_ranges(books: List[Dict[str, Any]], lines: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    max_line = len(lines)
    ranges = []
    for idx, book in enumerate(books):
        start = book["start_line"]
        end = books[idx + 1]["start_line"] if idx + 1 < len(books) else max_line
        ranges.append({**book, "end_line_exclusive": end, "lines": list(range(start, end))})
    return ranges


def safe_filename(value: str, index: int) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|]+', "_", value).strip(" .")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return f"{index:02d}-{cleaned or f'Book {index}'}.epub"


def split_books(epub_path: Path, output_dir: Path, ranges: List[Dict[str, Any]], overwrite: bool) -> None:
    epubsplit = load_epubsplit()
    output_dir.mkdir(parents=True, exist_ok=True)
    splitter = epubsplit.SplitEpub(str(epub_path))
    fast_split_lines(splitter)
    for index, item in enumerate(ranges, 1):
        out_path = output_dir / safe_filename(item["title"], index)
        if out_path.exists() and not overwrite:
            raise FileExistsError(f"{out_path} exists; pass --overwrite to replace it")
        print(f"[write] {out_path.name}: lines {item['start_line']}..{item['end_line_exclusive'] - 1}")
        splitter.write_split_epub(
            str(out_path),
            [str(x) for x in item["lines"]],
            titleopt=item["title"],
            languages=["zh"],
        )


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Automatically split an EPUB collection into single-book EPUBs.")
    parser.add_argument("epub", type=Path, help="Input collection EPUB")
    parser.add_argument("-o", "--output-dir", type=Path, default=Path("split-output"), help="Directory for output EPUBs")
    parser.add_argument("--dry-run", action="store_true", help="Only print detected book starts")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing output EPUBs")
    parser.add_argument("--no-llm", action="store_true", help="Skip vLLM and use the heuristic detector")
    parser.add_argument("--vllm-base-url", default=DEFAULT_VLLM_BASE_URL, help="OpenAI-compatible vLLM base URL")
    parser.add_argument("--model", default=None, help="vLLM model id. Defaults to first /v1/models entry")
    parser.add_argument("--llm-timeout", type=int, default=120, help="Seconds to wait for vLLM before falling back")
    parser.add_argument("--stream-llm", action="store_true", help="Print streamed LLM output to stderr while detecting splits")
    parser.add_argument("--expected-count", type=int, default=None, help="Optional expected output count hint for the detector")
    parser.add_argument("--report", type=Path, default=None, help="Write detection report JSON")
    args = parser.parse_args(argv)

    try:
        epubsplit = load_epubsplit()
    except ModuleNotFoundError as exc:
        if exc.name == "bs4":
            print("Missing dependency: beautifulsoup4. Install with: python3 -m pip install -r requirements.txt", file=sys.stderr)
            return 2
        raise

    epub_path = args.epub.resolve()
    expected_count = args.expected_count
    splitter = epubsplit.SplitEpub(str(epub_path))
    lines = fast_split_lines(splitter)
    toc_nodes = build_toc_nodes(splitter, lines)

    source = "heuristic"
    result: Dict[str, Any]
    if args.no_llm:
        result = heuristic_books(splitter, lines, expected_count)
    else:
        try:
            stream_callback = None
            if args.stream_llm:
                print("[llm stream]", file=sys.stderr)

                def stream_callback(piece: str) -> None:
                    print(piece, end="", file=sys.stderr, flush=True)

            result = ask_llm(
                epub_path,
                lines,
                expected_count,
                toc_nodes,
                args.vllm_base_url,
                args.model,
                args.llm_timeout,
                stream_callback=stream_callback,
            )
            if args.stream_llm:
                print("\n[/llm stream]", file=sys.stderr)
            source = f"llm:{result.get('_model', args.model or 'auto')}"
        except Exception as exc:
            print(f"[warn] vLLM unavailable or invalid response: {exc}", file=sys.stderr)
            result = heuristic_books(splitter, lines, expected_count)

    books = normalize_books(result, lines)
    if not books:
        print("No book starts detected. Try --no-llm/--expected-count or inspect EpubSplit line output.", file=sys.stderr)
        return 1

    ranges = line_ranges(books, lines)
    report = {
        "input": str(epub_path),
        "source": source,
        "expected_count": expected_count,
        "created_at": int(time.time()),
        "books": ranges,
        "notes": result.get("notes", []),
    }

    print(f"Detected {len(ranges)} book(s) via {source}:")
    for index, item in enumerate(ranges, 1):
        print(f"{index:02d}. line {item['start_line']:>4}-{item['end_line_exclusive'] - 1:<4} {item['title']} ({item['confidence']:.2f})")

    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    if not args.dry_run:
        split_books(epub_path, args.output_dir.resolve(), ranges, args.overwrite)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
