# legacy-reverse-mcp

[English](README.md) · **Русский**

`legacy-reverse-mcp` — это **source-first** MCP-сервер для быстрого понимания
легаси-бэкендов на **Java / Spring**. Он помогает разработчику или LLM-агенту находить
REST-эндпоинты, слои Spring/JAX-RS, связи внедрения зависимостей (DI), эвристические
трассы запросов, оценку влияния изменений и контекст-паки под конкретную задачу.

Исходники Java парсятся через `tree-sitter-java` в SQLite-индекс. Компиляция Java **не
требуется**.

При работе различайте два репозитория:

- **Этот репозиторий**: клон проекта `legacy-reverse-mcp` с Python CLI и MCP-сервером.
- **Целевой репозиторий**: проект на Java/Spring, который вы анализируете. На него
  указывают `LEGACY_REVERSE_REPO` и команда `scan`.

- **Стек:** Python 3.11+, [FastMCP](https://github.com/jlowin/fastmcp), SQLite, tree-sitter-java
- **Фреймворки:** Spring MVC (`@RestController`, `@GetMapping`, …) и JAX-RS
  (`jakarta.ws.rs` `@Path`, `@GET`, …); внедрение через конструктор Spring + Lombok
  (`@RequiredArgsConstructor` над `final`-полями)

## Что умеет и чего не умеет

**Умеет:** находить эндпоинты; классифицировать слои Spring/JAX-RS по стереотипам, именам
и пакетам; следовать controller → service → repository по синтаксическим вызовам и графу
DI; оценивать кандидатов на влияние изменений; собирать объяснённый контекст-пак;
формировать базовый отчёт по проекту.

**Не умеет** (намеренно, см. [docs/limitations.md](docs/limitations.md)): анализ
байт-кода, рантайм-резолв Spring (прокси/профили/условные бины), полный полиморфный граф
вызовов, анализ потоков данных. Возможны ложноположительные срабатывания — именно поэтому
результаты несут `confidence` + доказательства (evidence).

## Быстрый старт

### 1. Установка из исходников

Windows:

```powershell
git clone <repo-url>
cd legacy-reverse-mcp

py -3.11 -m venv .venv
.venv\Scripts\python -m pip install -e .
```

macOS/Linux:

```bash
git clone <repo-url>
cd legacy-reverse-mcp

python3.11 -m venv .venv
./.venv/bin/python -m pip install -e .
```

Для разработки и тестов поставьте опциональные зависимости:

Windows:

```powershell
.venv\Scripts\python -m pip install -e ".[dev]"
```

macOS/Linux:

```bash
./.venv/bin/python -m pip install -e ".[dev]"
```

### 2. Сканирование вашего Java/Spring-репозитория

Запускайте против **целевого Java/Spring-репозитория**, а не против этого MCP-репозитория:

macOS/Linux:

```bash
./.venv/bin/legacy-reverse scan --repo /path/to/java-project --report
```

Windows PowerShell:

```powershell
.venv\Scripts\legacy-reverse.exe scan --repo C:\path\to\java-project --report
```

Если виртуальное окружение активировано, работает и короткое `legacy-reverse scan …`.

Скан пишет индекс и базовые отчёты в целевой репозиторий:

```text
<repo>/.reverse/index.sqlite3
<repo>/.reverse/reports/baseline.md
<repo>/.reverse/reports/baseline.json
```

`--force` пересобирает существующий индекс. `--resolve` используйте только если нужно
разрешить версии Gradle-зависимостей и у целевого проекта есть рабочая сборка.

### 2b. Генерация описаний (опционально, но рекомендуется)

`scan` быстро строит **структурный** индекс. Шаг `describe` добавляет **смысл**: краткое
естественно-языковое описание того, что делает каждый класс и метод и зачем, плюс сводки
по пакетам/модулям/проекту. Эти описания питают `get_class_card`, обогащают `explain_class`
и (поскольку индексируются для поиска) позволяют `find_feature` отвечать на бизнес- и
русскоязычные запросы по теме.

```bash
./.venv/bin/legacy-reverse describe --repo /path/to/java-project
```

```powershell
.venv\Scripts\legacy-reverse.exe describe --repo C:\path\to\java-project
```

`describe` использует **подключаемый OpenAI-совместимый LLM**, настраиваемый через
переменные окружения. Без настроенного endpoint (или с `--no-llm`) он пишет добротные
детерминированные описания — то есть работает всегда:

| Переменная | По умолчанию | Назначение |
|------------|--------------|-----------|
| `LEGACY_REVERSE_LLM_BASE_URL` | *(пусто → LLM выключен)* | напр. `http://localhost:11434/v1` (Ollama), vLLM, llama.cpp, LM Studio |
| `LEGACY_REVERSE_LLM_MODEL` | `qwen3-coder-next` | имя модели |
| `LEGACY_REVERSE_LLM_API_KEY` | *(нет)* | опциональный bearer-токен |
| `LEGACY_REVERSE_LLM_LANG` | `ru` | язык генерируемых описаний |
| `LEGACY_REVERSE_LLM_TIMEOUT` / `_MAX_TOKENS` / `_TEMPERATURE` | `60` / `512` / `0.1` | тюнинг запроса |

Описания кешируются по content-hash в `<repo>/.reverse/descriptions.sqlite3`; кеш
**переживает `scan --force`**, поэтому повторные прогоны переописывают только изменённый
код. `--force` игнорирует кеш. То же можно запустить по MCP — инструментом
`generate_descriptions`.

### 2c. Плоский JSON архитектуры — экспорт / импорт / gigacode

Индекс можно отрендерить в **плоский JSON архитектуры** (и загрузить из него) — это
drop-in для вывода скилла GigaCode `architecture-generator`
(`project_architecture_flat.json`): на класс `{id, pkg, name, description, type, kind,
class_modifiers, extends, implements, fields, methods:[{sig, modifiers, description}]}`.

```bash
# сгенерировать плоский JSON из нашего индекса
legacy-reverse export-arch --repo /path/to/java-project --out arch.json

# загрузить описания из плоского JSON (напр. от GigaCode) обратно в индекс
legacy-reverse import-arch --repo /path/to/java-project --in arch.json
```

Импортированные описания приоритетнее LLM/fallback и переживают ре-скан, поэтому
рекомендуемый источник «смысла» — ваш скилл GigaCode: он создаёт JSON, мы его импортируем.

**gigacode-харнес.** `generate-arch` запускает скилл GigaCode за вас и импортирует
результат одним шагом:

```bash
legacy-reverse generate-arch --repo /path/to/java-project
```

GigaCode CLI — форк Gemini CLI → headless `gigacode -p "<prompt>"`. Вызов полностью
конфигурируется через окружение, потому что точный триггер скилла и путь его вывода
известны только на вашей рабочей машине:

| Переменная | По умолчанию | Назначение |
|------------|--------------|-----------|
| `LEGACY_REVERSE_GIGACODE_CMD` | `gigacode` | бинарь CLI (ищется в PATH) |
| `LEGACY_REVERSE_GIGACODE_ARGS` | `-p` | флаги перед промптом (через пробел) |
| `LEGACY_REVERSE_GIGACODE_PROMPT` | запрос запустить `architecture-generator` и вывести JSON | промпт / триггер скилла |
| `LEGACY_REVERSE_GIGACODE_OUTPUT` | `stdout` | `stdout` или путь к JSON-файлу, который пишет скилл |
| `LEGACY_REVERSE_GIGACODE_TIMEOUT` | `900` | секунды |
| `LEGACY_REVERSE_GIGACODE_CWD` | репозиторий | рабочий каталог скилла |

Если gigacode не установлен/не залогинен, `generate-arch` вернёт понятную ошибку — запустите
скилл вручную и используйте `import-arch --in <file>`. По MCP: `export_architecture`,
`import_architecture`, `generate_architecture`.

### 3. Ручной запуск MCP-сервера

Перед подключением к MCP-клиенту запустите сервер один раз вручную. Используйте абсолютные
пути — MCP-клиенты часто стартуют серверы из другого рабочего каталога.

macOS/Linux:

```bash
LEGACY_REVERSE_REPO=/path/to/java-project /path/to/legacy-reverse-mcp/.venv/bin/python -m mcp_server
```

Windows PowerShell:

```powershell
$env:LEGACY_REVERSE_REPO="C:\path\to\java-project"
C:\path\to\legacy-reverse-mcp\.venv\Scripts\python.exe -m mcp_server
```

Если сервер стартует без падения, ваш MCP-клиент сможет запускать ту же команду как
stdio-MCP-сервер.

## Использование с MCP-клиентами

Во всех примерах ниже — плейсхолдеры. Замените:

- `/path/to/legacy-reverse-mcp` или `C:\path\to\legacy-reverse-mcp` — абсолютным путём к
  этому Python-проекту.
- `/path/to/java-project` или `C:\path\to\java-project` — абсолютным путём к целевому
  Java/Spring-репозиторию.

### Claude Code

macOS/Linux:

```bash
claude mcp add legacy-reverse \
  --env LEGACY_REVERSE_REPO=/path/to/java-project \
  -- /path/to/legacy-reverse-mcp/.venv/bin/python -m mcp_server
```

Windows PowerShell:

```powershell
claude mcp add legacy-reverse `
  --env LEGACY_REVERSE_REPO=C:\path\to\java-project `
  -- C:\path\to\legacy-reverse-mcp\.venv\Scripts\python.exe -m mcp_server
```

Проверьте, что Claude Code видит сервер:

```bash
claude mcp list
```

Перезапустите Claude Code после изменения MCP-конфигурации.

### Codex CLI

Добавьте stdio-MCP-сервер в конфиг Codex CLI согласно вашей локальной установке Codex.

macOS/Linux:

```toml
[mcp_servers.legacy_reverse]
command = "/path/to/legacy-reverse-mcp/.venv/bin/python"
args = ["-m", "mcp_server"]
env = { LEGACY_REVERSE_REPO = "/path/to/java-project" }
```

Windows:

```toml
[mcp_servers.legacy_reverse]
command = "C:\\path\\to\\legacy-reverse-mcp\\.venv\\Scripts\\python.exe"
args = ["-m", "mcp_server"]
env = { LEGACY_REVERSE_REPO = "C:\\path\\to\\java-project" }
```

Перезапустите Codex CLI после изменения MCP-конфигурации.

### Qwen Code CLI

Qwen Code читает MCP-серверы из `mcpServers` в `.qwen/settings.json` (по проекту) или
`~/.qwen/settings.json` (для пользователя). Можно править файл напрямую или использовать
`qwen mcp add`.

Проектный `.qwen/settings.json` (macOS/Linux):

```json
{
  "mcpServers": {
    "legacy-reverse": {
      "command": "/path/to/legacy-reverse-mcp/.venv/bin/python",
      "args": ["-m", "mcp_server"],
      "env": {
        "LEGACY_REVERSE_REPO": "/path/to/java-project"
      },
      "timeout": 30000
    }
  }
}
```

Проектный `.qwen/settings.json` (Windows):

```json
{
  "mcpServers": {
    "legacy-reverse": {
      "command": "C:\\path\\to\\legacy-reverse-mcp\\.venv\\Scripts\\python.exe",
      "args": ["-m", "mcp_server"],
      "env": {
        "LEGACY_REVERSE_REPO": "C:\\path\\to\\java-project"
      },
      "timeout": 30000
    }
  }
}
```

Альтернатива командой (macOS/Linux):

```bash
qwen mcp add legacy-reverse \
  -e LEGACY_REVERSE_REPO=/path/to/java-project \
  --timeout 30000 \
  /path/to/legacy-reverse-mcp/.venv/bin/python -m mcp_server
```

Альтернатива командой (Windows PowerShell):

```powershell
qwen mcp add legacy-reverse `
  -e LEGACY_REVERSE_REPO=C:\path\to\java-project `
  --timeout 30000 `
  C:\path\to\legacy-reverse-mcp\.venv\Scripts\python.exe -m mcp_server
```

Проверка настроенных серверов:

```bash
qwen mcp
```

Перезапустите Qwen Code в том же проекте после изменения MCP-конфигурации.

> GigaCode CLI — форк того же семейства Gemini CLI: схема `mcpServers` идентична, конфиг и
> расширения живут под `~/.gigacode/`. Подробности и упаковка расширения — в
> [docs/integration-qwen-gigacode.md](docs/integration-qwen-gigacode.md).

## Первые промпты

Когда MCP-клиент покажет инструменты `legacy-reverse`, попробуйте, например:

```text
Через legacy-reverse дай обзор проекта.
```

```text
Через legacy-reverse выведи REST-эндпоинты и сгруппируй их по модулям.
```

```text
Через legacy-reverse объясни класс com.example.SomeService с доказательствами и confidence.
```

```text
Через legacy-reverse найди фичу «банкротство»: классы и их методы.
```

```text
Через legacy-reverse собери context pack для задачи: «изменить правила валидации при открытии депозита».
```

## Траблшутинг

### `legacy-reverse: command not found`

- Убедитесь, что editable-установка прошла успешно.
- Активируйте venv или вызывайте Python из venv явно.
- На Windows используйте `py` или `.venv\Scripts\python.exe`, а не голый `python`.

### MCP-клиент стартует, но инструментов нет

- Используйте абсолютные пути в конфиге MCP.
- Проверьте, что `LEGACY_REVERSE_REPO` указывает на целевой Java/Spring-проект, а не на
  этот репозиторий `legacy-reverse-mcp`.
- Перезапустите MCP-клиент после изменения конфига.
- Сначала запустите сервер вручную, чтобы увидеть ошибки старта.

### `Index not found`

- Сначала выполните скан:

  ```bash
  ./.venv/bin/legacy-reverse scan --repo /path/to/java-project --report
  ```

  ```powershell
  .venv\Scripts\legacy-reverse.exe scan --repo C:\path\to\java-project --report
  ```

- Или попросите MCP-инструмент `scan_repository` просканировать репозиторий.
- Убедитесь, что у целевого Java-проекта есть `<repo>/.reverse/index.sqlite3`.

### Экранирование путей в Windows

- В строках JSON и TOML используйте двойной бэкслеш: `C:\\path\\to\\repo`.
- В командах PowerShell обычные бэкслеши допустимы: `C:\path\to\repo`.

## Справочник CLI

Если venv активирован или каталог `bin`/`Scripts` есть в `PATH`, можно использовать
короткую команду:

```bash
legacy-reverse scan --repo /path/to/java-project [--force] [--resolve] [--report]
legacy-reverse describe --repo /path/to/java-project [--force] [--no-llm]
legacy-reverse export-arch --repo /path/to/java-project --out arch.json
legacy-reverse import-arch --repo /path/to/java-project --in arch.json
legacy-reverse generate-arch --repo /path/to/java-project
legacy-reverse report --repo /path/to/java-project
```

Без активации вызывайте установленный консольный скрипт из venv напрямую:

```bash
./.venv/bin/legacy-reverse scan --repo /path/to/java-project --report
```

```powershell
.venv\Scripts\legacy-reverse.exe scan --repo C:\path\to\java-project --report
```

`scan` обходит репозиторий, определяет модули Maven/Gradle, парсит каждый не-тестовый
`.java`-файл, записывает наблюдаемые факты с доказательствами и внутриклассовые вызовы
методов, строит граф зависимостей и пишет индекс в `<repo>/.reverse/index.sqlite3`.

`describe` генерирует смысловые описания классов/методов и иерархии (LLM + детерминированный
fallback); кеш — в `<repo>/.reverse/descriptions.sqlite3`.

`export-arch` / `import-arch` — экспорт/импорт плоского JSON архитектуры;
`generate-arch` — запуск скилла gigacode `architecture-generator` с последующим импортом.

`report` пишет `baseline.md` и `baseline.json` в `<repo>/.reverse/reports/`: счётчики
инвентаря, топ модулей/пакетов, поверхность публичного API, кандидаты доменных областей,
низкоуверенные находки и ограничения инструмента.

## MCP-инструменты

Каждый эвристический инструмент возвращает структурированный ответ с `confidence`,
`limitations` и `warnings`; ошибки структурированы (`error`, `kind`, `suggestions`). Полные
схемы и примеры: [docs/mcp-api.md](docs/mcp-api.md).

| Инструмент | Назначение |
|-----------|-----------|
| `scan_repository(repo_path, force)` | Скан + (пере)сборка индекса |
| `list_endpoints(http_method, path_contains, limit)` | REST-эндпоинты (JAX-RS + Spring) |
| `explain_class(fqn)` | Наблюдаемые факты + выводы + связанные символы, всё с доказательствами |
| `trace_endpoint(endpoint_id \| http_method, path_contains)` | Трасса controller → service → repository с пошаговым + общим confidence |
| `get_change_impact(symbol)` | `direct_impacts` vs `candidate_impacts`, каждый с причиной/evidence/confidence |
| `generate_context_pack(task, max_tokens, max_items)` | Объяснённый пак: `selected_items` (с причинами) + `excluded_items` |
| `get_module_map()` | Модули, межмодульные зависимости, внешние координаты, число эндпоинтов |
| `get_project_overview()` | Стек, итоги, распределение ролей, топ модулей, находки |
| `find_code_areas(query, limit)` | FTS-поиск по классам/методам/эндпоинтам |
| `get_findings(subject, finding_type, limit)` | Выводы, сохранённые при скане, каждый с evidence + confidence |
| `get_config(key_contains, profile, limit)` | Spring-конфиг (`application*`/`bootstrap*`): файлы + свойства; секреты маскируются |
| `get_class_summary(fqn)` | Описание класса (LLM, если был `describe`, иначе детерминированное) |
| `generate_descriptions(force, no_llm)` | Сгенерировать смысловые описания класс/метод/иерархия по индексу (LLM + fallback) |
| `find_feature(topic, limit, methods_per_class)` | Тема/фича → карточки классов **с методами, параметрами и описаниями** (без grep) |
| `get_class_card(fqn)` | Полная карточка класса: id/pkg/name/description/type/kind/class_modifiers/extends/implements/fields/methods |
| `export_architecture(out_path?)` | Рендер индекса в плоский JSON архитектуры (reference-схема, drop-in для генератора GigaCode) |
| `import_architecture(in_path)` | Загрузить описания из плоского JSON в индекс (imported приоритетнее LLM/fallback) |
| `generate_architecture()` | Запустить скилл gigacode `architecture-generator` и импортировать его flat JSON (настраивается через env) |

## Интерпретация confidence

- **high**: прямой факт или вывод по прямым однозначным связям — стереотип-аннотация,
  эндпоинт из mapping-аннотации, вызов, найденный синтаксически в теле метода.
- **medium**: эвристический вывод по нескольким сигналам — слой по имени и пакету,
  service/repository по инъекции + именованию.
- **low**: догадка только по имени/пакету/совпадению ключевых слов.
- **unknown**: пригодного сигнала нет.

Детали и примеры: [docs/confidence-model.md](docs/confidence-model.md). Модель
«наблюдаемый факт vs вывод»: [docs/evidence-model.md](docs/evidence-model.md).

## Golden questions (оценка)

```bash
py eval/run_golden_questions.py
py eval/run_golden_questions.py --json
```

Детерминированный регрессионный слой: сканирует закоммиченную Java/Spring-фикстуру и
проверяет структурные критерии качества — наличие evidence/confidence/limitations,
найденные эндпоинты, непустой context pack. См.
[docs/golden-questions.md](docs/golden-questions.md).

## Тесты

Windows:

```powershell
.venv\Scripts\python -m pytest -q
```

macOS/Linux:

```bash
./.venv/bin/python -m pytest -q
```

## Структура

```text
cli.py                  CLI: scan (+ --report), describe, export-arch, import-arch, generate-arch, report
mcp_server.py           FastMCP-сервер + регистрация инструментов
models/evidence.py      Evidence / Confidence / Limitation / ObservedFact / InferredFinding
scanner/                парсер repo + java, сканеры spring/endpoint, эмиттер фактов, индексатор, пайплайн
index/                  schema.sql, repository (CRUD + факты), queries, search, findings
analysis/               инструменты на доказательствах: explain, trace, impact, context_pack, layers, report, flat_arch (экспорт/импорт плоского JSON)
summarizer/             llm.py (подключаемый LLM-клиент), describe.py (Phase-2 описания + кеш), harness.py (раннер gigacode-cli), детерминированные сводки классов/пакетов
eval/                   golden_questions.yaml, run_golden_questions.py, fixture/
tests/                  pytest-набор
docs/                   mcp-api, confidence-model, evidence-model, limitations, golden-questions, integration-qwen-gigacode
```

## Статус

Проверено на [Apache Fineract](https://github.com/apache/fineract) (47 модулей Gradle,
~5.3k не-тестовых классов): **974 эндпоинта** (971 JAX-RS + 3 Spring), классифицированы
роли, трассы конструкторного внедрения доходят до персистентности, граф модулей на 147
рёбер, 16.5k рёбер зависимостей классов, **~48k наблюдаемых фактов с доказательствами**,
рёбра внутриклассовых вызовов, FTS-индекс, базовый отчёт и зелёный прогон golden-questions.
Работы по слою описаний и плоскому JSON см. в [CHANGELOG.md](CHANGELOG.md).
