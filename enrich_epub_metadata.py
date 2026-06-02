#!/usr/bin/env python3
"""Search, normalize, and write book metadata for split EPUB files.

The network side is deliberately conservative: low request rate, cache first,
and no aggressive crawling.  Douban is used as the primary source; an
OpenAI-compatible local model can choose/clean candidates and produce a short
fallback description only when it is familiar with the book.
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import mimetypes
import os
import re
import shutil
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import warnings
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Dict, Iterable, List, Optional, Tuple
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile
from xml.dom.minidom import Document, Element, parseString

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)


DEFAULT_VLLM_BASE_URL = os.environ.get("VLLM_BASE_URL", "http://10.130.92.107:8000")
USER_AGENT = "Mozilla/5.0 (compatible; AutoEpubSplitter/0.2; gentle metadata enrichment)"
DC_NS = "http://purl.org/dc/elements/1.1/"
OPF_NS = "http://www.idpf.org/2007/opf"


def default_args(
    cache_dir: Optional[Path] = None,
    delay: float = 3.0,
    no_llm: bool = False,
    vllm_base_url: str = DEFAULT_VLLM_BASE_URL,
    model: Optional[str] = None,
    llm_timeout: int = 60,
    max_candidates: int = 5,
    no_author_extract: bool = False,
    no_cover_vision: bool = False,
    cover_vision_timeout: int = 45,
    llm_describe_miss: bool = False,
    metadata_sources: Optional[List[str]] = None,
    google_books_api_key: str = "",
) -> argparse.Namespace:
    return argparse.Namespace(
        cache_dir=Path(cache_dir or Path.home() / ".cache" / "auto_epub_splitter" / "douban"),
        delay=delay,
        max_candidates=max_candidates,
        no_llm=no_llm,
        vllm_base_url=vllm_base_url,
        model=model,
        llm_timeout=llm_timeout,
        no_author_extract=no_author_extract,
        author_front_files=8,
        author_front_chars=6000,
        no_cover_vision=no_cover_vision,
        cover_vision_timeout=cover_vision_timeout,
        llm_describe_miss=llm_describe_miss,
        metadata_sources=metadata_sources or ["douban"],
        google_books_api_key=google_books_api_key,
        work_hard=False,
    )


def clean_text(value: Any, limit: Optional[int] = None) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] if limit else text


def strip_title_noise(title: str) -> str:
    title = clean_text(title)
    title = re.sub(r"^\s*[《「“\"]?(.+?)[》」”\"]?\s*$", r"\1", title)
    title = re.sub(r"\s*[（(]\s*[上中下]+(?:[、,，和 ]*[上中下]+)*\s*[）)]\s*$", "", title)
    title = re.sub(r"\s*[（(]\s*(?:上|中|下)?\s*(?:册|卷)\s*[）)]\s*$", "", title)
    title = re.sub(r"\s*（[^）]*(套装|全集|推荐|新版|珍藏|插图|纪念|精装|修订)[^）]*）\s*$", "", title)
    title = re.sub(r"\s*\([^)]*(套装|全集|推荐|新版|珍藏|插图|纪念|精装|修订)[^)]*\)\s*$", "", title)
    title = re.sub(r"\s*[:：]\s*(豆瓣.*|.*推荐.*|.*经典.*)$", "", title)
    return title.strip(" -_·")


def http_get(url: str, cache_dir: Path, delay: float, binary: bool = False) -> bytes:
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = re.sub(r"[^A-Za-z0-9_.-]+", "_", url)[:180]
    cache_file = cache_dir / key
    if cache_file.exists():
        return cache_file.read_bytes()

    if delay > 0:
        time.sleep(delay)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=25) as resp:
        data = resp.read()
    cache_file.write_bytes(data)
    return data


def http_json(url: str, payload: Optional[Dict[str, Any]] = None, timeout: int = 120) -> Dict[str, Any]:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


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


def opf_path(epub: ZipFile) -> str:
    container = parseString(epub.read("META-INF/container.xml"))
    return container.getElementsByTagName("rootfile")[0].getAttribute("full-path")


def get_child_texts(parent: Element, tag_name: str) -> List[str]:
    values = []
    for node in parent.getElementsByTagName(tag_name):
        if node.firstChild:
            values.append(clean_text(node.firstChild.data))
    return values


def read_epub_metadata(epub_path: Path, args: Optional[argparse.Namespace] = None) -> Dict[str, Any]:
    with ZipFile(epub_path) as epub:
        opf_name = opf_path(epub)
        dom = parseString(epub.read(opf_name))
        metadata = dom.getElementsByTagName("metadata")[0]
        title = get_child_texts(metadata, "dc:title")
        authors = get_child_texts(metadata, "dc:creator")
        desc = get_child_texts(metadata, "dc:description")
        cover_href = find_existing_cover_href(epub, dom, opf_name)
        cover_source = "opf" if cover_href else ""
        if cover_href and args and not args.no_llm and not args.no_cover_vision:
            checked_href, checked_source = find_front_cover_image(
                epub,
                dom,
                opf_name,
                title[0] if title else "",
                authors,
                args,
                vision_only=True,
            )
            if checked_href:
                cover_href, cover_source = checked_href, checked_source
        if not cover_href:
            cover_href, cover_source = find_front_cover_image(epub, dom, opf_name, title[0] if title else "", authors, args)
        return {
            "title": strip_title_noise(title[0] if title else epub_path.stem),
            "authors": authors,
            "description": desc[0] if desc else "",
            "cover_href": cover_href,
            "cover_source": cover_source,
        }


def read_front_matter_text(epub_path: Path, max_files: int = 6, limit: int = 4500) -> str:
    with ZipFile(epub_path) as epub:
        opf_name = opf_path(epub)
        dom = parseString(epub.read(opf_name))
        manifest_by_id = {}
        for item in dom.getElementsByTagName("item"):
            manifest_by_id[item.getAttribute("id")] = (
                join_opf_path(opf_name, item.getAttribute("href")),
                item.getAttribute("media-type"),
            )
        chunks = []
        for itemref in dom.getElementsByTagName("itemref"):
            href, media_type = manifest_by_id.get(itemref.getAttribute("idref"), ("", ""))
            if "html" not in media_type and not href.lower().endswith((".html", ".xhtml", ".htm")):
                continue
            try:
                raw = epub.read(href).decode("utf-8", errors="replace")
            except Exception:
                continue
            soup = BeautifulSoup(raw, "html5lib")
            text = clean_text(soup.get_text("\n"))
            if text:
                chunks.append(f"--- {href} ---\n{text}")
            if len(chunks) >= max_files or sum(len(x) for x in chunks) >= limit:
                break
        return "\n".join(chunks)[:limit]


def find_existing_cover_href(epub: ZipFile, dom: Document, opf_name: str) -> Optional[str]:
    manifest = dom.getElementsByTagName("manifest")
    manifest_items = {}
    if manifest:
        for item in manifest[0].getElementsByTagName("item"):
            manifest_items[item.getAttribute("id")] = item.getAttribute("href")

    for meta in dom.getElementsByTagName("meta"):
        if meta.getAttribute("name").lower() == "cover":
            cover_id = meta.getAttribute("content")
            if cover_id in manifest_items:
                return join_opf_path(opf_name, manifest_items[cover_id])

    for item in dom.getElementsByTagName("item"):
        href = item.getAttribute("href")
        if re.search(r"cover|封面", href, re.I) and item.getAttribute("media-type").startswith("image/"):
            return join_opf_path(opf_name, href)
    return None


def manifest_maps(dom: Document, opf_name: str) -> Tuple[Dict[str, Tuple[str, str]], Dict[str, Tuple[str, str]]]:
    by_id = {}
    by_href = {}
    for item in dom.getElementsByTagName("item"):
        href = join_opf_path(opf_name, item.getAttribute("href"))
        media_type = item.getAttribute("media-type")
        by_id[item.getAttribute("id")] = (href, media_type)
        by_href[href] = (item.getAttribute("id"), media_type)
    return by_id, by_href


def normalize_href(base_href: str, href: str) -> str:
    from posixpath import dirname, normpath

    return normpath(dirname(base_href) + "/" + urllib.parse.unquote(href))


def image_size(data: bytes) -> Tuple[int, int]:
    if data.startswith(b"\xff\xd8"):
        idx = 2
        while idx + 9 < len(data):
            if data[idx] != 0xFF:
                idx += 1
                continue
            marker = data[idx + 1]
            idx += 2
            if marker in (0xD8, 0xD9):
                continue
            if idx + 2 > len(data):
                break
            length = struct.unpack(">H", data[idx : idx + 2])[0]
            if marker in range(0xC0, 0xC4) and idx + 7 < len(data):
                height = struct.unpack(">H", data[idx + 3 : idx + 5])[0]
                width = struct.unpack(">H", data[idx + 5 : idx + 7])[0]
                return width, height
            idx += length
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        return struct.unpack(">II", data[16:24])
    return 0, 0


def image_data_url(data: bytes, href: str) -> str:
    mime = mimetypes.guess_type(href)[0] or "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def choose_cover_with_vision(
    title: str,
    authors: List[str],
    candidates: List[Dict[str, Any]],
    base_url: str,
    model: Optional[str],
    timeout: int,
) -> Optional[int]:
    if not candidates:
        return None
    model = model or get_vllm_model(base_url)
    text = (
        "你是 EPUB 封面识别助手。请根据书名、作者和候选图片判断哪一张是这本书的封面。"
        "封面通常是整页竖向书封，可能包含书名/作者/出版社；不要选择章节插图、内页配图、logo、二维码或 Digital Lab 页面。"
        "如果没有明确封面，selected_index 返回 null。只输出 JSON。\n"
        f"书名：{title}\n作者线索：{', '.join(authors)}\n"
        "候选图片元数据：\n"
        + json.dumps(
            [
                {
                    "index": idx,
                    "href": c["href"],
                    "spine_index": c["spine_index"],
                    "width": c["width"],
                    "height": c["height"],
                    "page_text": c["page_text"],
                    "rule_score": c["score"],
                }
                for idx, c in enumerate(candidates)
            ],
            ensure_ascii=False,
        )
        + '\n输出格式：{"selected_index":0,"confidence":0.95,"reason":"..."}'
    )
    content: List[Dict[str, Any]] = [{"type": "text", "text": text}]
    for idx, candidate in enumerate(candidates):
        content.append({"type": "text", "text": f"候选 {idx}: {candidate['href']}"})
        content.append({"type": "image_url", "image_url": {"url": image_data_url(candidate["data"], candidate["href"])}})
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "temperature": 0,
        "max_tokens": 500,
    }
    data = http_json(base_url.rstrip("/") + "/v1/chat/completions", payload, timeout=timeout)
    result = extract_json_object(data["choices"][0]["message"]["content"])
    idx = result.get("selected_index")
    if idx is None:
        return None
    try:
        idx = int(idx)
    except Exception:
        return None
    confidence = float(result.get("confidence", 0) or 0)
    if 0 <= idx < len(candidates) and confidence >= 0.55:
        return idx
    return None


def find_front_cover_image(
    epub: ZipFile,
    dom: Document,
    opf_name: str,
    title: str,
    authors: List[str],
    args: Optional[argparse.Namespace] = None,
    vision_only: bool = False,
) -> Tuple[Optional[str], str]:
    manifest_by_id, _ = manifest_maps(dom, opf_name)
    candidates = []
    for spine_index, itemref in enumerate(dom.getElementsByTagName("itemref")[:8]):
        href, media_type = manifest_by_id.get(itemref.getAttribute("idref"), ("", ""))
        if not href or ("html" not in media_type and not href.lower().endswith((".html", ".xhtml", ".htm"))):
            continue
        try:
            raw = epub.read(href).decode("utf-8", errors="replace")
        except Exception:
            continue
        soup = BeautifulSoup(raw, "html5lib")
        page_text = clean_text(soup.get_text(" "), 160)
        for order, img in enumerate(soup.find_all(["img", "image"])):
            src = img.get("src") or img.get("xlink:href") or ""
            if not src:
                continue
            image_href = normalize_href(href, src)
            try:
                data = epub.read(image_href)
            except Exception:
                continue
            width, height = image_size(data)
            area = width * height
            ratio = height / width if width else 0
            score = 0.0
            if spine_index <= 1:
                score += 3
            elif spine_index <= 3:
                score += 1
            if re.search(r"^(cover|封面)$", page_text, re.I):
                score += 5
            elif re.search(r"cover|封面", page_text, re.I):
                score += 3
            if area >= 300_000:
                score += 2
            if 1.15 <= ratio <= 1.8:
                score += 2
            if re.search(r"logo|lab|简介|二维码|wechat|微信", page_text, re.I):
                score -= 3
            candidates.append(
                {
                    "href": image_href,
                    "data": data,
                    "score": score,
                    "spine_index": spine_index,
                    "order": order,
                    "width": width,
                    "height": height,
                    "page_text": page_text,
                }
            )
    candidates.sort(key=lambda x: (x["score"], -x["spine_index"], -x["order"]), reverse=True)
    vision_candidates = [c for c in candidates if c["score"] >= 4 and len(c["data"]) <= 4_000_000][:4]
    if args and not args.no_llm and not args.no_cover_vision and vision_candidates:
        try:
            selected = choose_cover_with_vision(
                strip_title_noise(title),
                [normalize_author(x) for x in authors if normalize_author(x)],
                vision_candidates,
                args.vllm_base_url,
                args.model,
                args.cover_vision_timeout,
            )
            if selected is not None:
                return vision_candidates[selected]["href"], "front_image_vision"
        except Exception as exc:
            print(f"[warn] cover vision check failed for {strip_title_noise(title)}: {exc}", file=sys.stderr)
    if vision_only:
        return None, ""
    if candidates and candidates[0]["score"] >= 7:
        return candidates[0]["href"], "front_image"
    return None, ""


def join_opf_path(opf_name: str, href: str) -> str:
    base = str(Path(opf_name).parent)
    return href if base == "." else f"{base}/{href}"


def rel_to_opf(opf_name: str, path: str) -> str:
    base = str(Path(opf_name).parent)
    if base == ".":
        return path
    prefix = base + "/"
    return path[len(prefix) :] if path.startswith(prefix) else path


def parse_author_abstract(abstract: str) -> Tuple[List[str], Dict[str, str]]:
    parts = [clean_text(x) for x in abstract.split("/") if clean_text(x)]
    info: Dict[str, str] = {}
    authors: List[str] = []
    if parts:
        for part in parts[:2]:
            if re.search(r"出版社|书店|出版", part):
                break
            if re.search(r"译|价格|元|CNY|HKD|^\d{4}", part, re.I):
                continue
            authors.append(normalize_author(part))
    for part in parts:
        if re.search(r"出版社|书店|出版", part):
            info["publisher"] = part
        elif re.match(r"\d{4}", part):
            info["date"] = part
    return [x for x in authors if x], info


def normalize_author(author: str) -> str:
    author = clean_text(author)
    author = author.replace("•", "·").replace("・", "·").replace("．", "·")
    author = re.sub(r"\s+", " ", author)
    author = re.sub(r"\s*(著|编著|主编|编)$", "", author)
    author = re.sub(r"\s*\([^)]*\)", "", author).strip()
    author = re.sub(r"^（([^）]+)）", r"[\1] ", author)
    author = re.sub(r"^〔([^〕]+)〕", r"[\1] ", author)
    author = re.sub(r"^\[(.*?)\]\s*", lambda m: f"[{m.group(1).strip()}] ", author)
    if not author.startswith("["):
        known = {
            "三岛由纪夫": "[日] 三岛由纪夫",
            "维克多雨果": "[法] 维克多·雨果",
            "雨果": "[法] 维克多·雨果",
            "VictorHugo": "[法] 维克多·雨果",
        }
        compact = re.sub(r"[·\s,，.。;；]", "", author)
        if compact in known:
            return known[compact]
    return author


def author_core(author: str) -> str:
    author = normalize_author(author)
    author = re.sub(r"^\[[^\]]+\]\s*", "", author)
    author = re.sub(r"[·\s,，.。;；]", "", author)
    return author.lower()


def authors_match(query_authors: List[str], candidate_authors: List[str]) -> bool:
    if not query_authors or not candidate_authors:
        return False
    query = [author_core(x) for x in query_authors if author_core(x)]
    cand = [author_core(x) for x in candidate_authors if author_core(x)]
    for q in query:
        for c in cand:
            if q and c and (q in c or c in q):
                return True
    return False


def title_relevance(query_title: str, candidate_title: str) -> float:
    title = strip_title_noise(candidate_title)
    q = strip_title_noise(query_title)
    if not q or not title:
        return 0.0
    if title == q:
        return 5.0
    if q in title or title in q:
        return 3.0
    return 0.0


def douban_suggest(title: str, author_hint: str, cache_dir: Path, delay: float) -> List[Dict[str, Any]]:
    query = f"{title} {author_hint}".strip()
    url = "https://book.douban.com/j/subject_suggest?q=" + urllib.parse.quote(query)
    try:
        data = http_get(url, cache_dir, delay).decode("utf-8", errors="replace")
        rows = json.loads(data)
    except Exception:
        return []
    candidates = []
    for row in rows:
        if row.get("type") != "b":
            continue
        authors = [normalize_author(row.get("author_name", ""))] if row.get("author_name") else []
        candidates.append(
            {
                "id": str(row.get("id", "")),
                "title": clean_text(row.get("title", "")),
                "url": row.get("url", ""),
                "cover_url": row.get("pic", ""),
                "authors": authors,
                "year": row.get("year", ""),
                "source": "douban_suggest",
            }
        )
    return candidates


def douban_search(title: str, cache_dir: Path, delay: float) -> List[Dict[str, Any]]:
    url = "https://book.douban.com/subject_search?cat=1001&search_text=" + urllib.parse.quote(title)
    try:
        data = http_get(url, cache_dir, delay).decode("utf-8", errors="replace")
    except Exception:
        return []
    match = re.search(r"window\.__DATA__\s*=\s*(\{.*?\});", data, re.S)
    if not match:
        return []
    try:
        items = json.loads(match.group(1)).get("items", [])
    except Exception:
        return []
    candidates = []
    for item in items:
        if item.get("tpl_name") != "search_subject":
            continue
        authors, info = parse_author_abstract(item.get("abstract", ""))
        candidates.append(
            {
                "id": str(item.get("id", "")),
                "title": clean_text(item.get("title", "")),
                "url": item.get("url", ""),
                "cover_url": item.get("cover_url", ""),
                "authors": authors,
                "publisher": info.get("publisher", ""),
                "date": info.get("date", ""),
                "rating": (item.get("rating") or {}).get("value") or None,
                "rating_count": (item.get("rating") or {}).get("count") or None,
                "source": "douban_search",
            }
        )
    return candidates


def google_books_search(title: str, author_hint: str, cache_dir: Path, delay: float, api_key: str = "") -> List[Dict[str, Any]]:
    query = f"{title} {author_hint}".strip() or title
    params = {
        "q": query,
        "maxResults": "10",
        "printType": "books",
        "langRestrict": "zh" if re.search(r"[\u4e00-\u9fff]", title + author_hint) else "",
    }
    if api_key:
        params["key"] = api_key
    url = "https://www.googleapis.com/books/v1/volumes?" + urllib.parse.urlencode({k: v for k, v in params.items() if v})
    try:
        data = http_get(url, cache_dir, delay).decode("utf-8", errors="replace")
        rows = json.loads(data).get("items", [])
    except Exception:
        return []

    candidates = []
    for row in rows:
        info = row.get("volumeInfo") or {}
        title_value = clean_text(info.get("title", ""))
        subtitle = clean_text(info.get("subtitle", ""))
        if subtitle and subtitle not in title_value:
            title_value = f"{title_value}: {subtitle}" if title_value else subtitle
        identifiers = info.get("industryIdentifiers") or []
        isbn = ""
        for ident in identifiers:
            if ident.get("type") in {"ISBN_13", "ISBN_10"} and ident.get("identifier"):
                isbn = clean_text(ident.get("identifier"))
                if ident.get("type") == "ISBN_13":
                    break
        image_links = info.get("imageLinks") or {}
        cover_url = image_links.get("thumbnail") or image_links.get("smallThumbnail") or ""
        if cover_url.startswith("http://"):
            cover_url = "https://" + cover_url[len("http://") :]
        candidates.append(
            {
                "id": str(row.get("id", "")),
                "title": title_value,
                "url": info.get("infoLink", ""),
                "cover_url": cover_url,
                "authors": [normalize_author(x) for x in info.get("authors", []) if normalize_author(x)],
                "publisher": clean_text(info.get("publisher", "")),
                "date": clean_text(info.get("publishedDate", "")),
                "description": clean_text(info.get("description", "")),
                "isbn": isbn,
                "tags": [clean_text(x) for x in info.get("categories", []) if clean_text(x)][:8],
                "rating": info.get("averageRating"),
                "rating_count": info.get("ratingsCount"),
                "source": "google_books",
            }
        )
    return candidates


def parse_subject_page(candidate: Dict[str, Any], cache_dir: Path, delay: float) -> Dict[str, Any]:
    if candidate.get("source") == "google_books":
        return candidate
    url = candidate.get("url")
    if not url:
        return candidate
    try:
        page = http_get(url, cache_dir, delay).decode("utf-8", errors="replace")
    except Exception:
        return candidate
    soup = BeautifulSoup(page, "html5lib")
    out = dict(candidate)

    ld = soup.find("script", {"type": "application/ld+json"})
    if ld and ld.string:
        try:
            data = json.loads(ld.string)
            out["title"] = clean_text(data.get("name") or out.get("title"))
            if data.get("isbn"):
                out["isbn"] = clean_text(data.get("isbn"))
            authors = data.get("author") or []
            if isinstance(authors, dict):
                authors = [authors]
            parsed = [normalize_author(x.get("name", "")) for x in authors if isinstance(x, dict)]
            if parsed:
                out["authors"] = parsed
        except Exception:
            pass

    og_desc = soup.find("meta", {"property": "og:description"})
    if og_desc:
        out["description"] = clean_text(og_desc.get("content", ""))
    og_img = soup.find("meta", {"property": "og:image"})
    if og_img:
        out["cover_url"] = og_img.get("content", out.get("cover_url", ""))

    info = soup.find(id="info")
    if info:
        text = "\n".join(info.stripped_strings)
        out.update(parse_info_text(text))

    rating = soup.select_one("strong.rating_num")
    if rating:
        out["rating"] = clean_text(rating.get_text())
    votes = soup.select_one("span[property='v:votes']")
    if votes:
        out["rating_count"] = clean_text(votes.get_text())

    summary = extract_section_text(soup, "内容简介")
    if summary:
        out["description"] = summary

    tags = extract_tags_from_page(page)
    if tags:
        out["tags"] = tags[:12]
    return out


def parse_info_text(text: str) -> Dict[str, Any]:
    fields: Dict[str, Any] = {}
    lines = [clean_text(x) for x in text.splitlines() if clean_text(x)]
    joined = "\n".join(lines)
    mapping = {"出版社": "publisher", "出品方": "producer", "副标题": "subtitle", "原作名": "original_title", "出版年": "date", "页数": "pages", "定价": "price", "装帧": "binding", "丛书": "series", "ISBN": "isbn"}
    for label, key in mapping.items():
        m = re.search(label + r"\s*:\s*([^\n]+)", joined)
        if m:
            fields[key] = clean_text(m.group(1))
    return fields


def extract_section_text(soup: BeautifulSoup, title: str) -> str:
    for h2 in soup.find_all("h2"):
        if title not in h2.get_text():
            continue
        container = h2.find_next_sibling()
        while container and container.name not in {"div", "span"}:
            container = container.find_next_sibling()
        if not container:
            continue
        full = container.find("span", class_="all")
        target = full or container
        text = "\n".join(clean_text(p.get_text()) for p in target.find_all("p"))
        return clean_text(text)
    return ""


def extract_tags_from_page(page: str) -> List[str]:
    m = re.search(r"criteria\s*=\s*'([^']+)'", page)
    if not m:
        return []
    tags = []
    for part in html.unescape(m.group(1)).split("|"):
        if part.startswith("7:"):
            tag = clean_text(part[2:])
            if tag and tag not in tags:
                tags.append(tag)
    return tags


def score_candidate(query_title: str, query_authors: List[str], candidate: Dict[str, Any]) -> float:
    score = title_relevance(query_title, candidate.get("title", ""))
    if candidate.get("rating_count"):
        try:
            score += min(float(candidate["rating_count"]) / 10000, 1.5)
        except Exception:
            pass
    cand_authors = " ".join(candidate.get("authors") or [])
    if authors_match(query_authors, candidate.get("authors") or []):
        score += 2
    elif query_authors and cand_authors:
        score -= 4
    if candidate.get("cover_url") and "default" not in candidate.get("cover_url", ""):
        score += 0.3
    return score


def plausible_candidate(query_title: str, query_authors: List[str], candidate: Dict[str, Any]) -> bool:
    if title_relevance(query_title, candidate.get("title", "")) < 3:
        return False
    cand_authors = candidate.get("authors") or []
    if query_authors and cand_authors and not authors_match(query_authors, cand_authors):
        return False
    if query_authors and not cand_authors and title_relevance(query_title, candidate.get("title", "")) < 5:
        return False
    return True


def choose_with_llm(
    title: str,
    authors: List[str],
    candidates: List[Dict[str, Any]],
    base_url: str,
    model: Optional[str],
    timeout: int,
) -> Optional[Dict[str, Any]]:
    if not candidates:
        return None
    model = model or get_vllm_model(base_url)
    compact = []
    for idx, c in enumerate(candidates):
        compact.append(
            {
                "index": idx,
                "title": c.get("title", ""),
                "authors": c.get("authors", []),
                "publisher": c.get("publisher", ""),
                "date": c.get("date", c.get("year", "")),
                "isbn": c.get("isbn", ""),
                "rating": c.get("rating"),
                "rating_count": c.get("rating_count"),
                "description": clean_text(c.get("description", ""), 260),
                "url": c.get("url", ""),
            }
        )
    prompt = (
        "你是书籍元数据清洗助手。请从多个来源的候选中选择最匹配目标书的一项，并清洗最终元数据。\n"
        f"目标标题：{title}\n目标作者线索：{', '.join(authors)}\n\n"
        "规则：标题尽量只保留主标题；必要时保留主标题+副标题。删除推荐语、版本说明、套装说明。"
        "作者只保留主要作者/编者，不要译者。外国或古代作者尽量保留国别方括号，例如 [美] 杰克·伦敦。"
        "如果目标作者线索是简称，而豆瓣候选作者明显是同一作者的更完整译名，应使用更完整译名。"
        "如果候选明显不匹配，selected_index 返回 null。不要编造候选来源没有给出的 ISBN/出版社等事实。"
        "中文书优先相信豆瓣的中文标题、作者和简介；外文书可优先相信 Google Books 的原文元数据。\n\n"
        "只输出 JSON："
        '{"selected_index":0,"confidence":0.95,"metadata":{"title":"...","authors":["..."],"description":"...","publisher":"","date":"","isbn":"","tags":[],"rating":"","rating_count":""},"reason":"..."}\n\n'
        f"候选：{json.dumps(compact, ensure_ascii=False)}"
    )
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": "你只输出可解析 JSON。"}, {"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 1800,
        "response_format": {"type": "json_object"},
    }
    data = http_json(base_url.rstrip("/") + "/v1/chat/completions", payload, timeout=timeout)
    result = extract_json_object(data["choices"][0]["message"]["content"])
    idx = result.get("selected_index")
    if idx is None:
        return None
    try:
        selected = dict(candidates[int(idx)])
    except Exception:
        return None
    llm_meta = result.get("metadata") or {}
    # The prompt only sends a short description preview to keep the request
    # small, so keep full parsed subject-page fields unless the candidate lacks
    # them.  LLM output is mainly trusted for title/author cleanup.
    for key in ["title", "authors"]:
        if llm_meta.get(key):
            selected[key] = llm_meta[key]
    for key, value in llm_meta.items():
        if key in {"title", "authors"}:
            continue
        if value and not selected.get(key):
            selected[key] = value
    selected["_llm_reason"] = result.get("reason", "")
    selected["_llm_confidence"] = result.get("confidence", 0)
    return selected


def fallback_description_with_llm(title: str, authors: List[str], base_url: str, model: Optional[str], timeout: int) -> str:
    model = model or get_vllm_model(base_url)
    prompt = (
        "请为一本书写很短的中文内容简介。只有在你确实知道这本书时才写；不熟悉就返回空字符串。"
        "不要编造具体奖项、出版社、情节或作者生平。只输出 JSON。\n"
        f"书名：{title}\n作者：{', '.join(authors)}\n"
        '{"description":"...或空字符串"}'
    )
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": "你只输出可解析 JSON。"}, {"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 500,
        "response_format": {"type": "json_object"},
    }
    data = http_json(base_url.rstrip("/") + "/v1/chat/completions", payload, timeout=timeout)
    result = extract_json_object(data["choices"][0]["message"]["content"])
    return clean_text(result.get("description", ""))


def fallback_metadata_with_llm(title: str, authors: List[str], base_url: str, model: Optional[str], timeout: int) -> Dict[str, Any]:
    model = model or get_vllm_model(base_url)
    prompt = (
        "请在你确实熟悉这本书时，补充最小元数据。"
        "不熟悉就返回空字段。不要编造 ISBN、出版社、评分、标签或具体事实。"
        "简介只能写非常概括的一句话，并在不确定时留空。只输出 JSON。\n"
        f"书名：{title}\n作者：{', '.join(authors)}\n"
        '{"description":"","tags":[]}'
    )
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": "你只输出可解析 JSON。"}, {"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 500,
        "response_format": {"type": "json_object"},
    }
    data = http_json(base_url.rstrip("/") + "/v1/chat/completions", payload, timeout=timeout)
    result = extract_json_object(data["choices"][0]["message"]["content"])
    return {
        "description": clean_text(result.get("description", "")),
        "tags": [clean_text(x) for x in result.get("tags", []) if clean_text(x)][:8],
    }


def prefer_better_authors(author_hints: List[str], selected_authors: List[str]) -> List[str]:
    if not selected_authors:
        return author_hints
    if not author_hints:
        return selected_authors
    if len(author_hints) != len(selected_authors):
        return author_hints
    merged = []
    for hint, selected in zip(author_hints, selected_authors):
        h = normalize_author(hint)
        s = normalize_author(selected)
        if authors_match([h], [s]) and len(author_core(s)) > len(author_core(h)):
            merged.append(s)
        else:
            merged.append(h)
    return merged


def extract_author_with_llm(
    title: str,
    front_text: str,
    base_url: str,
    model: Optional[str],
    timeout: int,
) -> List[str]:
    if not front_text.strip():
        return []
    model = model or get_vllm_model(base_url)
    prompt = (
        "你是图书版权页/扉页信息抽取助手。请从给定文本中找出这本书的主要作者或编者。"
        "不要返回译者、校者、设计者、出品人、出版机构。"
        "如果无法确定作者，返回空数组。"
        "作者格式要规范：外国/古代作者尽量用国别方括号，例如 [古希腊] 荷马、[美] 杰克·伦敦；中点用 ·。"
        "删除“著/编著/主编”等角色词。只输出 JSON。\n\n"
        f"书名：{title}\n"
        f"前几页文本：\n{front_text}\n\n"
        '{"authors":["..."],"confidence":0.9,"reason":"..."}'
    )
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": "你只输出可解析 JSON。"}, {"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 600,
        "response_format": {"type": "json_object"},
    }
    data = http_json(base_url.rstrip("/") + "/v1/chat/completions", payload, timeout=timeout)
    result = extract_json_object(data["choices"][0]["message"]["content"])
    if float(result.get("confidence", 0) or 0) < 0.45:
        return []
    return [normalize_author(x) for x in result.get("authors", []) if normalize_author(x)]


def normalize_author_hints_with_llm(
    authors: List[str],
    base_url: str,
    model: Optional[str],
    timeout: int,
) -> List[str]:
    authors = [normalize_author(x) for x in authors if normalize_author(x)]
    if not authors:
        return []
    if all(x.startswith("[") for x in authors):
        return authors
    model = model or get_vllm_model(base_url)
    prompt = (
        "你是图书作者名格式规范化助手。请只做作者格式清洗，不要增删作者。"
        "保持输入作者数量和顺序不变。若确实知道外国或古代作者的国别/时代，请补方括号前缀，"
        "例如 三岛由纪夫 -> [日] 三岛由纪夫，荷马 -> [古希腊] 荷马，杰克·伦敦 -> [美] 杰克·伦敦。"
        "如果不确定国别，不要猜，保留原样。不要返回译者。中点用 ·。只输出 JSON。\n"
        f"作者：{json.dumps(authors, ensure_ascii=False)}\n"
        '{"authors":["..."],"confidence":0.9,"reason":"..."}'
    )
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": "你只输出可解析 JSON。"}, {"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 500,
        "response_format": {"type": "json_object"},
    }
    data = http_json(base_url.rstrip("/") + "/v1/chat/completions", payload, timeout=timeout)
    result = extract_json_object(data["choices"][0]["message"]["content"])
    if float(result.get("confidence", 0) or 0) < 0.5:
        return authors
    normalized = [normalize_author(x) for x in result.get("authors", []) if normalize_author(x)]
    return normalized if len(normalized) == len(authors) else authors


def is_generic_collection_author(author: str) -> bool:
    author = normalize_author(author)
    return bool(re.search(r"等|多人|合集|编委|节目组|编辑部|出版社|杂志社|译文纪实|epub|www|\.com|,|，|、|;|；", author, re.I))


def opf_authors_trustworthy(epub_meta: Dict[str, Any]) -> Tuple[List[str], str]:
    authors = [normalize_author(x) for x in epub_meta.get("authors", []) if normalize_author(x)]
    if not authors:
        return [], "opf_empty"
    if len(authors) == 1 and not is_generic_collection_author(authors[0]):
        return authors, "opf_single_author"
    return [], "opf_multi_or_generic"


def resolve_author_hints(epub_path: Path, epub_meta: Dict[str, Any], args: argparse.Namespace) -> Tuple[List[str], str]:
    opf_authors, opf_source = opf_authors_trustworthy(epub_meta)
    if opf_authors:
        if not args.no_llm:
            try:
                normalized = normalize_author_hints_with_llm(opf_authors, args.vllm_base_url, args.model, args.llm_timeout)
                if normalized != opf_authors:
                    return normalized, opf_source + "_llm_normalized"
            except Exception as exc:
                print(f"[warn] OPF author normalization failed for {epub_meta['title']}: {exc}", file=sys.stderr)
        return opf_authors, opf_source
    if args.no_author_extract or args.no_llm:
        return [], opf_source if args.no_author_extract else "disabled"

    try:
        front_text = read_front_matter_text(epub_path, args.author_front_files, args.author_front_chars)
        authors = extract_author_with_llm(
            epub_meta["title"],
            front_text,
            args.vllm_base_url,
            args.model,
            args.llm_timeout,
        )
        return authors, "front_matter_llm" if authors else "front_matter_empty"
    except Exception as exc:
        print(f"[warn] author extraction failed for {epub_meta['title']}: {exc}", file=sys.stderr)
        return [], "front_matter_failed"


def find_metadata(epub_path: Path, epub_meta: Dict[str, Any], args: argparse.Namespace) -> Dict[str, Any]:
    title = strip_title_noise(epub_meta["title"])
    author_hints, author_source = resolve_author_hints(epub_path, epub_meta, args)
    author_hint = author_hints[0] if author_hints else ""

    sources = set(getattr(args, "metadata_sources", None) or ["douban"])
    candidates: List[Dict[str, Any]] = []
    if "douban" in sources:
        candidates += douban_suggest(title, "", args.cache_dir, args.delay)
        if author_hint:
            candidates += douban_suggest(title, author_hint, args.cache_dir, args.delay)
        if author_hint:
            candidates += douban_search(f"{title} {author_hint}", args.cache_dir, args.delay)
        candidates += douban_search(title, args.cache_dir, args.delay)
    if "google_books" in sources:
        candidates += google_books_search(title, author_hint, args.cache_dir, args.delay, getattr(args, "google_books_api_key", "") or "")

    by_id: Dict[str, Dict[str, Any]] = {}
    for cand in candidates:
        key = f"{cand.get('source', '')}:{cand.get('id', '') or cand.get('url', '')}"
        if key and key not in by_id:
            by_id[key] = cand
    candidates = sorted(by_id.values(), key=lambda c: score_candidate(title, author_hints, c), reverse=True)[: args.max_candidates]
    detailed = [parse_subject_page(c, args.cache_dir, args.delay) for c in candidates]
    plausible = [c for c in detailed if plausible_candidate(title, author_hints, c)]

    selected: Optional[Dict[str, Any]] = None
    if plausible and not args.no_llm:
        try:
            selected = choose_with_llm(title, author_hints, plausible, args.vllm_base_url, args.model, args.llm_timeout)
        except Exception as exc:
            print(f"[warn] LLM candidate selection failed for {title}: {exc}", file=sys.stderr)
    if selected is None and plausible:
        best = plausible[0]
        if score_candidate(title, author_hints, best) >= 3:
            selected = best

    if selected is None:
        selected = {"title": title, "authors": author_hints, "description": "", "tags": [], "_match": "miss"}
        if args.llm_describe_miss and not args.no_llm:
            try:
                selected.update(fallback_metadata_with_llm(title, author_hints, args.vllm_base_url, args.model, args.llm_timeout))
                selected["_description_source"] = "llm_fallback"
            except Exception as exc:
                print(f"[warn] LLM fallback description failed for {title}: {exc}", file=sys.stderr)
    else:
        selected["_match"] = selected.get("source") or "metadata"

    selected["title"] = strip_title_noise(selected.get("title") or title)
    selected_authors = [normalize_author(x) for x in selected.get("authors", []) if normalize_author(x)]
    selected["authors"] = prefer_better_authors(author_hints, selected_authors)
    selected["_author_hints"] = author_hints
    selected["_author_source"] = author_source
    return selected


def ensure_text_node(dom: Document, node: Element, value: str) -> None:
    while node.firstChild:
        node.removeChild(node.firstChild)
    node.appendChild(dom.createTextNode(value))


def remove_children(parent: Element, tag_name: str) -> None:
    for node in list(parent.getElementsByTagName(tag_name)):
        if node.parentNode is parent:
            parent.removeChild(node)


def append_text(dom: Document, parent: Element, tag_name: str, text: str, attrs: Optional[Dict[str, str]] = None) -> Element:
    node = dom.createElement(tag_name)
    if attrs:
        for key, value in attrs.items():
            node.setAttribute(key, value)
    node.appendChild(dom.createTextNode(text))
    parent.appendChild(node)
    return node


def add_or_replace_metadata(
    epub_path: Path,
    out_path: Path,
    metadata: Dict[str, Any],
    cover_bytes: Optional[bytes],
    cover_ext: str,
    existing_cover_href: Optional[str] = None,
) -> None:
    with ZipFile(epub_path, "r") as zin:
        opf_name = opf_path(zin)
        dom = parseString(zin.read(opf_name))
        metadata_node = dom.getElementsByTagName("metadata")[0]
        manifest = dom.getElementsByTagName("manifest")[0]
        spine = dom.getElementsByTagName("spine")[0] if dom.getElementsByTagName("spine") else None

        for tag in ["dc:title", "dc:creator", "dc:description", "dc:publisher", "dc:date", "dc:subject"]:
            remove_children(metadata_node, tag)
        append_text(dom, metadata_node, "dc:title", metadata.get("title", ""))
        for author in metadata.get("authors") or []:
            append_text(dom, metadata_node, "dc:creator", author, {"opf:role": "aut"})
        if metadata.get("description"):
            append_text(dom, metadata_node, "dc:description", metadata["description"])
        if metadata.get("publisher"):
            append_text(dom, metadata_node, "dc:publisher", metadata["publisher"])
        if metadata.get("date"):
            append_text(dom, metadata_node, "dc:date", str(metadata["date"]))
        for tag in metadata.get("tags") or []:
            append_text(dom, metadata_node, "dc:subject", str(tag))

        if metadata.get("isbn"):
            append_text(dom, metadata_node, "dc:identifier", str(metadata["isbn"]), {"opf:scheme": "ISBN"})
        if metadata.get("id") or metadata.get("url"):
            append_text(dom, metadata_node, "dc:identifier", str(metadata.get("id") or metadata.get("url")), {"opf:scheme": "Douban"})
        if metadata.get("rating"):
            append_meta(dom, metadata_node, "douban:rating", str(metadata["rating"]))
        if metadata.get("rating_count"):
            append_meta(dom, metadata_node, "douban:rating_count", str(metadata["rating_count"]))

        added_paths: Dict[str, bytes] = {}
        if cover_bytes:
            cover_path = f"images/cover{cover_ext}"
            cover_href = rel_to_opf(opf_name, cover_path)
            ensure_cover_manifest_meta(dom, metadata_node, manifest, opf_name, cover_path)
            added_paths[cover_path] = cover_bytes
            if spine is not None:
                add_cover_page(dom, manifest, spine, opf_name, cover_href, added_paths)
        elif existing_cover_href:
            ensure_cover_manifest_meta(dom, metadata_node, manifest, opf_name, existing_cover_href)

        ncx_name = find_ncx_name(dom, opf_name)
        ncx_data = zin.read(ncx_name) if ncx_name and ncx_name in zin.namelist() else None
        if ncx_data:
            ncx_dom = parseString(ncx_data)
            for text_node in ncx_dom.getElementsByTagName("docTitle"):
                texts = text_node.getElementsByTagName("text")
                if texts:
                    ensure_text_node(ncx_dom, texts[0], metadata.get("title", ""))
            added_paths[ncx_name] = ncx_dom.toxml(encoding="utf-8")

        out_path.parent.mkdir(parents=True, exist_ok=True)
        with ZipFile(out_path, "w") as zout:
            names_written = set()
            if "mimetype" in zin.namelist():
                zout.writestr("mimetype", zin.read("mimetype"), compress_type=ZIP_STORED)
                names_written.add("mimetype")
            for info in zin.infolist():
                if info.filename in names_written or info.filename in added_paths or info.filename == opf_name:
                    continue
                zout.writestr(info, zin.read(info.filename))
                names_written.add(info.filename)
            zout.writestr(opf_name, dom.toxml(encoding="utf-8"), compress_type=ZIP_DEFLATED)
            for path, data in added_paths.items():
                zout.writestr(path, data, compress_type=ZIP_DEFLATED)


def append_meta(dom: Document, metadata_node: Element, name: str, content: str) -> None:
    meta = dom.createElement("meta")
    meta.setAttribute("name", name)
    meta.setAttribute("content", content)
    metadata_node.appendChild(meta)


def remove_cover_meta(metadata_node: Element) -> None:
    for meta in list(metadata_node.getElementsByTagName("meta")):
        if meta.parentNode is metadata_node and meta.getAttribute("name") == "cover":
            metadata_node.removeChild(meta)


def remove_manifest_item_by_id(manifest: Element, item_id: str) -> None:
    for item in list(manifest.getElementsByTagName("item")):
        if item.parentNode is manifest and item.getAttribute("id") == item_id:
            manifest.removeChild(item)


def ensure_cover_manifest_meta(dom: Document, metadata_node: Element, manifest: Element, opf_name: str, cover_path: str) -> None:
    cover_href = rel_to_opf(opf_name, cover_path)
    remove_manifest_item_by_id(manifest, "coverimageid")
    existing_item = None
    for item in manifest.getElementsByTagName("item"):
        if item.parentNode is manifest and item.getAttribute("href") == cover_href:
            existing_item = item
            break
    if existing_item is None:
        existing_item = dom.createElement("item")
        existing_item.setAttribute("href", cover_href)
        manifest.appendChild(existing_item)
    existing_item.setAttribute("id", "coverimageid")
    existing_item.setAttribute("media-type", mimetypes.guess_type(cover_path)[0] or "image/jpeg")
    remove_cover_meta(metadata_node)
    append_meta(dom, metadata_node, "cover", "coverimageid")


def add_cover_page(dom: Document, manifest: Element, spine: Element, opf_name: str, cover_href: str, added_paths: Dict[str, bytes]) -> None:
    cover_page_path = "cover.xhtml"
    cover_page_href = rel_to_opf(opf_name, cover_page_path)
    remove_manifest_item_by_id(manifest, "cover")
    item = dom.createElement("item")
    item.setAttribute("id", "cover")
    item.setAttribute("href", cover_page_href)
    item.setAttribute("media-type", "application/xhtml+xml")
    manifest.appendChild(item)
    has_cover_ref = any(x.getAttribute("idref") == "cover" for x in spine.getElementsByTagName("itemref"))
    if not has_cover_ref:
        itemref = dom.createElement("itemref")
        itemref.setAttribute("idref", "cover")
        itemref.setAttribute("linear", "yes")
        if spine.firstChild:
            spine.insertBefore(itemref, spine.firstChild)
        else:
            spine.appendChild(itemref)
    added_paths[cover_page_path] = f'''<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Cover</title></head><body><div style="text-align:center"><img src="{cover_href}" alt="cover"/></div></body></html>'''.encode("utf-8")


def find_ncx_name(dom: Document, opf_name: str) -> Optional[str]:
    for item in dom.getElementsByTagName("item"):
        if item.getAttribute("media-type") == "application/x-dtbncx+xml":
            return join_opf_path(opf_name, item.getAttribute("href"))
    return None


def download_cover(metadata: Dict[str, Any], cache_dir: Path, delay: float) -> Tuple[Optional[bytes], str]:
    url = metadata.get("cover_url") or ""
    if not url or "default" in url:
        return None, ".jpg"
    try:
        data = http_get(url, cache_dir, delay, binary=True)
    except Exception:
        return None, ".jpg"
    ext = Path(urllib.parse.urlparse(url).path).suffix.lower()
    if ext not in {".jpg", ".jpeg", ".png", ".webp"}:
        ext = ".jpg"
    return data, ext


def output_path_for(epub: Path, input_root: Path, output_dir: Optional[Path], inplace: bool) -> Path:
    if inplace:
        return epub
    if output_dir:
        try:
            rel = epub.relative_to(input_root)
        except ValueError:
            rel = Path(epub.name)
        return output_dir / rel
    return epub.with_name(epub.stem + ".metadata.epub")


def iter_epubs(paths: List[Path]) -> List[Path]:
    out = []
    for path in paths:
        if path.is_dir():
            out.extend(sorted(path.glob("*.epub")))
        elif path.suffix.lower() == ".epub":
            out.append(path)
    return out


def enrich_epub_file(
    epub_path: Path,
    out_path: Path,
    args: Optional[argparse.Namespace] = None,
) -> Dict[str, Any]:
    """Enrich one EPUB and return the metadata report entry."""
    args = args or default_args()
    epub_path = Path(epub_path)
    out_path = Path(out_path)
    current = read_epub_metadata(epub_path, args)
    metadata = find_metadata(epub_path, current, args)

    cover_bytes = None
    cover_ext = ".jpg"
    if not current.get("cover_href"):
        cover_bytes, cover_ext = download_cover(metadata, args.cache_dir, args.delay)

    add_or_replace_metadata(epub_path, out_path, metadata, cover_bytes, cover_ext, current.get("cover_href"))
    return {
        "input": str(epub_path),
        "output": str(out_path),
        "metadata": metadata,
        "cover_source": current.get("cover_source", ""),
        "cover_href": current.get("cover_href", ""),
        "downloaded_cover": bool(cover_bytes),
        "wrote": True,
    }


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Enrich EPUB metadata from Douban with conservative requests.")
    parser.add_argument("inputs", nargs="+", type=Path, help="EPUB files or directories")
    parser.add_argument("-o", "--output-dir", type=Path, default=None, help="Write enriched EPUBs under this directory")
    parser.add_argument("--inplace", action="store_true", help="Replace input EPUBs after writing through a temp file")
    parser.add_argument("--dry-run", action="store_true", help="Search and show metadata without writing EPUBs")
    parser.add_argument("--delay", type=float, default=3.0, help="Seconds between uncached Douban/cover requests")
    parser.add_argument("--cache-dir", type=Path, default=Path(".cache/douban"), help="HTTP cache directory")
    parser.add_argument("--max-candidates", type=int, default=5, help="Maximum Douban candidates to detail per book")
    parser.add_argument(
        "--metadata-source",
        action="append",
        choices=["douban", "google_books"],
        default=None,
        help="Metadata source to query. Can be repeated. Defaults to douban.",
    )
    parser.add_argument("--google-books-api-key", default="", help="Optional Google Books API key")
    parser.add_argument("--no-llm", action="store_true", help="Do not use vLLM for candidate choice/cleanup/fallback intro")
    parser.add_argument("--vllm-base-url", default=DEFAULT_VLLM_BASE_URL, help="OpenAI-compatible vLLM base URL")
    parser.add_argument("--model", default=None, help="vLLM model id. Defaults to first /v1/models entry")
    parser.add_argument("--llm-timeout", type=int, default=60, help="Seconds to wait for each LLM metadata call")
    parser.add_argument("--no-author-extract", action="store_true", help="Do not ask LLM to extract author from EPUB front matter")
    parser.add_argument("--author-front-files", type=int, default=8, help="Number of front matter HTML files to inspect for author extraction")
    parser.add_argument("--author-front-chars", type=int, default=6000, help="Maximum front matter text characters sent to LLM")
    parser.add_argument("--no-cover-vision", action="store_true", help="Do not ask vision-capable vLLM to confirm front-matter cover images")
    parser.add_argument("--cover-vision-timeout", type=int, default=45, help="Seconds to wait for each cover vision check")
    parser.add_argument("--llm-describe-miss", action="store_true", help="Let LLM add a cautious fallback description for Douban misses")
    parser.add_argument("--work-hard", action="store_true", help="Reserved for later broader cover/web search; currently no extra crawling")
    parser.add_argument("--report", type=Path, default=None, help="Write JSON report")
    args = parser.parse_args(argv)

    epubs = iter_epubs([x.resolve() for x in args.inputs])
    if not epubs:
        print("No EPUB files found.", file=sys.stderr)
        return 1
    if args.inplace and args.output_dir:
        print("--inplace and --output-dir cannot be used together.", file=sys.stderr)
        return 2

    input_root = epubs[0].parent if len(epubs) == 1 else Path(os.path.commonpath([str(p.parent) for p in epubs]))
    report = []
    for idx, epub in enumerate(epubs, 1):
        print(f"[{idx}/{len(epubs)}] {epub.name}")
        current = read_epub_metadata(epub, args)
        metadata = find_metadata(epub, current, args)
        author_note = f" author_source={metadata.get('_author_source')}"
        if metadata.get("_author_hints"):
            author_note += f" hints={'; '.join(metadata.get('_author_hints') or [])}"
        print(f"  -> {metadata.get('title')} / {'; '.join(metadata.get('authors') or [])} [{metadata.get('_match')}]{author_note}")
        if metadata.get("description"):
            print(f"     {clean_text(metadata['description'], 100)}")
        cover_bytes = None
        cover_ext = ".jpg"
        if not current.get("cover_href"):
            cover_bytes, cover_ext = download_cover(metadata, args.cache_dir, args.delay)
            if cover_bytes:
                print("     cover: downloaded from metadata source")
        else:
            print(f"     cover: kept existing EPUB cover ({current.get('cover_source')}: {current.get('cover_href')})")

        out_path = output_path_for(epub, input_root, args.output_dir.resolve() if args.output_dir else None, args.inplace)
        if not args.dry_run:
            if args.inplace:
                with NamedTemporaryFile(suffix=".epub", delete=False) as tmp:
                    tmp_path = Path(tmp.name)
                add_or_replace_metadata(epub, tmp_path, metadata, cover_bytes, cover_ext, current.get("cover_href"))
                shutil.move(str(tmp_path), epub)
            else:
                add_or_replace_metadata(epub, out_path, metadata, cover_bytes, cover_ext, current.get("cover_href"))
        report.append({"input": str(epub), "output": str(out_path), "metadata": metadata, "wrote": not args.dry_run})

    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
