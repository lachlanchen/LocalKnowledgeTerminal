[English](../README.md) · [العربية](README.ar.md) · [Español](README.es.md) · [Français](README.fr.md) · [日本語](README.ja.md) · [한국어](README.ko.md) · [Tiếng Việt](README.vi.md) · [中文 (简体)](README.zh-Hans.md) · [中文（繁體）](README.zh-Hant.md) · [Deutsch](README.de.md) · [Русский](README.ru.md)

[![LazyingArt banner — Local Knowledge Terminal](../docs/assets/banner.svg)](../docs/assets/banner.svg)

# Local Knowledge Terminal

**Inteligencia privada basada en libros, en tu propio equipo.**

[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![Model](https://img.shields.io/badge/Qwen3-4B%20%2F%208B-6f42c1?style=flat-square)](https://huggingface.co/Qwen/Qwen3-8B-GGUF)
[![Runtime](https://img.shields.io/badge/llama.cpp-v0.3.0-173f35?style=flat-square)](https://github.com/ggml-org/llama.cpp)
[![Target](https://img.shields.io/badge/Raspberry%20Pi%205-8GB-C51A4A?style=flat-square&logo=raspberrypi)](https://www.raspberrypi.com/products/raspberry-pi-5/)
[![Sponsor](https://img.shields.io/badge/Sponsor-lachlanchen-EA4AAA?style=flat-square&logo=githubsponsors&logoColor=white)](https://github.com/sponsors/lachlanchen)

Local Knowledge Terminal (LKT) convierte una colección privada de libros en
tarjetas multilingües con citas. Su primera biblioteca combina ediciones
estructuradas de **Word Origins**, **The Book of Answers**, **The Book of
Questions**, un **English Root Dictionary** y un **English Affix Dictionary**.
Qwen3-4B Q4_K_M funciona localmente en una Raspberry Pi 5 de 8 GB, con Qwen3-8B
como perfil opcional más lento; la recuperación, la inferencia, el historial y
la interfaz web funcionan sin una API en la nube.

## Pruébalo con una colección

Si ya tienes una colección privada y acotada de libros o diccionarios, el
[sprint inicial de adecuación por 250 USD](https://lazying.art/lkt/) comienza con
una evaluación gratuita. Abarca una colección, un objetivo lingüístico y un
equipo existente; después entrega un mapa de datos, privacidad, citas y
procedencia, una muestra acordada de hasta 12 unidades fuente y 20 preguntas de
prueba, hasta dos tarjetas citadas en el navegador cuando el material sea
utilizable, una recomendación clara de seguir o no seguir y una ronda de
corrección factual. El alcance escrito define la unidad fuente—por ejemplo, un
pasaje, registro o página representativa—antes del pago.
El hardware, el envío, el OCR personalizado, la conversión masiva, el despliegue
en producción y el soporte continuo quedan fuera de este alcance fijo.

Para ver exactamente cómo son esos tres entregables sin compartir material de
clientes, consulta el
[informe de muestra de adecuación](../docs/sample-fit-report.md). Aplica el
formato a la colección de referencia documentada del propio LKT y no representa
un resultado de cliente ni una afirmación sobre un encargo pagado.

## Seis experiencias independientes, un contrato de tarjeta

- **Word Origin** emplea su propio recuperador y prompt de una sola entrada para
  crear un grafo dirigido e interactivo de ascendencia. Conserva los morfemas
  ramificados y distingue claramente los nodos respaldados por el libro del
  contexto lingüístico aportado por el modelo.
- **Word Card** recupera varias entradas pertinentes de Word Origins y compone
  una vista mnemotécnica multilingüe y compacta. Inglés, japonés y chino
  permanecen fijos, mientras francés y árabe rotan en un cuarto panel.
- **Book Answer** realiza una selección reproducible entre 318 tarjetas
  revisadas, conserva las traducciones publicadas de la respuesta y añade una
  nota reflexiva.
- **Book Question** busca por tema entre 291 preguntas revisadas y recurre a una
  selección reproducible cuando no existe coincidencia léxica.
- **Root Graph** prioriza 4.018 registros de raíces con contenido, después las
  entradas de afijos coincidentes que sirven de apoyo, y guarda un grafo
  recursivo de familias de palabras.
- **Affix Graph** invierte esa prioridad entre 5.179 registros de afijos con
  contenido y el Root Dictionary, a la vez que conserva un grafo completo de la
  palabra central.

Cada modo tiene su propia política de recuperación y un prompt estricto. Word
Origin y Word Card comparten deliberadamente el mismo índice de Word Origins,
aunque lo presentan de forma distinta; Answer y Question utilizan libros y
motores de recuperación separados. Los seis modos producen el mismo JSON de
tarjeta versionado. El texto japonés de los libros de tarjetas conserva furigana
a nivel de token y las vistas chinas reciben pinyin determinista completo con
marcas de tono. La interfaz web ya representa ese JSON; más adelante los
adaptadores de tinta electrónica y audio lo consumirán sin cambiar el código de
corpus, recuperación o modelo.

Un espacio independiente de **Chat / Benchmark** conversa directamente con
Qwen e informa del tiempo transcurrido, los tokens de entrada y salida y la
velocidad de generación. Está identificado claramente como salida bruta y sin
citas del modelo, y nunca se guarda como tarjeta fundamentada en libros. Sus
observaciones se conservan en una tabla separada del registro local de
conocimiento. Cada prompt repetido vuelve a ejecutar Qwen: el registro es
historial, no caché. Desde cualquier tarjeta, **Discuss this card** abre Model
Lab con esa tarjeta guardada y su fragmento recuperado como contexto acotado.
Cada sesión activa de Model Lab recibe también un hilo de consulta duradero. Los
turnos sucesivos conservan el linaje padre/hijo; una discusión de tarjeta se
vincula a su átomo normalizado de contenido fuente, mientras la respuesta de
Qwen permanece explícitamente sin citar.

## Presentación del producto

El navegador es un escenario editorial de tarjetas, no un panel de chat. Cada
diapositiva visible es una composición de una sola pantalla, sin desplazamiento,
con una idea central grande y una cita de fuente compacta. Word Origin reserva
el centro para un grafo dirigido de Cytoscape.js. Word Card utiliza inglés/IPA
grande sobre paneles fijos en japonés y chino y un panel rotativo en francés y
árabe. Answer y Question utilizan un carrusel interno de idiomas—inglés, ruby
japonés y ruby de pinyin chino—y dividen las frases excepcionalmente largas en
diapositivas legibles adicionales. El análisis gramatical local aceptado añade
colores discretos de función al mismo texto exacto, sin leyenda ni metadatos
abarrotados. Las tarjetas guardadas forman carruseles externos independientes
por modo, con controles anterior/siguiente.
Root, Affix y Word Origin comparten un único visor de grafos Cytoscape: un grafo
guardado completo, un mapa general en la esquina y diapositivas internas de
enfoque que amplían una raíz, prefijo, sufijo o rama histórica sin duplicar el
grafo.
El modo de pantalla completa oculta toda la interfaz de la aplicación y
`/?display=1` abre el mismo documento de tarjeta como una superficie apta para
quiosco. El CSS de impresión y el JSON de tarjeta versionado establecen límites
limpios para una futura representación en tinta electrónica.

### Pantalla activa de Raspberry Pi

Word Origin utiliza nodos dimensionados según el contenido, un grafo de
ascendencia completo, paneles multilingües de significado, diapositivas de ramas
y un restablecimiento al mejor ajuste con un solo clic.

![Grafo activo de Word Origin en la Raspberry Pi](../docs/assets/word-origin.png)

Word Card mantiene dominantes la palabra y el sonido en inglés mientras
presenta grandes paneles estables en japonés y chino junto al panel rotativo en
francés y árabe.

![Word Card multilingüe activa en la Raspberry Pi](../docs/assets/word-card.png)

Cada tarjeta generada recibe un ID nuevo y permanece en el registro de tarjetas.
Una segunda base de datos normalizada, `knowledge.sqlite3`, almacena términos,
acepciones, pronunciaciones, segmentos de fonemas/grafemas, morfemas, historia,
traducciones, gramática, procedencia, revisiones y linaje de consultas aceptados
como átomos reutilizables. Las tarjetas son vistas reconstruibles sobre esos
átomos. Un grafo de propiedades LadybugDB es una proyección derivada para
recorridos y siempre puede reconstruirse desde SQLite.
Las tarjetas aceptadas de Book Answer y Book Question también colocan sus textos
exactos y revisados en inglés, japonés y chino en este almacén normalizado. Cada
idioma es un átomo de contenido independiente vinculado a la cita del libro
propiedad de la recuperación; la reflexión del modelo se excluye
deliberadamente de esa evidencia bibliográfica. Qwen segmenta cada idioma en un
trabajo acotado separado. Un resultado solo se acepta cuando sus partes
ordenadas reconstruyen la frase revisada carácter por carácter; las partes
aceptadas, los enlaces de evidencia, la revisión del modelo y los análisis
sustituidos siguen siendo conocimiento reutilizable en vez de mero marcado de
presentación.

### Prueba de pasaje a procedencia

El [ejemplo de pasaje de PocketPolyglot](../examples/artifacts/pocketpolyglot-passage-graph.json)
convierte un pasaje alineado y escrito por el proyecto en un pequeño grafo de
conceptos revisado manualmente. Cada relación se resuelve hasta la unidad de
pasaje exacta, el fragmento y el hash fijado del archivo fuente mediante las API
de conocimiento de producción de LKT. Reconstrúyelo o verifica que el artefacto
confirmado esté actualizado:

```bash
python examples/pocketpolyglot_passage_graph.py
python examples/pocketpolyglot_passage_graph.py --check
```

### Prueba de reunión bilingüe guionizada

El [ejemplo de reunión bilingüe](../examples/artifacts/scripted-bilingual-meeting-knowledge.json)
asocia diez intervenciones individuales en inglés y mandarín, con marca de
tiempo, a diez unidades tipadas de conocimiento revisadas manualmente. Cada
unidad conserva hablante, marca temporal, intervalo exacto de caracteres de la
transcripción, hash del archivo fuente y relación del grafo respaldada por
evidencia. Su registro de revisión incluye una corrección cuya versión anterior
se conserva como sustituida mediante el ciclo real de artefactos de
`KnowledgeStore`. El mismo artefacto dispone de una
[prueba interactiva en el navegador](https://lazying.art/meeting-intelligence/)
para seguir una unidad hasta las palabras exactas de su fuente.

La transcripción y los tiempos son materiales guionizados propiedad del
proyecto. Esto no es una prueba comparativa de precisión de ASR, diarización,
extracción o traducción, ni un despliegue o resultado de cliente. Reconstruye o
verifica el JSON portátil:

```bash
python examples/scripted_bilingual_meeting_knowledge.py
python examples/scripted_bilingual_meeting_knowledge.py --check
```

La preparación utiliza trabajos pequeños conscientes de sus dependencias:
recuperar evidencia, preparar un significado, dividir componentes, expandir
recursivamente cada rama de origen, preparar de forma independiente cada
idioma/pronunciación, validar y componer. Las etapas correctas se guardan de
inmediato como puntos de control; un idioma o rama débil puede reintentarse sin
descartar el resto.

El trabajador instalado de baja prioridad hace crecer los seis mazos visibles
en rondas equilibradas. Question y Answer extraen de sus propios libros
revisados; Word Card y Word Origin comparten una investigación atómica y acotada
de una palabra; Root y Affix recorren independientemente sus propios diccionarios
depurados y utilizan el otro libro de morfología, además de coincidencias
acotadas de Word Origins, como RAG complementario cuando corresponde. Siempre
elige el modo visible con menos elementos, de modo que ningún camino rápido
pueda adelantarse demasiado a otro.
Durante la puesta al día, pausa las nuevas extracciones de Question/Answer y
mantiene como máximo un tema léxico autónomo sin terminar en curso. La
comprobación de equilibrio se ejecuta a intervalos limitados incluso cuando
queda enriquecimiento opcional en cola; los trabajos léxicos se reclaman antes
que ese enriquecimiento gramatical del libro.

Cada fuente inédita sigue pasando por sus puertas normales de Qwen local, RAG y
publicación. Las identidades estables de fuente y término impiden repeticiones
entre reinicios. El análisis atómico de palabras puede derivar una vista
Root/Affix cuando la palabra realmente contiene uno, pero los recorridos
independientes de los libros Root/Affix garantizan que esos productos crezcan
aunque una palabra seleccionada no tenga un afijo productivo. No se inventa
ningún componente solo para equilibrar las pestañas.

La preparación de Root/Affix divide el trabajo costoso en dos llamadas locales
reanudables: primero grafo/historia y después una pequeña presentación
multilingüe. El grafo tiene un límite mayor de 1.200 tokens (1.400 para una
reparación nueva), mientras la llamada de idioma utiliza 512 tokens (640 para
reparación). Una respuesta JSON truncada nunca se realimenta recursivamente a
Qwen. Cada etapa validada se guarda con su modelo y la huella exacta de la
evidencia, por lo que un fallo posterior no desperdicia el grafo.

El navegador básico comienza por Question y continúa por Answer → Word Card →
Word Origin → Root → Affix después de que cada tarjeta complete todas sus
diapositivas internas. La configuración de pantalla puede elegir un modo o
cualquier subconjunto, manteniendo ese orden canónico; los seis están
seleccionados por defecto. De manera predeterminada, las tarjetas guardadas se
barajan dentro de cada modo, con una opción estable de más reciente a más
antigua. Cada modo posee un recorrido barajado independiente solo de tarjetas
aceptadas, por lo que cambiar de pestaña no fusiona sus colecciones ni repite la
misma tarjeta en cada visita. Las tarjetas recién aceptadas se colocan primero
en el recorrido restante de ese modo. Una pestaña explícita o una URL `?mode=`
permanece dentro del modo, y cualquier actividad de puntero, tacto o teclado
reinicia la permanencia completa de la tarjeta actual antes de reanudar el
movimiento ambiental.

Este límite de propiedad es deliberado: las frases revisadas del libro, sus
traducciones y citas proceden de registros del corpus local y el modelo nunca
las reescribe; los nuevos datos explicativos o léxicos los produce el modelo
local configurado, no se introducen a mano en SQLite. Un borrador deficiente
queda fuera del mazo visible. Los candidatos de diccionario y la
pronunciación/ruby determinista son asimismo resultados de herramientas y
recuperación locales, no datos de tarjeta escritos a mano. FreeDict proporciona
una puerta de corrección inglés-árabe exacta cuando OMW no contiene un lema
árabe para el sentido elegido; Qwen debe copiar un candidato recuperado y el
sistema adjunta su ID de evidencia tras validarlo. La versión completa y fijada
de JMdict comprueba localmente formas y lecturas japonesas: una lectura exacta
no consume una llamada al modelo, una corrección única es determinista y solo
una forma escrita realmente ambigua recibe una pequeña selección de Qwen
restringida a las lecturas recuperadas. `/api/health` trata ambos índices
compactos de corrección como fuentes obligatorias e informa de su disponibilidad,
versiones, hashes y número de entradas.
La generación autónoma se detiene cuando la Raspberry Pi presenta bajo voltaje,
limitación o temperatura alta, y se reanuda cuando la condición desaparece. El
cliente web carga el modo seleccionado completo (hasta 1.000 tarjetas
aceptadas), conserva primero la más reciente y baraja las demás una vez por
recorrido del carrusel. Consulta las tarjetas aceptadas sin interrumpir la
visualización actual e inserta a continuación un resultado recién publicado.
Tanto el estado compacto como `/api/health` informan de la cobertura finita de
libros y del progreso léxico planificado/aceptado sin programar trabajo. Las
solicitudes interactivas de palabras siguen siendo inmediatas y reutilizan los
mismos átomos persistidos que la preparación autónoma.

El pie de conocimiento adquirido muestra una ventana móvil de 18 puntos de
tarjeta como máximo; las flechas y el contador exacto `current / total` siguen
cubriendo todo el mazo guardado sin límite. Los puntos de idioma de
Question/Answer pertenecen únicamente a la tarjeta actual y se sustituyen cuando
esta cambia.

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

## Regla de fundamentación

El modelo de lenguaje escribe explicaciones y ayudas lingüísticas que falten,
pero nunca escribe la lista de citas. LKT adjunta ID de entrada, fragmentos,
secciones, números de página, localizadores digitales y traducciones revisadas
de los libros de tarjetas directamente desde los registros de recuperación.
Word Origin puede añadir contexto lingüístico fiable, pero cada nodo del grafo
registra si procede del ancla bibliográfica o del conocimiento del modelo. Si el
libro configurado no contiene evidencia, la aplicación no genera una tarjeta.

## Mapa del repositorio

| Ruta | Responsabilidad |
| --- | --- |
| `lkt/corpus.py` | Ingesta de Word Origins, índice SQLite atómico, recuperación exacta + FTS |
| `lkt/morphology.py` | Ingesta de JSONL depurado de Root/Affix, procedencia, recuperación exacta + FTS |
| `lkt/card_books.py` | Ingesta multilingüe de Answer/Question, búsqueda y selecciones deterministas |
| `lkt/deck.py` | Preparación alterna, de uno en uno, de libros y léxico |
| `lkt/device.py` | Puerta de disponibilidad eléctrica/térmica de la Pi para inferencia en segundo plano |
| `lkt/retrieval.py` | Políticas RAG independientes para Word Origin, Word Card, Answer y Question |
| `lkt/llm.py` | Adaptador pequeño de llama.cpp y un prompt estricto por experiencia |
| `lkt/service.py` | Composición y normalización de tarjetas |
| `lkt/pronunciation.py` | Pinyin/ruby determinista e IPA sin conexión versionado |
| `lkt/store.py` | Tarjetas versionadas, artefactos de preparación, revisiones, archivo y registro de chat |
| `lkt/knowledge.py` | Conocimiento atómico establecido, evidencia, trabajos, revisiones y linaje de consultas |
| `lkt/preparation.py` | Planificación divide y vencerás de palabras/contenido consciente de dependencias |
| `lkt/atomic.py` | Preparación atómica acotada y ensamblaje determinista de tarjetas |
| `lkt/graph.py` | Proyección de recorrido LadybugDB reconstruible desde átomos SQLite aceptados |
| `lkt/lexicon.py` | Evidencia compacta de corrección WordNet multilingüe |
| `lkt/freedict.py` | Ingesta exacta inglés-árabe de FreeDict y recuperación de correcciones |
| `lkt/jmdict.py` | Índice completo de lectura por forma exacta de JMdict y procedencia |
| `lkt/web.py` | API HTTP sin dependencias y servidor de interfaz web |
| `lkt/outputs.py` | Límite estable de salida para web/tinta electrónica/audio |
| `lkt/static/` | Interfaz de clase escritorio, suficientemente adaptable para futuro uso en quiosco |
| `scripts/` | Herramientas reproducibles de ejecución, instalación, actualización y prueba de humo en Pi |
| `systemd/` | Servicios reforzados del modelo y la aplicación |
| `docs/lineage.md` | Procedencia exacta del proyecto anterior y del corpus |
| `docs/product-brief.md` | Requisitos duraderos del propietario y criterios de aceptación |
| `docs/knowledge-architecture.md` | Contrato de SQLite atómico, proyección del grafo y preparación por etapas |
| `docs/owner-request-log.md` | Dirección del propietario cronológica y con privacidad protegida |
| `docs/voice-hardware.md` | Selección de micrófono compatible y pruebas de audio por etapas |
| `docs/mode-roadmap.md` | Plan de extensión para futuros libros de sufijos, afijos y raíces |

## Desarrollo local

Instala la pequeña dependencia fijada de pronunciación y ejecuta las pruebas:

```powershell
cd C:\Users\Administrator\Projects\LocalKnowledge
python -m pip install -e .
python -m unittest discover -s tests -v
python -m compileall -q lkt tests
```

Crea un índice local desde la exportación estructurada del libro:

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

Con un servidor llama.cpp escuchando en el puerto 8081:

```powershell
python -m lkt.cli generate abacus --mode word
python -m lkt.cli generate "Should I begin?" --mode answer
python -m lkt.cli generate technology --mode question
python -m lkt.cli generate inspection --mode root
python -m lkt.cli generate abnormal --mode affix
python -m lkt.cli serve
```

Abre <http://127.0.0.1:8090>.

## Estructura de Raspberry Pi 5

```text
/home/lachlan/LocalKnowledgeTerminal/
├── source/      # this Git repository; updated with fetch + fast-forward pull
├── runtime/     # pinned llama.cpp plus optional knowledge Python environment
├── models/      # Qwen GGUF (not committed)
├── data/        # corpus index and saved cards (not committed)
└── logs/        # bootstrap logs (not committed)
```

Artefactos de ejecución fijados:

| Artefacto | Revisión | Integridad |
| --- | --- | --- |
| llama.cpp | `v0.3.0` / `c1d0e7a004015f23bc0233470b747b596f29b264` | Archivo fuente fijado por commit |
| Qwen3-4B-GGUF | `bc640142c66e1fdd12af0bd68f40445458f3869b` | Q4_K_M SHA-256 `7485fe6f…534fdf5` |
| Archivo de modelo | `Qwen3-4B-Q4_K_M.gguf` | 2.497.280.256 bytes |

El servicio de la Pi expone una sola ranura de inferencia (`--parallel 1`). Por
ello, la composición de tarjetas y las solicitudes de Model Lab se procesan de
forma secuencial, lo que mantiene previsibles el uso de memoria y la latencia en
lugar de hacer competir cuatro núcleos entre trabajos.

Qwen3-8B ha demostrado ser utilizable como modelo opcional de preparación que
prioriza la calidad. En la Pi desplegada generó una prueba multilingüe de 120
tokens a 1,78 tokens/s, con unos 6,28 GiB de RSS, 1,85 GiB de memoria del sistema
aún disponible y sin limitación térmica actual. Qwen3-4B es la opción rápida y
sin conexión predeterminada. La selección de modelo es explícita y reversible:

```bash
tmux new-session -d -s lkt-8b-download \
  './scripts/download_qwen3_8b.sh > ../logs/qwen3-8b-download.log 2>&1'
sudo ./scripts/select_model.sh status
sudo ./scripts/select_model.sh 8b
sudo ./scripts/select_model.sh 4b
sudo ./scripts/benchmark_models.sh
```

Solo se carga un modelo cada vez. El perfil 4B predeterminado usa un contexto de
3.072 tokens; el perfil opcional 8B usa uno de 2.048 y un lote menor para respetar
el límite de memoria de 8 GB. Si su servidor no alcanza un estado correcto,
`select_model.sh 8b` restaura automáticamente el perfil 4B.
El descargador reanuda una transferencia parcial, verifica el SHA-256 oficial y
solo entonces expone atómicamente el GGUF definitivo.
El benchmark activa un modelo cada vez, ejecuta la misma prueba multilingüe
acotada de calidad y velocidad, registra el tiempo transcurrido, la tasa de
tokens de llama.cpp y la memoria del proceso, y después restaura el modelo que
estaba activo antes de la prueba.

Instala el entorno compacto y opcional de conocimiento y construye la proyección
del grafo:

```bash
./scripts/install_knowledge_runtime.sh
./scripts/rebuild_graph.sh
```

Esto instala eSpeak NG para IPA local, fija LadybugDB 0.19.1 y Wn 1.1.1 en un
entorno aislado e instala únicamente los léxicos OMW 2.0 en inglés, japonés,
chino mandarín, francés y árabe. También verifica el archivo completo y fijado
de JMdict, construye el índice de lectura por forma exacta y elimina la descarga
original. Los volcados completos de Wiktionary se excluyen deliberadamente. La
extracción de IPA utiliza el modo de texto silencioso y no activa la salida de
voz.

En la Pi:

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

Para el desarrollo posterior Windows → GitHub → Pi:

```bash
cd /home/lachlan/LocalKnowledgeTerminal/source
./scripts/update_pi_tmux.sh
tmux attach -t lkt-update
```

El contenedor tmux mantiene activo el despliegue durante transiciones de SSH o
del navegador y escribe en `~/LocalKnowledgeTerminal/logs/update-pi.log`. El
script idempotente subyacente `scripts/install_services.sh` instala las tres
unidades systemd, las activa al arrancar, las inicia en orden modelo → web →
trabajador, verifica ambos endpoints de estado e instala la entrada gráfica de
inicio automático. `scripts/update_pi.sh` ejecuta toda la puerta de pruebas
antes de invocar ese instalador de servicios con `--restart`.

Después abre `http://127.0.0.1:8090` en el escritorio VNC de la Pi o
`http://<pi-lan-address>:8090` desde la red local de confianza.

El instalador también coloca `desktop/lkt-kiosk.desktop` en el directorio XDG de
inicio automático del usuario de la Pi e instala `scripts/open_kiosk.sh` como
`/usr/local/bin/lkt-open-kiosk`. En el siguiente inicio de sesión gráfico, el
lanzador espera al endpoint local de estado y abre exactamente un perfil
dedicado de Chromium en `http://127.0.0.1:8090/?display`. Volver a ejecutar el
lanzador es inocuo: detecta ese perfil y no abre otra ventana. Chromium se
inicia como una aplicación normal a pantalla completa, no como un quiosco
bloqueado, por lo que **Esc** abandona la pantalla completa y vuelve al
escritorio controlable de la Pi. Las URL explícitas de modo siguen disponibles
para uso deliberado mediante VNC.

## Datos y derechos de autor

Los PDF de libros, los corpus extraídos, los pesos de modelos, los índices
generados y las tarjetas guardadas se excluyen deliberadamente de Git. Durante
la instalación, proporciona una exportación JSONL local obtenida legalmente.
LKT registra cada SHA-256 en su índice SQLite para que una tarjeta generada
pueda rastrearse hasta la compilación exacta del corpus. Consulta
[`docs/corpora.md`](../docs/corpora.md) para conocer el conjunto de referencia
verificado.

## Linaje

LKT es un sucesor limpio y local-first inspirado por
[`WordsCardEink`](https://github.com/lachlanchen/WordsCardEink) y
[`WordOrigins`](https://github.com/lachlanchen/WordOrigins). No importa su
entorno monolítico ni sus dependencias de hardware. Consulta
[`docs/lineage.md`](../docs/lineage.md) para ver los commits fijados y las ideas
conservadas.

## Apoyo

| Donate | PayPal | Stripe |
| --- | --- | --- |
| [![Donate](https://img.shields.io/badge/Donate-LazyingArt-0EA5E9?style=for-the-badge&logo=ko-fi&logoColor=white)](https://chat.lazying.art/donate) | [![PayPal](https://img.shields.io/badge/PayPal-RongzhouChen-00457C?style=for-the-badge&logo=paypal&logoColor=white)](https://paypal.me/RongzhouChen) | [![Stripe](https://img.shields.io/badge/Stripe-Donate-635BFF?style=for-the-badge&logo=stripe&logoColor=white)](https://buy.stripe.com/aFadR8gIaflgfQV6T4fw400) |

## Cita

Si LKT respalda tu trabajo, cítalo mediante el menú **Cite this repository** de
GitHub, que lee [`CITATION.cff`](../CITATION.cff), o utiliza:

```bibtex
@software{chen_local_knowledge_terminal_2026,
  author = {Chen, Lachlan},
  title = {Local Knowledge Terminal},
  year = {2026},
  version = {0.1.0},
  url = {https://github.com/lachlanchen/LocalKnowledgeTerminal}
}
```
