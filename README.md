# AutoEpubSplitter

English | [简体中文](README.zh-CN.md)

AutoEpubSplitter is a Calibre plugin and command-line toolkit for splitting EPUB collection books into individual volumes, then enriching the resulting EPUB metadata.

It is designed for messy real-world EPUB collections where table-of-contents structure, HTML layout, title formatting, and cover placement are not standardized. The splitter can use a local OpenAI-compatible LLM to judge book boundaries, while EPUB rewriting is handled with bundled code adapted from the GPLv3 EpubSplit plugin.

## Features

- Split EPUB collections into single-book EPUBs.
- Keep multi-volume continuous works together when appropriate, such as a novel split into upper/middle/lower volumes.
- Use a local OpenAI-compatible LLM for split-point detection, author extraction, cover-image selection, and metadata cleanup.
- Conservative heuristic fallback when the LLM is unavailable.
- Enrich metadata from Douban Books and optionally Google Books.
- Prefer covers already embedded in the EPUB; download metadata-source covers only when no internal cover is found.
- Calibre GUI with progress/log windows for long-running jobs.
- Command-line scripts for batch or debugging workflows.

## License

This project is released under GPLv3. See [LICENSE](LICENSE).

AutoEpubSplitter bundles and adapts code from [EpubSplit](https://github.com/JimmXinu/EpubSplit), also GPLv3, by Jim Miller. See [NOTICE](NOTICE) and `calibre_plugin/epubsplit_LICENSE.txt`.

Metadata providers such as Douban Books and Google Books are external services. Users are responsible for following their terms of service. The tool uses conservative request rates and local caching by default.

## Get The Source

```bash
git clone https://github.com/deletoe/AutoEpubSplitter.git
cd AutoEpubSplitter
git submodule update --init --recursive
```

Install Python dependencies if you plan to use the command-line scripts:

```bash
python3 -m pip install -r requirements.txt
```

## Build The Calibre Plugin

```bash
python3 build_calibre_plugin.py
```

The plugin zip will be written to:

```text
dist/AutoEpubSplitter.zip
```

## Install In Calibre

1. Open Calibre.
2. Go to `Preferences` -> `Plugins`.
3. Click `Load plugin from file`.
4. Select `dist/AutoEpubSplitter.zip`.
5. Accept Calibre's third-party plugin warning.
6. Restart Calibre.
7. Select one book that has an EPUB format.
8. Click the `Auto EPUB Splitter` toolbar button.

The plugin first detects split points and shows a confirmation dialog. After you confirm, it writes split EPUBs, enriches metadata, and adds the new books to the current Calibre library.

For safety, test the plugin in a temporary Calibre library before using it on a large production library.

## Plugin Configuration

In Calibre:

`Preferences` -> `Plugins` -> `AutoEpubSplitter` -> `Customize plugin`

Available settings include:

- Enable or disable local LLM usage.
- OpenAI-compatible base URL.
- Model name. Leave empty to use the first model returned by `/v1/models`.
- Split-detection LLM timeout.
- Split-detection LLM max tokens. Default is `65536`.
- Metadata-cleanup LLM timeout.
- Enable cover vision selection.
- Cover vision timeout.
- Enable author extraction from front matter.
- Enable cautious LLM fallback descriptions when metadata sources miss.
- Enable Douban Books metadata source.
- Enable Google Books metadata source.
- Optional Google Books API key.
- Metadata request delay.
- Max metadata candidates per book.
- HTTP cache directory.

The default LLM URL is a local/private example used during development. Change it to your own OpenAI-compatible endpoint, such as a local vLLM server. You can also disable LLM usage and rely on heuristic fallback plus metadata-source scoring.

## Command-Line Splitter

Preview split points:

```bash
python3 auto_split_epub.py --dry-run "samples/example-collection.epub"
```

Skip the LLM and use heuristics:

```bash
python3 auto_split_epub.py --no-llm --dry-run "samples/example-collection.epub"
```

Write split EPUBs:

```bash
python3 auto_split_epub.py --overwrite -o split-output "samples/example-collection.epub"
```

Common options:

- `--vllm-base-url`: OpenAI-compatible endpoint. Defaults to `VLLM_BASE_URL`, then the built-in development default.
- `--model`: Model id. Defaults to the first `/v1/models` entry.
- `--llm-timeout`: Seconds to wait for the LLM.
- `--expected-count`: Optional output-count hint. It is only a hint and does not force trimming or padding.
- `--split-llm-max-tokens 65536`: Max tokens for split-detection LLM responses.
- `--report report.json`: Write a split detection report.

## Command-Line Metadata Enrichment

Dry-run one EPUB:

```bash
python3 enrich_epub_metadata.py --dry-run split-output/01-example.epub
```

Write enriched EPUBs to a new directory:

```bash
python3 enrich_epub_metadata.py -o metadata-output split-output
```

Replace files in place:

```bash
python3 enrich_epub_metadata.py --inplace split-output
```

Use both Douban and Google Books:

```bash
python3 enrich_epub_metadata.py \
  --metadata-source douban \
  --metadata-source google_books \
  split-output
```

Common options:

- `--delay 5`: Slow down uncached metadata/cover requests.
- `--cache-dir .cache/douban`: HTTP cache directory.
- `--max-candidates 5`: Maximum candidates to inspect per book.
- `--metadata-source douban`: Query Douban Books. Can be repeated.
- `--metadata-source google_books`: Query Google Books. Can be repeated.
- `--google-books-api-key`: Optional Google Books API key.
- `--no-llm`: Do not call the LLM.
- `--no-author-extract`: Do not ask the LLM to extract authors from front matter.
- `--no-cover-vision`: Do not ask a vision-capable LLM to choose internal covers.
- `--llm-describe-miss`: Let the LLM write cautious descriptions when metadata sources miss.
- `--report metadata-report.json`: Write a metadata report.

## Development Notes

The repository intentionally ignores sample EPUBs, generated split outputs, caches, reports, and plugin build artifacts. Bring your own test EPUBs under `samples/` when developing locally.

Run a quick plugin build check:

```bash
python3 build_calibre_plugin.py
unzip -l dist/AutoEpubSplitter.zip
```

On macOS with Calibre installed in `/Applications`, you can run plugin import checks with:

```bash
/Applications/calibre.app/Contents/MacOS/calibre-debug -c "print('calibre ok')"
```
