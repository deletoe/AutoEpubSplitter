# AutoEpubSplitter

[English](README.md) | 简体中文

AutoEpubSplitter 是一个 Calibre 插件和命令行工具集，用来把 EPUB 合集自动拆分成单册图书，并为拆分后的 EPUB 补充元数据。

它主要面向真实世界里格式并不规范的 EPUB 合集：目录层级、HTML 排版、标题格式、封面位置都可能很混乱。拆分断点可以交给本地 OpenAI-compatible LLM 判断，实际 EPUB 资源重写则复用并内置了 GPLv3 的 EpubSplit 插件代码。

## 功能

- 将 EPUB 合集拆分成单册 EPUB。
- 遇到连续作品的多卷/上中下册时，可以按阅读直觉保留为一个整体。
- 使用本地 OpenAI-compatible LLM 判断拆分点、抽取作者、选择内置封面、清洗元数据。
- LLM 不可用时有保守启发式回退。
- 从豆瓣读书补充元数据，也可选用 Google Books。
- 优先保留 EPUB 内置封面；只有内部没有封面时才尝试使用元数据源封面。
- Calibre 插件带进度和日志窗口，适合长时间任务。
- 同时提供命令行脚本，方便批处理和调试。

## 许可证

本项目使用 GPLv3 发布。见 [LICENSE](LICENSE)。

AutoEpubSplitter 内置并改编了 [EpubSplit](https://github.com/JimmXinu/EpubSplit) 的部分代码。EpubSplit 由 Jim Miller 开发，同样使用 GPLv3。见 [NOTICE](NOTICE) 和 `calibre_plugin/epubsplit_LICENSE.txt`。

豆瓣读书、Google Books 等元数据来源是外部服务。用户需要自行遵守相关服务条款。本工具默认采用较保守的请求间隔和本地缓存。

## 获取源码

```bash
git clone https://github.com/deletoe/AutoEpubSplitter.git
cd AutoEpubSplitter
git submodule update --init --recursive
```

如果要使用命令行脚本，请安装 Python 依赖：

```bash
python3 -m pip install -r requirements.txt
```

## 构建 Calibre 插件

```bash
python3 build_calibre_plugin.py
```

插件 zip 会生成在：

```text
dist/AutoEpubSplitter.zip
```

## 在 Calibre 中安装

1. 打开 Calibre。
2. 进入 `首选项` -> `插件`。
3. 点击 `从文件加载插件`。
4. 选择 `dist/AutoEpubSplitter.zip`。
5. 确认 Calibre 的第三方插件警告。
6. 重启 Calibre。
7. 在书库里选中一本带 EPUB 格式的合集书。
8. 点击工具栏里的 `Auto EPUB Splitter`。

插件会先识别拆分点并弹出确认框。确认后，它会写出拆分 EPUB、补充元数据，并把新书加入当前 Calibre 书库。

建议先在临时 Calibre 书库里测试，再对正式书库大量使用。

## 插件配置

在 Calibre 中进入：

`首选项` -> `插件` -> `AutoEpubSplitter` -> `自定义插件`

可配置内容包括：

- 是否启用本地 LLM。
- OpenAI-compatible base URL。
- 模型名。留空则使用 `/v1/models` 返回的第一个模型。
- 拆分判断 LLM timeout。
- 元数据清洗 LLM timeout。
- 是否启用封面视觉识别。
- 封面视觉 timeout。
- 是否从书籍前几页抽取作者。
- 元数据源 miss 时是否允许 LLM 谨慎补简介。
- 是否启用豆瓣读书元数据源。
- 是否启用 Google Books 元数据源。
- 可选 Google Books API key。
- 元数据请求间隔。
- 每本书最多候选数量。
- HTTP 缓存目录。

默认 LLM 地址是开发期间使用的本地/内网示例。公开使用时请改成你自己的 OpenAI-compatible endpoint，例如本地 vLLM 服务。也可以关闭 LLM，只使用启发式拆分和元数据源评分。

## 命令行拆分

只预览拆分点：

```bash
python3 auto_split_epub.py --dry-run "samples/example-collection.epub"
```

跳过 LLM，直接用目录启发式：

```bash
python3 auto_split_epub.py --no-llm --dry-run "samples/example-collection.epub"
```

执行拆分：

```bash
python3 auto_split_epub.py --overwrite -o split-output "samples/example-collection.epub"
```

常用参数：

- `--vllm-base-url`: OpenAI-compatible endpoint。默认读取 `VLLM_BASE_URL`，否则使用内置开发默认值。
- `--model`: 模型名。不传时自动读取 `/v1/models` 的第一个模型。
- `--llm-timeout`: 等待 LLM 的秒数。
- `--expected-count`: 可选预计输出数量提示。它只是提示，不会强行裁剪或补足结果。
- `--report report.json`: 保存拆分识别报告。

## 命令行元数据补全

预览单本，不写文件：

```bash
python3 enrich_epub_metadata.py --dry-run split-output/01-example.epub
```

批量写到新目录：

```bash
python3 enrich_epub_metadata.py -o metadata-output split-output
```

原地替换：

```bash
python3 enrich_epub_metadata.py --inplace split-output
```

同时使用豆瓣和 Google Books：

```bash
python3 enrich_epub_metadata.py \
  --metadata-source douban \
  --metadata-source google_books \
  split-output
```

常用参数：

- `--delay 5`: 放慢未缓存的元数据/封面请求。
- `--cache-dir .cache/douban`: HTTP 缓存目录。
- `--max-candidates 5`: 每本书最多检查的候选数量。
- `--metadata-source douban`: 查询豆瓣读书，可重复传。
- `--metadata-source google_books`: 查询 Google Books，可重复传。
- `--google-books-api-key`: 可选 Google Books API key。
- `--no-llm`: 不调用 LLM。
- `--no-author-extract`: 不让 LLM 从正文前几页抽取作者。
- `--no-cover-vision`: 不让视觉模型判断内置封面。
- `--llm-describe-miss`: 元数据源 miss 时允许 LLM 谨慎补简介。
- `--report metadata-report.json`: 保存元数据报告。

## 开发说明

仓库默认忽略样本 EPUB、拆分输出、缓存、报告和插件构建产物。开发时可以把自己的测试 EPUB 放到 `samples/`。

快速构建插件：

```bash
python3 build_calibre_plugin.py
unzip -l dist/AutoEpubSplitter.zip
```

macOS 下如果 Calibre 安装在 `/Applications`，可以用下面的命令做简单导入检查：

```bash
/Applications/calibre.app/Contents/MacOS/calibre-debug -c "print('calibre ok')"
```
