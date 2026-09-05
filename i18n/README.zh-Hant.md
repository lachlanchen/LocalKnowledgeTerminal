[English](../README.md) · [العربية](README.ar.md) · [Español](README.es.md) · [Français](README.fr.md) · [日本語](README.ja.md) · [한국어](README.ko.md) · [Tiếng Việt](README.vi.md) · [中文 (简体)](README.zh-Hans.md) · [中文（繁體）](README.zh-Hant.md) · [Deutsch](README.de.md) · [Русский](README.ru.md)

[![LazyingArt banner — Local Knowledge Terminal](../docs/assets/banner.svg)](../docs/assets/banner.svg)

# Local Knowledge Terminal

**在自己的硬體上運行、以私人藏書為依據的智慧系統。**

[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![Model](https://img.shields.io/badge/Qwen3-4B%20%2F%208B-6f42c1?style=flat-square)](https://huggingface.co/Qwen/Qwen3-8B-GGUF)
[![Runtime](https://img.shields.io/badge/llama.cpp-v0.3.0-173f35?style=flat-square)](https://github.com/ggml-org/llama.cpp)
[![Target](https://img.shields.io/badge/Raspberry%20Pi%205-8GB-C51A4A?style=flat-square&logo=raspberrypi)](https://www.raspberrypi.com/products/raspberry-pi-5/)
[![Sponsor](https://img.shields.io/badge/Sponsor-lachlanchen-EA4AAA?style=flat-square&logo=githubsponsors&logoColor=white)](https://github.com/sponsors/lachlanchen)

Local Knowledge Terminal（LKT）把一套私人藏書轉化為附有引文的多語言卡片。第一個資料庫整合了 **Word Origins**、**The Book of Answers**、**The Book of Questions**、一本**英語詞根詞典**和一本**英語詞綴詞典**的結構化版本。Qwen3-4B Q4_K_M 可在 8 GB Raspberry Pi 5 上本機運行，也可選用速度較慢的 Qwen3-8B 設定；檢索、推論、歷史記錄和瀏覽器介面皆不依賴雲端 API。

## 用一套藏書試用

如果你已擁有範圍明確的私人書籍或詞典藏書，可以先申請免費的適配檢查，再開始[創始期 250 美元藏書適配衝刺](https://lazying.art/lkt/)。服務面向一套藏書、一個語言目標和一台現有機器，交付內容包括資料／隱私／引文地圖、一個經協議且最多包含 12 個來源單元和 20 個測試問題的樣本、材料可用時最多兩張附引文的瀏覽器卡片、一項繼續／停止建議，以及一次事實修正。付款前，書面範圍會明確「來源單元」的定義，例如一段文字、一筆記錄或一張代表性頁面。
硬體、運送、客製 OCR、批次轉換、生產部署和持續支援不在此固定範圍內。

若想在不分享任何客戶材料的前提下確切瞭解這三項交付內容，請閱讀[藏書適配報告範例](../docs/sample-fit-report.md)。它把同一格式套用於 LKT 自己已有文件記錄的參考藏書，並明確不代表客戶成果或付費專案成績。

## 六種獨立體驗，一套卡片契約

- **Word Origin** 使用專屬的單詞條檢索器和提示詞，建立範圍明確、可互動的有向詞源圖。系統保留分支詞素，並清楚區分有書籍依據的節點與模型補充的語言學背景。
- **Word Card** 檢索多筆相關的 Word Origins 詞條，組合成精簡的多語言記憶檢視。英語、日語和中文固定顯示，法語與阿拉伯語則在第四個面板輪替。
- **Book Answer** 從 318 張已審閱卡片中進行可重現抽取，保留出版物中的答案譯文，並補充一段反思文字。
- **Book Question** 依主題搜尋 291 個已審閱問題；沒有詞彙匹配時，退回可重現抽取。
- **Root Graph** 優先處理 4,018 筆含實質內容的詞根記錄，再尋找完全符合的輔助詞綴條目，並儲存遞迴詞族圖。
- **Affix Graph** 對 5,179 筆含實質內容的詞綴記錄和 Root Dictionary 採取相反優先順序，同時保留一張完整的中心詞圖。

每種模式都有自己的檢索政策與嚴格模型提示。Word Origin 與 Word Card 刻意共用同一個 Word Origins 索引，但呈現方式不同；Answer 與 Question 使用不同書籍和檢索引擎。六種模式都輸出同一套帶版本的卡片 JSON。日語卡片內文保留詞元級振假名，中文檢視則獲得確定性產生、完整標調的拼音。目前網頁介面負責呈現這些 JSON；未來電子紙和音訊轉接器也會讀取它，而不必修改語料庫、檢索或模型程式碼。

獨立的 **Chat / Benchmark** 工作區直接與 Qwen 對話，並回報實際耗時、提示／輸出 token 數和產生速度。它會被醒目標示為沒有引證的原始模型輸出，絕不儲存為有書籍依據的卡片。觀察結果保存在本機知識帳本的獨立資料表中。即使提示重複，每次仍會重新運行 Qwen；帳本是歷史記錄，而非快取。在任何卡片上選擇 **Discuss this card**，即可在 Model Lab 中開啟該卡片，並把已儲存卡片及檢索摘錄作為範圍明確的上下文。
每次即時 Model Lab 工作階段還會獲得一個可持久保留的探究執行緒。連續輪次保留父子沿襲；卡片討論會連結至其正規化來源內容原子，而 Qwen 的回應始終明確標記為無引文。

## 產品展示

瀏覽器不是聊天儀表板，而是編輯式卡片舞台。每張可見投影片都是無須捲動的單螢幕構圖，以一個醒目的核心概念搭配一則精簡來源引文。Word Origin 把中央區域留給 Cytoscape.js 有向圖。Word Card 在固定的日語／中文面板上方突出顯示英語單詞與 IPA，第四個面板輪替法語／阿拉伯語。Answer 和 Question 使用內層語言輪播——英語、帶注音的日語和帶拼音注音的中文——並把特別長的句子拆成額外的易讀投影片。經接受的本機語法分析會在完全相同的文字上加入低調的語法角色配色，不增加圖例或擁擠的中繼資料。已儲存卡片按模式形成彼此獨立的外層輪播，並附上一張／下一張控制項。
Root、Affix 和 Word Origin 共用同一個 Cytoscape 圖形呈現器：完整的已儲存圖、角落概覽圖，以及聚焦於詞根、前綴、後綴或歷史分支而不重複整張圖的內層投影片。
全螢幕展示模式會隱藏所有應用程式介面，而 `/?display=1` 會把同一份卡片文件作為適合資訊看板的畫面開啟。列印 CSS 與帶版本的卡片 JSON 為日後電子紙呈現提供清楚邊界。

### Raspberry Pi 實機展示

Word Origin 使用隨內容調整大小的節點、完整的詞源圖、多語言釋義面板、分支投影片，以及一鍵最佳適配重設。

![Raspberry Pi 上的即時 Word Origin 圖](../docs/assets/word-origin.png)

Word Card 讓英語單詞和發音成為視覺重點，同時並排呈現醒目而穩定的日語與中文面板，以及輪替的法語／阿拉伯語面板。

![Raspberry Pi 上的即時多語言 Word Card](../docs/assets/word-card.png)

每張產生的卡片都會取得新 ID，並保留在卡片帳本中。另一個正規化的 `knowledge.sqlite3` 資料庫把經接受的術語、義項、發音、音素／字素片段、詞素、歷史、翻譯、語法、來源、修訂和探究沿襲保存為可重用原子。卡片是從這些原子重建的檢視。LadybugDB 屬性圖是衍生的遍歷投影，始終可從 SQLite 重建。
經接受的 Book Answer 和 Book Question 卡片也會把經過審閱的英語、日語和中文原文準確存入這個正規化儲存。每種語言都是獨立內容原子，並連結到檢索系統所屬的書籍引文；模型反思內容被刻意排除於書籍證據之外。Qwen 以彼此獨立且範圍明確的工作切分各種語言。只有在有序片段能逐字重建已審閱句子時，結果才會被接受；經接受片段、證據連結、模型修訂及已被取代的分析，都保留為可重用知識，而不只是展示標記。

### 段落到來源的證明

[PocketPolyglot 段落範例](../examples/artifacts/pocketpolyglot-passage-graph.json)把一段專案自創的對齊文字轉成一張經人工複核的小型概念圖。每項關係都透過 LKT 的生產知識 API，解析至準確的段落單元、摘錄和固定的來源檔案雜湊。可以重新建置它，或驗證提交的成品是否仍是最新版本：

```bash
python examples/pocketpolyglot_passage_graph.py
python examples/pocketpolyglot_passage_graph.py --check
```

### 腳本化雙語會議證明

[雙語會議範例](../examples/artifacts/scripted-bilingual-meeting-knowledge.json)把十條分別附時間戳的英語和普通話發言映射成十個有類型、經人工審閱的知識單元。每個單元保留發言者、時間戳、準確的轉錄字元範圍、來源檔案雜湊，以及有證據支持的圖關係。其審閱帳本包含一次修正，並透過真實的 `KnowledgeStore` 成品生命週期，把早期版本保留為已取代版本。
同一成品另有[互動式瀏覽器證明](https://lazying.art/meeting-intelligence/)，可從一個單元追溯至準確的來源文字。

轉錄文字與時間資訊均為專案自有的腳本化測試素材。這不是 ASR、說話者分離、資訊擷取或翻譯準確率基準，也不是客戶部署或客戶成果。可以重新建置或驗證可攜式 JSON：

```bash
python examples/scripted_bilingual_meeting_knowledge.py
python examples/scripted_bilingual_meeting_knowledge.py --check
```

準備程序採用小型且能辨識相依關係的工作：檢索證據、準備一個義項、拆分組成部分、遞迴展開每條詞源分支、分別準備各語言／發音、驗證，最後組合。成功階段會立即建立檢查點；某種語言或某個分支較弱時可單獨重試，而不會丟棄其他結果。

已安裝的低優先級工作程序以均衡輪次擴充所有六個可見牌組。Question 與 Answer 從各自審閱過的書籍中抽取；Word Card 與 Word Origin 共用一次範圍明確的原子化單字研究；Root 與 Affix 各自從其潤飾後的詞典獨立抽取，並把另一部詞法書及有限的 Word Origins 匹配作為相關輔助 RAG。它始終選擇可見卡片數量最少的模式，因此任何快速路徑都不會遠遠領先其他模式。
追趕期間，系統暫停新的 Question／Answer 抽取，並且最多只允許一個未完成的自主詞彙主題處於處理中。即使選用的強化工作仍在佇列中，均衡檢查也會以有限間隔執行；詞彙工作會先於書籍語法強化工作領取。

每個尚未見過的來源仍須通過一般的本機 Qwen、RAG 與發布關卡。穩定的來源和術語識別碼可避免重新啟動後重複。若一個單字確實包含某個詞根或詞綴，原子化單字分析可以衍生 Root／Affix 檢視；同時，Root／Affix 書籍的獨立走訪可確保，即使選中的單字沒有具生產力的詞綴，這些產品仍會繼續成長。系統絕不會為了平衡分頁而虛構組成部分。

Root／Affix 準備把昂貴工作拆成兩次可恢復的本機呼叫：先處理圖／歷史，再建立小型多語言展示。圖的 token 上限為 1,200（一次全新修復時為 1,400），語言呼叫使用 512 token（修復時為 640）。截斷的 JSON 回應絕不會遞迴重新輸入 Qwen。每個通過驗證的階段都會連同模型與精確的證據指紋一併儲存，因此後續階段失敗不會浪費已完成的圖。

純瀏覽器從 Question 開始，並在每張卡片完成所有內層投影片後，依序繼續 Answer → Word Card → Word Origin → Root → Affix。顯示設定可以選擇任一模式或任意子集，同時保留這套標準順序；預設選取全部六種。預設情況下，已儲存卡片會在各個已選模式內隨機排列，也可選擇穩定的新到舊順序。每種模式擁有獨立、僅含已接受卡片的隨機遍歷，因此跨分頁不會合併其集合，也不會在每次造訪時重複同一張卡片。新接受卡片會放在該模式剩餘遍歷的最前面。明確選取分頁或使用 `?mode=` URL 時會維持該模式；指標、觸控或鍵盤操作會重新開始目前卡片的完整停留時間，之後環境動態才會繼續。

這項所有權邊界是刻意設計的：經審閱的書籍句子、譯文與引文來自本機語料記錄，模型絕不改寫；新的解釋性或詞彙資料由設定好的本機模型產生，而不是人工輸入 SQLite。不良草稿不會進入可見牌組。詞典候選和確定性發音／注音同樣是本機檢索／工具輸出，並非人工編寫的卡片資料。當 OMW 沒有選定義項的阿拉伯語詞目時，FreeDict 提供精確的英阿修正關卡；Qwen 必須複製一個檢索出的候選項，系統驗證後會附加該候選項的證據 ID。固定版本的完整 JMdict 會在本機檢查日語形式和讀音：完全相符的讀音不花費模型呼叫，唯一修正以確定方式完成，只有真正有歧義的書寫形式才會獲得一次小型 Qwen 選擇，且選項限制於檢索出的讀音。`/api/health` 把兩個精簡的修正索引都視為必要來源，並回報其就緒狀態、版本、雜湊與條目數量。
當 Raspberry Pi 目前發生欠壓、降頻或高溫時，自主產生會暫停，並在狀況解除後恢復。網頁用戶端載入完整的已選模式（最多 1,000 張已接受卡片），把最新卡片保留在首位，並於每一輪輪播中將其他卡片各自打亂一次。它會輪詢已接受卡片而不中斷目前展示，並將新發布結果插入下一位。精簡狀態資訊與 `/api/health` 都會回報有限書籍涵蓋範圍和詞彙規劃／接受進度，但不會因此安排工作。互動式單字請求會立即處理，並重用與自主準備相同的持久原子。

已取得知識的頁尾最多顯示由 18 個卡片圓點組成的移動視窗；箭頭導覽和精確的 `目前 / 總數` 計數器仍涵蓋完整且無限成長的已儲存牌組。Question／Answer 的語言圓點只屬於目前卡片，換卡時也會替換。

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

## 依據規則

語言模型負責撰寫說明和缺少的語言輔助內容，但絕不撰寫引文清單。LKT 直接從檢索記錄附加條目 ID、摘錄、章節、頁碼、數位定位資訊和經審閱的卡片書籍譯文。Word Origin 可以補充可靠的語言學背景，但圖中每個節點都會記錄它來自書籍錨點還是模型知識。如果設定的書籍沒有證據，應用程式便不會產生卡片。

## 儲存庫地圖

| 路徑 | 職責 |
| --- | --- |
| `lkt/corpus.py` | Word Origins 匯入、原子化 SQLite 索引、精確與 FTS 檢索 |
| `lkt/morphology.py` | Root／Affix 潤飾版 JSONL 匯入、來源記錄、精確與 FTS 檢索 |
| `lkt/card_books.py` | 多語言 Answer／Question 匯入、搜尋和確定性抽取 |
| `lkt/deck.py` | 書籍與詞彙交替逐項準備 |
| `lkt/device.py` | 背景推論使用的 Pi 電源／溫度就緒關卡 |
| `lkt/retrieval.py` | 獨立的 Word Origin、Word Card、Answer 與 Question RAG 政策 |
| `lkt/llm.py` | 精簡的 llama.cpp 轉接器，以及每種體驗各自的一條嚴格提示 |
| `lkt/service.py` | 卡片組合與正規化 |
| `lkt/pronunciation.py` | 確定性拼音／注音和帶版本的離線 IPA |
| `lkt/store.py` | 帶版本卡片、準備成品、修訂、封存和聊天帳本 |
| `lkt/knowledge.py` | 原子化既有知識、證據、工作、修訂和探究沿襲 |
| `lkt/preparation.py` | 能辨識相依關係的分治式單字／內容規劃 |
| `lkt/atomic.py` | 有界原子準備與確定性卡片組裝 |
| `lkt/graph.py` | 由已接受 SQLite 原子重建的 LadybugDB 遍歷投影 |
| `lkt/lexicon.py` | 精簡的多語言 WordNet 修正證據 |
| `lkt/freedict.py` | 精確 FreeDict 英阿匯入與修正檢索 |
| `lkt/jmdict.py` | 完整 JMdict 精確形式讀音索引與來源記錄 |
| `lkt/web.py` | 無相依套件的 HTTP API 與 GUI 伺服器 |
| `lkt/outputs.py` | 穩定的網頁／電子紙／音訊輸出邊界 |
| `lkt/static/` | 桌面級 GUI，並具備足夠的響應能力供日後資訊看板使用 |
| `scripts/` | 可重現的 Pi 執行環境、安裝、更新及冒煙測試工具 |
| `systemd/` | 強化的模型與應用程式服務 |
| `docs/lineage.md` | 精確的舊專案與語料來源 |
| `docs/product-brief.md` | 持久的所有者需求與驗收標準 |
| `docs/knowledge-architecture.md` | 原子 SQLite、圖投影和分階段準備契約 |
| `docs/owner-request-log.md` | 按時間排列、私人資訊已刪減的所有者指令 |
| `docs/voice-hardware.md` | 支援的麥克風選擇和分階段音訊測試 |
| `docs/mode-roadmap.md` | 未來後綴、詞綴與詞根書籍的擴充計畫 |

## 本機開發

安裝精簡且固定版本的發音相依套件，然後執行測試套件：

```powershell
cd C:\Users\Administrator\Projects\LocalKnowledge
python -m pip install -e .
python -m unittest discover -s tests -v
python -m compileall -q lkt tests
```

從結構化書籍匯出檔建置本機索引：

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

當 llama.cpp 伺服器監聽 8081 連接埠時：

```powershell
python -m lkt.cli generate abacus --mode word
python -m lkt.cli generate "Should I begin?" --mode answer
python -m lkt.cli generate technology --mode question
python -m lkt.cli generate inspection --mode root
python -m lkt.cli generate abnormal --mode affix
python -m lkt.cli serve
```

開啟 <http://127.0.0.1:8090>。

## Raspberry Pi 5 目錄配置

```text
/home/lachlan/LocalKnowledgeTerminal/
├── source/      # this Git repository; updated with fetch + fast-forward pull
├── runtime/     # pinned llama.cpp plus optional knowledge Python environment
├── models/      # Qwen GGUF (not committed)
├── data/        # corpus index and saved cards (not committed)
└── logs/        # bootstrap logs (not committed)
```

固定版本的執行環境成品：

| 成品 | 版本 | 完整性 |
| --- | --- | --- |
| llama.cpp | `v0.3.0` / `c1d0e7a004015f23bc0233470b747b596f29b264` | 以提交固定版本的原始碼封存檔 |
| Qwen3-4B-GGUF | `bc640142c66e1fdd12af0bd68f40445458f3869b` | Q4_K_M SHA-256 `7485fe6f…534fdf5` |
| 模型檔案 | `Qwen3-4B-Q4_K_M.gguf` | 2,497,280,256 位元組 |

Pi 服務只開放一個推論插槽（`--parallel 1`）。因此卡片組合與 Model Lab 請求會依序處理，使記憶體用量和延遲保持可預測，而不是讓四個 CPU 核心在多項工作之間爭用資源。

Qwen3-8B 已證實可作為選用的品質優先準備模型。在已部署的 Pi 上，它以每秒 1.78 token 產生一個 120-token 多語言探針，RSS 約為 6.28 GiB，系統仍有 1.85 GiB 記憶體可用，且當時沒有溫度降頻。Qwen3-4B 是反應較快的離線預設模型。模型選擇明確且可逆：

```bash
tmux new-session -d -s lkt-8b-download \
  './scripts/download_qwen3_8b.sh > ../logs/qwen3-8b-download.log 2>&1'
sudo ./scripts/select_model.sh status
sudo ./scripts/select_model.sh 8b
sudo ./scripts/select_model.sh 4b
sudo ./scripts/benchmark_models.sh
```

任何時候只載入一個模型。預設 4B 設定使用 3,072-token 上下文；選用的 8B 設定使用 2,048-token 上下文和較小批次，以保護 8 GB 記憶體邊界。如果其伺服器未能進入健康狀態，`select_model.sh 8b` 會自動恢復 4B 設定。
下載器支援續傳部分檔案，驗證官方 SHA-256，之後才以原子方式公開最終 GGUF。
基準測試每次只啟用一個模型，執行相同且有界的多語言品質／速度探針，記錄實際耗時、llama.cpp token 速率和程序記憶體，最後恢復測試前啟用的模型。

安裝精簡的選用知識執行環境並建置圖投影：

```bash
./scripts/install_knowledge_runtime.sh
./scripts/rebuild_graph.sh
```

這會安裝供本機 IPA 使用的 eSpeak NG，將 LadybugDB 0.19.1 和 Wn 1.1.1 固定安裝在隔離環境中，並只安裝 OMW 2.0 的英語、日語、華語中文、法語和阿拉伯語詞庫。它也會驗證固定版本的完整 JMdict 封存檔、建立精確形式讀音索引，然後移除原始下載檔案。完整 Wiktionary 傾印被刻意排除。IPA 擷取使用安靜文字模式，不啟用語音輸出。

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

日後進行 Windows → GitHub → Pi 開發時：

```bash
cd /home/lachlan/LocalKnowledgeTerminal/source
./scripts/update_pi_tmux.sh
tmux attach -t lkt-update
```

tmux 包裝器讓部署在 SSH 或瀏覽器切換期間持續執行，並寫入 `~/LocalKnowledgeTerminal/logs/update-pi.log`。底層冪等的 `scripts/install_services.sh` 會安裝全部三個 systemd 單元、設定開機啟用、按模型 → 網頁 → 工作程序順序啟動、驗證兩個健康端點，並安裝圖形介面自動啟動項目。`scripts/update_pi.sh` 會在使用 `--restart` 呼叫該服務安裝器之前執行完整測試關卡。

然後在 Pi 的 VNC 桌面開啟 `http://127.0.0.1:8090`，或從受信任的區域網路開啟 `http://<pi-lan-address>:8090`。

安裝器也會把 `desktop/lkt-kiosk.desktop` 放進 Pi 使用者的 XDG 自動啟動目錄，並把 `scripts/open_kiosk.sh` 安裝為 `/usr/local/bin/lkt-open-kiosk`。下次圖形登入時，啟動器會等待本機健康端點，再以一個專用 Chromium 設定檔開啟唯一的 `http://127.0.0.1:8090/?display`。再次執行啟動器也無妨：它會偵測該設定檔，不會再開新視窗。Chromium 以一般全螢幕應用程式而非鎖定式資訊看板啟動，因此按 **Esc** 即可離開全螢幕並返回可控制的 Pi 桌面。明確的模式 URL 仍可用於刻意的 VNC 操作。

## 資料與著作權

書籍 PDF、擷取後的語料、模型權重、產生的索引和已儲存卡片都被刻意排除於 Git 之外。安裝時請提供一份合法取得的本機 JSONL 匯出檔。LKT 在 SQLite 索引中記錄每個 SHA-256，因此產生的卡片可追溯到確切的語料建置版本。已驗證參考資料集請見 [`docs/corpora.md`](../docs/corpora.md)。

## 沿襲關係

LKT 是一個全新實作、本機優先的後繼專案，其設計參考了 [`WordsCardEink`](https://github.com/lachlanchen/WordsCardEink) 和 [`WordOrigins`](https://github.com/lachlanchen/WordOrigins)。它不匯入這些專案的單體執行環境或硬體相依套件。固定提交與保留的設計理念請見 [`docs/lineage.md`](../docs/lineage.md)。

## 支持

| Donate | PayPal | Stripe |
| --- | --- | --- |
| [![Donate](https://img.shields.io/badge/Donate-LazyingArt-0EA5E9?style=for-the-badge&logo=ko-fi&logoColor=white)](https://chat.lazying.art/donate) | [![PayPal](https://img.shields.io/badge/PayPal-RongzhouChen-00457C?style=for-the-badge&logo=paypal&logoColor=white)](https://paypal.me/RongzhouChen) | [![Stripe](https://img.shields.io/badge/Stripe-Donate-635BFF?style=for-the-badge&logo=stripe&logoColor=white)](https://buy.stripe.com/aFadR8gIaflgfQV6T4fw400) |

## 引用

如果 LKT 對你的工作有幫助，請使用 GitHub 的 **Cite this repository** 選單引用；此選單會讀取 [`CITATION.cff`](../CITATION.cff)。也可使用：

```bibtex
@software{chen_local_knowledge_terminal_2026,
  author = {Chen, Lachlan},
  title = {Local Knowledge Terminal},
  year = {2026},
  version = {0.1.0},
  url = {https://github.com/lachlanchen/LocalKnowledgeTerminal}
}
```
