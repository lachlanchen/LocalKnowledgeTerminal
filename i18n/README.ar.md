[English](../README.md) · [العربية](README.ar.md) · [Español](README.es.md) · [Français](README.fr.md) · [日本語](README.ja.md) · [한국어](README.ko.md) · [Tiếng Việt](README.vi.md) · [中文 (简体)](README.zh-Hans.md) · [中文（繁體）](README.zh-Hant.md) · [Deutsch](README.de.md) · [Русский](README.ru.md)

[![LazyingArt banner — Local Knowledge Terminal](../docs/assets/banner.svg)](../docs/assets/banner.svg)

# Local Knowledge Terminal

**ذكاء خاص يستند إلى الكتب ويعمل على أجهزتك أنت.**

[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![Model](https://img.shields.io/badge/Qwen3-4B%20%2F%208B-6f42c1?style=flat-square)](https://huggingface.co/Qwen/Qwen3-8B-GGUF)
[![Runtime](https://img.shields.io/badge/llama.cpp-v0.3.0-173f35?style=flat-square)](https://github.com/ggml-org/llama.cpp)
[![Target](https://img.shields.io/badge/Raspberry%20Pi%205-8GB-C51A4A?style=flat-square&logo=raspberrypi)](https://www.raspberrypi.com/products/raspberry-pi-5/)
[![Sponsor](https://img.shields.io/badge/Sponsor-lachlanchen-EA4AAA?style=flat-square&logo=githubsponsors&logoColor=white)](https://github.com/sponsors/lachlanchen)

يحوّل Local Knowledge Terminal (LKT) مجموعة كتب خاصة إلى بطاقات متعددة اللغات
ومسنَدة بالمراجع. تجمع مكتبته الأولى نسخًا منظّمة من **Word Origins** و**The Book
of Answers** و**The Book of Questions** و**English Root Dictionary** و**English
Affix Dictionary**. يعمل Qwen3-4B Q4_K_M محليًا على Raspberry Pi 5 بذاكرة 8
غيغابايت، مع Qwen3-8B كملف اختياري أبطأ؛ ويعمل الاسترجاع والاستدلال والسجل
وواجهة المتصفح من دون واجهة برمجية سحابية.

## جرّبه مع مجموعة واحدة

إذا كانت لديك بالفعل مجموعة محدودة من الكتب أو القواميس الخاصة، فإن
[تجربة ملاءمة المجموعة التأسيسية بقيمة 250 دولارًا أمريكيًا](https://lazying.art/lkt/)
تبدأ بفحص ملاءمة مجاني. تشمل مجموعة واحدة وهدفًا لغويًا واحدًا وجهازًا قائمًا
واحدًا، ثم تقدّم خريطة للبيانات والخصوصية والاستشهادات، وعينة متفقًا عليها لا
تتجاوز 12 وحدة مصدر و20 سؤال اختبار، وما يصل إلى بطاقتين مسنَدتين في المتصفح
حين تكون المادة قابلة للاستخدام، وتوصية واضحة بالمضي أو التوقف، وجولة واحدة من
التصحيح الواقعي. يحدّد النطاق المكتوب وحدة المصدر—مثل مقطع أو سجل أو صفحة
تمثيلية—قبل الدفع.
لا يشمل هذا النطاق الثابت الأجهزة أو الشحن أو OCR مخصصًا أو التحويل بالجملة أو
النشر للإنتاج أو الدعم المستمر.

لرؤية شكل هذه المخرجات الثلاثة بدقة من دون مشاركة أي مادة تخص عميلًا، اقرأ
[نموذج تقرير ملاءمة المجموعة](../docs/sample-fit-report.md). يطبّق التقرير التنسيق
على مجموعة LKT المرجعية الموثقة نفسها، وهو صراحةً ليس نتيجة عميل ولا ادعاءً عن
مهمة مدفوعة.

## ست تجارب مستقلة وعقد موحّد للبطاقات

- يستخدم **Word Origin** مسترجعًا ومحفّزًا خاصين بإدخال واحد لإنشاء رسم نسب موجّه
  تفاعلي ومحدود. تُحفَظ المورفيمات المتفرعة، ويظهر بوضوح الفرق بين العقد التي
  يدعمها الكتاب والسياق اللغوي الذي يضيفه النموذج.
- يسترجع **Word Card** عدة إدخالات ذات صلة من Word Origins وينشئ عرضًا مختصرًا
  متعدد اللغات للتذكّر. تظل الإنجليزية واليابانية والصينية ثابتة، بينما تتناوب
  الفرنسية والعربية في لوحة رابعة.
- يجري **Book Answer** سحبًا قابلًا للتكرار من 318 بطاقة مراجَعة، ويحافظ على
  ترجمات الإجابة المنشورة، ويضيف ملاحظة تأملية.
- يبحث **Book Question** في 291 سؤالًا مراجَعًا حسب الموضوع، ويلجأ إلى سحب قابل
  للتكرار عند غياب تطابق معجمي.
- يعطي **Root Graph** الأولوية لـ4,018 سجل جذر غني بالمحتوى، ثم إدخالات اللواصق
  الداعمة المطابقة، ويحفظ رسمًا عوديًا لعائلة الكلمات.
- يعكس **Affix Graph** هذه الأولوية عبر 5,179 سجل لاصقة غني بالمحتوى وقاموس
  الجذور، مع الاحتفاظ برسم كامل واحد للكلمة المركزية.

لكل وضع سياسة استرجاع ومحفّز نموذج صارم خاصان به. يتشارك Word Origin وWord Card
فهرس Word Origins نفسه عمدًا مع تقديمه بطريقتين مختلفتين؛ ويستخدم Answer وQuestion
كتابين ومحركي استرجاع منفصلين. تنتج الأوضاع الستة كلها صيغة JSON واحدة ذات إصدار
للبطاقة. يحتفظ نص كتاب البطاقات الياباني بالفوريغانا على مستوى الرمز، وتتلقى
العروض الصينية نظام pinyin حتميًا كاملًا بعلامات النغمات. تعرض واجهة الويب صيغة
JSON هذه اليوم؛ وستستهلكها محولات الحبر الإلكتروني والصوت لاحقًا من دون تغيير
الشيفرة الخاصة بالمجموعة أو الاسترجاع أو النموذج.

تتحدث مساحة عمل مستقلة باسم **Chat / Benchmark** مباشرةً إلى Qwen وتعرض الزمن
الفعلي ورموز الإدخال والإخراج وسرعة التوليد. وهي موسومة بوضوح كمخرجات نموذج خام
غير مسنَدة، ولا تُخزّن قط كبطاقة كتاب مسنَدة. تُحفَظ ملاحظاتها في جدول منفصل من
سجل المعرفة المحلي. ويظل كل محفّز مكرر يشغّل Qwen من جديد؛ فالسجل تاريخ وليس
ذاكرة تخزين مؤقت. ومن أي بطاقة، يفتح **Discuss this card** مختبر النموذج مع تلك
البطاقة المحفوظة ومقتطفها المسترجَع كسياق محدود.
وتحصل كل جلسة حية في Model Lab أيضًا على سلسلة استفسار دائمة. تحتفظ الجولات
المتعاقبة بنسب الأصل/الفرع؛ وترتبط مناقشة البطاقة بذرة محتوى مصدر مطبّعة، بينما
تبقى إجابة Qwen موسومة صراحةً بأنها غير مسنَدة.

## عرض المنتج

المتصفح منصة تحريرية للبطاقات لا لوحة دردشة. كل شريحة ظاهرة تكوين من شاشة واحدة
من دون تمرير، بفكرة أساسية كبيرة واستشهاد مصدر واحد مختصر. يخصص Word Origin
وسطه لرسم موجّه باستخدام Cytoscape.js. ويضع Word Card الإنجليزية/IPA بحجم كبير
فوق لوحتي اليابانية والصينية الثابتتين ولوحة الفرنسية/العربية المتناوبة. ويستخدم
Answer وQuestion دوّار لغات داخليًا—الإنجليزية، وروبي اليابانية، وروبي pinyin
الصينية—ويقسم الجمل الطويلة على نحو غير معتاد إلى شرائح إضافية مقروءة. يضيف
التحليل النحوي المحلي المقبول ألوانًا هادئة للأدوار إلى النص نفسه تمامًا؛ ولا
يضيف مفتاحًا أو بيانات وصفية مزدحمة. تكوّن البطاقات المحفوظة دوّارات خارجية
مستقلة ومحلية لكل وضع مع أدوات السابق/التالي.
تتشارك Root وAffix وWord Origin عارض رسم واحدًا من Cytoscape: رسم محفوظ كامل،
وخريطة نظرة عامة في الزاوية، وشرائح تركيز داخلية تكبّر جذرًا أو بادئة أو لاحقة أو
فرعًا تاريخيًا من دون تكرار الرسم.
يخفي وضع العرض ملء الشاشة جميع عناصر التطبيق، ويفتح `/?display=1` وثيقة البطاقة
نفسها كسطح عرض ملائم لوضع الكشك. توفر CSS الطباعة وصيغة JSON ذات الإصدار حدودًا
نظيفة للعرض اللاحق على الحبر الإلكتروني.

### عرض Raspberry Pi الحي

يستخدم Word Origin عقدًا متكيفة مع حجم المحتوى، ورسم نسب كاملًا، ولوحات معنى
متعددة اللغات، وشرائح للفروع، وإعادة ضبط إلى أفضل ملاءمة بنقرة واحدة.

![رسم Word Origin الحي على Raspberry Pi](../docs/assets/word-origin.png)

يحافظ Word Card على بروز الكلمة الإنجليزية وصوتها، ويعرض لوحتي اليابانية
والصينية الكبيرتين والثابتتين بجوار لوحة الفرنسية/العربية المتناوبة.

![بطاقة Word Card متعددة اللغات حية على Raspberry Pi](../docs/assets/word-card.png)

تحصل كل بطاقة مولّدة على معرّف جديد وتبقى في سجل البطاقات. وتخزّن قاعدة بيانات
ثانية مطبّعة باسم `knowledge.sqlite3` المصطلحات والمعاني والنطق ومقاطع
الفونيم/الغرافيم والمورفيمات والتاريخ والترجمات والنحو والمنشأ والمراجعات ونسب
الاستفسارات المقبولة بوصفها ذرات قابلة لإعادة الاستخدام. البطاقات عروض قابلة
لإعادة البناء فوق تلك الذرات. رسم خصائص LadybugDB إسقاط مشتق للتنقل ويمكن دائمًا
إعادة بنائه من SQLite.
وتضع بطاقات Book Answer وBook Question المقبولة أيضًا نصوصها الإنجليزية
واليابانية والصينية الدقيقة والمراجَعة في هذا المخزن المطبّع. كل لغة ذرة محتوى
مستقلة مرتبطة باستشهاد الكتاب الذي يملكه الاسترجاع؛ ويُستبعد تأمل النموذج عمدًا
من أدلة الكتاب. يقسّم Qwen كل لغة في مهمة محدودة منفصلة. لا تُقبل النتيجة إلا
إذا أعادت أجزاؤها المرتبة بناء الجملة المراجَعة حرفًا بحرف؛ وتبقى الأجزاء
المقبولة وروابط الأدلة وإصدار النموذج والتحليلات المستبدلة معرفة قابلة لإعادة
الاستخدام، لا مجرد ترميز للعرض.

### برهان المقطع إلى المنشأ

يحوّل [مثال مقطع PocketPolyglot](../examples/artifacts/pocketpolyglot-passage-graph.json)
مقطعًا محاذيًا كتبه المشروع إلى رسم مفاهيم صغير مراجَع يدويًا. تُحل كل علاقة إلى
وحدة المقطع والمقتطف وبصمة ملف المصدر المثبتة بدقة عبر واجهات معرفة LKT
الإنتاجية. أعد بناءه أو تحقق من أن الأثر المثبَت حديث:

```bash
python examples/pocketpolyglot_passage_graph.py
python examples/pocketpolyglot_passage_graph.py --check
```

### برهان الاجتماع الثنائي اللغة المكتوب

يربط [مثال الاجتماع الثنائي اللغة](../examples/artifacts/scripted-bilingual-meeting-knowledge.json)
عشر عبارات إنجليزية وصينية منفردة ذات طوابع زمنية بعشر وحدات معرفة مصنفة ومراجَعة
يدويًا. تحتفظ كل وحدة بالمتحدث والطابع الزمني والمدى الحرفي الدقيق للنص وبصمة
ملف المصدر وعلاقة الرسم المسنَدة بالأدلة. يتضمن سجل المراجعة تصحيحًا واحدًا
احتُفظ بإصداره السابق كمستبدَل عبر دورة آثار `KnowledgeStore` الحقيقية.
وللأثر نفسه [برهان تفاعلي في المتصفح](https://lazying.art/meeting-intelligence/)
لتتبّع الوحدة إلى كلمات مصدرها الدقيقة.

النص والتوقيت تجهيزات مكتوبة يملكها المشروع. هذا ليس معيارًا لدقة ASR أو فصل
المتحدثين أو الاستخراج أو الترجمة، وليس نشرًا لعميل ولا نتيجة عميل. أعد بناء JSON
المحمول أو تحقّق منه:

```bash
python examples/scripted_bilingual_meeting_knowledge.py
python examples/scripted_bilingual_meeting_knowledge.py --check
```

يستخدم التحضير مهام صغيرة تراعي الاعتماديات: استرجاع الأدلة، وتحضير معنى واحد،
وتقسيم المكونات، وتوسيع كل فرع أصل عوديًا، وتحضير كل لغة/نطق بصورة مستقلة، ثم
التحقق والتركيب. تُحفظ المراحل الناجحة فورًا كنقاط متابعة؛ ويمكن إعادة محاولة لغة
أو فرع ضعيف من دون التخلص من البقية.

ينمّي العامل منخفض الأولوية المثبّت الرزم الست الظاهرة في جولات متوازنة. يسحب
Question وAnswer من كتابيهما المراجَعين؛ ويتشارك Word Card وWord Origin تحقيقًا
ذرّيًا محدودًا واحدًا للكلمة؛ ويسحب Root وAffix كل منهما بصورة مستقلة من قاموسه
المنقح ويستخدم كتاب المورفولوجيا الآخر مع مطابقات محدودة من Word Origins كـRAG
مصاحب ذي صلة. ويختار دائمًا الوضع الظاهر الأقل امتلاءً، فلا يمكن لمسار سريع أن
يتقدم كثيرًا على غيره.
أثناء اللحاق، يوقف سحوبات Question/Answer الجديدة ولا يبقي أكثر من موضوع معجمي
ذاتي غير منتهٍ قيد التنفيذ. يعمل فحص التوازن بفاصل محدود حتى مع بقاء إثراء
اختياري في الطابور؛ وتُطالب المهام المعجمية قبل ذلك الإثراء النحوي للكتاب.

يظل كل مصدر غير مرئي يمر عبر بوابات Qwen المحلية وRAG والنشر المعتادة. تمنع
هويات المصدر والمصطلح المستقرة التكرار عبر إعادة التشغيل. قد يستخرج تحليل الكلمة
الذرّي عرض Root/Affix حين تحتوي الكلمة واحدًا فعلًا، لكن مسارات كتاب Root/Affix
المستقلة تضمن نمو تلك المنتجات حتى حين لا تحتوي الكلمة المختارة لاصقة منتجة. لا
يُختلق أي مكوّن لمجرد موازنة علامات التبويب.

يقسم تحضير Root/Affix العمل المكلف إلى استدعاءين محليين قابلين للاستئناف:
الرسم/التاريخ أولًا، ثم عرض صغير متعدد اللغات. للرسم سقف أكبر يبلغ 1,200 رمز
(1,400 لإصلاح جديد واحد)، بينما يستخدم استدعاء اللغة 512 رمزًا (640 للإصلاح).
لا تُعاد تغذية استجابة JSON مبتورة إلى Qwen بصورة عودية. تُحفظ كل مرحلة تم
التحقق منها مع نموذجها وبصمة دليلها الدقيقة، لذلك لا يهدر فشل مرحلة لاحقة الرسم.

يبدأ المتصفح المجرد بـQuestion، ثم يتابع Answer ← Word Card ← Word Origin ← Root
← Affix بعد إكمال كل بطاقة لجميع شرائحها الداخلية. يمكن لإعدادات العرض اختيار أي
وضع منفرد أو أي مجموعة فرعية مع الحفاظ على ذلك الترتيب القياسي؛ وتكون الستة
مختارة افتراضيًا. تُخلط البطاقات المحفوظة داخل كل وضع افتراضيًا، مع خيار ثابت من
الأحدث إلى الأقدم. يملك كل وضع دورة مختلطة مستقلة للبطاقات المقبولة فقط، ولذلك
لا يؤدي عبور علامات التبويب إلى دمج مجموعاتها أو تكرار البطاقة نفسها في كل
زيارة. توضع البطاقة المقبولة حديثًا أولًا في بقية دورة ذلك الوضع. تظل علامة تبويب
صريحة أو رابط `?mode=` محليًا للوضع، ويعيد نشاط المؤشر أو اللمس أو لوحة المفاتيح
بدء مدة إقامة البطاقة الحالية كاملة قبل استئناف الحركة المحيطة.

حد الملكية هذا مقصود: تأتي جمل الكتب المراجَعة وترجماتها واستشهاداتها من سجلات
المجموعة المحلية ولا يعيد النموذج كتابتها قط؛ وتنتج البيانات التفسيرية أو
المعجمية الجديدة من النموذج المحلي المضبوط، لا من إدخال يدوي إلى SQLite. تبقى
المسودة السيئة خارج الرزمة الظاهرة. وبالمثل، مرشحو القاموس والنطق/الروبي الحتمي
نتاج استرجاع/أدوات محلية لا بيانات بطاقات مكتوبة يدويًا. يوفر FreeDict بوابة
تصحيح إنجليزية-عربية دقيقة حين لا يملك OMW لِمّة عربية للمعنى المختار؛ ويجب على
Qwen نسخ مرشح مسترجَع واحد، ثم يرفق النظام معرّف دليله بعد التحقق. يفحص إصدار
JMdict الكامل المثبّت الصيغ والقراءات اليابانية محليًا: القراءة المطابقة لا تكلف
استدعاء نموذج، والتصحيح الفريد حتمي، ولا يحصل إلا شكل مكتوب ملتبس حقًا على اختيار
صغير من Qwen محصور في القراءات المسترجَعة. يعامل `/api/health` فهرسي التصحيح
المختصرين كمصدرين مطلوبين ويعرض جاهزيتهما وإصداراتهما وبصماتهما وعدد إدخالاتهما.
يتوقف التوليد الذاتي أثناء انخفاض جهد Raspberry Pi الحالي أو الاختناق أو ارتفاع
الحرارة، ويُستأنف بعد زوال الحالة. يحمّل عميل الويب الوضع المحدد كاملًا (حتى
1,000 بطاقة مقبولة)، ويبقي الأحدث أولًا، ويخلط كل بطاقة أخرى مرة واحدة في كل
دورة دوّار. يستطلع البطاقات المقبولة من دون مقاطعة العرض الحالي ويدرج النتيجة
المنشورة حديثًا تاليًا. يعرض كل من الملخص المضغوط و`/api/health` تغطية الكتب
المحدودة وتقدم المعجم المخطط/المقبول من دون جدولة عمل. تبقى طلبات الكلمات
التفاعلية فورية وتعيد استخدام الذرات الدائمة نفسها التي يستخدمها التحضير الذاتي.

يعرض تذييل المعرفة المكتسبة نافذة متحركة من 18 نقطة بطاقة كحد أقصى؛ وتظل أسهم
التنقل والعداد الدقيق `current / total` تغطي الرزمة المحفوظة غير المحدودة كاملة.
وتنتمي نقاط لغات Question/Answer إلى البطاقة الحالية فقط وتُستبدل عند تغيرها.

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

## قاعدة الإسناد

يكتب النموذج اللغوي الشروح والمساعدات اللغوية الناقصة، لكنه لا يكتب قائمة
الاستشهادات أبدًا. يرفق LKT معرّفات الإدخالات والمقتطفات والأقسام وأرقام الصفحات
والمواضع الرقمية وترجمات كتب البطاقات المراجَعة مباشرةً من سجلات الاسترجاع. قد
يضيف Word Origin سياقًا لغويًا موثوقًا، لكن كل عقدة في الرسم تسجل إن كانت من
مرساة الكتاب أم من معرفة النموذج. إذا لم يكن للكتاب المضبوط دليل، فلا ينشئ
التطبيق بطاقة.

## خريطة المستودع

| المسار | المسؤولية |
| --- | --- |
| `lkt/corpus.py` | إدخال Word Origins، فهرس SQLite ذري، استرجاع مطابق + FTS |
| `lkt/morphology.py` | إدخال Root/Affix بصيغة JSONL منقحة، المنشأ، استرجاع مطابق + FTS |
| `lkt/card_books.py` | إدخال Answer/Question متعدد اللغات، البحث، والسحوبات الحتمية |
| `lkt/deck.py` | تحضير كتابي ومعجمي بالتناوب، واحدًا في كل مرة |
| `lkt/device.py` | بوابة جاهزية الطاقة/الحرارة على Pi للاستدلال في الخلفية |
| `lkt/retrieval.py` | سياسات RAG مستقلة لـWord Origin وWord Card وAnswer وQuestion |
| `lkt/llm.py` | محول llama.cpp صغير ومحفّز صارم واحد لكل تجربة |
| `lkt/service.py` | تركيب البطاقات وتطبيعها |
| `lkt/pronunciation.py` | pinyin/روبي حتمي وIPA غير متصل ذي إصدار |
| `lkt/store.py` | بطاقات ذات إصدار، وآثار التحضير، والمراجعات، والأرشيف، وسجل الدردشة |
| `lkt/knowledge.py` | معرفة ذرية مثبتة، وأدلة، ومهام، ومراجعات، ونسب الاستفسارات |
| `lkt/preparation.py` | تخطيط كلمات/محتوى بأسلوب فرق تسد يراعي الاعتماديات |
| `lkt/atomic.py` | تحضير ذري محدود وتجميع بطاقات حتمي |
| `lkt/graph.py` | إسقاط تنقل LadybugDB قابل لإعادة البناء من ذرات SQLite المقبولة |
| `lkt/lexicon.py` | أدلة تصحيح WordNet متعددة اللغات ومضغوطة |
| `lkt/freedict.py` | إدخال FreeDict إنجليزي-عربي مطابق واسترجاع التصحيحات |
| `lkt/jmdict.py` | فهرس قراءة JMdict كامل مطابق للصيغة ومنشؤه |
| `lkt/web.py` | واجهة HTTP وخادم GUI بلا اعتماديات |
| `lkt/outputs.py` | حد مخرجات ثابت للويب/الحبر الإلكتروني/الصوت |
| `lkt/static/` | واجهة رسومية بمستوى سطح المكتب، مستجيبة بما يكفي لاستخدام الكشك لاحقًا |
| `scripts/` | أدوات قابلة للتكرار لوقت تشغيل Pi والتثبيت والتحديث واختبار الدخان |
| `systemd/` | خدمات النموذج والتطبيق المحصنة |
| `docs/lineage.md` | المنشأ الدقيق للمشروع القديم والمجموعة |
| `docs/product-brief.md` | متطلبات المالك الدائمة ومعايير القبول |
| `docs/knowledge-architecture.md` | عقد SQLite الذري وإسقاط الرسم والتحضير المرحلي |
| `docs/owner-request-log.md` | توجيهات المالك مرتبة زمنيًا ومنقحة الخصوصية |
| `docs/voice-hardware.md` | اختيار الميكروفون المدعوم واختبارات الصوت المرحلية |
| `docs/mode-roadmap.md` | خطة توسعة كتب اللواحق واللواصق والجذور المستقبلية |

## التطوير المحلي

ثبّت اعتمادية النطق الصغيرة المثبّتة الإصدار، ثم شغّل حزمة الاختبارات:

```powershell
cd C:\Users\Administrator\Projects\LocalKnowledge
python -m pip install -e .
python -m unittest discover -s tests -v
python -m compileall -q lkt tests
```

أنشئ فهرسًا محليًا من تصدير الكتاب المنظّم:

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

مع خادم llama.cpp يستمع على المنفذ 8081:

```powershell
python -m lkt.cli generate abacus --mode word
python -m lkt.cli generate "Should I begin?" --mode answer
python -m lkt.cli generate technology --mode question
python -m lkt.cli generate inspection --mode root
python -m lkt.cli generate abnormal --mode affix
python -m lkt.cli serve
```

افتح <http://127.0.0.1:8090>.

## تخطيط Raspberry Pi 5

```text
/home/lachlan/LocalKnowledgeTerminal/
├── source/      # this Git repository; updated with fetch + fast-forward pull
├── runtime/     # pinned llama.cpp plus optional knowledge Python environment
├── models/      # Qwen GGUF (not committed)
├── data/        # corpus index and saved cards (not committed)
└── logs/        # bootstrap logs (not committed)
```

آثار وقت التشغيل المثبّتة:

| الأثر | المراجعة | السلامة |
| --- | --- | --- |
| llama.cpp | `v0.3.0` / `c1d0e7a004015f23bc0233470b747b596f29b264` | أرشيف مصدر مثبّت على commit |
| Qwen3-4B-GGUF | `bc640142c66e1fdd12af0bd68f40445458f3869b` | Q4_K_M SHA-256 `7485fe6f…534fdf5` |
| ملف النموذج | `Qwen3-4B-Q4_K_M.gguf` | 2,497,280,256 بايت |

تعرض خدمة Pi خانة استدلال واحدة (`--parallel 1`). لذلك تُعالج عملية تركيب
البطاقات وطلبات Model Lab بالتتابع، مما يبقي استخدام الذاكرة والزمن المتوقعين
بدل جعل الأنوية الأربعة تتنافس بين المهام.

ثبتت قابلية استخدام Qwen3-8B كنموذج تحضير اختياري يقدّم الجودة. على جهاز Pi
المنشور أنتج اختبارًا متعدد اللغات من 120 رمزًا بسرعة 1.78 رمز/ثانية، وبنحو 6.28
GiB من RSS، مع بقاء 1.85 GiB من ذاكرة النظام متاحة ومن دون اختناق حراري حالي.
Qwen3-4B هو الافتراضي السريع غير المتصل. اختيار النموذج صريح وقابل للعكس:

```bash
tmux new-session -d -s lkt-8b-download \
  './scripts/download_qwen3_8b.sh > ../logs/qwen3-8b-download.log 2>&1'
sudo ./scripts/select_model.sh status
sudo ./scripts/select_model.sh 8b
sudo ./scripts/select_model.sh 4b
sudo ./scripts/benchmark_models.sh
```

لا يُحمّل إلا نموذج واحد في كل مرة. يستخدم ملف 4B الافتراضي سياقًا من 3,072
رمزًا؛ ويستخدم ملف 8B الاختياري سياقًا من 2,048 رمزًا ودفعة أصغر لحماية حد
ذاكرة 8 غيغابايت. إذا لم يصبح خادمه سليمًا، يعيد `select_model.sh 8b` ملف 4B
تلقائيًا.
يستأنف المُنزّل نقلًا جزئيًا، ويتحقق من SHA-256 الرسمي، ثم يكشف ملف GGUF النهائي
ذريًا فقط.
ينشّط المعيار نموذجًا واحدًا في كل مرة، ويشغّل اختبار الجودة/السرعة المحدود متعدد
اللغات نفسه، ويسجل الزمن الفعلي ومعدل رموز llama.cpp وذاكرة العملية، ثم يعيد
النموذج الذي كان نشطًا قبل الاختبار.

ثبّت وقت تشغيل المعرفة الاختياري المضغوط وأنشئ إسقاط الرسم:

```bash
./scripts/install_knowledge_runtime.sh
./scripts/rebuild_graph.sh
```

يثبّت هذا eSpeak NG لـIPA المحلي، ويثبّت LadybugDB 0.19.1 وWn 1.1.1 في بيئة
معزولة، ثم يثبّت فقط معاجم OMW 2.0 الإنجليزية واليابانية والصينية المندرينية
والفرنسية والعربية. ويتحقق أيضًا من أرشيف JMdict الكامل المثبّت، وينشئ فهرس
القراءة المطابق للصيغة، ويحذف التنزيل الخام. تُستبعد تفريغات Wiktionary الكاملة
عمدًا. يستخدم استخراج IPA وضع النص الصامت ولا يفعّل إخراج الكلام.

على Pi:

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

لتطوير Windows ← GitHub ← Pi لاحقًا:

```bash
cd /home/lachlan/LocalKnowledgeTerminal/source
./scripts/update_pi_tmux.sh
tmux attach -t lkt-update
```

يحافظ غلاف tmux على استمرار النشر عبر انتقالات SSH أو المتصفح، ويكتب إلى
`~/LocalKnowledgeTerminal/logs/update-pi.log`. يثبّت السكربت الأساسي المتكرر
`scripts/install_services.sh` وحدات systemd الثلاث، ويمكّنها عند الإقلاع، ويبدأها
بترتيب النموذج ← الويب ← العامل، ويتحقق من نقطتي الصحة، ويثبّت إدخال التشغيل
التلقائي الرسومي. ويشغّل `scripts/update_pi.sh` بوابة الاختبار الكاملة قبل استدعاء
مثبّت الخدمة ذلك مع `--restart`.

ثم افتح `http://127.0.0.1:8090` في سطح مكتب VNC الخاص بـPi، أو
`http://<pi-lan-address>:8090` من الشبكة المحلية الموثوقة.

يضع المثبّت أيضًا `desktop/lkt-kiosk.desktop` في مجلد التشغيل التلقائي XDG
لمستخدم Pi، ويثبّت `scripts/open_kiosk.sh` باسم
`/usr/local/bin/lkt-open-kiosk`. عند تسجيل الدخول الرسومي التالي ينتظر المشغّل
نقطة الصحة المحلية ويفتح ملف Chromium مخصصًا واحدًا بالضبط على
`http://127.0.0.1:8090/?display`. تشغيل المشغّل مرة أخرى غير ضار: فهو يكتشف ذلك
الملف ولا يفتح نافذة أخرى. يبدأ Chromium كتطبيق عادي بملء الشاشة لا ككشك مقفل،
لذلك يخرج **Esc** من ملء الشاشة ويعيدك إلى سطح مكتب Pi القابل للتحكم. وتظل روابط
الأوضاع الصريحة متاحة لاستخدام VNC المتعمد.

## البيانات وحقوق النشر

تُستبعد ملفات PDF للكتب والمجموعات المستخرجة وأوزان النماذج والفهارس المولّدة
والبطاقات المحفوظة من Git عمدًا. قدّم تصدير JSONL محليًا حصلت عليه بصورة قانونية
أثناء التثبيت. يسجل LKT قيمة SHA-256 لكل منها في فهرس SQLite كي يمكن تتبع البطاقة
المولدة إلى بناء المجموعة الدقيق. راجع [`docs/corpora.md`](../docs/corpora.md)
لمعرفة المجموعة المرجعية المتحقق منها.

## النسب

LKT وريث نظيف ومحلي أولًا، مستلهم من
[`WordsCardEink`](https://github.com/lachlanchen/WordsCardEink) و
[`WordOrigins`](https://github.com/lachlanchen/WordOrigins). وهو لا يستورد وقت
تشغيلهما الأحادي أو اعتماديات الأجهزة. راجع
[`docs/lineage.md`](../docs/lineage.md) لمعرفة الالتزامات المثبّتة والأفكار
المحتفظ بها.

## الدعم

| Donate | PayPal | Stripe |
| --- | --- | --- |
| [![Donate](https://img.shields.io/badge/Donate-LazyingArt-0EA5E9?style=for-the-badge&logo=ko-fi&logoColor=white)](https://chat.lazying.art/donate) | [![PayPal](https://img.shields.io/badge/PayPal-RongzhouChen-00457C?style=for-the-badge&logo=paypal&logoColor=white)](https://paypal.me/RongzhouChen) | [![Stripe](https://img.shields.io/badge/Stripe-Donate-635BFF?style=for-the-badge&logo=stripe&logoColor=white)](https://buy.stripe.com/aFadR8gIaflgfQV6T4fw400) |

## الاستشهاد

إذا أفاد LKT عملك، فاستشهد به من قائمة GitHub **Cite this repository** التي تقرأ
[`CITATION.cff`](../CITATION.cff)، أو استخدم:

```bibtex
@software{chen_local_knowledge_terminal_2026,
  author = {Chen, Lachlan},
  title = {Local Knowledge Terminal},
  year = {2026},
  version = {0.1.0},
  url = {https://github.com/lachlanchen/LocalKnowledgeTerminal}
}
```
