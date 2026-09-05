[English](../README.md) · [العربية](README.ar.md) · [Español](README.es.md) · [Français](README.fr.md) · [日本語](README.ja.md) · [한국어](README.ko.md) · [Tiếng Việt](README.vi.md) · [中文 (简体)](README.zh-Hans.md) · [中文（繁體）](README.zh-Hant.md) · [Deutsch](README.de.md) · [Русский](README.ru.md)

[![LazyingArt banner — Local Knowledge Terminal](../docs/assets/banner.svg)](../docs/assets/banner.svg)

# Local Knowledge Terminal

**내 하드웨어에서 실행되는, 책에 근거한 비공개 지식 환경.**

[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![Model](https://img.shields.io/badge/Qwen3-4B%20%2F%208B-6f42c1?style=flat-square)](https://huggingface.co/Qwen/Qwen3-8B-GGUF)
[![Runtime](https://img.shields.io/badge/llama.cpp-v0.3.0-173f35?style=flat-square)](https://github.com/ggml-org/llama.cpp)
[![Target](https://img.shields.io/badge/Raspberry%20Pi%205-8GB-C51A4A?style=flat-square&logo=raspberrypi)](https://www.raspberrypi.com/products/raspberry-pi-5/)
[![Sponsor](https://img.shields.io/badge/Sponsor-lachlanchen-EA4AAA?style=flat-square&logo=githubsponsors&logoColor=white)](https://github.com/sponsors/lachlanchen)

Local Knowledge Terminal(LKT)은 비공개 도서 컬렉션을 출처가 표시된 다국어 카드로
바꿉니다. 첫 라이브러리는 구조화된 **Word Origins**, **The Book of Answers**,
**The Book of Questions**, **English Root Dictionary**, **English Affix Dictionary**를
결합합니다. Qwen3-4B Q4_K_M은 8 GB Raspberry Pi 5에서 로컬로 실행되며, 더 느린
Qwen3-8B 프로필도 선택할 수 있습니다. 검색, 추론, 기록, 브라우저 GUI는 클라우드 API
없이 작동합니다.

## 하나의 컬렉션으로 시험하기

범위가 분명한 비공개 도서 또는 사전 컬렉션이 있다면,
[초기 USD 250 컬렉션 적합성 스프린트](https://lazying.art/lkt/)는 무료 적합성 확인으로
시작합니다. 컬렉션 하나, 언어 목표 하나, 기존 기기 한 대를 대상으로 하며, 데이터·개인정보·
인용 지도, 합의된 대표 표본(최대 소스 단위 12개와 시험 질문 20개), 자료를 사용할 수 있을
때 출처가 표시된 브라우저 카드 최대 2개, 진행 여부 권고, 사실 관계 수정 1회를 제공합니다.
결제 전에 서면 범위에서 소스 단위(예: 구절, 레코드 또는 대표 페이지)를 정의합니다.
하드웨어, 배송, 맞춤 OCR, 대량 변환, 프로덕션 배포, 지속 지원은 이 고정 범위에 포함되지
않습니다.

고객 자료를 공유하지 않고 세 가지 산출물이 실제로 어떤 모습인지 확인하려면
[컬렉션 적합성 샘플 보고서](../docs/sample-fit-report.md)를 읽어 보세요. LKT가 자체 문서화한
참조 컬렉션에 같은 형식을 적용한 것으로, 고객 성과나 유료 프로젝트 실적을 뜻하지 않습니다.

## 독립적인 여섯 경험, 하나의 카드 계약

- **Word Origin**은 전용 단일 항목 검색기와 프롬프트를 사용해 범위가 제한된 대화형 방향성
  어원 그래프를 만듭니다. 형태소 분기를 보존하고 책이 뒷받침하는 노드와 모델이 보완한
  언어학적 맥락을 명확히 구분합니다.
- **Word Card**는 관련 Word Origins 항목 여러 개를 검색해 간결한 다국어 기억 보기를
  구성합니다. 영어, 일본어, 중국어는 고정하고 네 번째 패널에서 프랑스어와 아랍어를 번갈아
  표시합니다.
- **Book Answer**는 검토된 카드 318개 중에서 재현 가능한 추첨을 하고, 출판된 답변 번역을
  보존하며 성찰 메모를 덧붙입니다.
- **Book Question**은 검토된 질문 291개를 주제로 검색하고 어휘 일치가 없으면 재현 가능한
  추첨으로 전환합니다.
- **Root Graph**는 내용이 있는 어근 레코드 4,018개를 우선한 뒤 정확히 뒷받침하는 접사 항목을
  사용해 재귀적인 어족 그래프를 저장합니다.
- **Affix Graph**는 내용이 있는 접사 레코드 5,179개와 Root Dictionary 사이에서 그 우선순위를
  뒤집으면서도 중심 단어의 완전한 그래프 하나를 유지합니다.

각 모드에는 고유한 검색 정책과 엄격한 모델 프롬프트가 있습니다. Word Origin과 Word Card는
같은 Word Origins 인덱스를 의도적으로 공유하지만 표현 방식은 다릅니다. Answer와 Question은
서로 다른 책과 검색 엔진을 사용합니다. 여섯 모드는 모두 동일한 버전 관리 카드 JSON을 만듭니다.
일본어 카드 책 텍스트는 토큰 단위 후리가나를 유지하고, 중국어 보기에는 결정론적인 성조 부호
병음을 붙입니다. 현재는 웹 GUI가 이 JSON을 렌더링하며, 나중에는 코퍼스·검색·모델 코드를
바꾸지 않고 전자잉크와 오디오 어댑터도 이를 사용합니다.

별도의 **Chat / Benchmark** 작업 공간은 Qwen과 직접 대화하며 경과 시간, 프롬프트/출력 토큰,
생성 속도를 보고합니다. 이는 인용되지 않은 원시 모델 출력으로 명확히 표시되며, 책에 근거한
카드로 저장되지 않습니다. 관측값은 로컬 지식 원장의 별도 테이블에 보관됩니다. 같은 프롬프트를
반복해도 매번 Qwen을 다시 실행합니다. 원장은 기록이지 캐시가 아닙니다. 어느 카드에서든
**Discuss this card**를 선택하면 저장된 카드와 검색된 발췌문을 제한된 맥락으로 삼아 Model Lab을
엽니다. 각 Model Lab 세션에는 영속적인 질의 스레드도 부여됩니다. 이어지는 턴은 부모/자식 계보를
유지합니다. 카드 토론은 정규화된 소스 콘텐츠 원자와 연결되며 Qwen 응답은 계속 인용되지 않은
것으로 명시됩니다.

## 제품 화면

브라우저는 채팅 대시보드가 아니라 편집된 카드 무대입니다. 보이는 모든 슬라이드는 큰 핵심 생각과
간결한 출처 하나를 담은, 스크롤 없는 한 화면 구성입니다. Word Origin은 중앙에 Cytoscape.js
방향성 그래프를 둡니다. Word Card는 큰 영어 단어/IPA 아래에 일본어와 중국어를 고정하고
프랑스어/아랍어 패널을 전환합니다. Answer와 Question은 영어, 일본어 루비, 중국어 병음 루비로
이루어진 내부 언어 캐러셀을 사용하며, 유난히 긴 문장은 읽기 쉬운 추가 슬라이드로 나눕니다.
승인된 로컬 문법 분석은 똑같은 텍스트에 은은한 역할 색만 더하며 범례나 복잡한 메타데이터는
추가하지 않습니다. 저장된 카드는 모드별로 독립된 바깥 캐러셀을 만들고 이전/다음 조작을 제공합니다.
Root, Affix, Word Origin은 하나의 Cytoscape 그래프 렌더러를 공유합니다. 완전한 저장 그래프,
모서리 개요 지도, 어근·접두사·접미사·역사 분기로 확대하는 내부 초점 슬라이드를 그래프 중복 없이
보여 줍니다. 전체 화면 표시 모드는 모든 애플리케이션 크롬을 숨기며, `/?display=1`은 같은 카드
문서를 키오스크 친화적인 화면 표면으로 엽니다. 인쇄 CSS와 버전 관리 카드 JSON은 향후 전자잉크
렌더링을 위한 경계를 명확히 합니다.

### Raspberry Pi 실시간 화면

Word Origin은 내용 크기에 맞춘 노드, 완전한 계보 그래프, 다국어 의미 패널, 분기 슬라이드,
원클릭 최적 맞춤 재설정을 제공합니다.

![Raspberry Pi에서 실행 중인 Word Origin 그래프](../docs/assets/word-origin.png)

Word Card는 영어 단어와 발음을 크게 유지하면서 안정적인 일본어·중국어 대형 패널을 전환되는
프랑스어/아랍어 패널 옆에 보여 줍니다.

![Raspberry Pi에서 실행 중인 다국어 Word Card](../docs/assets/word-card.png)

생성된 모든 카드는 새 ID를 받고 카드 원장에 남습니다. 별도의 정규화된 `knowledge.sqlite3`
데이터베이스는 승인된 용어, 의미, 발음, 음소/문자소 구간, 형태소, 역사, 번역, 문법, 출처 계보,
개정, 질의 계보를 재사용 가능한 원자로 저장합니다. 카드는 이 원자들로 재구성할 수 있는 보기입니다.
LadybugDB 속성 그래프는 파생된 탐색 투영이며 언제든 SQLite에서 다시 만들 수 있습니다.
승인된 Book Answer와 Book Question 카드도 검토된 영어·일본어·중국어 원문을 이 정규화 저장소에
둡니다. 각 언어는 검색이 소유한 도서 인용에 연결된 독립 콘텐츠 원자이며, 모델의 성찰은 도서 근거에서
의도적으로 제외됩니다. Qwen은 언어마다 별도의 제한 작업으로 분할합니다. 순서가 지정된 부분이 검토된
문장을 문자 단위까지 정확히 재구성할 때만 결과를 승인합니다. 승인된 부분, 근거 링크, 모델 개정,
대체된 분석은 표시 전용 마크업이 아니라 재사용 가능한 지식으로 남습니다.

### 구절에서 출처 계보까지의 증명

[PocketPolyglot 구절 예제](../examples/artifacts/pocketpolyglot-passage-graph.json)는 프로젝트가
작성하고 정렬한 구절 하나를 작고 수동 검토된 개념 그래프로 바꿉니다. 모든 관계는 LKT의 프로덕션
지식 API를 통해 정확한 구절 단위, 발췌문, 고정된 소스 파일 해시까지 해석됩니다. 다시 만들거나
커밋된 산출물이 최신인지 검증하세요.

```bash
python examples/pocketpolyglot_passage_graph.py
python examples/pocketpolyglot_passage_graph.py --check
```

### 대본형 이중 언어 회의 증명

[이중 언어 회의 예제](../examples/artifacts/scripted-bilingual-meeting-knowledge.json)는 개별 타임스탬프가
있는 영어·중국어 발화 10개를 형식이 지정되고 수동 검토된 지식 단위 10개에 대응시킵니다. 각 단위는
화자, 타임스탬프, 정확한 트랜스크립트 문자 범위, 소스 파일 해시, 근거가 뒷받침하는 그래프 관계를
유지합니다. 검토 원장에는 수정 1건이 포함되며, 그 이전 버전은 실제 `KnowledgeStore` 산출물 수명
주기를 통해 superseded 상태로 보존됩니다. 같은 산출물에는 한 단위에서 정확한 소스 단어까지 따라갈
수 있는 [대화형 브라우저 증명](https://lazying.art/meeting-intelligence/)도 있습니다.

트랜스크립트와 타이밍은 프로젝트 소유의 대본형 픽스처입니다. 이는 ASR, 화자 분리, 추출, 번역의
정확도 벤치마크가 아니며 고객 배포나 고객 결과도 아닙니다. 이식 가능한 JSON을 다시 만들거나 검증하세요.

```bash
python examples/scripted_bilingual_meeting_knowledge.py
python examples/scripted_bilingual_meeting_knowledge.py --check
```

준비 과정은 근거 검색, 의미 하나 준비, 구성 요소 분할, 각 기원 분기의 재귀적 확장, 언어/발음별 독립
준비, 검증, 조합으로 이루어진 작고 의존성을 인식하는 작업을 사용합니다. 성공한 단계는 즉시 체크포인트로
저장되므로 약한 언어나 분기 하나만 나머지를 버리지 않고 다시 시도할 수 있습니다.

설치된 저우선순위 워커는 보이는 여섯 덱을 균형 잡힌 라운드로 확장합니다. Question과 Answer는 각자의
검토된 책에서 뽑고, Word Card와 Word Origin은 하나의 제한된 원자형 단어 조사를 공유합니다. Root와
Affix는 각자의 다듬어진 사전에서 독립적으로 뽑고, 다른 형태론 책과 제한된 Word Origins 일치를 관련
보조 RAG로 사용합니다. 항상 보이는 모드 중 카드 수가 가장 적은 모드를 선택하므로 빠른 경로 하나가
다른 경로보다 크게 앞서갈 수 없습니다. 따라잡는 동안에는 새 Question/Answer 추첨을 멈추고 완료되지
않은 자율 어휘 주제를 한 번에 최대 하나만 유지합니다. 선택적 보강이 대기 중이어도 균형 검사는 제한된
간격으로 실행되며, 어휘 작업은 도서 문법 보강보다 먼저 가져옵니다.

처음 보는 모든 소스는 평소의 로컬 Qwen, RAG, 공개 관문을 거칩니다. 안정적인 소스 및 용어 ID가
재시작 사이의 중복을 막습니다. 원자형 단어 분석은 단어에 실제로 어근/접사가 있을 때 Root/Affix 보기를
파생할 수 있지만, 독립적인 Root/Affix 도서 순회는 선택한 단어에 생산적인 접사가 없어도 두 제품이
계속 성장하도록 보장합니다. 탭 균형을 맞추기 위해 구성 요소를 꾸며 내지 않습니다.

Root/Affix 준비는 비용이 큰 작업을 재개 가능한 두 번의 로컬 호출로 나눕니다. 먼저 그래프/역사,
그다음 작은 다국어 표현입니다. 그래프는 1,200토큰(새 복구 한 번은 1,400), 언어 호출은 512토큰
(복구 시 640)을 상한으로 합니다. 잘린 JSON 응답을 재귀적으로 Qwen에 다시 넣지 않습니다. 검증된
각 단계는 모델과 정확한 근거 지문과 함께 저장되므로 이후 단계 실패가 그래프를 낭비하지 않습니다.

기본 브라우저는 Question에서 시작하고, 각 카드가 모든 내부 슬라이드를 완료할 때마다 Answer →
Word Card → Word Origin → Root → Affix 순으로 진행합니다. 표시 설정은 이 표준 순서를 유지하면서
모드 하나 또는 일부를 선택할 수 있고, 기본값은 여섯 모드 전체입니다. 저장 카드는 선택한 모드 안에서
기본적으로 섞이며 안정적인 최신순 옵션도 있습니다. 각 모드는 승인된 카드만으로 독립적인 무작위 순회를
소유하므로 탭을 건너도 컬렉션이 합쳐지지 않고 방문할 때마다 같은 카드가 반복되지 않습니다. 새로 승인된
카드는 해당 모드에 남은 순회의 맨 앞에 놓입니다. 명시적인 탭이나 `?mode=` URL은 해당 모드에 머물며,
포인터·터치·키보드 활동은 주변 움직임이 재개되기 전에 현재 카드의 전체 체류 시간을 다시 시작합니다.

이 소유권 경계는 의도적입니다. 검토된 책 문장, 번역, 인용은 로컬 코퍼스 레코드에서 나오며 모델이
다시 쓰지 않습니다. 새로운 설명이나 어휘 데이터는 SQLite에 손으로 입력하는 대신 설정된 로컬 모델이
생성합니다. 나쁜 초안은 보이는 덱 밖에 남습니다. 사전 후보와 결정론적 발음/루비 역시 수작업 카드
데이터가 아니라 로컬 검색/도구 출력입니다. 선택한 의미에 대해 OMW에 아랍어 표제어가 없을 때 FreeDict가
정확한 영어-아랍어 수정 관문을 제공합니다. Qwen은 검색된 후보 하나를 그대로 복사해야 하며 시스템은
검증 뒤 그 후보의 근거 ID를 붙입니다. 고정된 전체 JMdict 릴리스는 일본어 표기와 읽기를 로컬에서
검사합니다. 정확한 읽기는 모델 호출이 필요 없고, 고유한 수정은 결정론적이며, 실제로 모호한 표기만
검색된 읽기로 제한된 작은 Qwen 선택을 받습니다. `/api/health`는 두 압축 수정 인덱스를 필수 소스로
취급하고 준비 상태, 버전, 해시, 항목 수를 보고합니다.
Raspberry Pi가 현재 저전압, 스로틀링 또는 고온 상태이면 자율 생성을 멈추고 상태가 해소되면 재개합니다.
웹 클라이언트는 선택 모드 전체(승인 카드 최대 1,000개)를 불러와 최신 카드를 앞에 두고 나머지는 캐러셀
순회마다 한 번 섞습니다. 현재 화면을 방해하지 않고 승인된 카드를 폴링하며 새로 게시된 결과를 다음에
삽입합니다. 간결한 상태와 `/api/health`는 작업을 예약하지 않으면서 한정된 도서 범위 및 어휘 계획/승인
진행 상황을 모두 보고합니다. 대화형 단어 요청은 즉시 처리되며 자율 준비와 같은 영속 원자를 재사용합니다.

획득 지식 바닥글은 최대 18개의 카드 점이 움직이는 창을 렌더링합니다. 화살표 탐색과 정확한
`current / total` 카운터는 제한 없이 저장된 덱 전체를 계속 다룹니다. Question/Answer 언어 점은
현재 카드에만 속하며 카드가 바뀌면 교체됩니다.

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

## 근거 규칙

언어 모델은 설명과 부족한 언어 보조 정보를 작성하지만 인용 목록은 쓰지 않습니다. LKT는 항목 ID,
발췌문, 절, 페이지 번호, 디지털 위치, 검토된 카드 도서 번역을 검색 레코드에서 직접 붙입니다.
Word Origin은 신뢰할 수 있는 언어학적 맥락을 더할 수 있지만 모든 그래프 노드는 책 기준점과 모델
지식 중 어디에서 왔는지 기록합니다. 설정된 책에 근거가 없으면 앱은 카드를 생성하지 않습니다.

## 저장소 구성

| 경로 | 역할 |
| --- | --- |
| `lkt/corpus.py` | Word Origins 수집, 원자형 SQLite 인덱스, 정확 일치 + FTS 검색 |
| `lkt/morphology.py` | Root/Affix 정제 JSONL 수집, 출처 계보, 정확 일치 + FTS 검색 |
| `lkt/card_books.py` | 다국어 Answer/Question 수집, 검색, 결정론적 추첨 |
| `lkt/deck.py` | 책과 어휘를 번갈아 한 번에 하나씩 준비 |
| `lkt/device.py` | 백그라운드 추론을 위한 Pi 전원/온도 준비 관문 |
| `lkt/retrieval.py` | 독립된 Word Origin, Word Card, Answer, Question RAG 정책 |
| `lkt/llm.py` | 작은 llama.cpp 어댑터와 경험별 엄격한 프롬프트 |
| `lkt/service.py` | 카드 조합과 정규화 |
| `lkt/pronunciation.py` | 결정론적 병음/루비와 버전 관리 오프라인 IPA |
| `lkt/store.py` | 버전 관리 카드, 준비 산출물, 개정, 보관, 채팅 원장 |
| `lkt/knowledge.py` | 원자형 확립 지식, 근거, 작업, 개정, 질의 계보 |
| `lkt/preparation.py` | 의존성을 인식한 분할 정복 단어/콘텐츠 계획 |
| `lkt/atomic.py` | 제한된 원자형 준비와 결정론적 카드 조립 |
| `lkt/graph.py` | 승인된 SQLite 원자로 재구축 가능한 LadybugDB 탐색 투영 |
| `lkt/lexicon.py` | 압축 다국어 WordNet 수정 근거 |
| `lkt/freedict.py` | 정확한 FreeDict 영어-아랍어 수집 및 수정 검색 |
| `lkt/jmdict.py` | 전체 JMdict 정확 표기 읽기 인덱스와 출처 계보 |
| `lkt/web.py` | 의존성 없는 HTTP API와 GUI 서버 |
| `lkt/outputs.py` | 안정적인 웹/전자잉크/오디오 출력 경계 |
| `lkt/static/` | 데스크톱급 GUI, 향후 키오스크에 충분한 반응형 구성 |
| `scripts/` | 재현 가능한 Pi 런타임, 설치, 업데이트, 스모크 테스트 도구 |
| `systemd/` | 강화된 모델 및 애플리케이션 서비스 |
| `docs/lineage.md` | 레거시 프로젝트와 코퍼스의 정확한 계보 |
| `docs/product-brief.md` | 지속적인 소유자 요구 사항과 승인 기준 |
| `docs/knowledge-architecture.md` | 원자형 SQLite, 그래프 투영, 단계별 준비 계약 |
| `docs/owner-request-log.md` | 개인정보를 제거한 소유자 지시의 시간순 기록 |
| `docs/voice-hardware.md` | 지원 마이크 선택과 단계별 오디오 시험 |
| `docs/mode-roadmap.md` | 향후 접미사, 접사, 어근 도서 확장 계획 |

## 로컬 개발

작은 고정 버전 발음 의존성을 설치한 다음 테스트 모음을 실행합니다.

```powershell
cd C:\Users\Administrator\Projects\LocalKnowledge
python -m pip install -e .
python -m unittest discover -s tests -v
python -m compileall -q lkt tests
```

구조화된 도서 내보내기에서 로컬 인덱스를 만듭니다.

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

llama.cpp 서버가 포트 8081에서 수신 대기 중인 경우:

```powershell
python -m lkt.cli generate abacus --mode word
python -m lkt.cli generate "Should I begin?" --mode answer
python -m lkt.cli generate technology --mode question
python -m lkt.cli generate inspection --mode root
python -m lkt.cli generate abnormal --mode affix
python -m lkt.cli serve
```

<http://127.0.0.1:8090>을 엽니다.

## Raspberry Pi 5 구성

```text
/home/lachlan/LocalKnowledgeTerminal/
├── source/      # this Git repository; updated with fetch + fast-forward pull
├── runtime/     # pinned llama.cpp plus optional knowledge Python environment
├── models/      # Qwen GGUF (not committed)
├── data/        # corpus index and saved cards (not committed)
└── logs/        # bootstrap logs (not committed)
```

고정 런타임 산출물:

| 산출물 | 리비전 | 무결성 |
| --- | --- | --- |
| llama.cpp | `v0.3.0` / `c1d0e7a004015f23bc0233470b747b596f29b264` | 커밋으로 고정된 소스 아카이브 |
| Qwen3-4B-GGUF | `bc640142c66e1fdd12af0bd68f40445458f3869b` | Q4_K_M SHA-256 `7485fe6f…534fdf5` |
| 모델 파일 | `Qwen3-4B-Q4_K_M.gguf` | 2,497,280,256바이트 |

Pi 서비스는 추론 슬롯 하나(`--parallel 1`)를 노출합니다. 따라서 카드 조합과 Model Lab 요청은
순차 처리되어, 네 CPU 코어가 작업 사이에서 경쟁하지 않고 메모리 사용량과 지연 시간을 예측 가능하게
유지합니다.

Qwen3-8B는 선택 가능한 품질 우선 준비 모델로 사용할 수 있음이 입증되었습니다. 배포된 Pi에서
120토큰 다국어 프로브를 초당 1.78토큰, 약 6.28 GiB RSS, 사용 가능한 시스템 메모리 1.85 GiB,
현재 열 스로틀링 없음 상태로 생성했습니다. Qwen3-4B는 반응성이 좋은 오프라인 기본값입니다.
모델 선택은 명시적이며 되돌릴 수 있습니다.

```bash
tmux new-session -d -s lkt-8b-download \
  './scripts/download_qwen3_8b.sh > ../logs/qwen3-8b-download.log 2>&1'
sudo ./scripts/select_model.sh status
sudo ./scripts/select_model.sh 8b
sudo ./scripts/select_model.sh 4b
sudo ./scripts/benchmark_models.sh
```

한 번에 모델 하나만 불러옵니다. 기본 4B 프로필은 3,072토큰 컨텍스트를 사용하고, 선택형 8B 프로필은
8 GB 메모리 경계를 지키기 위해 2,048토큰 컨텍스트와 더 작은 배치를 사용합니다. 서버가 정상 상태가
되지 않으면 `select_model.sh 8b`가 4B 프로필을 자동 복원합니다. 다운로더는 부분 전송을 재개하고
공식 SHA-256을 검증한 다음에만 최종 GGUF를 원자적으로 노출합니다. 벤치마크는 한 번에 모델 하나를
활성화해 같은 제한된 다국어 품질/속도 프로브를 실행하고, 경과 시간, llama.cpp 토큰 속도, 프로세스
메모리를 기록한 뒤 벤치마크 전 활성 모델을 복원합니다.

간결한 선택형 지식 런타임을 설치하고 그래프 투영을 만듭니다.

```bash
./scripts/install_knowledge_runtime.sh
./scripts/rebuild_graph.sh
```

이 과정은 로컬 IPA를 위해 eSpeak NG를 설치하고, 격리 환경에 LadybugDB 0.19.1과 Wn 1.1.1을
고정한 뒤 OMW 2.0 영어·일본어·중국어·프랑스어·아랍어 어휘집만 설치합니다. 또한 고정된 전체 JMdict
아카이브를 검증하고 정확 표기 읽기 인덱스를 만든 다음 원시 다운로드를 삭제합니다. 전체 Wiktionary
덤프는 의도적으로 제외합니다. IPA 추출은 조용한 텍스트 모드를 사용하며 음성 출력을 켜지 않습니다.

Pi에서:

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

이후 Windows → GitHub → Pi 개발에서는:

```bash
cd /home/lachlan/LocalKnowledgeTerminal/source
./scripts/update_pi_tmux.sh
tmux attach -t lkt-update
```

tmux 래퍼는 SSH나 브라우저 전환 후에도 배포를 계속하고 `~/LocalKnowledgeTerminal/logs/update-pi.log`에
기록합니다. 그 아래의 멱등적 `scripts/install_services.sh`는 systemd 유닛 세 개를 모두 설치하고 부팅
시 활성화하며 model → web → worker 순서로 시작하고 두 상태 확인 엔드포인트를 검증한 다음 그래픽 자동
시작 항목을 설치합니다. `scripts/update_pi.sh`는 서비스 설치 프로그램을 `--restart`로 호출하기 전에
전체 테스트 관문을 실행합니다.

그런 다음 Pi의 VNC 데스크톱에서 `http://127.0.0.1:8090`을 열거나, 신뢰할 수 있는 로컬 네트워크에서
`http://<pi-lan-address>:8090`을 엽니다.

설치 프로그램은 `desktop/lkt-kiosk.desktop`도 Pi 사용자의 XDG 자동 시작 디렉터리에 놓고,
`scripts/open_kiosk.sh`를 `/usr/local/bin/lkt-open-kiosk`로 설치합니다. 다음 그래픽 로그인에서
런처는 로컬 상태 확인 엔드포인트를 기다린 뒤 `http://127.0.0.1:8090/?display`에 전용 Chromium
프로필을 정확히 하나 엽니다. 런처를 다시 실행해도 안전합니다. 해당 프로필을 감지해 다른 창을 열지
않습니다. Chromium은 잠긴 키오스크가 아닌 일반 전체 화면 앱으로 시작하므로 **Esc**를 누르면 전체 화면을
나가 조작 가능한 Pi 데스크톱으로 돌아옵니다. 의도적인 VNC 사용에는 명시적 모드 URL도 계속 사용할 수
있습니다.

## 데이터와 저작권

도서 PDF, 추출 코퍼스, 모델 가중치, 생성 인덱스, 저장 카드는 의도적으로 Git에서 제외합니다.
설치 중 합법적으로 확보한 로컬 JSONL 내보내기를 제공하세요. 생성 카드를 정확한 코퍼스 빌드까지
추적할 수 있도록 LKT는 각 SHA-256을 SQLite 인덱스에 기록합니다. 검증된 참조 세트는
[`docs/corpora.md`](../docs/corpora.md)를 참조하세요.

## 계보

LKT는 [`WordsCardEink`](https://github.com/lachlanchen/WordsCardEink)와
[`WordOrigins`](https://github.com/lachlanchen/WordOrigins)에서 영감을 받은 깔끔한 로컬 우선 후속
프로젝트입니다. 두 프로젝트의 단일형 런타임이나 하드웨어 의존성을 가져오지 않습니다. 고정 커밋과
계승한 아이디어는 [`docs/lineage.md`](../docs/lineage.md)를 참조하세요.

## 후원

| Donate | PayPal | Stripe |
| --- | --- | --- |
| [![Donate](https://img.shields.io/badge/Donate-LazyingArt-0EA5E9?style=for-the-badge&logo=ko-fi&logoColor=white)](https://chat.lazying.art/donate) | [![PayPal](https://img.shields.io/badge/PayPal-RongzhouChen-00457C?style=for-the-badge&logo=paypal&logoColor=white)](https://paypal.me/RongzhouChen) | [![Stripe](https://img.shields.io/badge/Stripe-Donate-635BFF?style=for-the-badge&logo=stripe&logoColor=white)](https://buy.stripe.com/aFadR8gIaflgfQV6T4fw400) |

## 인용

LKT가 작업에 도움이 되었다면 [`CITATION.cff`](../CITATION.cff)를 읽는 GitHub의
**Cite this repository** 메뉴 또는 다음 형식을 사용해 인용하세요.

```bibtex
@software{chen_local_knowledge_terminal_2026,
  author = {Chen, Lachlan},
  title = {Local Knowledge Terminal},
  year = {2026},
  version = {0.1.0},
  url = {https://github.com/lachlanchen/LocalKnowledgeTerminal}
}
```
