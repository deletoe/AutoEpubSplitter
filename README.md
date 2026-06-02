# AutoEpubSplitter

第一步目标：把 EPUB 合集自动拆成单册 EPUB，并给输出文件写入基础标题元数据。

脚本复用 `EpubSplit/epubsplit.py` 负责实际 EPUB 拆分和资源重写，`auto_split_epub.py`
负责识别每本书的起始断点：

1. 默认会尝试调用 OpenAI-compatible vLLM：`http://10.130.92.107:8000`。
2. vLLM 不可用、超时或返回无效 JSON 时，自动回退到 EPUB 目录树/标题启发式。
3. 支持 `--dry-run` 先查看识别出的书名和 line 范围。

## 安装依赖

```bash
python3 -m pip install -r requirements.txt
```

## 只预览拆分点

```bash
python3 auto_split_epub.py --dry-run "samples/博尔赫斯全集第二辑（套装共12册） (【阿根廷】豪尔赫·路易斯·博尔赫斯) (z-library.sk, 1lib.sk, z-lib.sk).epub"
```

如果想跳过大模型，直接用目录启发式：

```bash
python3 auto_split_epub.py --no-llm --dry-run "samples/三岛由纪夫作品集 (套装共15册) (三岛由纪夫 (Mishima Yukio)) (z-library.sk, 1lib.sk, z-lib.sk).epub"
```

## 执行拆分

```bash
python3 auto_split_epub.py --no-llm --overwrite -o split-output "samples/博尔赫斯全集第二辑（套装共12册） (【阿根廷】豪尔赫·路易斯·博尔赫斯) (z-library.sk, 1lib.sk, z-lib.sk).epub"
```

常用参数：

- `--vllm-base-url`: 指定 vLLM 地址，默认读取 `VLLM_BASE_URL`，否则使用 `http://10.130.92.107:8000`
- `--model`: 指定模型名；不传时自动读取 `/v1/models` 的第一个模型
- `--llm-timeout`: 等待 vLLM 的秒数，默认 120
- `--expected-count`: 可选的预计输出数量提示；不传就不提供数量信息，也不会从文件名猜测。传入后只作为检测提示，不会强行裁剪结果
- `--report report.json`: 保存识别报告

默认 prompt 会把目录树和带标题的候选断点交给 LLM，并明确说明：通常拆到单册出版物；遇到父级系列/主题分组时选择子级真实书名；遇到连续作品的上下册、多卷本时可保留为一个整体 EPUB。

## 搜索并写入元数据

`enrich_epub_metadata.py` 会先读取 EPUB 现有标题/作者，再温和地查询豆瓣读书：

1. 使用豆瓣 suggest 和搜索页拿候选。
2. 低频请求详情页，默认每个未缓存请求间隔 3 秒。
3. 用本地 vLLM 选择候选并清洗标题/作者；失败时使用保守评分 fallback。
4. 写入标题、主要作者、简介、封面、ISBN/豆瓣 ID、出版社、日期、标签、评分等。

封面会优先使用 EPUB 内部资源：

- 先读取 OPF 已声明的 cover。
- 如果 OPF 没声明，会检查前几个排版页，寻找 `Cover`/`封面` 页面里的大幅竖图。
- 默认会把前几张高分候选图和书名/作者发给支持视觉的 vLLM 判断；模型不可用或不支持视觉时自动回退到规则判断。
- 只有 EPUB 内部找不到封面时，才会尝试使用豆瓣封面。

作者线索会优先从更可靠的位置来：

- 如果 OPF 里只有一个非泛化作者，会自动视为可信作者，后续搜索和写入都优先使用这个作者。
- 多作者合集默认会读取每本拆分 EPUB 的前几页文本，让 LLM 抽取主要作者/编者；抽不到就留空，不再强行沿用合集级作者。
- 从正文抽出的作者会用于 `标题 + 作者` 搜索，并且会过滤掉作者明显不匹配的豆瓣候选。

先预览，不写文件：

```bash
python3 enrich_epub_metadata.py --dry-run split-output/01-寻路中国.epub
```

批量写到新目录：

```bash
python3 enrich_epub_metadata.py -o metadata-output split-output
```

原地替换前建议先 dry-run 或写到新目录确认：

```bash
python3 enrich_epub_metadata.py --inplace split-output
```

常用参数：

- `--delay 5`: 放慢豆瓣/封面未缓存请求
- `--cache-dir .cache/douban`: HTTP 缓存目录
- `--max-candidates 5`: 每本书最多抓取详情的候选数量
- `--no-llm`: 不调用 LLM，只用豆瓣候选评分和解析结果
- `--no-author-extract`: 不从正文前几页抽取作者
- `--author-front-files 8`: 抽取作者时读取前几个 HTML 文件
- `--author-front-chars 6000`: 抽取作者时最多发送给 LLM 的文本长度
- `--no-cover-vision`: 不调用视觉模型确认内置封面，直接使用规则识别
- `--cover-vision-timeout 45`: 每次封面视觉确认的等待秒数
- `--llm-describe-miss`: 豆瓣 miss 时允许 LLM 谨慎补一条简介；默认 miss 不写简介，避免把未验证信息写得像真元数据
- `--work-hard`: 预留给后续更积极的封面/网络搜索；当前不会额外爬取
- `--report metadata-report.json`: 保存每本书的匹配和写入结果
