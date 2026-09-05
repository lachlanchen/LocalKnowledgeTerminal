[English](../README.md) · [العربية](README.ar.md) · [Español](README.es.md) · [Français](README.fr.md) · [日本語](README.ja.md) · [한국어](README.ko.md) · [Tiếng Việt](README.vi.md) · [中文 (简体)](README.zh-Hans.md) · [中文（繁體）](README.zh-Hant.md) · [Deutsch](README.de.md) · [Русский](README.ru.md)

[![LazyingArt banner — Local Knowledge Terminal](../docs/assets/banner.svg)](../docs/assets/banner.svg)

# Local Knowledge Terminal

**自分のハードウェアで動く、書籍に根拠を置いたプライベートな知識環境。**

[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![Model](https://img.shields.io/badge/Qwen3-4B%20%2F%208B-6f42c1?style=flat-square)](https://huggingface.co/Qwen/Qwen3-8B-GGUF)
[![Runtime](https://img.shields.io/badge/llama.cpp-v0.3.0-173f35?style=flat-square)](https://github.com/ggml-org/llama.cpp)
[![Target](https://img.shields.io/badge/Raspberry%20Pi%205-8GB-C51A4A?style=flat-square&logo=raspberrypi)](https://www.raspberrypi.com/products/raspberry-pi-5/)
[![Sponsor](https://img.shields.io/badge/Sponsor-lachlanchen-EA4AAA?style=flat-square&logo=githubsponsors&logoColor=white)](https://github.com/sponsors/lachlanchen)

Local Knowledge Terminal（LKT）は、プライベートな書籍コレクションを、出典付きの
多言語カードへ変換します。最初のライブラリは、構造化された **Word Origins**、
**The Book of Answers**、**The Book of Questions**、**English Root Dictionary**、
**English Affix Dictionary** を組み合わせたものです。8 GB の Raspberry Pi 5 上で
Qwen3-4B Q4_K_M がローカルに動作し、必要に応じて低速な Qwen3-8B プロファイルも
選べます。検索、推論、履歴、ブラウザー GUI はクラウド API なしで動作します。

## 1つのコレクションで試す

範囲の明確な私有の書籍・辞書コレクションをお持ちなら、
[初回 USD 250 のコレクション適合性スプリント](https://lazying.art/lkt/)は無料の
適合性確認から始まります。対象は1つのコレクション、1つの言語目標、1台の既存
マシンです。データ・プライバシー・引用の対応表、合意済みの代表サンプル（最大12の
ソース単位と20のテスト質問）、素材が利用可能な場合は最大2枚の出典付きブラウザー
カード、実施可否の提言、事実に関する1回の修正を納品します。支払い前に書面の
スコープで、ソース単位（たとえば一節、1レコード、代表ページ）を定義します。
ハードウェア、配送、独自 OCR、一括変換、本番デプロイ、継続サポートは、この固定
スコープには含まれません。

顧客の素材を共有せずに、この3つの納品物の具体像を確認するには、
[サンプルのコレクション適合性レポート](../docs/sample-fit-report.md)をご覧ください。
LKT 自身が文書化した参照コレクションに同じ形式を適用したもので、顧客成果や有償案件の
実績を示すものではありません。

## 6つの独立した体験、1つのカード契約

- **Word Origin** は専用の1エントリー検索器とプロンプトを使い、範囲を限定した
  対話可能な有向語源グラフを作ります。形態素の分岐を維持し、書籍に裏付けられた
  ノードとモデルが補った言語学的文脈を明確に区別します。
- **Word Card** は複数の関連する Word Origins エントリーを検索し、簡潔な多言語
  記憶ビューを構成します。英語・日本語・中国語は固定し、4番目のパネルでフランス語と
  アラビア語を切り替えます。
- **Book Answer** は査読済み318枚のカードから再現可能な抽選を行い、公開済みの
  回答翻訳を維持したうえで、内省的な注記を加えます。
- **Book Question** は査読済み291問をテーマで検索し、語彙的一致がない場合は
  再現可能な抽選に切り替えます。
- **Root Graph** は内容を持つ語根レコード4,018件を優先し、次に正確に対応する
  接辞エントリーを使って、再帰的な語族グラフを保存します。
- **Affix Graph** は内容を持つ接辞レコード5,179件と Root Dictionary の優先順を
  逆にしながら、中心語の完全なグラフを1つ保持します。

各モードには、固有の検索方針と厳格なモデルプロンプトがあります。Word Origin と
Word Card は同じ Word Origins インデックスを意図的に共有しつつ、異なる見せ方を
します。Answer と Question は別々の書籍と検索エンジンを使います。6モードすべてが
同じバージョン管理されたカード JSON を生成します。日本語のカード書籍テキストは
トークン単位のふりがなを保持し、中国語ビューには決定論的な声調記号付きピンインを
付与します。現在は Web GUI がその JSON を描画し、将来はコーパス・検索・モデルの
コードを変更せずに、電子ペーパーと音声アダプターも利用します。

独立した **Chat / Benchmark** ワークスペースは Qwen と直接対話し、経過時間、
プロンプト／出力トークン、生成速度を報告します。これは根拠のない生のモデル出力と
明示され、書籍に根拠を置くカードとしては保存されません。観測値はローカル知識台帳の
別テーブルに保持されます。同じプロンプトを繰り返しても、その都度 Qwen が実行されます。
台帳は履歴であり、キャッシュではありません。どのカードからでも **Discuss this card**
を選ぶと、保存済みカードと検索された抜粋を限定された文脈として Model Lab が開きます。
各 Model Lab セッションには、永続的な問いのスレッドも割り当てられます。後続のターンは
親子の系譜を保持します。カードについての議論は正規化されたソース内容アトムに結び付き、
Qwen の応答は引き続き出典なしと明示されます。

## 製品表示

ブラウザーはチャットのダッシュボードではなく、編集されたカードを見せる舞台です。
各スライドは、中心となる大きなアイデアと簡潔な出典1つを備えた、スクロール不要の
1画面構成です。Word Origin は中央に Cytoscape.js の有向グラフを配置します。
Word Card は大きな英単語／IPA の下に日本語と中国語を固定し、フランス語／アラビア語の
パネルを切り替えます。Answer と Question は、英語、日本語ルビ、中国語ピンインルビの
内部言語カルーセルを使い、非常に長い文は読みやすい追加スライドに分けます。承認済みの
ローカル文法分析は、まったく同じテキストに控えめな役割色を加えます。凡例や混雑した
メタデータは加えません。保存カードはモードごとに独立した外側カルーセルを構成し、
前／次の操作を備えます。
Root、Affix、Word Origin は1つの Cytoscape グラフ描画器を共有します。完全な保存済み
グラフ、隅の概観マップ、語根・接頭辞・接尾辞・履歴分岐へズームする内部フォーカス
スライドを、グラフを複製せずに表示します。
全画面表示ではアプリケーションの枠をすべて隠し、`/?display=1` は同じカード文書を
キオスク向け画面として開きます。印刷 CSS とバージョン管理されたカード JSON は、
将来の電子ペーパー描画との境界を明確にします。

### Raspberry Pi でのライブ表示

Word Origin は内容に合わせたサイズのノード、完全な語源グラフ、多言語の意味パネル、
分岐スライド、ワンクリックの最適表示リセットを備えます。

![Raspberry Pi 上のライブ Word Origin グラフ](../docs/assets/word-origin.png)

Word Card は英単語と発音を大きく保ちながら、安定した大きな日本語・中国語パネルと、
切り替わるフランス語／アラビア語パネルを並べます。

![Raspberry Pi 上のライブ多言語 Word Card](../docs/assets/word-card.png)

生成した各カードには新しい ID が与えられ、カード台帳に残ります。もう1つの正規化済み
`knowledge.sqlite3` データベースは、承認済みの用語、語義、発音、音素／書記素の区分、
形態素、履歴、翻訳、文法、来歴、改訂、問いの系譜を再利用可能なアトムとして保存します。
カードはそれらのアトムから再構成できるビューです。LadybugDB のプロパティグラフは
派生した探索用投影であり、SQLite からいつでも再構築できます。
承認済みの Book Answer と Book Question カードも、査読済みの英語・日本語・中国語の
正確なテキストをこの正規化ストアに格納します。各言語は、検索側が管理する書籍出典に
結び付いた独立の内容アトムです。モデルによる考察は、その書籍証拠から意図的に除外します。
Qwen は言語ごとに別々の限定ジョブで分割します。並べた部分から査読済み文を一文字も違わず
復元できる場合のみ、結果を承認します。承認部分、証拠リンク、モデル改訂、置換された分析は、
表示専用のマークアップではなく再利用可能な知識として残ります。

### 一節から来歴へつなぐ実証

[PocketPolyglot の一節例](../examples/artifacts/pocketpolyglot-passage-graph.json)は、
プロジェクトが作成した整列済みの一節を、小さな手動レビュー済み概念グラフへ変換します。
すべての関係は、LKT の本番知識 API を通じて、正確な一節単位、抜粋、固定された
ソースファイルハッシュまで解決できます。再構築するか、コミット済み成果物が最新か確認します。

```bash
python examples/pocketpolyglot_passage_graph.py
python examples/pocketpolyglot_passage_graph.py --check
```

### 台本付き二言語会議の実証

[二言語会議の例](../examples/artifacts/scripted-bilingual-meeting-knowledge.json)は、個別に
タイムスタンプを付けた英語・中国語の10発話を、型付けされ手動レビューされた10の知識単位に
対応させます。各単位は、話者、タイムスタンプ、トランスクリプト内の正確な文字範囲、
ソースファイルハッシュ、証拠に裏付けられたグラフ関係を保持します。レビュー台帳には1件の
修正が含まれ、その旧版は実際の `KnowledgeStore` 成果物ライフサイクルを通じて superseded
として保持されます。同じ成果物には、単位から正確な元の語句までたどれる
[対話型ブラウザー実証](https://lazying.art/meeting-intelligence/)もあります。

トランスクリプトと時刻は、プロジェクト所有の台本付きフィクスチャです。これは ASR、
話者分離、抽出、翻訳の精度ベンチマークではなく、顧客への導入や顧客成果でもありません。
ポータブル JSON を再構築または検証します。

```bash
python examples/scripted_bilingual_meeting_knowledge.py
python examples/scripted_bilingual_meeting_knowledge.py --check
```

準備は、証拠の検索、1つの意味の準備、構成要素の分割、各語源分岐の再帰的展開、各言語／
発音の独立準備、検証、構成という、依存関係を考慮した小さなジョブで進みます。成功した段階は
即時にチェックポイントされるため、弱い言語や分岐だけを、残りを破棄せず再試行できます。

インストール済みの低優先度ワーカーは、6つの表示デッキすべてを均衡したラウンドで育てます。
Question と Answer は各自の査読済み書籍から選び、Word Card と Word Origin は1つの限定的な
アトミック単語調査を共有します。Root と Affix はそれぞれ独自の整備済み辞書から独立に選び、
もう一方の形態論書籍と限定された Word Origins の一致を、関連する補助 RAG として使います。
常に表示モードのうちカード数が最少のものを選ぶため、高速な経路だけが他より大きく先行しません。
追い付き中は新しい Question/Answer の抽選を止め、未完了の自律的な語彙主題を同時に最大1件に
抑えます。任意の補強がキューに残っていても、均衡確認は限定間隔で動きます。語彙ジョブは書籍の
文法補強より先に取得されます。

未処理の各ソースは、通常どおりローカル Qwen、RAG、公開ゲートを通過します。安定したソースと
用語 ID が再起動をまたぐ重複を防ぎます。アトミックな単語分析は、単語が実際に語根／接辞を
含む場合に Root/Affix ビューを派生できますが、独立した Root/Affix 書籍巡回により、選ばれた
単語に生産的な接辞がなくても各プロダクトは成長します。タブの均衡だけを目的に構成要素を
捏造することはありません。

Root/Affix の準備は高コストの作業を再開可能な2回のローカル呼び出しに分けます。最初に
グラフ／履歴、次に小さな多言語表示です。グラフは1,200トークン（新規修復1回は1,400）、
言語呼び出しは512トークン（修復時640）を上限とします。途中で切れた JSON 応答を再帰的に
Qwen へ戻すことはありません。検証済みの各段階はモデルと正確な証拠フィンガープリントと共に
保存されるため、後段の失敗でグラフが無駄になりません。

素のブラウザーは Question から始まり、各カードがすべての内部スライドを終えるたびに
Answer → Word Card → Word Origin → Root → Affix と進みます。表示設定では標準順を保ったまま
任意の1モードまたは部分集合を選べ、既定では6つすべてが選択されます。保存済みカードは既定で
選択モード内をシャッフルし、安定した新しい順も選べます。各モードは承認済みカードだけの独立した
シャッフル巡回を持つため、タブをまたいでもコレクションが混ざらず、毎回同じカードになりません。
新しく承認されたカードは、そのモードの残りの巡回の先頭に置かれます。明示的なタブまたは
`?mode=` URL はそのモード内に留まり、ポインター、タッチ、キーボード操作があると現在カードの
滞在時間全体を再開してから周囲の動きが再開します。

この所有境界は意図的です。査読済みの書籍文、翻訳、引用はローカルのコーパスレコードに由来し、
モデルが書き換えることはありません。新しい説明や語彙データは、SQLite への手入力ではなく設定済み
ローカルモデルが生成します。不良な草稿は表示デッキの外に残ります。辞書候補と決定論的な発音／ルビも、
手作業で作ったカードデータではなくローカル検索／ツール出力です。選択した語義について OMW に
アラビア語見出し語がない場合、FreeDict が正確な英語―アラビア語の修正ゲートを提供します。
Qwen は検索候補を1つそのままコピーしなければならず、システムは検証後にその候補の証拠 ID を付けます。
固定された JMdict 完全版は、日本語の表記と読みをローカルで検査します。完全一致する読みはモデル呼び出し
不要、一意の修正は決定論的で、本当に曖昧な表記だけが検索済み読みに限定した小さな Qwen 選択を受けます。
`/api/health` は両方の簡潔な修正インデックスを必須ソースとして扱い、準備状態、バージョン、ハッシュ、
エントリー数を報告します。
Raspberry Pi で電圧不足、スロットリング、高温が発生中は自律生成を一時停止し、状態解消後に再開します。
Web クライアントは選択モード全体（承認済み最大1,000枚）を読み込み、最新を先頭に保ち、残りを
カルーセル巡回ごとに1回シャッフルします。現在の表示を中断せず承認済みカードをポーリングし、
新しく公開された結果を次に挿入します。簡潔な状態表示と `/api/health` は、有限な書籍の網羅状況と
語彙の計画済み／承認済み進捗を、作業をスケジュールせずに報告します。対話的な単語リクエストは
即時のままで、自律準備と同じ永続化アトムを再利用します。

獲得知識フッターは最大18個のカードドットが動く窓を描画します。矢印ナビゲーションと正確な
`current / total` カウンターは、制限のない保存デッキ全体を引き続き扱います。Question/Answer の
言語ドットは現在カードだけに属し、カードが変わると置換されます。

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

## 根拠付けの規則

言語モデルは説明と不足する言語補助を記述しますが、引用一覧は記述しません。LKT は、
エントリー ID、抜粋、節、ページ番号、デジタル位置情報、査読済みカード書籍翻訳を検索レコードから
直接付与します。Word Origin は信頼できる言語学的文脈を加えることがありますが、各グラフノードは
書籍のアンカーとモデル知識のどちらに由来するかを記録します。設定した書籍に証拠がなければ、
アプリはカードを生成しません。

## リポジトリ構成

| パス | 担当 |
| --- | --- |
| `lkt/corpus.py` | Word Origins の取り込み、アトミック SQLite インデックス、完全一致＋FTS 検索 |
| `lkt/morphology.py` | Root/Affix 整備済み JSONL の取り込み、来歴、完全一致＋FTS 検索 |
| `lkt/card_books.py` | 多言語 Answer/Question の取り込み、検索、決定論的な抽選 |
| `lkt/deck.py` | 書籍と語彙を交互に1件ずつ準備 |
| `lkt/device.py` | バックグラウンド推論用 Pi 電源／温度準備ゲート |
| `lkt/retrieval.py` | Word Origin、Word Card、Answer、Question の独立 RAG 方針 |
| `lkt/llm.py` | 小さな llama.cpp アダプターと体験ごとの厳格なプロンプト |
| `lkt/service.py` | カード構成と正規化 |
| `lkt/pronunciation.py` | 決定論的なピンイン／ルビとバージョン管理されたオフライン IPA |
| `lkt/store.py` | バージョン管理カード、準備成果物、改訂、アーカイブ、チャット台帳 |
| `lkt/knowledge.py` | アトミックな確立知識、証拠、ジョブ、改訂、問いの系譜 |
| `lkt/preparation.py` | 依存関係を考慮した分割統治型の単語／内容計画 |
| `lkt/atomic.py` | 限定されたアトミック準備と決定論的カード組み立て |
| `lkt/graph.py` | 承認済み SQLite アトムから再構築可能な LadybugDB 探索投影 |
| `lkt/lexicon.py` | 簡潔な多言語 WordNet 修正証拠 |
| `lkt/freedict.py` | 正確な FreeDict 英語―アラビア語の取り込みと修正検索 |
| `lkt/jmdict.py` | JMdict 完全版の完全一致表記読みインデックスと来歴 |
| `lkt/web.py` | 依存関係のない HTTP API と GUI サーバー |
| `lkt/outputs.py` | 安定した Web／電子ペーパー／音声出力境界 |
| `lkt/static/` | デスクトップ級 GUI、将来のキオスクにも十分なレスポンシブ性 |
| `scripts/` | 再現可能な Pi ランタイム、インストール、更新、スモークテスト用ツール |
| `systemd/` | 強化済みモデル／アプリケーションサービス |
| `docs/lineage.md` | 旧プロジェクトとコーパスの正確な来歴 |
| `docs/product-brief.md` | 継続的な所有者要件と受け入れ基準 |
| `docs/knowledge-architecture.md` | アトミック SQLite、グラフ投影、段階的準備の契約 |
| `docs/owner-request-log.md` | プライバシーを除去した所有者指示の時系列ログ |
| `docs/voice-hardware.md` | 対応マイクの選択と段階的音声テスト |
| `docs/mode-roadmap.md` | 将来の接尾辞、接辞、語根書籍に向けた拡張計画 |

## ローカル開発

小さな固定版発音依存関係をインストールし、テスト一式を実行します。

```powershell
cd C:\Users\Administrator\Projects\LocalKnowledge
python -m pip install -e .
python -m unittest discover -s tests -v
python -m compileall -q lkt tests
```

構造化された書籍エクスポートからローカルインデックスを構築します。

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

llama.cpp サーバーがポート8081で待ち受けている場合：

```powershell
python -m lkt.cli generate abacus --mode word
python -m lkt.cli generate "Should I begin?" --mode answer
python -m lkt.cli generate technology --mode question
python -m lkt.cli generate inspection --mode root
python -m lkt.cli generate abnormal --mode affix
python -m lkt.cli serve
```

<http://127.0.0.1:8090> を開きます。

## Raspberry Pi 5 の構成

```text
/home/lachlan/LocalKnowledgeTerminal/
├── source/      # this Git repository; updated with fetch + fast-forward pull
├── runtime/     # pinned llama.cpp plus optional knowledge Python environment
├── models/      # Qwen GGUF (not committed)
├── data/        # corpus index and saved cards (not committed)
└── logs/        # bootstrap logs (not committed)
```

固定ランタイム成果物：

| 成果物 | リビジョン | 完全性 |
| --- | --- | --- |
| llama.cpp | `v0.3.0` / `c1d0e7a004015f23bc0233470b747b596f29b264` | コミットで固定したソースアーカイブ |
| Qwen3-4B-GGUF | `bc640142c66e1fdd12af0bd68f40445458f3869b` | Q4_K_M SHA-256 `7485fe6f…534fdf5` |
| モデルファイル | `Qwen3-4B-Q4_K_M.gguf` | 2,497,280,256 バイト |

Pi サービスは1つの推論スロット（`--parallel 1`）を公開します。そのためカード構成と
Model Lab リクエストは順番に処理され、4つの CPU コアをジョブ間で競合させず、メモリ使用量と
レイテンシーを予測可能に保ちます。

Qwen3-8B は、品質優先の任意準備モデルとして利用可能であることを実証済みです。配備済み Pi では、
120トークンの多言語プローブを毎秒1.78トークン、RSS 約6.28 GiB、システムメモリ残量1.85 GiB、
現在の温度スロットリングなしで生成しました。Qwen3-4B は応答性の高いオフライン既定値です。
モデル選択は明示的で元に戻せます。

```bash
tmux new-session -d -s lkt-8b-download \
  './scripts/download_qwen3_8b.sh > ../logs/qwen3-8b-download.log 2>&1'
sudo ./scripts/select_model.sh status
sudo ./scripts/select_model.sh 8b
sudo ./scripts/select_model.sh 4b
sudo ./scripts/benchmark_models.sh
```

同時に読み込むモデルは1つだけです。既定の4Bプロファイルは3,072トークンのコンテキストを使い、
任意の8Bプロファイルは8 GBのメモリ境界を守るため2,048トークンのコンテキストと小さなバッチを
使います。サーバーが正常にならない場合、`select_model.sh 8b` は4Bプロファイルを自動的に復元します。
ダウンローダーは中断した転送を再開し、公式 SHA-256 を検証した後でのみ最終 GGUF をアトミックに
公開します。ベンチマークは一度に1モデルを有効化し、同じ限定多言語品質／速度プローブを実行し、
経過時間、llama.cpp のトークン速度、プロセスメモリを記録して、実行前のモデルに戻します。

任意の簡潔な知識ランタイムをインストールし、グラフ投影を構築します。

```bash
./scripts/install_knowledge_runtime.sh
./scripts/rebuild_graph.sh
```

これにより、ローカル IPA 用 eSpeak NG をインストールし、LadybugDB 0.19.1 と Wn 1.1.1 を
隔離環境に固定した後、OMW 2.0 の英語、日本語、中国語、フランス語、アラビア語辞書だけを
導入します。また、固定済み JMdict 完全版アーカイブを検証し、完全一致の表記読みインデックスを
構築して、生のダウンロードを削除します。完全な Wiktionary ダンプは意図的に除外します。
IPA 抽出は静かなテキストモードを使い、音声出力を有効にしません。

Pi 上では：

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

後の Windows → GitHub → Pi 開発では：

```bash
cd /home/lachlan/LocalKnowledgeTerminal/source
./scripts/update_pi_tmux.sh
tmux attach -t lkt-update
```

tmux ラッパーは SSH やブラウザー遷移をまたいでデプロイを継続し、
`~/LocalKnowledgeTerminal/logs/update-pi.log` に書き込みます。基盤となる冪等な
`scripts/install_services.sh` は3つの systemd ユニットをすべてインストールして起動時に有効化し、
model → web → worker の順に開始し、両方のヘルスエンドポイントを検証して、グラフィカルな
自動起動エントリーをインストールします。`scripts/update_pi.sh` はサービスインストーラーを
`--restart` 付きで呼ぶ前に、すべてのテストゲートを実行します。

その後、Pi の VNC デスクトップで `http://127.0.0.1:8090` を開くか、信頼できるローカル
ネットワークから `http://<pi-lan-address>:8090` を開きます。

インストーラーは `desktop/lkt-kiosk.desktop` を Pi ユーザーの XDG 自動起動ディレクトリにも
配置し、`scripts/open_kiosk.sh` を `/usr/local/bin/lkt-open-kiosk` としてインストールします。
次回のグラフィカルログイン時に、ランチャーはローカルのヘルスエンドポイントを待ち、専用の
Chromium プロファイルを `http://127.0.0.1:8090/?display` で1つだけ開きます。再度実行しても
問題ありません。そのプロファイルを検出し、別ウィンドウを開きません。Chromium はロックされた
キオスクではなく通常の全画面アプリとして起動するため、**Esc** で全画面を終了して操作可能な
Pi デスクトップへ戻れます。意図的に VNC で使う場合は、明示的なモード URL も利用できます。

## データと著作権

書籍 PDF、抽出コーパス、モデル重み、生成インデックス、保存済みカードは意図的に Git から
除外しています。インストール時には、合法的に入手したローカル JSONL エクスポートを指定して
ください。生成カードを正確なコーパスビルドまで追跡できるよう、LKT は各 SHA-256 を SQLite
インデックスに記録します。検証済み参照セットについては
[`docs/corpora.md`](../docs/corpora.md)をご覧ください。

## 系譜

LKT は、[`WordsCardEink`](https://github.com/lachlanchen/WordsCardEink) と
[`WordOrigins`](https://github.com/lachlanchen/WordOrigins) から着想を得た、クリーンで
ローカルファーストな後継です。両者の一枚岩のランタイムやハードウェア依存関係は取り込みません。
固定コミットと継承したアイデアについては [`docs/lineage.md`](../docs/lineage.md)をご覧ください。

## 支援

| Donate | PayPal | Stripe |
| --- | --- | --- |
| [![Donate](https://img.shields.io/badge/Donate-LazyingArt-0EA5E9?style=for-the-badge&logo=ko-fi&logoColor=white)](https://chat.lazying.art/donate) | [![PayPal](https://img.shields.io/badge/PayPal-RongzhouChen-00457C?style=for-the-badge&logo=paypal&logoColor=white)](https://paypal.me/RongzhouChen) | [![Stripe](https://img.shields.io/badge/Stripe-Donate-635BFF?style=for-the-badge&logo=stripe&logoColor=white)](https://buy.stripe.com/aFadR8gIaflgfQV6T4fw400) |

## 引用

LKT が研究や制作に役立った場合は、[`CITATION.cff`](../CITATION.cff) を読む GitHub の
**Cite this repository** メニュー、または次の形式で引用してください。

```bibtex
@software{chen_local_knowledge_terminal_2026,
  author = {Chen, Lachlan},
  title = {Local Knowledge Terminal},
  year = {2026},
  version = {0.1.0},
  url = {https://github.com/lachlanchen/LocalKnowledgeTerminal}
}
```
