# Слой 3 (Граф: КАК связано) — дизайн

**Дата:** 2026-07-20
**Тип:** дизайн-документ (что нужно для Слоя 3 и как это должно работать в идеале).
**Контекст:** продолжение [позиционирования в 4-слойной MCP-концепции](2026-07-20-mcp-layers-positioning.md).
Все примеры здесь — абстрактные/выдуманные; деталей реальных сканируемых проектов нет.

---

## 1. Роль слоя — «сеть безопасности по impact»

В концепции Слой 3 (БЛОК В навигационного скилла) **обязателен для любой задачи**. Его работа
одна: по цели изменения (класс / поле / эндпоинт) вернуть **полный** набор затронутых компонентов
и **как** они связаны — чтобы агент обновил всё и не оставил висячих ссылок. Это не «что делает
код» (Слой 2), а «что сломается, если это тронуть».

Контракт слоя (за вычетом JPA — см. §4):

- `trace_endpoint` — REST → controller → service → repository
- `find_blast_radius` — все компоненты, затронутые изменением класса/поля
- `check_layer_violations` — например, controller → repository мимо service
- `find_spring_wiring` — граф DI-инъекций бина
- `find_circular_dependencies` — циклы между пакетами/классами
- глубже (`find_taint_flows` / `get_program_slice` / `get_call_graph`) — только через CBMC-tier

## 2. Идеальный мир — two-tier, evidence как шов

Слой 3 — это **одна и та же поверхность инструментов в двух уровнях грунтованности**:

| Tier | Источник | Confidence | Доступность |
|---|---|---|---|
| **Base (no-compile)** | наш SQLite-граф поверх tree-sitter | medium | всегда |
| **Precision (CBMC)** | knowledge graph бинаря `codebase-memory-mcp` (CALLS/DATA_FLOWS/HTTP_CALLS + Cypher `query_graph`) | high | когда проект проиндексирован в CBMC |

Каждый ответ инструмента честно помечает грунтованность через нашу evidence-модель
(`ObservedFact`/`InferredFinding` + `limitations`, например `no_call_graph` / `syntactic_calls`).
Это наш дифференциатор: base-tier работает **без сборки** (типичный легаси-сценарий), а precision-tier
апгрейдит те же ответы, когда граф доступен, — без смены контракта. Того и другого в спецификации
нет: там Слой 3 держится на байткоде (jQAssistant) / CPG (Joern) и без компиляции не существует.

## 3. Что уже есть в индексе (субстрат ~70%)

Base-tier недалеко — большинство рёбер уже пишется сканером (таблицы в `index/schema.sql`):

| Ребро / сущность | Таблица | Статус |
|---|---|---|
| Зависимости класс→класс (5 видов: `field_injection`, `method_param`, `return_type`, `import`, `inheritance`) | `class_dependency` | ✅ |
| Сырые вызовы методов | `method_call` | ⚠️ callee по **имени + `receiver_type_fqn`**, не резолвится в `method.id` |
| REST-эндпоинты + переатрибуция интерфейс→контроллер | `endpoint` (+ `superseded`) | ✅ |
| Цепочка endpoint → service → repo | `endpoint_trace` (со `step` и `confidence`) | ✅ |
| implements | `class_interface` | ✅ |
| DI-инъекции | `field.is_injected` + `field.annotation_names` + `class_dependency(field_injection)` | ⚠️ данные есть, нет явного «граф бина» |
| Циклы | `finding(circular_dependency)` | ⚠️ тип находки есть, полноценного SCC-обхода нет |
| Evidence / confidence / limitations | `observed_facts` / `inferred_findings` / `evidence` / `limitations` | ✅ шов готов |

## 4. Границы слоя

**JPA-связи и `repository → таблица БД` — НЕ здесь.** Для этого есть отдельный скилл под конкретную
JPA-реализацию. Поэтому:

- `list_entity_relations` и DB-mapping в контракт нашего Слоя 3 **не входят**;
- `trace_endpoint` / `find_blast_radius` останавливаются на **repository-узле** и передают его дальше
  как handoff-точку для JPA-скилла (см. блок F), а не резолвят таблицу сами.

**Data flow / taint / program slice** — вне base-tier по дизайну (нужен CPG или CBMC); доступны только
в precision-tier.

## 5. Чего не хватает (гэпы base-tier)

1. **Резолвинг call graph** — главный гэп. `method_call.callee_name` + `receiver_type_fqn` нужно
   резолвить в конкретный `method.id` (в том же классе или супертипе). Это снимает `no_call_graph`
   для внутрипроектных вызовов на medium-confidence.
2. **DI-граф как явные рёбра** — `injected_by` / `injects` для бина поверх уже имеющихся данных.
3. **Graph-примитивы** — reachability (BFS вперёд/назад), SCC (Tarjan для циклов), поиск путей,
   обратная достижимость для blast-radius. Чистый Python поверх рёбер, детерминированно.
4. **Tool surface** — привести к контракту (`trace_endpoint` есть; `get_change_impact` → выровнять
   в `find_blast_radius`; `check_layer_violations` / `find_spring_wiring` / `find_circular_dependencies`
   — новые).

## 6. План — строительные блоки

**A. Единый типизированный edge-слой** (модель запроса, не новая таблица).
`analysis/graph.py` — представление над `class_dependency` + резолвнутым `method_call` +
`class_interface` + `endpoint_trace` с типами рёбер `CALLS / INJECTS / EXTENDS / IMPLEMENTS /
HANDLES / DEPENDS_ON`, каждое с evidence + confidence. Резолвинг callee (гэп №1) живёт здесь.

**B. Graph-алгоритмы** (`analysis/graph_algo.py`): `reachable(node, dir, depth)`,
`strongly_connected()` (Tarjan), `shortest_paths(a, b)`, `reverse_reachable(node)` (для blast-radius).
Без внешних зависимостей.

**C. Инструменты** поверх A+B, каждый через `meta()`-конверт:
- `find_spring_wiring(bean)` → `{injected_by, injects, scope, annotations}` из INJECTS-рёбер;
- `check_layer_violations(module?)` → пары, где путь controller→repository есть, а через service —
  нет (layer из `analysis/layers.py` + CALLS);
- `find_circular_dependencies(scope?)` → SCC на CALLS/DEPENDS_ON, апгрейд текущего `finding`;
- `find_blast_radius(class, field?)` → reverse-reachable + затронутые endpoint'ы/DTO/бины;
  выровнять с `get_change_impact` (direct/candidate);
- `trace_endpoint` — уже есть; на последнем шаге отдаёт repository-узел как handoff для JPA-скилла.

**D. Evidence / confidence на каждом ребре и ответе.** Синтаксически резолвнутый вызов = medium +
`limitations: [syntactic_calls]`. Полиморфизм / прокси Spring → `spring_proxies`. Ответ инструмента —
«слабое звено» по пути (min-confidence).

**E. CBMC-adapter (precision-tier, опционально).** `analysis/graph_cbmc.py`: когда бинарь + проект в
графе доступны, делегирует `get_call_graph` / blast-radius / taint в `trace_path` / `query_graph`
CBMC и помечает provenance = high. Тот же контракт, апгрейд грунтованности. Контракт бинаря
зафиксирован (caller_names/callee_names, деривация имени проекта, режимы search_graph) — сверен по
исходникам форка.

**F. Handoff-контракт с JPA-скиллом.** Слой 3 не лезет в JPA: `trace_endpoint` / `find_blast_radius`
возвращают repository-узел (FQN + метод) в стабильном виде, чтобы JPA-скилл продолжил до таблицы.
Этот стык документируется как часть контракта.

## 7. Идеальный воркфлоу агента (БЛОК В)

```
изменение X →
  find_blast_radius(X)            ← ВСЕГДА: полный список затронутого
  [если API]  trace_endpoint      ← цепочка до repository (дальше → JPA-скилл)
  [если DI]   find_spring_wiring  ← кто инжектит, что инжектит
  check_layer_violations          ← не создаёт ли правка нарушение слоёв
→ агент правит ВСЁ из blast radius, не только очевидное
```

Ценность — **полнота impact**: «что ещё это заденет», причём с честным confidence, чтобы агент
(в спеке — модель с 3B active-параметров) не принял синтаксическую догадку за факт.

## 8. Фазировка

1. **Фаза 1 (no-compile, делаем сами):** блоки A–D + резолвинг call graph + 3 новых инструмента.
   Всё на tree-sitter, medium-confidence, evidence-честно. Закрывает base-tier Слоя 3 без единой
   внешней зависимости.
2. **Фаза 2 (precision, когда есть бинарь):** блок E — CBMC-адаптер апгрейдит call graph /
   blast-radius / добавляет taint & slice на high-confidence.
3. **Стык:** блок F — handoff в JPA-скилл для `repository → таблица`.
