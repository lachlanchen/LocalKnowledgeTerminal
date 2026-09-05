[English](../README.md) · [العربية](README.ar.md) · [Español](README.es.md) · [Français](README.fr.md) · [日本語](README.ja.md) · [한국어](README.ko.md) · [Tiếng Việt](README.vi.md) · [中文 (简体)](README.zh-Hans.md) · [中文（繁體）](README.zh-Hant.md) · [Deutsch](README.de.md) · [Русский](README.ru.md)

[![LazyingArt banner — Local Knowledge Terminal](../docs/assets/banner.svg)](../docs/assets/banner.svg)

# Local Knowledge Terminal

**Private, buchgestützte Intelligenz auf der eigenen Hardware.**

[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![Model](https://img.shields.io/badge/Qwen3-4B%20%2F%208B-6f42c1?style=flat-square)](https://huggingface.co/Qwen/Qwen3-8B-GGUF)
[![Runtime](https://img.shields.io/badge/llama.cpp-v0.3.0-173f35?style=flat-square)](https://github.com/ggml-org/llama.cpp)
[![Target](https://img.shields.io/badge/Raspberry%20Pi%205-8GB-C51A4A?style=flat-square&logo=raspberrypi)](https://www.raspberrypi.com/products/raspberry-pi-5/)
[![Sponsor](https://img.shields.io/badge/Sponsor-lachlanchen-EA4AAA?style=flat-square&logo=githubsponsors&logoColor=white)](https://github.com/sponsors/lachlanchen)

Local Knowledge Terminal (LKT) verwandelt eine private Büchersammlung in
belegte, mehrsprachige Karten. Die erste Bibliothek vereint strukturierte
Ausgaben von **Word Origins**, **The Book of Answers**, **The Book of Questions**,
einem **English Root Dictionary** und einem **English Affix Dictionary**.
Qwen3-4B Q4_K_M läuft lokal auf einem Raspberry Pi 5 mit 8 GB; Qwen3-8B steht als
optionales, langsameres Profil zur Verfügung. Abruf, Inferenz, Verlauf und
Browseroberfläche arbeiten ohne Cloud-API.

## Mit einer Sammlung ausprobieren

Wer bereits eine klar begrenzte private Buch- oder Wörterbuchsammlung besitzt,
kann mit dem
[Gründungs-Sprint zur Eignungsprüfung für 250 USD](https://lazying.art/lkt/)
beginnen, der eine kostenlose Vorprüfung umfasst. Er gilt für eine Sammlung,
ein Sprachziel und einen vorhandenen Rechner. Geliefert werden anschließend eine
Daten-, Datenschutz-, Zitations- und Provenienzkarte, eine vereinbarte Stichprobe
mit höchstens 12 Quelleinheiten und 20 Testfragen, bei verwertbarem Material bis
zu zwei belegte Browserkarten, eine klare Go-/No-Go-Empfehlung und eine sachliche
Korrekturrunde. Vor der Zahlung definiert der schriftliche Umfang die
Quelleinheit—etwa eine Passage, einen Datensatz oder eine repräsentative Seite.
Hardware, Versand, individuelles OCR, Massenkonvertierung,
Produktivbereitstellung und laufende Betreuung sind in diesem Festumfang nicht
enthalten.

Wie diese drei Ergebnisse konkret aussehen, lässt sich ohne Offenlegung von
Kundenmaterial im
[Beispielbericht zur Sammlungseignung](../docs/sample-fit-report.md) nachlesen.
Er wendet das Format auf LKTs eigene dokumentierte Referenzsammlung an und ist
ausdrücklich weder ein Kundenergebnis noch eine Behauptung über einen bezahlten
Auftrag.

## Sechs eigenständige Erlebnisse, ein Kartenvertrag

- **Word Origin** verwendet einen eigenen Eintrags-Retriever und Prompt für
  einen begrenzten, interaktiven und gerichteten Abstammungsgraphen. Verzweigte
  Morpheme bleiben erhalten; buchgestützte Knoten und vom Modell gelieferter
  linguistischer Kontext werden sichtbar unterschieden.
- **Word Card** ruft mehrere passende Word-Origins-Einträge ab und erstellt eine
  kompakte mehrsprachige Gedächtnisansicht. Englisch, Japanisch und Chinesisch
  bleiben fest, während Französisch und Arabisch in einem vierten Feld wechseln.
- **Book Answer** zieht reproduzierbar aus 318 geprüften Karten, bewahrt die
  veröffentlichten Übersetzungen der Antwort und ergänzt eine Reflexionsnotiz.
- **Book Question** durchsucht 291 geprüfte Fragen nach Themen und greift auf
  eine reproduzierbare Ziehung zurück, wenn keine lexikalische Übereinstimmung
  vorliegt.
- **Root Graph** priorisiert 4.018 inhaltstragende Wurzeleinträge, anschließend
  exakt passende unterstützende Affixeinträge, und speichert einen rekursiven
  Wortfamiliengraphen.
- **Affix Graph** kehrt diese Priorität über 5.179 inhaltstragende Affixeinträge
  und das Root Dictionary um, behält dabei aber einen vollständigen Graphen für
  das zentrale Wort.

Jeder Modus besitzt eine eigene Abrufrichtlinie und einen strikten Modellprompt.
Word Origin und Word Card teilen bewusst denselben Word-Origins-Index, stellen
ihn aber unterschiedlich dar; Answer und Question verwenden getrennte Bücher
und Abrufsysteme. Alle sechs Modi erzeugen dasselbe versionierte Karten-JSON.
Japanischer Kartenbuchtext behält Furigana auf Tokenebene, chinesische Ansichten
erhalten deterministisches, vollständiges Pinyin mit Tonzeichen. Die
Weboberfläche stellt dieses JSON heute dar; E-Ink- und Audioadapter werden es
später nutzen, ohne Korpus-, Abruf- oder Modellcode zu verändern.

Ein separater Arbeitsbereich **Chat / Benchmark** spricht direkt mit Qwen und
meldet Laufzeit, Ein- und Ausgabetokens sowie Generierungsgeschwindigkeit. Er ist
deutlich als rohe, unbelegte Modellausgabe gekennzeichnet und wird nie als
buchgestützte Karte gespeichert. Seine Beobachtungen liegen in einer separaten
Tabelle des lokalen Wissensjournals. Jeder wiederholte Prompt führt Qwen erneut
aus; das Journal ist Verlauf, kein Cache. Von jeder Karte aus öffnet **Discuss
this card** das Model Lab mit der gespeicherten Karte und ihrem abgerufenen
Auszug als begrenztem Kontext.
Jede aktive Model-Lab-Sitzung erhält außerdem einen beständigen Anfrage-Thread.
Aufeinanderfolgende Durchgänge bewahren die Eltern-/Kindabstammung; eine
Kartendiskussion verweist auf ihr normalisiertes Quellinhaltsatom, während die
Qwen-Antwort ausdrücklich unbelegt bleibt.

## Produktdarstellung

Der Browser ist eine redaktionelle Kartenbühne und kein Chat-Dashboard. Jede
sichtbare Folie ist eine nicht scrollbare Ein-Bildschirm-Komposition mit einer
großen Kernaussage und einem knappen Quellenbeleg. Word Origin reserviert die
Mitte für einen gerichteten Cytoscape.js-Graphen. Word Card zeigt großes
Englisch/IPA über festen japanischen und chinesischen Feldern sowie einem
wechselnden französisch-arabischen Feld. Answer und Question verwenden ein
inneres Sprachenkarussell—Englisch, japanisches Ruby und chinesisches
Pinyin-Ruby—und teilen ungewöhnlich lange Sätze in zusätzliche lesbare Folien.
Akzeptierte lokale Grammatikanalysen fügen demselben exakten Text dezente
Rollenfarben hinzu; weder Legende noch überladene Metadaten kommen hinzu.
Gespeicherte Karten bilden unabhängige, modusspezifische Außenkarussells mit
Vor-/Zurück-Steuerung.
Root, Affix und Word Origin teilen sich einen Cytoscape-Graphenrenderer: einen
vollständigen gespeicherten Graphen, eine Übersichtskarte in der Ecke und innere
Fokusfolien, die eine Wurzel, ein Präfix, ein Suffix oder einen historischen Ast
vergrößern, ohne den Graphen zu duplizieren.
Der Vollbildmodus blendet die gesamte Anwendungsoberfläche aus, und
`/?display=1` öffnet dasselbe Kartendokument als kioskfreundliche Anzeigefläche.
Druck-CSS und das versionierte Karten-JSON schaffen klare Grenzen für spätere
E-Ink-Darstellung.

### Live-Anzeige auf dem Raspberry Pi

Word Origin nutzt inhaltsabhängig dimensionierte Knoten, einen vollständigen
Abstammungsgraphen, mehrsprachige Bedeutungsfelder, Astfolien und ein
Best-Fit-Zurücksetzen mit einem Klick.

![Live-Word-Origin-Graph auf dem Raspberry Pi](../docs/assets/word-origin.png)

Word Card stellt das englische Wort und seinen Klang in den Vordergrund und
zeigt zugleich große, stabile japanische und chinesische Felder neben dem
wechselnden französisch-arabischen Feld.

![Mehrsprachige Live-Word-Card auf dem Raspberry Pi](../docs/assets/word-card.png)

Jede erzeugte Karte erhält eine neue ID und verbleibt im Kartenjournal. Eine
zweite, normalisierte Datenbank `knowledge.sqlite3` speichert akzeptierte
Begriffe, Bedeutungen, Aussprachen, Phonem-/Graphemsegmente, Morpheme, Geschichte,
Übersetzungen, Grammatik, Provenienz, Revisionen und Anfrageabstammung als
wiederverwendbare Atome. Karten sind rekonstruierbare Ansichten über diesen
Atomen. Ein LadybugDB-Eigenschaftsgraph ist eine abgeleitete Traversalprojektion
und kann jederzeit aus SQLite neu aufgebaut werden.
Akzeptierte Book-Answer- und Book-Question-Karten legen außerdem ihre exakten,
geprüften englischen, japanischen und chinesischen Texte in diesem normalisierten
Speicher ab. Jede Sprache ist ein unabhängiges Inhaltsatom, das mit dem vom Abruf
verwalteten Buchbeleg verknüpft ist; die Modellreflexion wird bewusst von diesem
Buchnachweis ausgeschlossen. Qwen segmentiert jede Sprache in einem eigenen
begrenzten Auftrag. Ein Ergebnis wird nur akzeptiert, wenn seine geordneten Teile
den geprüften Satz Zeichen für Zeichen rekonstruieren; akzeptierte Teile,
Beleglinks, Modellrevision und ersetzte Analysen bleiben wiederverwendbares
Wissen statt reiner Darstellungsmarkierung.

### Passage-zu-Provenienz-Nachweis

Das [PocketPolyglot-Passagenbeispiel](../examples/artifacts/pocketpolyglot-passage-graph.json)
verwandelt eine projekteigene ausgerichtete Passage in einen kleinen, manuell
geprüften Begriffsgraphen. Jede Beziehung wird über LKTs produktive
Wissensschnittstellen bis zur exakten Passageneinheit, zum Auszug und zum
festgeschriebenen Quelldatei-Hash aufgelöst. Erstellen Sie ihn neu oder prüfen
Sie, ob das eingecheckte Artefakt aktuell ist:

```bash
python examples/pocketpolyglot_passage_graph.py
python examples/pocketpolyglot_passage_graph.py --check
```

### Nachweis eines geskripteten zweisprachigen Meetings

Das [zweisprachige Meetingbeispiel](../examples/artifacts/scripted-bilingual-meeting-knowledge.json)
ordnet zehn einzeln mit Zeitstempeln versehene englische und mandarinchinesische
Äußerungen zehn typisierten, manuell geprüften Wissenseinheiten zu. Jede Einheit
bewahrt Sprecher, Zeitstempel, exakte Zeichenspanne im Transkript,
Quelldatei-Hash und beleggestützte Graphbeziehung. Das Revisionsjournal enthält
eine Korrektur, deren frühere Version über den echten `KnowledgeStore`-
Artefaktlebenszyklus als ersetzt erhalten bleibt. Dasselbe Artefakt besitzt
einen [interaktiven Browsernachweis](https://lazying.art/meeting-intelligence/),
mit dem eine Einheit bis zu ihren exakten Quellwörtern verfolgt werden kann.

Transkript und Zeitangaben sind projekteigene geskriptete Fixtures. Dies ist
weder ein Genauigkeitsbenchmark für ASR, Sprechertrennung, Extraktion oder
Übersetzung noch eine Kundenbereitstellung oder ein Kundenergebnis. Erstellen
oder prüfen Sie das portable JSON:

```bash
python examples/scripted_bilingual_meeting_knowledge.py
python examples/scripted_bilingual_meeting_knowledge.py --check
```

Die Vorbereitung nutzt kleine, abhängigkeitsbewusste Aufträge: Belege abrufen,
eine Bedeutung vorbereiten, Bestandteile aufteilen, jeden Ursprungsast rekursiv
erweitern, jede Sprache/Aussprache unabhängig vorbereiten, validieren und danach
zusammensetzen. Erfolgreiche Stufen werden sofort als Prüfpunkte gespeichert;
eine schwache Sprache oder ein schwacher Ast kann erneut versucht werden, ohne
den Rest zu verwerfen.

Der installierte Worker mit niedriger Priorität lässt alle sechs sichtbaren
Decks in ausgewogenen Runden wachsen. Question und Answer ziehen aus ihren
jeweiligen geprüften Büchern; Word Card und Word Origin teilen eine begrenzte
atomare Wortuntersuchung; Root und Affix ziehen unabhängig aus dem jeweils
eigenen überarbeiteten Wörterbuch und nutzen das andere Morphologiebuch sowie
begrenzte Word-Origins-Treffer als passenden Begleit-RAG. Er wählt stets den am
wenigsten gefüllten sichtbaren Modus, sodass kein schneller Pfad anderen weit
vorauseilen kann.
Beim Aufholen pausiert er neue Question-/Answer-Ziehungen und hält höchstens ein
unfertiges autonomes lexikalisches Thema in Bearbeitung. Die Ausgewogenheits-
prüfung läuft in begrenzten Abständen, auch wenn noch optionale Anreicherung
wartet; lexikalische Aufträge werden vor dieser Buchgrammatikanreicherung
beansprucht.

Jede noch ungesehene Quelle durchläuft weiterhin die normalen lokalen Qwen-,
RAG- und Publikationsprüfungen. Stabile Quellen- und Begriffsidentitäten
verhindern Wiederholungen über Neustarts hinweg. Atomare Wortanalyse kann eine
Root-/Affix-Ansicht ableiten, wenn das Wort tatsächlich eines enthält;
unabhängige Root-/Affix-Buchläufe sorgen jedoch dafür, dass diese Produkte auch
dann wachsen, wenn ein gewähltes Wort kein produktives Affix besitzt. Kein
Bestandteil wird nur zum Ausgleich der Tabs erfunden.

Die Root-/Affix-Vorbereitung teilt aufwendige Arbeit in zwei fortsetzbare lokale
Aufrufe: zuerst Graph/Geschichte, dann eine kleine mehrsprachige Präsentation.
Der Graph hat ein höheres Limit von 1.200 Tokens (1.400 für eine frische
Reparatur), der Sprachaufruf verwendet 512 Tokens (640 zur Reparatur). Eine
abgeschnittene JSON-Antwort wird nie rekursiv wieder in Qwen eingespeist. Jede
validierte Stufe wird mit ihrem Modell und exakten Belegfingerabdruck
gespeichert, sodass ein späterer Fehler den Graphen nicht verschwendet.

Der schlichte Browser startet mit Question und fährt mit Answer → Word Card →
Word Origin → Root → Affix fort, nachdem jede Karte alle inneren Folien
durchlaufen hat. Anzeigeeinstellungen können einen einzelnen Modus oder eine
beliebige Teilmenge wählen und behalten dabei diese kanonische Reihenfolge;
standardmäßig sind alle sechs ausgewählt. Gespeicherte Karten werden
standardmäßig innerhalb ihres Modus gemischt, alternativ gibt es eine stabile
Sortierung von neu nach alt. Jeder Modus besitzt einen unabhängigen, nur
akzeptierte Karten umfassenden Mischdurchlauf; Tabwechsel führen daher weder
Sammlungen zusammen noch wiederholen bei jedem Besuch dieselbe Karte. Neu
akzeptierte Karten stehen am Anfang des verbleibenden Durchlaufs ihres Modus.
Ein expliziter Tab oder eine `?mode=`-URL bleibt modusspezifisch, und Zeiger-,
Berührungs- oder Tastaturaktivität startet die volle Verweildauer der aktuellen
Karte neu, bevor die Umgebungsbewegung fortgesetzt wird.

Diese Eigentumsgrenze ist bewusst gesetzt: Geprüfte Buchsätze, Übersetzungen
und Belege stammen aus den lokalen Korpusdatensätzen und werden nie vom Modell
umgeschrieben; neue erklärende oder lexikalische Daten erzeugt das konfigurierte
lokale Modell, sie werden nicht von Hand in SQLite eingegeben. Ein schlechter
Entwurf bleibt außerhalb des sichtbaren Decks. Wörterbuchkandidaten und
deterministische Aussprache/Ruby sind ebenfalls lokale Abruf-/Werkzeugausgaben
statt handgeschriebener Kartendaten. FreeDict stellt eine exakte Englisch-
Arabisch-Korrekturprüfung bereit, wenn OMW für die gewählte Bedeutung kein
arabisches Lemma hat; Qwen muss einen abgerufenen Kandidaten übernehmen, und das
System hängt nach der Validierung dessen Beleg-ID an. Die festgeschriebene
vollständige JMdict-Version prüft japanische Formen und Lesungen lokal: Eine
exakte Lesung kostet keinen Modellaufruf, eine eindeutige Korrektur ist
deterministisch, und nur eine wirklich mehrdeutige Schriftform erhält eine
kleine, auf abgerufene Lesungen beschränkte Qwen-Auswahl. `/api/health` behandelt
beide kompakten Korrekturindizes als erforderliche Quellen und meldet
Bereitschaft, Versionen, Hashes und Eintragszahlen.
Autonome Generierung pausiert bei aktueller Unterspannung, Drosselung oder hoher
Temperatur des Raspberry Pi und setzt nach Behebung des Zustands fort. Der
Webclient lädt den vollständigen gewählten Modus (bis zu 1.000 akzeptierte
Karten), hält die neueste zuerst und mischt alle übrigen einmal pro
Karusselldurchlauf. Er fragt akzeptierte Karten ab, ohne die aktuelle Anzeige zu
unterbrechen, und fügt ein neu veröffentlichtes Ergebnis als Nächstes ein.
Sowohl der kompakte Status als auch `/api/health` melden begrenzte Buchabdeckung
und geplanten/akzeptierten lexikalischen Fortschritt, ohne Arbeit einzuplanen.
Interaktive Wortanfragen bleiben unmittelbar und verwenden dieselben dauerhaft
gespeicherten Atome wie die autonome Vorbereitung.

Die Fußzeile für erworbenes Wissen zeigt ein gleitendes Fenster aus höchstens 18
Kartenpunkten; Pfeilnavigation und der exakte Zähler `current / total` decken
weiterhin das gesamte unbegrenzte gespeicherte Deck ab. Question-/Answer-
Sprachpunkte gehören nur zur aktuellen Karte und werden beim Kartenwechsel
ersetzt.

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

## Belegregel

Das Sprachmodell schreibt Erklärungen und fehlende Sprachhilfen, jedoch niemals
die Belegliste. LKT hängt Eintrags-IDs, Auszüge, Abschnitte, Seitenzahlen,
digitale Fundstellen und geprüfte Kartenbuchübersetzungen direkt aus den
Abrufdatensätzen an. Word Origin darf zuverlässigen linguistischen Kontext
ergänzen, aber jeder Graphknoten hält fest, ob er vom Buchanker oder aus dem
Modellwissen stammt. Enthält das konfigurierte Buch keinen Beleg, erzeugt die
Anwendung keine Karte.

## Repository-Übersicht

| Pfad | Aufgabe |
| --- | --- |
| `lkt/corpus.py` | Word-Origins-Import, atomarer SQLite-Index, exakter + FTS-Abruf |
| `lkt/morphology.py` | Überarbeiteter Root-/Affix-JSONL-Import, Provenienz, exakter + FTS-Abruf |
| `lkt/card_books.py` | Mehrsprachiger Answer-/Question-Import, Suche und deterministische Ziehungen |
| `lkt/deck.py` | Abwechselnde einzelne Buch- und Lexikonvorbereitung |
| `lkt/device.py` | Strom-/Temperatur-Bereitschaftsprüfung des Pi für Hintergrundinferenz |
| `lkt/retrieval.py` | Unabhängige RAG-Richtlinien für Word Origin, Word Card, Answer und Question |
| `lkt/llm.py` | Kleiner llama.cpp-Adapter und ein strikter Prompt je Erlebnis |
| `lkt/service.py` | Kartenzusammenstellung und Normalisierung |
| `lkt/pronunciation.py` | Deterministisches Pinyin/Ruby und versioniertes Offline-IPA |
| `lkt/store.py` | Versionierte Karten, Vorbereitungsartefakte, Revisionen, Archiv und Chatjournal |
| `lkt/knowledge.py` | Atomares gesichertes Wissen, Belege, Aufträge, Revisionen und Anfrageabstammung |
| `lkt/preparation.py` | Abhängigkeitsbewusste Teile-und-herrsche-Planung für Wörter/Inhalte |
| `lkt/atomic.py` | Begrenzte atomare Vorbereitung und deterministische Kartenzusammenstellung |
| `lkt/graph.py` | Aus akzeptierten SQLite-Atomen rekonstruierbare LadybugDB-Traversalprojektion |
| `lkt/lexicon.py` | Kompakte mehrsprachige WordNet-Korrekturbelege |
| `lkt/freedict.py` | Exakter Englisch-Arabisch-FreeDict-Import und Korrekturabruf |
| `lkt/jmdict.py` | Vollständiger JMdict-Index exakter Formen/Lesungen und Provenienz |
| `lkt/web.py` | Abhängigkeitsfreie HTTP-API und GUI-Server |
| `lkt/outputs.py` | Stabile Ausgabegrenze für Web/E-Ink/Audio |
| `lkt/static/` | Desktopgerechte, für spätere Kiosknutzung ausreichend responsive GUI |
| `scripts/` | Reproduzierbare Werkzeuge für Pi-Laufzeit, Installation, Update und Smoke-Test |
| `systemd/` | Gehärtete Modell- und Anwendungsdienste |
| `docs/lineage.md` | Exakte Provenienz des Vorgängerprojekts und Korpus |
| `docs/product-brief.md` | Dauerhafte Eigentümeranforderungen und Abnahmekriterien |
| `docs/knowledge-architecture.md` | Vertrag für atomares SQLite, Graphprojektion und stufenweise Vorbereitung |
| `docs/owner-request-log.md` | Chronologische, datenschutzbereinigte Eigentümervorgaben |
| `docs/voice-hardware.md` | Unterstützte Mikrofonauswahl und gestufte Audiotests |
| `docs/mode-roadmap.md` | Ausbauplan für künftige Suffix-, Affix- und Wurzelbücher |

## Lokale Entwicklung

Installieren Sie die kleine festgeschriebene Ausspracheabhängigkeit und führen
Sie anschließend die Testsuite aus:

```powershell
cd C:\Users\Administrator\Projects\LocalKnowledge
python -m pip install -e .
python -m unittest discover -s tests -v
python -m compileall -q lkt tests
```

Erstellen Sie aus dem strukturierten Buchexport einen lokalen Index:

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

Mit einem llama.cpp-Server auf Port 8081:

```powershell
python -m lkt.cli generate abacus --mode word
python -m lkt.cli generate "Should I begin?" --mode answer
python -m lkt.cli generate technology --mode question
python -m lkt.cli generate inspection --mode root
python -m lkt.cli generate abnormal --mode affix
python -m lkt.cli serve
```

Öffnen Sie <http://127.0.0.1:8090>.

## Raspberry-Pi-5-Struktur

```text
/home/lachlan/LocalKnowledgeTerminal/
├── source/      # this Git repository; updated with fetch + fast-forward pull
├── runtime/     # pinned llama.cpp plus optional knowledge Python environment
├── models/      # Qwen GGUF (not committed)
├── data/        # corpus index and saved cards (not committed)
└── logs/        # bootstrap logs (not committed)
```

Festgeschriebene Laufzeitartefakte:

| Artefakt | Revision | Integrität |
| --- | --- | --- |
| llama.cpp | `v0.3.0` / `c1d0e7a004015f23bc0233470b747b596f29b264` | Auf Commit festgeschriebenes Quellarchiv |
| Qwen3-4B-GGUF | `bc640142c66e1fdd12af0bd68f40445458f3869b` | Q4_K_M SHA-256 `7485fe6f…534fdf5` |
| Modelldatei | `Qwen3-4B-Q4_K_M.gguf` | 2.497.280.256 Byte |

Der Pi-Dienst bietet einen Inferenzplatz (`--parallel 1`). Kartenzusammenstellung
und Model-Lab-Anfragen werden daher nacheinander verarbeitet. Das hält
Speicherverbrauch und Latenz berechenbar, statt vier CPU-Kerne über Aufträge
konkurrieren zu lassen.

Qwen3-8B ist nachweislich als optionales, qualitätsorientiertes
Vorbereitungsmodell einsetzbar. Auf dem bereitgestellten Pi erzeugte es eine
mehrsprachige 120-Token-Probe mit 1,78 Tokens/s, etwa 6,28 GiB RSS, noch 1,85 GiB
freiem Systemspeicher und ohne aktuelle Temperaturdrosselung. Qwen3-4B ist der
reaktionsschnelle Offline-Standard. Die Modellauswahl ist ausdrücklich und
umkehrbar:

```bash
tmux new-session -d -s lkt-8b-download \
  './scripts/download_qwen3_8b.sh > ../logs/qwen3-8b-download.log 2>&1'
sudo ./scripts/select_model.sh status
sudo ./scripts/select_model.sh 8b
sudo ./scripts/select_model.sh 4b
sudo ./scripts/benchmark_models.sh
```

Es wird immer nur ein Modell geladen. Das Standardprofil 4B nutzt einen Kontext
von 3.072 Tokens; das optionale Profil 8B verwendet einen Kontext von 2.048
Tokens und eine kleinere Batchgröße, um die 8-GB-Speichergrenze einzuhalten.
Wird sein Server nicht fehlerfrei, stellt `select_model.sh 8b` automatisch das
4B-Profil wieder her.
Der Downloader setzt eine teilweise Übertragung fort, prüft den offiziellen
SHA-256 und stellt erst dann die endgültige GGUF-Datei atomar bereit.
Der Benchmark aktiviert jeweils ein Modell, führt dieselbe begrenzte
mehrsprachige Qualitäts-/Geschwindigkeitsprobe aus, zeichnet Laufzeit,
llama.cpp-Tokenrate und Prozessspeicher auf und stellt danach das zuvor aktive
Modell wieder her.

Installieren Sie die kompakte optionale Wissenslaufzeit und bauen Sie die
Graphprojektion:

```bash
./scripts/install_knowledge_runtime.sh
./scripts/rebuild_graph.sh
```

Dies installiert eSpeak NG für lokales IPA, schreibt LadybugDB 0.19.1 und Wn
1.1.1 in einer isolierten Umgebung fest und installiert nur die englischen,
japanischen, mandarinchinesischen, französischen und arabischen Lexika von OMW
2.0. Außerdem wird das festgeschriebene vollständige JMdict-Archiv geprüft, der
Index für exakte Schriftformlesungen aufgebaut und der Rohdownload entfernt.
Vollständige Wiktionary-Dumps sind bewusst ausgeschlossen. Die IPA-Extraktion
arbeitet im stillen Textmodus und aktiviert keine Sprachausgabe.

Auf dem Pi:

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

Für die spätere Entwicklung Windows → GitHub → Pi:

```bash
cd /home/lachlan/LocalKnowledgeTerminal/source
./scripts/update_pi_tmux.sh
tmux attach -t lkt-update
```

Die tmux-Hülle hält die Bereitstellung über SSH- oder Browserwechsel hinweg am
Laufen und schreibt `~/LocalKnowledgeTerminal/logs/update-pi.log`. Das zugrunde
liegende idempotente `scripts/install_services.sh` installiert alle drei
systemd-Einheiten, aktiviert sie beim Start, startet sie in der Reihenfolge
Modell → Web → Worker, prüft beide Health-Endpunkte und installiert den
grafischen Autostart-Eintrag. `scripts/update_pi.sh` durchläuft die vollständige
Testsperre, bevor es diesen Dienstinstaller mit `--restart` aufruft.

Öffnen Sie danach `http://127.0.0.1:8090` auf dem VNC-Desktop des Pi oder
`http://<pi-lan-address>:8090` aus dem vertrauenswürdigen lokalen Netz.

Der Installer legt außerdem `desktop/lkt-kiosk.desktop` im XDG-Autostartordner
des Pi-Benutzers ab und installiert `scripts/open_kiosk.sh` als
`/usr/local/bin/lkt-open-kiosk`. Bei der nächsten grafischen Anmeldung wartet
der Starter auf den lokalen Health-Endpunkt und öffnet genau ein eigenes
Chromium-Profil unter `http://127.0.0.1:8090/?display`. Ein erneuter Start ist
harmlos: Das Profil wird erkannt und kein weiteres Fenster geöffnet. Chromium
startet als normale Vollbildanwendung statt als gesperrter Kiosk, daher verlässt
**Esc** den Vollbildmodus und kehrt zum steuerbaren Pi-Desktop zurück.
Ausdrückliche Modus-URLs bleiben für bewusste VNC-Nutzung verfügbar.

## Daten und Urheberrecht

Die Buch-PDFs, extrahierten Korpora, Modellgewichte, erzeugten Indizes und
gespeicherten Karten sind bewusst von Git ausgeschlossen. Stellen Sie bei der
Installation einen rechtmäßig erworbenen lokalen JSONL-Export bereit. LKT
zeichnet jeden SHA-256 im SQLite-Index auf, damit eine erzeugte Karte auf den
exakten Korpusstand zurückgeführt werden kann. Unter
[`docs/corpora.md`](../docs/corpora.md) finden Sie den geprüften Referenzbestand.

## Abstammung

LKT ist ein sauberer, local-first Nachfolger, beeinflusst von
[`WordsCardEink`](https://github.com/lachlanchen/WordsCardEink) und
[`WordOrigins`](https://github.com/lachlanchen/WordOrigins). Deren monolithische
Laufzeit oder Hardwareabhängigkeiten werden nicht übernommen. Die
festgeschriebenen Commits und beibehaltenen Ideen stehen in
[`docs/lineage.md`](../docs/lineage.md).

## Unterstützung

| Donate | PayPal | Stripe |
| --- | --- | --- |
| [![Donate](https://img.shields.io/badge/Donate-LazyingArt-0EA5E9?style=for-the-badge&logo=ko-fi&logoColor=white)](https://chat.lazying.art/donate) | [![PayPal](https://img.shields.io/badge/PayPal-RongzhouChen-00457C?style=for-the-badge&logo=paypal&logoColor=white)](https://paypal.me/RongzhouChen) | [![Stripe](https://img.shields.io/badge/Stripe-Donate-635BFF?style=for-the-badge&logo=stripe&logoColor=white)](https://buy.stripe.com/aFadR8gIaflgfQV6T4fw400) |

## Zitieren

Wenn LKT Ihre Arbeit unterstützt, zitieren Sie es über GitHubs Menü **Cite this
repository**, das [`CITATION.cff`](../CITATION.cff) liest, oder verwenden Sie:

```bibtex
@software{chen_local_knowledge_terminal_2026,
  author = {Chen, Lachlan},
  title = {Local Knowledge Terminal},
  year = {2026},
  version = {0.1.0},
  url = {https://github.com/lachlanchen/LocalKnowledgeTerminal}
}
```
