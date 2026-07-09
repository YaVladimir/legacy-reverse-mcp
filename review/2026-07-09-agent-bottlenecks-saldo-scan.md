# Review — узкие места индекса для ИИ-агента (скан `saldoforreverse`)

- Дата: 2026-07-09
- Цель: свежий скан реального Spring Boot 4.x проекта (`saldoforreverse`), затем поиск
  мест, где индекс/находки вводят будущего ИИ-агента (который пишет фичи по этим данным)
  в заблуждение или недодают контекста.
- Прогон: `scan --report` (175 классов, 19 эндпоинтов, 11 scheduled, 4 kafka-listener)
  + deep-скан (`batch_generate` через `claude`/haiku, 5 потоков, чанки по 5) —
  175/175 классов получили описания.

## Итог

Проект вскрылся чисто: эндпоинты корректно переотнесены с openapi-интерфейсов на
конкретные контроллеры, cron/kafka-атрибуты захвачены как raw-текст, конфиг и профили
проиндексированы. Файловый счётчик 224 `.java` → 175 проиндексировано — **не дефект**:
137 `src/main` + 38 `build/generated` = 175, а 49 — это `src/test` (осознанно не
индексируются, лимитация `tests_not_indexed`).

Найденные узкие места ниже. **B1/B2 (gradle, spring-jpa) — по указанию заказчика
пропускаются** (задокументированы для полноты, но не чинятся в этой итерации).

---

## B1 (High) — Spring Data JPA репозитории классифицируются как `unknown` · SKIP

Все 8 интерфейсов `interface XRepository extends JpaRepository<E, ID>` получают
`role=unknown`; отчёт печатает **`Repositories: 0`**, хотя слой данных явно есть.
Наследование `JpaRepository`/`CrudRepository`/… — definitive-сигнал (Spring Data
создаёт прокси-бин), не догадка. `scanner/spring_scanner.py:classify_role` осознанно
пасует на этом (комментарий на строках 44-47).

> **Статус: SKIP** — по прямому указанию заказчика находки по spring-jpa не трогаем.

## B2 (High) — Gradle Kotlin DSL + version catalog не парсятся → `External deps: 0` · SKIP

`scanner/dependency_scanner.py:parse_gradle_module` читает только `build.gradle` и
`dependencies.gradle`, но не `build.gradle.kts`. Вдобавок проект использует Gradle
version catalog (`implementation(libs.spring.web)`), где реальные GAV-координаты
живут в `gradle/libs.versions.toml`. В итоге стек фреймворков (Spring Boot 4.1,
Kafka, JPA, Redis, Vault, resilience4j, liquibase, mapstruct, …) агенту невидим.

> **Статус: SKIP** — по прямому указанию заказчика находки по gradle не трогаем.

## B3 (Medium) — value-record'ы всплывают как «Possibly a service» — FIX

`analysis/layers.py:compute_low_confidence_findings` присваивает слой по токену
пакета/суффиксу имени, **не глядя на `kind`**. В пакете `service.bank` пять record'ов —
`AccountBalance`, `AccountInfo`, `OAuthTokens`, `PagedTransactions`, `Transaction` —
это носители данных (value objects), но каждый помечен низкоуверенной находкой
«Possibly a service». Для агента это активно вредно: он может начать обращаться с
record'ом как со Spring-бином/сервисом, внедрять его, вешать бизнес-логику.

**Правило:** record/enum/annotation — это данные, а не поведенческий бин. Такие kind'ы
никогда не должны угадываться как `service`/`controller`/`repository`/`component` по
имени/пакету. DTO-именованный record → `dto` допустимо; иначе — не выдавать находку.

**Фикс:** в `compute_low_confidence_findings` (и симметрично в `infer_spring_layer`,
который питает `explain_class`) пропускать component-слои для kind'ов-носителей данных.

## B4 (Medium) — reattributed контракт-интерфейсы `*Api` всплывают как «Possibly a controller» — FIX

Openapi-generated интерфейсы `AuthApi/BankApi/DealsApi/ReportsApi/StrategyApi` несут
маппинги, которые скан **уже переотнёс** на конкретные контроллеры (все их endpoint-
строки `superseded=1`). При этом каждый интерфейс продолжает висеть находкой «Possibly
a controller». Это избыточно и сбивает агента: реальный контроллер уже определён, а
интерфейс — разрешённый контракт, а не «возможно контроллер».

**Фикс:** в `compute_low_confidence_findings` не выдавать «possibly a controller» для
класса, у которого есть endpoint-строки и все они `superseded` (переотнесены).

## B5 (note, future) — async-поверхность captured как raw-текст, топики не резолвятся

`@Scheduled(cron=…)` и `@KafkaListener(topics=…)` захвачены в
`method_annotation.attributes` как сырой текст. Но топики ссылаются на константы
(`topics = NotificationProducer.TOPIC`), которые нигде не резолвятся в литерал. Агенту,
реализующему фичу «кто слушает топик X / какой cron у джобы Y», приходится грепать
исходники вручную. Это ценная, но крупная фича (структурные факты
scheduled-job→cron и listener→topic→groupId c резолвингом констант) — вынесено в
следующую итерацию, не gradle/jpa.

---

## План этой итерации

Чиним **B3** и **B4** (точность низкоуверенных находок — прямой риск для генерации
кода агентом), с тестами на fixture. B1/B2 — skip. B5 — следующая итерация.
