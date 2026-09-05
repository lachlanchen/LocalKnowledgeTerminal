[English](../README.md) · [العربية](README.ar.md) · [Español](README.es.md) · [Français](README.fr.md) · [日本語](README.ja.md) · [한국어](README.ko.md) · [Tiếng Việt](README.vi.md) · [中文 (简体)](README.zh-Hans.md) · [中文（繁體）](README.zh-Hant.md) · [Deutsch](README.de.md) · [Русский](README.ru.md)

[![LazyingArt banner — Local Knowledge Terminal](../docs/assets/banner.svg)](../docs/assets/banner.svg)

# Local Knowledge Terminal

**Une intelligence privée, fondée sur vos livres et exécutée sur votre matériel.**

[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![Model](https://img.shields.io/badge/Qwen3-4B%20%2F%208B-6f42c1?style=flat-square)](https://huggingface.co/Qwen/Qwen3-8B-GGUF)
[![Runtime](https://img.shields.io/badge/llama.cpp-v0.3.0-173f35?style=flat-square)](https://github.com/ggml-org/llama.cpp)
[![Target](https://img.shields.io/badge/Raspberry%20Pi%205-8GB-C51A4A?style=flat-square&logo=raspberrypi)](https://www.raspberrypi.com/products/raspberry-pi-5/)
[![Sponsor](https://img.shields.io/badge/Sponsor-lachlanchen-EA4AAA?style=flat-square&logo=githubsponsors&logoColor=white)](https://github.com/sponsors/lachlanchen)

Local Knowledge Terminal (LKT) transforme une collection privée de livres en
fiches multilingues accompagnées de citations. Sa première bibliothèque réunit
des éditions structurées de **Word Origins**, **The Book of Answers**, **The Book
of Questions**, d’un **English Root Dictionary** et d’un **English Affix
Dictionary**. Qwen3-4B Q4_K_M s’exécute localement sur un Raspberry Pi 5 de 8 Go,
avec Qwen3-8B comme profil facultatif plus lent ; la recherche, l’inférence,
l’historique et l’interface web fonctionnent sans API cloud.

## Essayez-le avec une collection

Si vous possédez déjà une collection privée et délimitée de livres ou de
dictionnaires, le
[sprint fondateur d’évaluation à 250 USD](https://lazying.art/lkt/) commence par
une vérification d’adéquation gratuite. Il couvre une collection, un objectif
linguistique et une machine existante, puis fournit une cartographie des
données, de la confidentialité, des citations et de la provenance, un
échantillon convenu plafonné à 12 unités sources et 20 questions de test,
jusqu’à deux fiches citées dans le navigateur lorsque les documents sont
exploitables, une recommandation claire de poursuivre ou non et une passe de
correction factuelle. Le périmètre écrit définit l’unité source—par exemple un
passage, une entrée ou une page représentative—avant le paiement.
Le matériel, l’expédition, l’OCR sur mesure, la conversion en masse, le
déploiement en production et le support continu ne font pas partie de ce
périmètre fixe.

Pour voir précisément à quoi ressemblent ces trois livrables sans communiquer
de données client, consultez le
[rapport d’évaluation d’exemple](../docs/sample-fit-report.md). Il applique ce
format à la propre collection de référence documentée de LKT et ne constitue ni
un résultat client ni une affirmation relative à une mission payée.

## Six expériences indépendantes, un même contrat de fiche

- **Word Origin** utilise son propre moteur de recherche à une entrée et son
  propre prompt pour créer un graphe d’ascendance dirigé, interactif et borné.
  Les morphèmes ramifiés sont conservés ; les nœuds étayés par le livre et le
  contexte linguistique fourni par le modèle sont clairement distingués.
- **Word Card** extrait plusieurs entrées pertinentes de Word Origins et compose
  une vue mnémotechnique multilingue compacte. L’anglais, le japonais et le
  chinois restent fixes, tandis que le français et l’arabe alternent dans un
  quatrième panneau.
- **Book Answer** effectue un tirage reproductible parmi 318 fiches relues,
  conserve les traductions publiées de la réponse et ajoute une note de
  réflexion.
- **Book Question** recherche par thème parmi 291 questions relues et revient à
  un tirage reproductible lorsqu’aucune correspondance lexicale n’existe.
- **Root Graph** donne la priorité à 4 018 entrées de racines porteuses de
  contenu, puis aux entrées d’affixes exactes qui les complètent, et enregistre
  un graphe récursif de familles de mots.
- **Affix Graph** inverse cette priorité sur 5 179 entrées d’affixes porteuses de
  contenu et le Root Dictionary, tout en conservant un graphe complet du mot
  central.

Chaque mode possède sa propre politique de recherche et un prompt strict. Word
Origin et Word Card partagent volontairement le même index Word Origins tout en
le présentant différemment ; Answer et Question utilisent des livres et des
moteurs de recherche distincts. Les six modes produisent le même JSON de fiche
versionné. Le texte japonais des livres de fiches conserve le furigana au niveau
des jetons, et les vues chinoises reçoivent un pinyin déterministe complet avec
marques de tons. L’interface web affiche déjà ce JSON ; les adaptateurs pour
l’encre électronique et l’audio le consommeront plus tard sans modifier le code
du corpus, de la recherche ou du modèle.

Un espace **Chat / Benchmark** distinct dialogue directement avec Qwen et
indique le temps écoulé, les jetons d’entrée et de sortie ainsi que la vitesse
de génération. Il est clairement signalé comme sortie brute et non citée du
modèle, et n’est jamais stocké comme fiche fondée sur un livre. Ses observations
sont conservées dans une table séparée du registre local des connaissances.
Chaque prompt répété exécute encore Qwen : le registre est un historique, pas
un cache. Depuis n’importe quelle fiche, **Discuss this card** ouvre Model Lab
avec cette fiche enregistrée et son extrait retrouvé comme contexte borné.
Chaque session Model Lab active reçoit également un fil de recherche durable.
Les tours successifs conservent la filiation parent/enfant ; une discussion de
fiche est reliée à son atome normalisé de contenu source tandis que la réponse
de Qwen reste explicitement non citée.

## Présentation du produit

Le navigateur est une scène éditoriale pour les fiches, et non un tableau de
discussion. Chaque diapositive visible est une composition sans défilement sur
un seul écran, avec une grande idée centrale et une citation de source concise.
Word Origin réserve son centre à un graphe dirigé Cytoscape.js. Word Card place
un grand affichage anglais/IPA au-dessus de panneaux japonais et chinois fixes,
avec un panneau français/arabe tournant. Answer et Question emploient un
carrousel linguistique intérieur—anglais, ruby japonais et ruby pinyin chinois—
et répartissent les phrases exceptionnellement longues sur des diapositives
lisibles supplémentaires. L’analyse grammaticale locale acceptée ajoute des
couleurs fonctionnelles discrètes au texte strictement identique, sans légende
ni métadonnées encombrantes. Les fiches enregistrées forment des carrousels
extérieurs indépendants propres à chaque mode, avec commandes précédent/suivant.
Root, Affix et Word Origin partagent un même moteur de graphe Cytoscape : un
graphe enregistré complet, une carte d’ensemble dans un coin et des
diapositives intérieures qui zooment sur une racine, un préfixe, un suffixe ou
une branche historique sans dupliquer le graphe.
Le mode plein écran masque toute l’interface de l’application, et `/?display=1`
ouvre le même document de fiche comme surface adaptée à un kiosque. Le CSS
d’impression et le JSON versionné des fiches offrent des limites propres pour
un futur rendu sur écran à encre électronique.

### Affichage en direct sur Raspberry Pi

Word Origin utilise des nœuds dimensionnés selon leur contenu, un graphe
d’ascendance complet, des panneaux de sens multilingues, des diapositives par
branche et une remise au meilleur ajustement en un clic.

![Graphe Word Origin en direct sur le Raspberry Pi](../docs/assets/word-origin.png)

Word Card maintient le mot anglais et sa prononciation au premier plan, tout en
présentant de grands panneaux japonais et chinois stables à côté du panneau
français/arabe tournant.

![Word Card multilingue en direct sur le Raspberry Pi](../docs/assets/word-card.png)

Chaque fiche générée reçoit un nouvel identifiant et reste dans le registre des
fiches. Une seconde base normalisée, `knowledge.sqlite3`, conserve comme atomes
réutilisables les termes, sens, prononciations, segments de phonèmes/graphèmes,
morphèmes, données historiques, traductions, éléments grammaticaux, provenance,
révisions et filiations de recherche acceptés. Les fiches sont des vues
reconstructibles sur ces atomes. Un graphe de propriétés LadybugDB constitue
une projection dérivée pour le parcours et peut toujours être reconstruit
depuis SQLite.
Les fiches Book Answer et Book Question acceptées placent aussi leurs textes
anglais, japonais et chinois exacts et relus dans ce stockage normalisé. Chaque
langue est un atome de contenu indépendant relié à la citation du livre détenue
par la recherche ; la réflexion du modèle est volontairement exclue de cette
preuve livresque. Qwen segmente chaque langue dans une tâche bornée distincte.
Un résultat n’est accepté que si ses parties ordonnées reconstruisent la phrase
relue caractère par caractère ; les parties acceptées, les liens de preuve, la
révision du modèle et les analyses remplacées restent des connaissances
réutilisables plutôt qu’un simple balisage de présentation.

### Preuve du passage à la provenance

L’[exemple de passage PocketPolyglot](../examples/artifacts/pocketpolyglot-passage-graph.json)
transforme un passage aligné, écrit par le projet, en un petit graphe conceptuel
relu manuellement. Chaque relation se résout jusqu’à l’unité de passage, à
l’extrait et à l’empreinte fixée du fichier source exacts au moyen des API de
connaissance de production de LKT. Reconstruisez-le ou vérifiez que l’artefact
versionné est à jour :

```bash
python examples/pocketpolyglot_passage_graph.py
python examples/pocketpolyglot_passage_graph.py --check
```

### Preuve de réunion bilingue scénarisée

L’[exemple de réunion bilingue](../examples/artifacts/scripted-bilingual-meeting-knowledge.json)
associe dix prises de parole individuelles horodatées en anglais et en mandarin
à dix unités de connaissance typées et relues manuellement. Chaque unité
conserve son locuteur, son horodatage, la plage exacte de caractères de la
transcription, l’empreinte du fichier source et une relation de graphe étayée
par les preuves. Son registre de révision comporte une correction dont la
version antérieure demeure marquée comme remplacée au travers du véritable
cycle de vie des artefacts `KnowledgeStore`. Le même artefact dispose d’une
[preuve interactive dans le navigateur](https://lazying.art/meeting-intelligence/)
qui permet de remonter d’une unité jusqu’aux mots exacts de sa source.

La transcription et les repères temporels sont des données scénarisées détenues
par le projet. Il ne s’agit ni d’un banc d’essai de précision pour l’ASR, la
diarisation, l’extraction ou la traduction, ni d’un déploiement ou résultat
client. Reconstruisez ou vérifiez le JSON portable :

```bash
python examples/scripted_bilingual_meeting_knowledge.py
python examples/scripted_bilingual_meeting_knowledge.py --check
```

La préparation emploie de petites tâches conscientes de leurs dépendances :
retrouver les preuves, préparer un sens, diviser les composants, développer
récursivement chaque branche d’origine, préparer chaque langue/prononciation
indépendamment, valider, puis composer. Les étapes réussies sont immédiatement
enregistrées comme points de reprise ; une langue ou une branche faible peut
être relancée sans rejeter le reste.

Le worker installé et peu prioritaire fait progresser les six jeux visibles en
tours équilibrés. Question et Answer puisent dans leurs propres livres relus ;
Word Card et Word Origin partagent une même investigation atomique et bornée
d’un mot ; Root et Affix puisent indépendamment dans leur dictionnaire épuré et
emploient l’autre livre morphologique ainsi que des correspondances bornées de
Word Origins comme RAG complémentaire pertinent. Il choisit toujours le mode
visible le moins rempli, afin qu’aucune voie rapide ne prenne trop d’avance sur
les autres.
Pendant le rattrapage, il suspend les nouveaux tirages Question/Answer et ne
laisse au maximum qu’un sujet lexical autonome inachevé en cours. Le contrôle
d’équilibre s’exécute à intervalle borné même si des enrichissements facultatifs
restent en attente ; les tâches lexicales sont attribuées avant cet
enrichissement grammatical des livres.

Chaque source encore inconnue passe néanmoins par les étapes normales de Qwen
local, de RAG et de publication. Les identités stables des sources et des termes
évitent les répétitions après redémarrage. L’analyse atomique d’un mot peut
dériver une vue Root/Affix lorsque le mot en contient véritablement une, mais
les parcours indépendants des livres Root/Affix garantissent que ces produits
progressent même si le mot choisi ne comporte aucun affixe productif. Aucun
composant n’est inventé simplement pour équilibrer les onglets.

La préparation Root/Affix répartit le travail coûteux en deux appels locaux
reprenables : graphe/histoire d’abord, puis une courte présentation multilingue.
Le graphe dispose d’une limite plus élevée de 1 200 jetons (1 400 pour une
nouvelle réparation), tandis que l’appel linguistique en utilise 512 (640 pour
une réparation). Une réponse JSON tronquée n’est jamais réinjectée
récursivement dans Qwen. Chaque étape validée est enregistrée avec son modèle et
l’empreinte précise de ses preuves, si bien qu’un échec tardif ne gaspille pas
le graphe.

Le navigateur sans habillage commence par Question, puis enchaîne Answer → Word
Card → Word Origin → Root → Affix lorsque chaque fiche a achevé toutes ses
diapositives intérieures. Les réglages d’affichage peuvent choisir un seul mode
ou n’importe quel sous-ensemble tout en conservant cet ordre canonique ; les six
sont sélectionnés par défaut. Les fiches enregistrées sont mélangées dans chaque
mode par défaut, avec une option stable de la plus récente à la plus ancienne.
Chaque mode possède son propre passage mélangé limité aux fiches acceptées ; le
changement d’onglet ne fusionne donc pas leurs collections et ne répète pas la
même fiche à chaque visite. Les nouvelles fiches acceptées sont placées en tête
du passage restant de leur mode. Un onglet explicite ou une URL `?mode=` reste
dans son mode, et toute activité du pointeur, de l’écran tactile ou du clavier
relance le temps d’affichage complet de la fiche actuelle avant la reprise du
mouvement ambiant.

Cette frontière de responsabilité est volontaire : les phrases de livres
relues, leurs traductions et leurs citations viennent des entrées du corpus
local et ne sont jamais réécrites par le modèle ; les nouvelles données
explicatives ou lexicales sont produites par le modèle local configuré et non
saisies à la main dans SQLite. Un mauvais brouillon reste hors du jeu visible.
Les propositions de dictionnaire et la prononciation/ruby déterministe sont de
même le résultat d’outils et de recherches locaux, et non des données de fiches
saisies à la main. FreeDict fournit un contrôle correctif anglais-arabe exact
lorsqu’OMW ne dispose d’aucun lemme arabe pour le sens choisi ; Qwen doit copier
l’un des candidats retrouvés et le système associe l’identifiant de preuve de
ce candidat après validation. La version complète et fixée de JMdict vérifie
localement les formes et lectures japonaises : une lecture exacte ne coûte
aucun appel au modèle, une correction unique est déterministe, et seule une
forme écrite réellement ambiguë reçoit une petite sélection Qwen limitée aux
lectures retrouvées. `/api/health` traite les deux index correctifs compacts
comme des sources obligatoires et rapporte leur état, leurs versions, leurs
empreintes et leur nombre d’entrées.
La génération autonome s’interrompt si le Raspberry Pi est actuellement en
sous-tension, bridé ou trop chaud, puis reprend lorsque la situation est
rétablie. Le client web charge l’intégralité du mode sélectionné (jusqu’à 1 000
fiches acceptées), conserve la plus récente en premier et mélange chaque autre
fiche une fois par passage du carrousel. Il interroge les fiches acceptées sans
interrompre l’affichage courant et insère ensuite un résultat nouvellement
publié. L’état compact et `/api/health` indiquent tous deux la couverture finie
des livres et la progression lexicale planifiée/acceptée sans planifier de
travail. Les demandes de mots interactives restent immédiates et réutilisent les
mêmes atomes persistants que la préparation autonome.

Le pied de page des connaissances acquises affiche une fenêtre mobile de 18
points de fiche au maximum ; les flèches et le compteur exact `current / total`
couvrent toujours la totalité du jeu enregistré, sans limite. Les points de
langue Question/Answer appartiennent uniquement à la fiche actuelle et sont
remplacés lorsqu’elle change.

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

## Règle de fondement

Le modèle de langage rédige les explications et les aides linguistiques
manquantes, mais jamais la liste des citations. LKT associe les identifiants
d’entrée, extraits, sections, numéros de page, localisateurs numériques et
traductions relues des livres de fiches directement depuis les résultats de
recherche. Word Origin peut ajouter un contexte linguistique fiable, mais
chaque nœud du graphe indique s’il provient de l’ancre livresque ou des
connaissances du modèle. Si le livre configuré n’apporte aucune preuve,
l’application ne génère pas de fiche.

## Plan du dépôt

| Chemin | Responsabilité |
| --- | --- |
| `lkt/corpus.py` | Ingestion de Word Origins, index SQLite atomique, recherche exacte + FTS |
| `lkt/morphology.py` | Ingestion du JSONL épuré Root/Affix, provenance, recherche exacte + FTS |
| `lkt/card_books.py` | Ingestion multilingue d’Answer/Question, recherche et tirages déterministes |
| `lkt/deck.py` | Préparation alternée, un élément à la fois, des livres et du lexique |
| `lkt/device.py` | Contrôle électrique/thermique du Pi avant l’inférence en arrière-plan |
| `lkt/retrieval.py` | Politiques RAG indépendantes pour Word Origin, Word Card, Answer et Question |
| `lkt/llm.py` | Petit adaptateur llama.cpp et un prompt strict par expérience |
| `lkt/service.py` | Composition et normalisation des fiches |
| `lkt/pronunciation.py` | Pinyin/ruby déterministe et IPA hors ligne versionné |
| `lkt/store.py` | Fiches versionnées, artefacts de préparation, révisions, archives et registre de discussion |
| `lkt/knowledge.py` | Connaissances atomiques établies, preuves, tâches, révisions et filiation des recherches |
| `lkt/preparation.py` | Planification diviser-pour-régner des mots/contenus, consciente des dépendances |
| `lkt/atomic.py` | Préparation atomique bornée et assemblage déterministe des fiches |
| `lkt/graph.py` | Projection de parcours LadybugDB reconstructible depuis les atomes SQLite acceptés |
| `lkt/lexicon.py` | Preuves compactes de correction WordNet multilingue |
| `lkt/freedict.py` | Ingestion anglaise-arabe exacte de FreeDict et recherche des corrections |
| `lkt/jmdict.py` | Index complet des lectures JMdict par forme exacte et provenance |
| `lkt/web.py` | API HTTP sans dépendance et serveur d’interface web |
| `lkt/outputs.py` | Frontière stable des sorties web/encre électronique/audio |
| `lkt/static/` | Interface de niveau bureau, assez adaptative pour un futur usage en kiosque |
| `scripts/` | Outils reproductibles d’exécution, d’installation, de mise à jour et de test de fumée du Pi |
| `systemd/` | Services renforcés du modèle et de l’application |
| `docs/lineage.md` | Provenance exacte de l’ancien projet et du corpus |
| `docs/product-brief.md` | Exigences durables du propriétaire et critères d’acceptation |
| `docs/knowledge-architecture.md` | Contrat SQLite atomique, projection du graphe et préparation par étapes |
| `docs/owner-request-log.md` | Directives chronologiques du propriétaire, expurgées pour la confidentialité |
| `docs/voice-hardware.md` | Choix de microphone pris en charge et tests audio par étapes |
| `docs/mode-roadmap.md` | Plan d’extension pour les futurs livres de suffixes, affixes et racines |

## Développement local

Installez la petite dépendance de prononciation épinglée, puis lancez les tests :

```powershell
cd C:\Users\Administrator\Projects\LocalKnowledge
python -m pip install -e .
python -m unittest discover -s tests -v
python -m compileall -q lkt tests
```

Construisez un index local à partir de l’export structuré du livre :

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

Avec un serveur llama.cpp à l’écoute sur le port 8081 :

```powershell
python -m lkt.cli generate abacus --mode word
python -m lkt.cli generate "Should I begin?" --mode answer
python -m lkt.cli generate technology --mode question
python -m lkt.cli generate inspection --mode root
python -m lkt.cli generate abnormal --mode affix
python -m lkt.cli serve
```

Ouvrez <http://127.0.0.1:8090>.

## Agencement du Raspberry Pi 5

```text
/home/lachlan/LocalKnowledgeTerminal/
├── source/      # this Git repository; updated with fetch + fast-forward pull
├── runtime/     # pinned llama.cpp plus optional knowledge Python environment
├── models/      # Qwen GGUF (not committed)
├── data/        # corpus index and saved cards (not committed)
└── logs/        # bootstrap logs (not committed)
```

Artefacts d’exécution épinglés :

| Artefact | Révision | Intégrité |
| --- | --- | --- |
| llama.cpp | `v0.3.0` / `c1d0e7a004015f23bc0233470b747b596f29b264` | Archive source épinglée au commit |
| Qwen3-4B-GGUF | `bc640142c66e1fdd12af0bd68f40445458f3869b` | Q4_K_M SHA-256 `7485fe6f…534fdf5` |
| Fichier du modèle | `Qwen3-4B-Q4_K_M.gguf` | 2 497 280 256 octets |

Le service du Pi expose un seul emplacement d’inférence (`--parallel 1`). La
composition des fiches et les requêtes Model Lab sont donc traitées en série,
ce qui préserve un usage mémoire et une latence prévisibles au lieu de mettre
les quatre cœurs en concurrence entre les tâches.

Qwen3-8B a démontré sa viabilité comme modèle de préparation facultatif axé sur
la qualité. Sur le Pi déployé, il a produit une sonde multilingue de 120 jetons à
1,78 jeton/s, avec environ 6,28 Gio de RSS, 1,85 Gio de mémoire système encore
disponible et aucun bridage thermique en cours. Qwen3-4B est le choix hors ligne
réactif par défaut. La sélection du modèle est explicite et réversible :

```bash
tmux new-session -d -s lkt-8b-download \
  './scripts/download_qwen3_8b.sh > ../logs/qwen3-8b-download.log 2>&1'
sudo ./scripts/select_model.sh status
sudo ./scripts/select_model.sh 8b
sudo ./scripts/select_model.sh 4b
sudo ./scripts/benchmark_models.sh
```

Un seul modèle est chargé à la fois. Le profil 4B par défaut utilise un contexte
de 3 072 jetons ; le profil 8B facultatif emploie un contexte de 2 048 jetons et
un lot plus petit afin de respecter la limite de mémoire de 8 Go. Si son serveur
ne devient pas sain, `select_model.sh 8b` rétablit automatiquement le profil 4B.
Le téléchargeur reprend un transfert partiel, vérifie le SHA-256 officiel, puis
expose atomiquement le GGUF final.
Le banc d’essai active un modèle à la fois, exécute la même sonde multilingue
bornée de qualité/vitesse, enregistre le temps écoulé, le débit de jetons
llama.cpp et la mémoire du processus, puis rétablit le modèle actif avant le
test.

Installez l’environnement compact facultatif de connaissances et construisez la
projection du graphe :

```bash
./scripts/install_knowledge_runtime.sh
./scripts/rebuild_graph.sh
```

Cette opération installe eSpeak NG pour l’IPA local, épingle LadybugDB 0.19.1 et
Wn 1.1.1 dans un environnement isolé, puis installe uniquement les lexiques OMW
2.0 anglais, japonais, chinois mandarin, français et arabe. Elle vérifie aussi
l’archive complète et épinglée de JMdict, construit l’index de lecture par forme
exacte et supprime le téléchargement brut. Les dumps complets de Wiktionary sont
volontairement exclus. L’extraction IPA utilise le mode texte silencieux et
n’active aucune sortie vocale.

Sur le Pi :

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

Pour le développement ultérieur Windows → GitHub → Pi :

```bash
cd /home/lachlan/LocalKnowledgeTerminal/source
./scripts/update_pi_tmux.sh
tmux attach -t lkt-update
```

L’enveloppe tmux maintient le déploiement actif au fil des transitions SSH ou du
navigateur et écrit dans `~/LocalKnowledgeTerminal/logs/update-pi.log`. Le script
idempotent sous-jacent `scripts/install_services.sh` installe les trois unités
systemd, les active au démarrage, les lance dans l’ordre modèle → web → worker,
vérifie les deux points de contrôle et installe l’entrée graphique de démarrage
automatique. `scripts/update_pi.sh` exécute l’ensemble du sas de tests avant
d’appeler cet installateur de services avec `--restart`.

Ouvrez ensuite `http://127.0.0.1:8090` dans le bureau VNC du Pi, ou
`http://<pi-lan-address>:8090` depuis le réseau local de confiance.

L’installateur place également `desktop/lkt-kiosk.desktop` dans le dossier de
démarrage automatique XDG de l’utilisateur du Pi et installe
`scripts/open_kiosk.sh` sous `/usr/local/bin/lkt-open-kiosk`. À la prochaine
ouverture de session graphique, le lanceur attend le point de contrôle local et
ouvre exactement un profil Chromium dédié sur
`http://127.0.0.1:8090/?display`. Relancer le lanceur est sans danger : il détecte
ce profil et n’ouvre pas une autre fenêtre. Chromium démarre comme une
application plein écran normale plutôt que comme un kiosque verrouillé ;
**Esc** quitte donc le plein écran et rend le bureau du Pi contrôlable. Les URL
de mode explicites restent disponibles pour un usage VNC intentionnel.

## Données et droits d’auteur

Les PDF des livres, les corpus extraits, les poids des modèles, les index
générés et les fiches enregistrées sont volontairement exclus de Git. Fournissez
pendant l’installation un export JSONL local acquis légalement. LKT enregistre
chaque SHA-256 dans son index SQLite afin qu’une fiche générée puisse être
retracée jusqu’à la construction exacte du corpus. Consultez
[`docs/corpora.md`](../docs/corpora.md) pour connaître le jeu de référence
vérifié.

## Filiation

LKT est un successeur propre et local-first inspiré de
[`WordsCardEink`](https://github.com/lachlanchen/WordsCardEink) et de
[`WordOrigins`](https://github.com/lachlanchen/WordOrigins). Il n’importe pas
leur environnement monolithique ni leurs dépendances matérielles. Consultez
[`docs/lineage.md`](../docs/lineage.md) pour les commits épinglés et les idées
conservées.

## Soutien

| Donate | PayPal | Stripe |
| --- | --- | --- |
| [![Donate](https://img.shields.io/badge/Donate-LazyingArt-0EA5E9?style=for-the-badge&logo=ko-fi&logoColor=white)](https://chat.lazying.art/donate) | [![PayPal](https://img.shields.io/badge/PayPal-RongzhouChen-00457C?style=for-the-badge&logo=paypal&logoColor=white)](https://paypal.me/RongzhouChen) | [![Stripe](https://img.shields.io/badge/Stripe-Donate-635BFF?style=for-the-badge&logo=stripe&logoColor=white)](https://buy.stripe.com/aFadR8gIaflgfQV6T4fw400) |

## Citation

Si LKT vous est utile, citez-le à l’aide du menu GitHub **Cite this repository**,
qui lit [`CITATION.cff`](../CITATION.cff), ou utilisez :

```bibtex
@software{chen_local_knowledge_terminal_2026,
  author = {Chen, Lachlan},
  title = {Local Knowledge Terminal},
  year = {2026},
  version = {0.1.0},
  url = {https://github.com/lachlanchen/LocalKnowledgeTerminal}
}
```
