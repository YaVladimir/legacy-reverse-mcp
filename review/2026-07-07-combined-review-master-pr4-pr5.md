# Ревью подхода: master + ПР #4 + ПР #5 (2026-07-07)

Скоуп: вся линия работ ПР #3→#5 — `git diff 3b8913a..HEAD` на ветке
`review/combined-master-pr4-pr5` (локальная: origin/master + merge
`fix/batch-generate-gigacode-robustness` (ПР #4) + merge
`fix/endpoint-reattribution-safety` (ПР #5)). 19 файлов, +1644/−77.
Весь suite на совокупной ветке зелёный (108 тестов).

Метод: пять последовательных пасов независимыми агентами —
line-by-line скан, removed-behavior аудит, cross-file трассировка,
reuse/simplify/efficiency, altitude-проверка. Находки, помеченные
«воспроизведено», агенты подтвердили исполнением кода / реальным пайплайном.

## Главный вывод

Подход в целом верный, но скопление багов — не 14 отдельных ошибок, а **два
неверно выбранных примитива**: (A) необратимый DELETE endpoint-строк там, где
нужен supersede-маркер; (B) состояние батч-генерации, размазанное по четырём
носителям, плюс двойная идентичность классов (валидация по id, импорт по
pkg+name). Плюс два серьёзных бага сканера вне этих примитивов.

---

## Баги корректности — HIGH

### H1. Репозиторий по «неудачному» пути молча индексируется в ноль
`scanner/repo_scanner.py:21` (`_is_ignored_path`) проверяет компоненты
**абсолютного** пути, включая каталоги выше корня репо. Клон в
`C:\ci\build\myrepo` → каждый файл получает `True`, индекс из 0 классов без
единого предупреждения (**воспроизведено**: `_is_ignored_path(Path(r'C:\ci\build\myrepo\src\main\java\ru\bank\Foo.java')) → True`;
даже `build/generated/**` внутри такого репо игнорируется — внешний `build`
срабатывает первым). Точки применения: `repo_scanner.py:177`,
`java_indexer.py:82`, `config_scanner.py:75`.

Смежный механизм (removed-behavior): `prune_dirnames`
(`repo_scanner.py:34-42`) смотрит на `dirpath.name` без проверки «это корень
репо или вложенный build-каталог». Репозиторий, чей корень называется `build`
или `target`, на первом же шаге `os.walk` теряет всё, кроме
`generated`/`generated-sources` — `src/**` отсекается. До диффа оба сценария
работали (старый код фильтровал только детей в walk). Даже починка
`_is_ignored_path` на repo-relative не спасает от `prune_dirnames(root)` —
чинить оба места.

**Фикс:** обе проверки перевести на repo-relative части пути
(`path.relative_to(repo_root).parts`), корень репо исключить из policy.

### H2. `scan` падает целиком на легаси-кэше описаний
`summarizer/describe.py:171` — `reapply_imported` открывает
`descriptions.sqlite3` сырым `sqlite3.connect`, мимо `_open_cache`
(`describe.py:53`), где живёт миграция `ALTER TABLE ... ADD COLUMN
content_hash`. Проверяется только существование таблицы, затем
`imported_for_class` делает `SELECT ... content_hash ...`. Вызов из
`scanner/pipeline.py:87` — без try/except.

Сценарий (**воспроизведено**): пользователь делал `import-arch` на старой
версии (таблица по старой схеме, заполнена) → обновился → `scan --force` /
MCP `scan_repository(force=true)` → `OperationalError: no such column:
content_hash` → весь скан падает, индекс полупостроен, манифест не записан.
Любой битый/не-sqlite `descriptions.sqlite3` — та же фатальная ошибка.

**Фикс:** (1) использовать `_open_cache` (заодно reuse, см. Q4); (2)
best-effort стадия обязана деградировать до warning — try/except вокруг
вызова в pipeline и внутри `reapply_imported`.

### H3. Реаттрибуция теряет class-level `@RequestMapping` интерфейса
`scanner/java_indexer.py:488` — `base_path = class_base_path(class_anns)`
берётся только с конкретного контроллера; `:517-519` — `full_path =
join_paths(base_path, ep.sub_path)`; `:544-547` — исходная interface-строка
(с корректным путём) удаляется, когда все контроллеры заявились.

Сценарий (**воспроизведено реальным пайплайном**):
`@RequestMapping("/api/v1") interface DealsApi { @GetMapping("/deals") }` +
голый `@RestController class DealsController implements DealsApi` (типовой
openapi-generator код). Spring обслуживает `/api/v1/deals` (type-level
mapping наследуется с интерфейса), в индексе остаётся **единственная** строка
`/deals` — правильная удалена. Ломаются: `list_endpoints(path_contains=...)`,
`trace_endpoint` (not_found по реальному роуту), impact/context_pack
(неверный путь в evidence), FTS. Регрессия точности данных.

### H4. Interface-строка удерживается навсегда при собственной аннотации на override
`scanner/java_indexer.py:544-548` — `implementors` строится чисто по
иерархии (`owner in ancestors`), но контроллер, чей переопределённый метод
имеет **свою** mapping-аннотацию, пропускается (`continue` на ~:499) и
никогда не попадает в `claimers` → `implementors - claimers ≠ ∅` вечно.

Сценарий: контроллер A переопределяет без аннотации (claims), контроллер B —
со своей `@GetMapping` (skip) → interface-строка живёт как фантомный третий
эндпоинт. До ПР #5 случай работал правильно (A claim-ил → строка удалялась,
у B своя строка). Комментарий на :537-541 перечисляет только delegate и
unresolved как причины не-claim'а — третий случай пропущен.

**Фикс H3+H4 разом:** supersede вместо DELETE (см. вердикт A ниже).

---

## Баги корректности — MEDIUM

### M1. Старая база + новый код: `trace_endpoint` падает исключением
`index/schema.sql` (`CREATE VIEW IF NOT EXISTS` не пересоздаёт
`v_endpoint_full` в до-апгрейдной базе) + `analysis/trace.py:138`
(`full["annotation_inherited"]` по `sqlite3.Row`) → `IndexError: No item with
that key` вместо структурированной ошибки «rescan». Write-путь безопасен
(scan всегда unlink+init_db), ломается только чтение старой базы.

### M2. `--resume` валидирует старые chunk-файлы против свежей нарезки
`summarizer/batch_generate.py:373` (`if args.resume and path.exists():
continue` — старые границы чанков сохраняются) + `:392` (валидация по
`chunks[i]` из свежей пере-нарезки arch.json). Смена `--chunk-size` между
запусками или перегенерация arch.json → легитимные классы отбрасываются как
extraneous, свои числятся missing, ретраи гоняют старые файлы по кругу.
`--merge-only` от этого защищён явно (:348-358), resume — нет.

### M3. Retry частичного чанка затирает sidecar до признания замены годной
`batch_generate.py:201` (безусловная запись stdout поверх sidecar) +
`:397-401`. Старая гарантия ПР #3: partial-чанк не перезапускался → sidecar
не затирался. Теперь: retry выдал непарсибельный/худший вывод + прерывание до
импорта (Ctrl+C, падение) → sidecar затёрт, следующий `--resume` считает чанк
полностью неописанным — готовые описания потеряны из resume-состояния
(повторный LLM-расход), вопреки выводу «existing descriptions kept» (:406).

### M4. Двойная идентичность: валидация принимает по id, импорт матчит по pkg+name
Producer: `batch_generate.py:141-152` — ключ приёмки = нормализованный `id`;
согласованность pkg/name с отправленным не проверяется (комментарий :148-149
утверждает обратное). Consumer: `analysis/flat_arch.py:182-187` — `import_flat`
игнорирует id: fqn = pkg+name, при неудаче fallback
`WHERE simple_name = ? ORDER BY fqn LIMIT 1` (:152-158).

Сценарий: модель вернула точный id, но переписала `name`/`pkg` → валидация
«ok», импорт: либо молча в `unmatched_classes`, либо описание **ложится на
чужой класс** и закрепляется в durable-store с чужим structure-hash — ровно
«garbage in would stick», от чего валидация по замыслу защищает.

Смежное (порядкозависимость): `batch_generate.py:136,148-157` — fallback по
pkg+name позволяет записи с переписанным id перехватить `seen`-слот раньше
точной записи (та молча пропускается, без учёта в extraneous);
`name_to_key` — dict «последний выигрывает»: два отправленных класса с
одинаковым pkg+name но разными id (реальный случай: `src/**` и его копия в
`build/generated/**`) схлопываются — описание второго теряется и не попадает
в missing.

**Уточнение (реальный прогон, 350 классов, 17 мисматчей).** Gigacode вернул
`id` и `name` для всех 17, но не вернул `pkg` — импорт не смог их
сопоставить. Тот же разрыв, что выше, с обратным триггером: исходный сценарий
M4 — модель переписала pkg/name при верном id; здесь — id честный, просто pkg
не пришёл вовсе. Механизм идентичен: `_class_key` (`batch_generate.py:141-152`)
предпочитает id → валидация такие записи пропускает штатно; `import_flat`
(`flat_arch.py:182-183`, через `_resolve_class_row` :152-158) резолвит только
через `_entry_fqn` (pkg+name) → simple_name-фоллбэк и **вообще не смотрит на
`entry.get("id")`** — при этом `id` на экспорте (`_flat_id`,
`flat_arch.py:47-55`) это repo-relative путь без `.java`, почти всегда
уникальный и надёжнее pkg+name, которое gigacode может просто не вернуть.

Меняет направление фикса: не «унифицировать identity на pkg+name» (как
предложено выше в вердикте B), а наоборот — `import_flat` должен матчить в
первую очередь по `id` (джойн на `class.file_path`, repo-relative; для старых
абсолютных путей — см. L2), pkg+name/simple_name оставить fallback'ом для
записей без id. Валидация (`_class_key`) так уже делает; рассинхрон именно на
стороне импорта.

(Побочно подтверждено тем же прогоном: «10 классов не попали в gigacode» —
не баг, а честная работа `missing_classes` — prompt требует вернуть все
классы, недостача репортится, а не прячется, см. Q6.)

### M5. Свежесть класса решается произвольной строкой кэша
`summarizer/describe.py:150-151` — `imported_for_class` берёт `stored_hash`
из первой попавшейся строки с непустым `content_hash` без ORDER BY, при том
что хэши per-row. Частичный повторный импорт (обновилась только class-строка
с H2, method-строки несут H1) → либо свежее описание класса выкинуто как
stale, либо устаревшие описания методов восстановлены как свежие.
**Фикс:** нормализовать хэш в per-class хранение
(`imported_class(fqn, content_hash)` или единая строка).

### M6. `--merge-only` при дырке в нумерации chunk-файлов
`batch_generate.py:348-364` — `disk_chunks` это позиции sorted-glob
`chunk-????.json`, а out-файлы читаются по номеру `i`. Пропущен
`chunk-0002.json` (есть 0000/0001/0003) → `disk_chunks[2]` = содержимое
chunk-0003, валидируется против `out-chunk-0002.json` → массовый reject всех
чанков после дырки. **Фикс:** матчить по числовому суффиксу имени файла, не
по позиции.

### M7. Поле того же пакета не резолвится в FQN — ни при индексации, ни при экспорте
`scanner/java_indexer.py:96-118` (`_resolve_types_inplace`) резолвит тип
только через `imap` — карту из `import`-операторов файла (:86-93). Java не
требует import для типа из того же пакета, что и объявляющий класс — такой
тип остаётся как написан, `field.type_fqn` = голое `"SomeCheck"`, а не
`"com.example.SomeCheck"`. `tests/test_type_resolution.py` покрывает только
cross-package случай через явный import; same-package случай без теста.

Симптом виден в экспортируемом flat JSON (`flat_arch.py:73`,
`_to_flat_class`): `"fields"` одного класса вперемешку несут то короткое имя
(поле своего пакета), то полный `pkg.Name` (импортированное) — потребитель
(в т.ч. gigacode из M4) не может единообразно судить о типе поля без
побочного знания «пакет не указан = тот же, что у класса-владельца». Тот же
класс проблемы, что и M4 — неполная/непоследовательная квалификация
идентичности сквозь пайплайн, просто в другой паре producer/consumer.

Смежный эффект: `index_class_dependencies` (`java_indexer.py:311-367`)
осознанно и задокументированно (:314-317) фоллбэчит нерезолвленные простые
имена на **все** одноимённые классы в проекте («over-approximation, so
change-impact never misses a dependent»), последствие раскрыто через
`ambiguous_simple_name` (`impact.py:191-193`) — но `change_impact` при этом
безусловно помечает такие рёбра `confidence: high` (`impact.py:17,112`:
`_HIGH_VIA` включает `field_injection`/`inheritance` без разбора, была ли
связь резолвлена точно по fqn или подобрана приближённо по имени).
Прецизионное резолвление same-package типов не только починило бы `type` в
экспорте, но и бесплатно убрало бы часть этой approximation.

**Фикс:** резолвить same-package типы вторым проходом, симметрично
`index_class_dependencies` (когда все классы уже распарсены и есть
`fqn_to_id`), а не внутри `_resolve_types_inplace` (та работает файл-за-файлом
до того, как класс попадёт в БД — раньше нет данных, чтобы отличить «тип из
своего пакета» от «тип из wildcard-импорта», и оба сейчас остаются
неразличимо нерезолвленными). Для `field.type_fqn` /
`method.return_type` / `method_parameter.type_fqn` без `.` в значении: если
существует класс с fqn `{package}.{simple}`, переписать на него; иначе
оставить как есть — существующий simple-name-фоллбэк не трогается.

---

## Баги корректности — LOW

- **L1.** `batch_generate.py:379` требует `shutil.which(gigacode_cmd)` (PATH),
  а `harness.py:70-75,92-97` принимает env `GIGACODE`/`GIGACODE_CLI` — при
  установке только через env MCP `generate_architecture` работает, batch
  отказывается.
- **L2.** `docs/mcp-api.md:14-16` обещает «file_path always repo-relative» —
  но старый `index.sqlite3` (который read-тулы спокойно читают без требования
  рескана) отдаёт абсолютные пути. Смежно: batch уважает
  `LEGACY_REVERSE_GIGACODE_CMD/_ARGS/_CWD`, но игнорирует
  `_TIMEOUT/_OUTPUT/_PROMPT` — из доков не видно.

---

## Качество кода (пас №4, по убыванию ценности)

- **Q1 (efficiency).** `describe.py:179-188` `reapply_imported` — O(F×R):
  `WHERE ref_key = ? OR substr(ref_key,1,?) = ?` не использует PK-индекс →
  полный скан таблицы на каждый класс, внутри **каждого** скана. Замена: один
  SELECT всей таблицы + группировка по fqn-префиксу в Python.
- **Q2 (reuse).** `batch_generate.py:167-205` `_run_single_chunk` копирует
  ~30 строк `harness.py:105-153` `run_gigacode` (subprocess-kwargs, обработка
  Timeout/OSError, тексты ошибок, `_extract_json`). Научить `run_gigacode`
  отдавать сырой stdout → batch = обёртка. Windows-квирк `.cmd/.bat` и тексты
  ошибок перестанут разъезжаться; вернётся потерянный `hint` при argv=None.
- **Q3 (efficiency).** `java_indexer.py:443-447,491-495` — N+1: аннотации
  методов/классов дёргаются SELECT'ом в цикле (в т.ч. внутри BFS), хотя
  остальные 4 таблицы предзагружены. Предзагрузить двумя запросами.
- **Q4 (reuse).** `describe.py:171-178` — сырой connect вместо `_open_cache`
  (см. H2): замена 8 строк на 1, миграция приезжает бесплатно.
- **Q5 (simplify).** `batch_generate.py:390-394,426-433` — sidecar готового
  чанка парсится дважды, поиск `any(r[0]==i ...)` квадратичный; в точке skip
  сразу `raw_results.append((i, existing, {"resumed": True}))` — блок 426-433
  удаляется.
- **Q6 (simplify).** `batch_generate.py:459` — `missing_classes` пересчитывает
  то, что `_validate_chunk_result` уже вернул в `vinfo["missing"]`.
- **Q7 (simplify).** Мёртвый параметр `project` в `_make_chunk_json` (все
  вызовы передают его собственный fallback); ручная сборка merged-envelope
  :472-477 = `_make_chunk_json(original, merged_classes)`.
- **Q8.** `env=os.environ.copy()` — no-op (дефолт subprocess и так наследует).
- **Q9.** Локальный импорт `reapply_imported` в pipeline с комментарием про
  несуществующее ограничение (`summarizer.llm` — stdlib-only, цикла нет,
  pipeline уже импортирует summarizer на верхнем уровне).
- **Q10.** Кросс-модульные импорты приватных `_build_argv`/`_extract_json` из
  harness — сделать публичными; `_class_key` fallback даёт `"None.Name"` для
  классов без pkg, тогда как `_entry_fqn` возвращает голое имя — reuse
  `_entry_fqn` выровнял бы идентичности буквально.

Оправданные «дубли» (проверено, не трогать): `structure_hash` vs `_class_hash`
(намеренно разные, образцовая факторизация через `_stable_projection`);
`_normalize_id` vs `_flat_id` (разные направления); `ancestor_closure` vs
`find_ancestor_endpoint` (разная форма результата); per-file
`_is_ignored_path` в `_iter_java_files` не избыточна относительно
`prune_dirnames`.

---

## Вердикты по высоте (пас №5)

### A. Реаттрибуция: спорная — заменить DELETE на supersede
Буква evidence-модели не нарушена (таблица `observed_facts` не трогается),
provenance-колонки и `inherited_mapping_annotation` — правильное чутьё. Но
строка-замена — инференс (BFS + матчинг + эвристика удаления),
материализованный необратимой операцией в таблицу, которую все читатели
считают наблюдённой; `list_endpoints` отдаёт реаттрибутированный эндпоинт
неотличимым от прямого, без confidence. Все три симптома (H3, H4, delegate)
— баги именно условий необратимого удаления.

**Альтернатива:** колонка `endpoint.superseded` (или
`attribution = direct|interface_declared|reattributed`), `v_endpoint_full`
фильтрует по умолчанию. Ошибка эвристики деградирует до «лишняя строка», а не
«эндпоинт исчез»; delegate решается пометкой; читатели получают исправление
через view бесплатно. Цена сейчас: ~50 строк (колонка + WHERE + UPDATE вместо
DELETE + тесты). После появления внешних потребителей — миграция данных.

### B. Батч: контракт спорный, state-management неверный
Промпт отдаёт модели владение идентичностью (эхо id), а `_normalize_id` +
fallback компенсируют невыполнение — симптом неверного контракта. Правильная
форма: **единая идентичность — везде primary key = id, pkg+name — fallback
для записей без id** (**пересмотрено** после реального прогона в M4: pkg —
ненадёжное поле, gigacode может его не вернуть вовсе; id — repo-relative путь,
почти всегда уникальный и присутствует даже когда pkg/name искажены или
отсутствуют). `_class_key`/валидация уже так делают; довести до `import_flat`
и `_resolve_class_row` (сейчас там ровно наоборот — см. M4-уточнение); заодно
уйдёт порядкозависимость (~30 строк + ужесточить `_resolve_class_row`).

Истина о прогрессе размазана по 4 носителям (chunk-файлы, sidecar'ы,
out-файлы, память + перечитывание). Правильная форма: **манифест прогресса**
`batch-manifest.json` в work_dir (хэш arch.json, chunk_size, per-chunk
контент-хэши и статусы pending/partial/done/failed, принятые ключи). Resume =
сверка манифеста (несовпадение хэша → явный отказ вместо тихого mass-reject);
merge-only = тот же код-путь; M2/M3/M6 исчезают классом. Цена: рефакторинг
шагов 3–7 в `main()` (~100 строк; функция уже несёт `noqa: C901`). Ядро
(chunk → параллельные subprocess → validate → merge → import) — высота верная.

### C. Автовосстановление: высота верная
`index.sqlite3` = пересобираемый кэш, `descriptions.sqlite3` = durable-слой,
`reapply_imported` = материализация durable в кэш — правильно; альтернативы
(не сносить при force, инкрементальный рескан, read-side join при FTS со
summary) хуже или дороже. Стадия размещена верно (после детерминированных
summary, до FTS). Обязательные упрочнения: failure-isolation (H2) и
per-class хэш (M5) — оба локальны.

### Границы, расширяемость, тесты
- `pipeline.py` → summarizer: направление зависимости старее диффа, ок;
  настоящий (отложенный) вопрос — pipeline как app-оркестратор внутри scanner/.
- `batch_generate` как `__main__` — вторая CLI-поверхность рядом с click-based
  `cli.py` (другой стиль флагов, нет discoverability): интегрировать
  подкомандой `legacy-reverse batch-generate` (~30 строк) до того, как путь
  `python -m summarizer.batch_generate` разойдётся по инструкциям агентам.
- `_CODEGEN_ALLOWED_CHILD` покрывает kapt/protobuf/annotationProcessor, но не
  `target/generated-test-sources`, delombok, кастомные buildDir — нужен
  runtime-люк (`LEGACY_REVERSE_EXTRA_SOURCE_DIRS` / опция scan).
- **`docs/limitations.md` теперь противоречит коду**: «Generated code …
  invisible», а тесты диффа утверждают обратное. Нужен уточнённый
  LIMITATIONS-код: generated видим только если проект собран, свежесть
  зависит от последнего билда.
- Gigacode-лок мягкий: `HarnessConfig` — фактически нейтральный контракт
  «headless CLI, печатающий flat JSON»; задокументировать как таковой.
- Тесты: e2e-конвенция соблюдена (test_endpoint_reattribution,
  test_reapply_imported — образцовые). Системный пробел — **нет теста
  жизненного цикла resume/merge-only как последовательности запусков**
  (запуск → сбой → resume; чужие out-файлы → merge-only; свежая нарезка →
  отказ): все баги B жили в непокрытых переходах состояний. Манифест
  прогресса сделает такие тесты тривиальными.

---

## Рекомендуемый порядок исправлений

1. **H1 + H2** — тихая потеря данных и падение скана: блокеры мержа ПР.
2. **Supersede-рефакторинг A** — закрывает H3 + H4 (+ delegate) разом; делать
   до появления внешних потребителей `v_endpoint_full`.
3. **Манифест + единая id-идентичность в B** — закрывает M2/M3/M4/M6 классом
   (направление — id primary, pkg+name fallback; см. M4-уточнение).
4. M1 (structured error на старой базе), M5 (per-class хэш), M7 (same-package
   type resolution — второй проход, симметричный `index_class_dependencies`),
   L1/L2.
5. Качество: Q1–Q5 по пути соответствующих фиксов (Q4 сливается с H2).
6. Гигиена: cli-подкоманда, конфиг-люк codegen-каталогов, правка
   docs/limitations.md, lifecycle-тесты resume/merge-only.
