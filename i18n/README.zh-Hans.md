[English](../README.md) · [العربية](README.ar.md) · [Español](README.es.md) · [Français](README.fr.md) · [日本語](README.ja.md) · [한국어](README.ko.md) · [Tiếng Việt](README.vi.md) · [中文 (简体)](README.zh-Hans.md) · [中文（繁體）](README.zh-Hant.md) · [Deutsch](README.de.md) · [Русский](README.ru.md)

[![LazyingArt banner — Local Knowledge Terminal](../docs/assets/banner.svg)](../docs/assets/banner.svg)

# Local Knowledge Terminal

**在自己的硬件上运行、以私有藏书为依据的智能系统。**

[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![Model](https://img.shields.io/badge/Qwen3-4B%20%2F%208B-6f42c1?style=flat-square)](https://huggingface.co/Qwen/Qwen3-8B-GGUF)
[![Runtime](https://img.shields.io/badge/llama.cpp-v0.3.0-173f35?style=flat-square)](https://github.com/ggml-org/llama.cpp)
[![Target](https://img.shields.io/badge/Raspberry%20Pi%205-8GB-C51A4A?style=flat-square&logo=raspberrypi)](https://www.raspberrypi.com/products/raspberry-pi-5/)
[![Sponsor](https://img.shields.io/badge/Sponsor-lachlanchen-EA4AAA?style=flat-square&logo=githubsponsors&logoColor=white)](https://github.com/sponsors/lachlanchen)

Local Knowledge Terminal（LKT）把一套私有藏书转化为带引文的多语言卡片。首个资料库整合了 **Word Origins**、**The Book of Answers**、**The Book of Questions**、一本**英语词根词典**和一本**英语词缀词典**的结构化版本。Qwen3-4B Q4_K_M 可在 8 GB Raspberry Pi 5 上本地运行，也可选用速度较慢的 Qwen3-8B 配置；检索、推理、历史记录和浏览器界面都不依赖云端 API。

## 用一套藏书试用

如果你已经拥有范围明确的私有书籍或词典藏书，可以先申请免费的适配检查，再开始[创始期 250 美元藏书适配冲刺](https://lazying.art/lkt/)。服务面向一套藏书、一个语言目标和一台现有机器，交付内容包括数据／隐私／引文地图、一个经商定且最多包含 12 个来源单元和 20 个测试问题的样本、在材料可用时最多两张带引文的浏览器卡片、一项继续／停止建议，以及一次事实性修正。付款前，书面范围会明确“来源单元”的含义，例如一段文字、一条记录或一张代表性页面。
硬件、运输、定制 OCR、批量转换、生产部署和持续支持不在这一固定范围内。

如果想在不分享任何客户材料的前提下准确了解这三项交付物，请阅读[藏书适配报告样例](../docs/sample-fit-report.md)。它把同一格式应用于 LKT 自己有文档记录的参考藏书，明确不代表客户成果或付费项目成绩。

## 六种独立体验，一套卡片契约

- **Word Origin** 使用专属的单词条检索器和提示词，生成边界明确、可交互的有向词源图。系统保留分支词素，并清楚区分有书籍依据的节点与模型补充的语言学背景。
- **Word Card** 检索多条相关的 Word Origins 词条，并组合成紧凑的多语言记忆视图。英语、日语和中文固定显示，法语与阿拉伯语在第四个面板轮换。
- **Book Answer** 从 318 张已审阅卡片中进行可复现抽取，保留出版物中的答案译文，并补充一段反思文字。
- **Book Question** 按主题检索 291 个已审阅问题；没有词汇匹配时，退回到可复现抽取。
- **Root Graph** 优先处理 4,018 条含实质内容的词根记录，再查找完全匹配的辅助词缀条目，并保存递归词族图。
- **Affix Graph** 对 5,179 条含实质内容的词缀记录和 Root Dictionary 采用相反的优先顺序，同时保留一张完整的中心词图。

每种模式都有自己的检索策略和严格的模型提示。Word Origin 与 Word Card 有意共用同一个 Word Origins 索引，但呈现方式不同；Answer 与 Question 使用不同的书籍和检索引擎。六种模式都输出同一套带版本的卡片 JSON。日语卡片正文保留词元级振假名，中文视图则获得确定性生成、完整标调的拼音。当前网页界面负责渲染这些 JSON；未来电子墨水屏和音频适配器也会读取它，而无须修改语料库、检索或模型代码。

独立的 **Chat / Benchmark** 工作区直接与 Qwen 对话，并报告实际耗时、提示／输出 token 数和生成速度。它被醒目标为未经引证的原始模型输出，绝不会存为有书籍依据的卡片。观察结果保存在本地知识账本的独立表中。即使提示重复，每次仍会重新运行 Qwen；账本是历史，不是缓存。在任意卡片上选择 **Discuss this card**，即可在 Model Lab 中打开该卡片，并把已经保存的卡片及检索片段作为边界明确的上下文。
每次实时 Model Lab 会话还会获得一个可持久保存的探究线程。连续轮次保留父子沿袭关系；卡片讨论会链接到其规范化来源内容原子，而 Qwen 的回答始终明确标记为无引文。

## 产品展示

浏览器不是聊天仪表盘，而是编辑式卡片舞台。每张可见幻灯片都是无需滚动的单屏构图，以一个醒目的核心概念配合一条简洁来源引文。Word Origin 把中央区域留给 Cytoscape.js 有向图。Word Card 在固定的日语／中文面板上方突出显示英语单词和 IPA，第四个面板轮换法语／阿拉伯语。Answer 和 Question 使用内层语言轮播——英语、带注音的日语和带拼音注音的中文——并把特别长的句子拆成额外的易读幻灯片。经接受的本地语法分析会在完全相同的文字上加入低调的语法角色配色，不添加图例或拥挤的元数据。保存的卡片按模式形成彼此独立的外层轮播，并配有上一张／下一张控件。
Root、Affix 和 Word Origin 共用一个 Cytoscape 图形渲染器：完整的已保存图、角落概览图，以及聚焦于词根、前缀、后缀或历史分支且不重复整张图的内层幻灯片。
全屏展示模式会隐藏应用界面，而 `/?display=1` 会把同一份卡片文档作为适合信息屏的画面打开。打印 CSS 与带版本的卡片 JSON 为后续电子墨水渲染提供清晰边界。

### Raspberry Pi 实机展示

Word Origin 使用根据内容调整尺寸的节点、完整的词源图、多语言释义面板、分支幻灯片和一键最佳适配复位。

![Raspberry Pi 上的实时 Word Origin 图](../docs/assets/word-origin.png)

Word Card 让英语单词和发音占据视觉重点，同时并排展示醒目而稳定的日语与中文面板，以及轮换的法语／阿拉伯语面板。

![Raspberry Pi 上的实时多语言 Word Card](../docs/assets/word-card.png)

每张生成的卡片都会获得新 ID，并保留在卡片账本中。另一个规范化的 `knowledge.sqlite3` 数据库把经接受的术语、义项、发音、音素／字素片段、词素、历史、翻译、语法、出处、修订和探究沿袭保存为可复用原子。卡片是由这些原子重建的视图。LadybugDB 属性图是派生的遍历投影，始终可以从 SQLite 重建。
经接受的 Book Answer 和 Book Question 卡片也会把经过审阅的英语、日语和中文原文准确存入这个规范化存储。每种语言都是独立内容原子，并链接到检索系统所有的书籍引文；模型反思内容被有意排除在书籍证据之外。Qwen 在彼此独立且边界明确的任务中切分各个语言。只有当有序片段能够逐字重建已审阅句子时，结果才会被接受；经接受的片段、证据链接、模型修订和被取代的分析都保留为可复用知识，而不只是展示标记。

### 段落到出处的证明

[PocketPolyglot 段落示例](../examples/artifacts/pocketpolyglot-passage-graph.json)把一段项目自创的对齐文本转换为一张经过人工复核的小型概念图。每条关系都通过 LKT 的生产知识 API，解析到准确的段落单元、摘录和固定的源文件哈希。可以重新构建它，或验证提交的制品是否仍为最新：

```bash
python examples/pocketpolyglot_passage_graph.py
python examples/pocketpolyglot_passage_graph.py --check
```

### 脚本化双语会议证明

[双语会议示例](../examples/artifacts/scripted-bilingual-meeting-knowledge.json)把十条分别带时间戳的英语和普通话发言映射为十个带类型、经人工审阅的知识单元。每个单元保留发言人、时间戳、准确的转录字符范围、源文件哈希和有证据支持的图关系。其审阅账本包含一次修正，并通过真实的 `KnowledgeStore` 制品生命周期把早期版本保留为已取代版本。
同一制品还配有[交互式浏览器证明](https://lazying.art/meeting-intelligence/)，可以从一个单元追溯到准确的来源文字。

转录文本和时间信息均为项目自有的脚本化测试素材。这不是 ASR、说话人分离、信息提取或翻译准确率基准，也不是客户部署或客户成果。可以重新构建或验证可移植 JSON：

```bash
python examples/scripted_bilingual_meeting_knowledge.py
python examples/scripted_bilingual_meeting_knowledge.py --check
```

准备过程采用小型且感知依赖关系的任务：检索证据、准备一个义项、拆分组成部分、递归扩展每条词源分支、分别准备各语言／发音、验证，最后组合。成功阶段会立即建立检查点；某一种语言或某一分支较弱时可以单独重试，而不会丢弃其余结果。

已安装的低优先级工作进程以均衡轮次扩充全部六个可见卡组。Question 与 Answer 从各自审阅过的书籍中抽取；Word Card 与 Word Origin 共用一次边界明确的原子化单词调查；Root 与 Affix 则分别从各自润色后的词典中独立抽取，并把另一部词法书以及有界的 Word Origins 匹配作为相关辅助 RAG。它始终选择可见卡片数量最少的模式，因此没有任何快速路径会远远领先于其他模式。
追赶期间，系统暂停新的 Question／Answer 抽取，并且最多只允许一个尚未完成的自主词汇主题处于处理中。即使可选增强任务仍在排队，均衡检查也会按有限间隔运行；词汇任务会在书籍语法增强任务之前领取。

每个从未见过的来源仍会通过正常的本地 Qwen、RAG 和发布门禁。稳定的来源与术语标识可避免重启后重复。若一个单词确实包含某个词根或词缀，原子化单词分析可以派生 Root／Affix 视图；与此同时，Root／Affix 书籍的独立遍历可以保证，即使选中的单词没有能产的词缀，这些产品也会继续增长。系统绝不会为了平衡标签页而杜撰组成部分。

Root／Affix 的准备把昂贵工作拆成两次可恢复的本地调用：先处理图／历史，再生成小型多语言展示。图的 token 上限为 1,200（一次全新修复时为 1,400），语言调用使用 512 token（修复时为 640）。被截断的 JSON 回应绝不会递归地重新输入 Qwen。每个通过验证的阶段都会连同模型和准确的证据指纹一起保存，因此后续阶段失败不会浪费已经完成的图。

裸浏览器从 Question 开始，并在每张卡片播放完所有内层幻灯片后，依次继续 Answer → Word Card → Word Origin → Root → Affix。显示设置可以选择任意单一模式或任意子集，同时保留这套规范顺序；默认选择全部六种。默认情况下，保存的卡片会在各个已选模式内部打乱，也可选择稳定的从新到旧顺序。每种模式拥有独立的、只包含已接受卡片的随机遍历，因此切换标签不会合并它们的集合，也不会在每次访问时重复同一张卡。新接受的卡片会被放到该模式剩余遍历的首位。明确选择标签或使用 `?mode=` URL 时始终停留在该模式；指针、触摸或键盘活动会重新开始当前卡片的完整停留时间，之后环境动画才会恢复。

这一所有权边界是有意设计的：经审阅的书籍句子、译文和引文来自本地语料记录，模型绝不会改写它们；新的解释性或词汇数据由配置好的本地模型生成，而不是人工录入 SQLite。糟糕的草稿不会进入可见卡组。词典候选和确定性发音／注音同样是本地检索／工具输出，而非手工编写的卡片数据。当 OMW 没有选定义项的阿拉伯语词目时，FreeDict 提供精确的英阿纠错门禁；Qwen 必须复制一个检索到的候选项，系统在验证后附上该候选项的证据 ID。固定版本的完整 JMdict 在本地检查日语写法和读音：完全匹配的读音不耗费模型调用，唯一修正以确定性方式完成，只有真正存在歧义的书写形式才会获得一次小型 Qwen 选择，且选择范围限制在检索出的读音内。`/api/health` 把两个紧凑的纠错索引都视为必需来源，并报告其就绪状态、版本、哈希与条目数量。
当 Raspberry Pi 当前存在欠压、降频或高温情况时，自主生成会暂停，并在状况消失后恢复。网页客户端载入完整的已选模式（最多 1,000 张已接受卡片），保留最新卡片在首位，并在每轮轮播中将其他卡片各打乱一次。它轮询已接受卡片而不中断当前展示，并把新发布结果插入下一位。紧凑状态信息与 `/api/health` 都会报告有限书籍覆盖范围以及词汇计划／接受进度，但不会因此调度工作。交互式单词请求会立即处理，并复用与自主准备相同的持久原子。

已获取知识的页脚最多显示由 18 个卡片圆点组成的移动窗口；箭头导航和准确的 `当前 / 总数` 计数器仍可覆盖完整且无限增长的已保存卡组。Question／Answer 的语言圆点只属于当前卡片，切换卡片时会一并替换。

```text
 Word Origin ──► best Word Origins entry ─────┐
   Word Card ──► multi-entry Word Origins ────┤
 Book Answer ──► reproducible answer draw ────┼──► independent prompts
Book Question ─► question search / draw ──────┘              │
                                                              ▼
                                                  Qwen3-8B / 4B on llama.cpp
                                                       │
                                      ┌────────────────┴───────────────┐
                                      ▼                                ▼
                              versioned card JSON            deterministic citations
                                      │
                            ┌─────────┼─────────┐
                            ▼         ▼         ▼
                          Web GUI   E-ink     Audio
                          (ready)  (adapter)  (adapter)
```

## 依据规则

语言模型负责撰写解释和缺失的语言辅助内容，但绝不撰写引文列表。LKT 直接从检索记录附加条目 ID、摘录、章节、页码、数字定位信息和审阅过的卡片书籍译文。Word Origin 可以补充可靠的语言学背景，但图中每个节点都会记录它来自书籍锚点还是模型知识。如果配置的书籍没有证据，应用就不会生成卡片。

## 仓库地图

| 路径 | 职责 |
| --- | --- |
| `lkt/corpus.py` | Word Origins 导入、原子化 SQLite 索引、精确检索与 FTS 检索 |
| `lkt/morphology.py` | Root／Affix 润色版 JSONL 导入、出处记录、精确检索与 FTS 检索 |
| `lkt/card_books.py` | 多语言 Answer／Question 导入、搜索和确定性抽取 |
| `lkt/deck.py` | 书籍与词汇交替逐项准备 |
| `lkt/device.py` | 后台推理使用的 Pi 电源／温度就绪门禁 |
| `lkt/retrieval.py` | 独立的 Word Origin、Word Card、Answer 与 Question RAG 策略 |
| `lkt/llm.py` | 精简的 llama.cpp 适配器，以及每种体验各自的一条严格提示 |
| `lkt/service.py` | 卡片组合与规范化 |
| `lkt/pronunciation.py` | 确定性拼音／注音及带版本的离线 IPA |
| `lkt/store.py` | 带版本卡片、准备制品、修订、归档和聊天账本 |
| `lkt/knowledge.py` | 原子化已确立知识、证据、任务、修订和探究沿袭 |
| `lkt/preparation.py` | 感知依赖关系的分治式单词／内容规划 |
| `lkt/atomic.py` | 有界原子准备与确定性卡片组装 |
| `lkt/graph.py` | 从已接受 SQLite 原子重建的 LadybugDB 遍历投影 |
| `lkt/lexicon.py` | 紧凑的多语言 WordNet 纠错证据 |
| `lkt/freedict.py` | 精确 FreeDict 英阿导入与纠错检索 |
| `lkt/jmdict.py` | 完整 JMdict 精确形式读音索引与出处记录 |
| `lkt/web.py` | 无依赖的 HTTP API 与 GUI 服务器 |
| `lkt/outputs.py` | 稳定的网页／电子墨水／音频输出边界 |
| `lkt/static/` | 桌面级 GUI，并具备足够的响应式能力供后续信息屏使用 |
| `scripts/` | 可复现的 Pi 运行时、安装、更新和冒烟测试工具 |
| `systemd/` | 加固后的模型与应用服务 |
| `docs/lineage.md` | 精确的旧项目与语料出处 |
| `docs/product-brief.md` | 持久的所有者需求与验收标准 |
| `docs/knowledge-architecture.md` | 原子 SQLite、图投影和分阶段准备契约 |
| `docs/owner-request-log.md` | 按时间排列、隐私信息已删减的所有者指令 |
| `docs/voice-hardware.md` | 支持的麦克风选择和分阶段音频测试 |
| `docs/mode-roadmap.md` | 未来后缀、词缀和词根书籍的扩展计划 |

## 本地开发

安装精简且固定版本的发音依赖，然后运行测试套件：

```powershell
cd C:\Users\Administrator\Projects\LocalKnowledge
python -m pip install -e .
python -m unittest discover -s tests -v
python -m compileall -q lkt tests
```

从结构化书籍导出文件构建本地索引：

```powershell
$env:LKT_DATA_DIR="$PWD\var"
python -m lkt.cli ingest "C:\path\to\word-origins-pdf2tex\json\entries.jsonl"
python -m lkt.cli ingest-card-book answer "C:\path\to\book-of-answers\json\multilingual-items.jsonl"
python -m lkt.cli ingest-card-book question "C:\path\to\book-of-questions\json\multilingual-items.jsonl"
python -m lkt.cli ingest-morphology root "C:\path\to\root-dictionary\output\json\entries-editorial.jsonl"
python -m lkt.cli ingest-morphology affix "C:\path\to\affix-dictionary\output\json\entries-editorial.jsonl"
python -m lkt.cli ingest-freedict "C:\path\to\eng-ara.tei"
python -m lkt.cli ingest-jmdict "C:\path\to\jmdict-eng-3.6.2.json" --release "3.6.2+20260824122934"
python -m lkt.cli audit-japanese-readings
python -m lkt.cli search abacus
python -m lkt.cli search technology --corpus question
python -m lkt.cli knowledge-status
python -m lkt.cli sync-card-knowledge
python -m lkt.cli plan-word inspection --display-languages en ja zh fr ar
python -m lkt.cli plan-translation inspection ar --prompt-version atomic-v2
python -m lkt.cli work-atomic --limit 1
python -m lkt.cli seed-deck --modes answer question
python -m lkt.cli seed-lexical --seed first-pass
```

当 llama.cpp 服务器监听 8081 端口时：

```powershell
python -m lkt.cli generate abacus --mode word
python -m lkt.cli generate "Should I begin?" --mode answer
python -m lkt.cli generate technology --mode question
python -m lkt.cli generate inspection --mode root
python -m lkt.cli generate abnormal --mode affix
python -m lkt.cli serve
```

打开 <http://127.0.0.1:8090>。

## Raspberry Pi 5 目录布局

```text
/home/lachlan/LocalKnowledgeTerminal/
├── source/      # this Git repository; updated with fetch + fast-forward pull
├── runtime/     # pinned llama.cpp plus optional knowledge Python environment
├── models/      # Qwen GGUF (not committed)
├── data/        # corpus index and saved cards (not committed)
└── logs/        # bootstrap logs (not committed)
```

固定版本的运行时制品：

| 制品 | 版本 | 完整性 |
| --- | --- | --- |
| llama.cpp | `v0.3.0` / `c1d0e7a004015f23bc0233470b747b596f29b264` | 以提交固定版本的源代码归档 |
| Qwen3-4B-GGUF | `bc640142c66e1fdd12af0bd68f40445458f3869b` | Q4_K_M SHA-256 `7485fe6f…534fdf5` |
| 模型文件 | `Qwen3-4B-Q4_K_M.gguf` | 2,497,280,256 字节 |

Pi 服务只开放一个推理槽位（`--parallel 1`）。因此卡片组合与 Model Lab 请求会顺序处理，使内存占用和延迟保持可预测，而不是让四个 CPU 核心在多个任务间争抢资源。

Qwen3-8B 已被证明可以作为可选的质量优先准备模型使用。在部署的 Pi 上，它以每秒 1.78 token 生成了一个 120-token 多语言探针，RSS 约为 6.28 GiB，系统仍有 1.85 GiB 内存可用，并且当时没有温度降频。Qwen3-4B 是响应更快的离线默认模型。模型选择明确且可逆：

```bash
tmux new-session -d -s lkt-8b-download \
  './scripts/download_qwen3_8b.sh > ../logs/qwen3-8b-download.log 2>&1'
sudo ./scripts/select_model.sh status
sudo ./scripts/select_model.sh 8b
sudo ./scripts/select_model.sh 4b
sudo ./scripts/benchmark_models.sh
```

任何时候只加载一个模型。默认 4B 配置使用 3,072-token 上下文；可选 8B 配置使用 2,048-token 上下文和更小的批次，以保护 8 GB 内存边界。如果其服务器未能进入健康状态，`select_model.sh 8b` 会自动恢复 4B 配置。
下载器可以续传部分文件，校验官方 SHA-256，之后才以原子方式公开最终 GGUF。
基准测试每次只激活一个模型，对各模型运行相同且有界的多语言质量／速度探针，记录实际耗时、llama.cpp token 速率和进程内存，最后恢复测试前启用的模型。

安装紧凑的可选知识运行时并构建图投影：

```bash
./scripts/install_knowledge_runtime.sh
./scripts/rebuild_graph.sh
```

这会安装用于本地 IPA 的 eSpeak NG，把 LadybugDB 0.19.1 和 Wn 1.1.1 固定安装在隔离环境中，并只安装 OMW 2.0 的英语、日语、普通话中文、法语和阿拉伯语词库。它还会验证固定版本的完整 JMdict 归档，构建精确形式读音索引，然后删除原始下载文件。有意不包含完整的 Wiktionary 转储。IPA 提取使用静默文本模式，不启用语音输出。

在 Pi 上：

```bash
./scripts/bootstrap_runtime.sh
sudo ./scripts/install_pi.sh \
  /path/to/entries.jsonl \
  /path/to/answers/multilingual-items.jsonl \
  /path/to/questions/multilingual-items.jsonl \
  /path/to/root/entries-editorial.jsonl \
  /path/to/affix/entries-editorial.jsonl
./scripts/smoke_test.sh
```

日后进行 Windows → GitHub → Pi 开发时：

```bash
cd /home/lachlan/LocalKnowledgeTerminal/source
./scripts/update_pi_tmux.sh
tmux attach -t lkt-update
```

tmux 封装器让部署在 SSH 或浏览器切换期间持续运行，并写入 `~/LocalKnowledgeTerminal/logs/update-pi.log`。底层幂等脚本 `scripts/install_services.sh` 会安装全部三个 systemd 单元，将其设为开机启动，按模型 → 网页 → 工作进程的顺序启动，验证两个健康端点，并安装图形界面自动启动项。`scripts/update_pi.sh` 在用 `--restart` 调用该服务安装器之前，会先运行完整测试门禁。

然后在 Pi 的 VNC 桌面中打开 `http://127.0.0.1:8090`，或从受信任的局域网打开 `http://<pi-lan-address>:8090`。

安装器还会把 `desktop/lkt-kiosk.desktop` 放入 Pi 用户的 XDG 自动启动目录，并把 `scripts/open_kiosk.sh` 安装为 `/usr/local/bin/lkt-open-kiosk`。下次图形登录时，启动器会等待本地健康端点，然后仅使用一个专用 Chromium 配置文件打开 `http://127.0.0.1:8090/?display`。重复运行启动器不会造成问题：它会识别该配置文件，不会另开窗口。Chromium 以普通全屏应用而不是锁定式信息屏启动，因此按 **Esc** 即可退出全屏并返回可控的 Pi 桌面。明确的模式 URL 仍可供有意的 VNC 操作使用。

## 数据与版权

书籍 PDF、提取后的语料、模型权重、生成的索引和已保存卡片都被有意排除在 Git 之外。安装时请提供一份合法取得的本地 JSONL 导出文件。LKT 在 SQLite 索引中记录每个 SHA-256，因此生成的卡片可追溯到准确的语料构建版本。已验证的参考资料集请见 [`docs/corpora.md`](../docs/corpora.md)。

## 沿袭关系

LKT 是一个全新实现的本地优先后继项目，其设计参考了 [`WordsCardEink`](https://github.com/lachlanchen/WordsCardEink) 和 [`WordOrigins`](https://github.com/lachlanchen/WordOrigins)。它不导入这些项目的单体运行时或硬件依赖。固定的提交版本和保留的设计思想请见 [`docs/lineage.md`](../docs/lineage.md)。

## 支持

| Donate | PayPal | Stripe |
| --- | --- | --- |
| [![Donate](https://img.shields.io/badge/Donate-LazyingArt-0EA5E9?style=for-the-badge&logo=ko-fi&logoColor=white)](https://chat.lazying.art/donate) | [![PayPal](https://img.shields.io/badge/PayPal-RongzhouChen-00457C?style=for-the-badge&logo=paypal&logoColor=white)](https://paypal.me/RongzhouChen) | [![Stripe](https://img.shields.io/badge/Stripe-Donate-635BFF?style=for-the-badge&logo=stripe&logoColor=white)](https://buy.stripe.com/aFadR8gIaflgfQV6T4fw400) |

## 引用

如果 LKT 对你的工作有所帮助，请使用 GitHub 的 **Cite this repository** 菜单引用；该菜单读取 [`CITATION.cff`](../CITATION.cff)。也可以使用：

```bibtex
@software{chen_local_knowledge_terminal_2026,
  author = {Chen, Lachlan},
  title = {Local Knowledge Terminal},
  year = {2026},
  version = {0.1.0},
  url = {https://github.com/lachlanchen/LocalKnowledgeTerminal}
}
```
