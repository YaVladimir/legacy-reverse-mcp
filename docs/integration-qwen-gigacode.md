# Подключение `legacy-reverse-mcp` к Qwen CLI и GigaCode CLI

Инструкция, как зарегистрировать `legacy-reverse-mcp` как **живой MCP-сервер** в Qwen Code
(`qwen`) и GigaCode CLI (`gigacode`) — по образцу `cryndoc/polisade-orchestrator`. Оба CLI —
форки семейства **Gemini CLI**, поэтому используют одинаковую схему: ключ `mcpServers` в
`settings.json` и/или упакованные расширения в `~/.<cli>/extensions/`.

Описаны **два способа**:

- **Способ A — прямой `settings.json`** (быстро, минимум файлов).
- **Способ B — упакованное расширение** (`qwen-extension.json`, переносимо/раздаётся — как в polisade).

---

## 0. Предусловия

Сервер запускается как **stdio**-процесс (`mcp.run()` в `mcp_server.py`) и читает индекс из
переменной окружения `LEGACY_REVERSE_REPO` (или из последнего `scan_repository(...)`).

1. **Окружение собрано** (venv + установка):
   ```bash
   cd C:/Users/Iakovenko/Documents/Claude/legacy-reverse-mcp-run
   py -m venv .venv
   ./.venv/Scripts/python.exe -m pip install -e .
   ```
2. **Индекс построен** для целевого репозитория (read-инструменты иначе вернут «Index not found»):
   ```bash
   ./.venv/Scripts/python.exe cli.py scan --repo "C:/Users/Iakovenko/IdeaProjects/fineract" --force
   ```
   Индекс пишется в `C:/Users/Iakovenko/IdeaProjects/fineract/.reverse/index.sqlite3`.
3. **(Рекомендуется) Сгенерированы описания** — смысловой слой (Phase 2), чтобы
   `get_class_card`/`find_feature`/`explain_class` отдавали «что и зачем», а не только структуру:
   ```bash
   ./.venv/Scripts/python.exe cli.py describe --repo "C:/Users/Iakovenko/IdeaProjects/fineract"
   ```
   Описания пишет подключаемый OpenAI-совместимый LLM, настраиваемый переменными
   окружения; без endpoint (или с `--no-llm`) пишется детерминированный fallback:

   | Переменная | По умолчанию | Назначение |
   |------------|--------------|-----------|
   | `LEGACY_REVERSE_LLM_BASE_URL` | *(пусто → LLM выключен)* | напр. `http://localhost:11434/v1` (Ollama), vLLM, llama.cpp |
   | `LEGACY_REVERSE_LLM_MODEL` | `qwen3-coder-next` | имя модели |
   | `LEGACY_REVERSE_LLM_API_KEY` | *(нет)* | опциональный токен |
   | `LEGACY_REVERSE_LLM_LANG` | `ru` | язык описаний |

   Кеш описаний — `<repo>/.reverse/descriptions.sqlite3` (переживает `scan --force`).
   Можно также запустить через MCP-инструмент `generate_descriptions`.
4. **(Опц.) Плоский JSON и gigacode-харнес.** Источник описаний по умолчанию — ваш
   gigacode-скилл `architecture-generator`: он генерит `project_architecture_flat.json`,
   а наш тул его импортирует (импортированные описания приоритетнее LLM/fallback и
   переживают ре-скан):
   ```bash
   # харнес: запустить скилл и импортировать его JSON одним шагом
   ./.venv/Scripts/python.exe cli.py generate-arch --repo "C:/.../fineract"
   # или вручную: ваш скилл создал arch.json → импортируем
   ./.venv/Scripts/python.exe cli.py import-arch --repo "C:/.../fineract" --in arch.json
   # экспорт нашего индекса в тот же формат (паритет с эталоном)
   ./.venv/Scripts/python.exe cli.py export-arch --repo "C:/.../fineract" --out arch.json
   ```
   gigacode — форк Gemini CLI (headless `gigacode -p "<prompt>"`). Инвокация
   конфигурируется (точный триггер скилла/путь вывода знаете только вы на рабочей машине):

   | Переменная | По умолчанию | Назначение |
   |------------|--------------|-----------|
   | `LEGACY_REVERSE_GIGACODE_CMD` | `gigacode` | бинарь CLI (ищется в PATH) |
   | `LEGACY_REVERSE_GIGACODE_ARGS` | `-p` | флаги перед промптом (через пробел) |
   | `LEGACY_REVERSE_GIGACODE_PROMPT` | запрос запустить `architecture-generator` и вывести JSON | промпт / триггер скилла |
   | `LEGACY_REVERSE_GIGACODE_OUTPUT` | `stdout` | `stdout` или путь к JSON-файлу, который пишет скилл |
   | `LEGACY_REVERSE_GIGACODE_TIMEOUT` | `900` | секунды |
   | `LEGACY_REVERSE_GIGACODE_CWD` | репозиторий | рабочий каталог скилла |

   MCP-инструменты: `export_architecture`, `import_architecture`, `generate_architecture`.
   Если gigacode не установлен/не залогинен — `generate-arch` вернёт понятную ошибку с
   подсказкой про ручной путь (`import-arch`).

### Параметры запуска (эта машина)

| Поле | Значение |
|------|----------|
| `command` | `C:/Users/Iakovenko/Documents/Claude/legacy-reverse-mcp-run/.venv/Scripts/python.exe` |
| `args` | `["mcp_server.py"]` |
| `cwd` | `C:/Users/Iakovenko/Documents/Claude/legacy-reverse-mcp-run` |
| `env.LEGACY_REVERSE_REPO` | `C:/Users/Iakovenko/IdeaProjects/fineract` |

> **Пути:** используйте прямые слэши `/` в JSON — это избавляет от экранирования `\\` и работает
> и для Python (`pathlib` нормализует), и для запуска процесса на Windows.

---

## 1. Qwen CLI

Qwen Code читает конфиг из двух мест (проектный приоритетнее глобального):

- глобально: `~/.qwen/settings.json` (на Windows: `C:/Users/Iakovenko/.qwen/settings.json`);
- по проекту: `<project>/.qwen/settings.json`.

### Способ A — `settings.json`

Добавьте блок `mcpServers` (если файл/ключ уже есть — слейте, не перезаписывайте остальное):

```json
{
  "mcpServers": {
    "legacy-reverse": {
      "command": "C:/Users/Iakovenko/Documents/Claude/legacy-reverse-mcp-run/.venv/Scripts/python.exe",
      "args": ["mcp_server.py"],
      "cwd": "C:/Users/Iakovenko/Documents/Claude/legacy-reverse-mcp-run",
      "env": { "LEGACY_REVERSE_REPO": "C:/Users/Iakovenko/IdeaProjects/fineract" },
      "timeout": 600000,
      "trust": false
    }
  }
}
```

Поля: `command`/`args`/`cwd`/`env` — обязательная часть для stdio; `timeout` (мс, по умолчанию
600000); `trust: true` — пропускать подтверждения вызовов инструментов (для локального
read-only сервера допустимо); опционально `includeTools`/`excludeTools` — белый/чёрный список
инструментов. Подстановка переменных в `env`: `$VAR` и `${VAR}`.

Альтернатива через CLI (точные флаги уточните в `qwen mcp add --help`):

```bash
qwen mcp add legacy-reverse \
  --cwd "C:/Users/Iakovenko/Documents/Claude/legacy-reverse-mcp-run" \
  -e LEGACY_REVERSE_REPO=C:/Users/Iakovenko/IdeaProjects/fineract \
  -- "C:/Users/Iakovenko/Documents/Claude/legacy-reverse-mcp-run/.venv/Scripts/python.exe" mcp_server.py
```

### Способ B — упакованное расширение (polisade-style)

Расширение делаем «самодостаточным»: манифест кладём **в корень самого проекта**, чтобы
`${extensionPath}` указывал на код и venv.

1. Создайте `qwen-extension.json` в корне
   `C:/Users/Iakovenko/Documents/Claude/legacy-reverse-mcp-run`:

   ```json
   {
     "name": "legacy-reverse",
     "version": "0.1.0",
     "mcpServers": {
       "legacy-reverse": {
         "command": "${extensionPath}${/}.venv${/}Scripts${/}python.exe",
         "args": ["${extensionPath}${/}mcp_server.py"],
         "cwd": "${extensionPath}",
         "env": { "LEGACY_REVERSE_REPO": "C:/Users/Iakovenko/IdeaProjects/fineract" }
       }
     }
   }
   ```

   `${extensionPath}` — каталог установки расширения; `${/}` — разделитель пути под текущую ОС.

2. Подключите расширение из каталога проекта (dev-режим, как `npm link`):

   ```bash
   cd C:/Users/Iakovenko/Documents/Claude/legacy-reverse-mcp-run
   qwen extensions link .
   ```

3. **Раздача на другие машины** (как polisade — zip в релиз и распаковка):

   ```bash
   mkdir -p ~/.qwen/extensions
   curl -sL https://<your-release-url>/legacy-reverse-qwen.zip | bsdtar -xvf - -C ~/.qwen/extensions/
   ```

   > **Caveat:** виртуальное окружение `.venv` **не переносимо** между машинами (в нём
   > зашиты абсолютные пути). В zip кладите код **без** `.venv`, а на целевой машине пересоберите:
   > `py -m venv .venv && ./.venv/Scripts/python.exe -m pip install -e .`. Альтернатива —
   > запускать через системный Python с уже установленным пакетом (`command: python`,
   > `args: ["-m", "mcp_server"]`) либо через `uvx`/`pipx`.

---

## 2. GigaCode CLI

GigaCode — форк того же семейства: конфиг и расширения живут под `~/.gigacode/`
(`C:/Users/Iakovenko/.gigacode/`), что подтверждается polisade
(`~/.gigacode/extensions/`). Схема `mcpServers` идентична Qwen.

### Способ A — `settings.json`

Тот же блок, что и для Qwen, но в `~/.gigacode/settings.json` (или `<project>/.gigacode/settings.json`):

```json
{
  "mcpServers": {
    "legacy-reverse": {
      "command": "C:/Users/Iakovenko/Documents/Claude/legacy-reverse-mcp-run/.venv/Scripts/python.exe",
      "args": ["mcp_server.py"],
      "cwd": "C:/Users/Iakovenko/Documents/Claude/legacy-reverse-mcp-run",
      "env": { "LEGACY_REVERSE_REPO": "C:/Users/Iakovenko/IdeaProjects/fineract" },
      "timeout": 600000,
      "trust": false
    }
  }
}
```

### Способ B — расширение

Каталог: `~/.gigacode/extensions/legacy-reverse/`. Манифест — тот же JSON, что в разделе 1.B.

```bash
mkdir -p ~/.gigacode/extensions
curl -sL https://<your-release-url>/legacy-reverse-gigacode.zip | bsdtar -xvf - -C ~/.gigacode/extensions/
```

### ⚠️ Verify-точки по GigaCode

Публичной документации по GigaCode CLI меньше, поэтому перед использованием сверьтесь с её
официальными доками по трём пунктам (всё остальное идентично Qwen/Gemini):

1. Точный путь файла настроек — `~/.gigacode/settings.json` (предполагается по аналогии).
2. Имя файла манифеста расширения — вероятно `gigacode-extension.json`; возможно, принимается
   и `qwen-extension.json`/`gemini-extension.json`.
3. Точная команда установки/линковки расширения (`gigacode extensions link .`?) и команда
   `gigacode mcp add ... / gigacode mcp list`.

---

## 3. Проверка

### 3.1. Через CLI

```bash
qwen mcp list        # ожидаем: legacy-reverse — connected, 18 tools
# в интерактивной сессии:
/mcp                 # показывает сервер и список инструментов
```
Для GigaCode — аналогично (`gigacode mcp list`, `/mcp`).

### 3.2. Stdio smoke-тест без CLI

Проверяет сам сервер (не зависит от CLI). Отправляем `initialize` + `tools/list` в stdin:

```bash
cd C:/Users/Iakovenko/Documents/Claude/legacy-reverse-mcp-run
printf '%s\n' \
'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}' \
'{"jsonrpc":"2.0","method":"notifications/initialized"}' \
'{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
| LEGACY_REVERSE_REPO="C:/Users/Iakovenko/IdeaProjects/fineract" ./.venv/Scripts/python.exe mcp_server.py
```

Проверенный результат на этой машине — сервер вернул **18 инструментов**:

```
explain_class, export_architecture, find_code_areas, find_feature,
generate_architecture, generate_context_pack, generate_descriptions,
get_change_impact, get_class_card, get_class_summary, get_config, get_findings,
get_module_map, get_project_overview, import_architecture, list_endpoints,
scan_repository, trace_endpoint
```

### 3.3. Через агента

Попросите агента вызвать `get_project_overview` или `list_endpoints(path_contains="loans")` —
должны прийти данные Fineract (стек JAX-RS+Spring, ~974 endpoint-а, 45 модулей).

---

## 4. Troubleshooting

| Симптом | Причина / решение |
|---------|-------------------|
| Сервер не стартует / «command not found» | Неверный путь к `python.exe`. Укажите абсолютный путь к venv-python; проверьте, что venv создан. |
| `RuntimeError: No repository indexed` | Не задан `LEGACY_REVERSE_REPO` и не вызывался `scan_repository`. Добавьте `env.LEGACY_REVERSE_REPO`. |
| `Index not found at …\.reverse\index.sqlite3` | Индекс не построен. Выполните `cli.py scan --repo <repo> --force` (см. п.0) или вызовите инструмент `scan_repository`. |
| Сервер «висит»/таймаут | Первый вызов `scan_repository` на большом репо долгий. Поднимите `timeout` (мс). Read-инструменты по готовому индексу — миллисекунды. |
| Битый JSON-RPC handshake | Не пишите ничего в **stdout** из своего кода — это канал протокола. FastMCP логирует в stderr; не подменяйте print-ом stdout. |
| Пути с `\` ломают JSON | Используйте `/` либо экранируйте `\\`. |
| GigaCode не видит сервер | Сверьте путь `settings.json` и имя манифеста (см. verify-точки в п.2). |

---

## 5. Как это соответствует паттерну polisade

Polisade раздаёт расширения именно так: `~/.qwen/extensions/` и `~/.gigacode/extensions/`,
распаковка релизного zip через `bsdtar`. Наш **Способ B** — тот же механизм, только вместо
slash-команд расширение объявляет `mcpServers`. **Способ A** (`settings.json`) — более простой
вариант для одной машины без упаковки.
