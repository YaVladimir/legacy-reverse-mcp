# Review — точность низкоуверенных находок для ИИ-агента

- Дата: 2026-07-09
- Цель: убрать места, где `compute_low_confidence_findings` выдаёт вводящие
  будущего ИИ-агента (пишущего фичи по индексу) в заблуждение низкоуверенные
  находки. Все примеры ниже — **вымышленные** (иллюстрация класса проблемы, не
  данные конкретного проекта).

## Проблема

`analysis/layers.py:compute_low_confidence_findings` присваивает слой по токену
пакета/суффиксу имени, **не глядя на `kind` класса и на статус его эндпоинтов**.
Это порождает два вида ложных находок, опасных при генерации кода агентом.

## B3 (Medium) — value-record'ы всплывают как «Possibly a service»

Пример (вымышленный): `record Money(String currency, long amount)` в пакете
`com.example.app.service.billing`. Это носитель данных (value object), но по токену
пакета `service` он получает находку «Possibly a service». Агент может начать
обращаться с record'ом как со Spring-бином — внедрять его, вешать бизнес-логику.

**Правило:** `record`/`enum`/`annotation` — это данные, а не поведенческий бин.
Такие kind'ы не должны угадываться как `service`/`controller`/`repository`/
`component` по имени/пакету (`dto`/`util`/`entity` — допустимо).

**Фикс:** в `compute_low_confidence_findings` (и симметрично в `infer_spring_layer`,
питающем `explain_class`) пропускать component-слои для kind'ов-носителей данных.

## B4 (Medium) — reattributed контракт-интерфейсы всплывают как «Possibly a controller»

Пример (вымышленный): openapi-generated `interface OrdersApi` несёт маппинги,
которые скан **уже переотнёс** на конкретный `@RestController OrderController`
(все endpoint-строки интерфейса `superseded=1`). При этом `OrdersApi` продолжает
висеть находкой «Possibly a controller». Это разрешённый контракт, а не
неклассифицированный контроллер — находка избыточна и сбивает агента.

**Фикс:** в `compute_low_confidence_findings` не выдавать «possibly a controller»
для класса, у которого есть endpoint-строки и все они `superseded`.

## План

Чиним B3 и B4 (точность низкоуверенных находок — прямой риск для генерации кода),
с тестами на fixture через реальный пайплайн.
