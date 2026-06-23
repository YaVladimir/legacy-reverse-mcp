-- ============================================================
-- legacy-reverse-mcp · SQLite schema
-- ============================================================
-- Соглашения:
--   - id: INTEGER PRIMARY KEY AUTOINCREMENT
--   - fqn: fully qualified name (com.example.pkg.ClassName)
--   - confidence: 'high' | 'medium' | 'low'
--   - role: тип узла внутри Spring-стека
-- ============================================================

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ------------------------------------------------------------
-- Мета: один scan-манифест на всю базу
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS scan_manifest (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_path       TEXT    NOT NULL,
    scanned_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    git_commit      TEXT,
    git_branch      TEXT,
    java_version    TEXT,
    build_tool      TEXT,           -- 'maven' | 'gradle' | 'unknown'
    total_files     INTEGER DEFAULT 0,
    total_classes   INTEGER DEFAULT 0,
    total_endpoints INTEGER DEFAULT 0,
    duration_ms     INTEGER
);

-- ------------------------------------------------------------
-- Модули (Maven submodule / Gradle subproject)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS module (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL UNIQUE,
    path            TEXT    NOT NULL,
    build_file      TEXT,           -- pom.xml | build.gradle | build.gradle.kts
    group_id        TEXT,
    artifact_id     TEXT,
    version         TEXT,
    packaging       TEXT            -- jar | war | pom
);

-- ------------------------------------------------------------
-- Пакеты
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS package (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    fqn             TEXT    NOT NULL UNIQUE,    -- com.example.service
    module_id       INTEGER REFERENCES module(id) ON DELETE CASCADE,
    path            TEXT                        -- относительный путь к директории
);

-- ------------------------------------------------------------
-- Классы / интерфейсы / enum-ы
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS class (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    fqn             TEXT    NOT NULL UNIQUE,    -- com.example.service.DealService
    simple_name     TEXT    NOT NULL,
    package_id      INTEGER REFERENCES package(id) ON DELETE CASCADE,
    module_id       INTEGER REFERENCES module(id) ON DELETE CASCADE,
    file_path       TEXT    NOT NULL,
    line_start      INTEGER,
    line_end        INTEGER,
    kind            TEXT    NOT NULL DEFAULT 'class',  -- class | interface | enum | annotation | record
    -- Spring-роль, определённая по аннотациям
    role            TEXT    NOT NULL DEFAULT 'unknown',
    -- controller | service | repository | entity | dto | config | component | util | unknown
    is_abstract     INTEGER NOT NULL DEFAULT 0,
    visibility      TEXT    NOT NULL DEFAULT 'public',
    superclass_fqn  TEXT,
    summary         TEXT                        -- LLM-сгенерированное резюме
);

CREATE INDEX IF NOT EXISTS idx_class_simple_name ON class(simple_name);
CREATE INDEX IF NOT EXISTS idx_class_role        ON class(role);
CREATE INDEX IF NOT EXISTS idx_class_module      ON class(module_id);

-- ------------------------------------------------------------
-- Аннотации на классах
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS class_annotation (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    class_id        INTEGER NOT NULL REFERENCES class(id) ON DELETE CASCADE,
    name            TEXT    NOT NULL,   -- @RestController | @Service | @Entity …
    attributes      TEXT                -- JSON: {"value": "/api/v1"}
);

CREATE INDEX IF NOT EXISTS idx_class_annotation_class ON class_annotation(class_id);

-- ------------------------------------------------------------
-- Интерфейсы, реализуемые классом
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS class_interface (
    class_id        INTEGER NOT NULL REFERENCES class(id) ON DELETE CASCADE,
    interface_fqn   TEXT    NOT NULL,
    PRIMARY KEY (class_id, interface_fqn)
);

-- ------------------------------------------------------------
-- Методы
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS method (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    class_id        INTEGER NOT NULL REFERENCES class(id) ON DELETE CASCADE,
    name            TEXT    NOT NULL,
    signature       TEXT    NOT NULL,   -- name(TypeA, TypeB): ReturnType
    return_type     TEXT,
    visibility      TEXT    NOT NULL DEFAULT 'public',
    is_static       INTEGER NOT NULL DEFAULT 0,
    is_constructor  INTEGER NOT NULL DEFAULT 0,
    line_start      INTEGER,
    line_end        INTEGER,
    summary         TEXT                -- краткое описание логики
);

CREATE INDEX IF NOT EXISTS idx_method_class  ON method(class_id);
CREATE INDEX IF NOT EXISTS idx_method_name   ON method(name);

-- ------------------------------------------------------------
-- Аннотации на методах
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS method_annotation (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    method_id       INTEGER NOT NULL REFERENCES method(id) ON DELETE CASCADE,
    name            TEXT    NOT NULL,
    attributes      TEXT                -- JSON
);

CREATE INDEX IF NOT EXISTS idx_method_annotation_method ON method_annotation(method_id);

-- ------------------------------------------------------------
-- Параметры методов
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS method_parameter (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    method_id       INTEGER NOT NULL REFERENCES method(id) ON DELETE CASCADE,
    position        INTEGER NOT NULL,
    name            TEXT,
    type_fqn        TEXT
);

-- ------------------------------------------------------------
-- Поля класса
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS field (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    class_id        INTEGER NOT NULL REFERENCES class(id) ON DELETE CASCADE,
    name            TEXT    NOT NULL,
    type_fqn        TEXT,
    visibility      TEXT    NOT NULL DEFAULT 'private',
    is_static       INTEGER NOT NULL DEFAULT 0,
    is_injected     INTEGER NOT NULL DEFAULT 0,  -- @Autowired / @Inject / constructor injection
    annotation_names TEXT                        -- JSON-массив: ["@Autowired", "@Qualifier"]
);

CREATE INDEX IF NOT EXISTS idx_field_class ON field(class_id);

-- ------------------------------------------------------------
-- Endpoint-ы (REST)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS endpoint (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    http_method     TEXT    NOT NULL,   -- GET | POST | PUT | DELETE | PATCH
    path            TEXT    NOT NULL,   -- /api/v1/deals
    full_path       TEXT,               -- с учётом @RequestMapping на классе
    controller_class_id INTEGER REFERENCES class(id) ON DELETE SET NULL,
    handler_method_id   INTEGER REFERENCES method(id) ON DELETE SET NULL,
    produces        TEXT,               -- application/json
    consumes        TEXT,
    request_dto_fqn  TEXT,
    response_dto_fqn TEXT,
    deprecated      INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_endpoint_path        ON endpoint(full_path);
CREATE INDEX IF NOT EXISTS idx_endpoint_http_method ON endpoint(http_method);

-- ------------------------------------------------------------
-- Эвристическая цепочка: Endpoint → Service → Repository
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS endpoint_trace (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint_id     INTEGER NOT NULL REFERENCES endpoint(id) ON DELETE CASCADE,
    step            INTEGER NOT NULL,   -- 0=controller, 1=service, 2=repository, 3=entity
    class_id        INTEGER REFERENCES class(id) ON DELETE SET NULL,
    method_id       INTEGER REFERENCES method(id) ON DELETE SET NULL,
    confidence      TEXT    NOT NULL DEFAULT 'low'  -- high | medium | low
);

-- ------------------------------------------------------------
-- Зависимости между классами (эвристика: import + field types)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS class_dependency (
    from_class_id   INTEGER NOT NULL REFERENCES class(id) ON DELETE CASCADE,
    to_class_id     INTEGER NOT NULL REFERENCES class(id) ON DELETE CASCADE,
    kind            TEXT    NOT NULL DEFAULT 'unknown',
    -- field_injection | method_param | return_type | import | inheritance
    PRIMARY KEY (from_class_id, to_class_id, kind)
);

-- ------------------------------------------------------------
-- Зависимости между модулями (из Maven/Gradle)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS module_dependency (
    from_module_id  INTEGER NOT NULL REFERENCES module(id) ON DELETE CASCADE,
    to_module_id    INTEGER NOT NULL REFERENCES module(id) ON DELETE CASCADE,
    scope           TEXT,               -- compile | test | runtime | provided
    PRIMARY KEY (from_module_id, to_module_id)
);

-- ------------------------------------------------------------
-- Внешние зависимости (Maven artifacts, не наши модули)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS external_dependency (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    module_id       INTEGER REFERENCES module(id) ON DELETE CASCADE,
    group_id        TEXT    NOT NULL,
    artifact_id     TEXT    NOT NULL,
    version         TEXT,
    scope           TEXT
);

CREATE INDEX IF NOT EXISTS idx_ext_dep_artifact ON external_dependency(group_id, artifact_id);

-- ------------------------------------------------------------
-- Конфиг-файлы (application.yml / .properties / XML)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS config_file (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    module_id       INTEGER REFERENCES module(id) ON DELETE CASCADE,
    file_path       TEXT    NOT NULL UNIQUE,
    kind            TEXT    NOT NULL DEFAULT 'unknown',
    -- application-yaml | application-properties | logback | persistence-xml | beans-xml | other
    profile         TEXT                -- spring profile: dev | prod | test
);

-- ------------------------------------------------------------
-- Отдельные config-ключи из application.yml / .properties
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS config_property (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    config_file_id  INTEGER NOT NULL REFERENCES config_file(id) ON DELETE CASCADE,
    key             TEXT    NOT NULL,
    value           TEXT,
    is_secret       INTEGER NOT NULL DEFAULT 0  -- содержит password/secret/token в ключе
);

CREATE INDEX IF NOT EXISTS idx_config_property_key ON config_property(key);

-- ------------------------------------------------------------
-- Суммаризации (кеш LLM-резюме)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS summary (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT    NOT NULL,   -- project | module | package | class | endpoint_group
    ref_id          INTEGER,            -- id из соответствующей таблицы (nullable для project)
    content         TEXT    NOT NULL,
    model           TEXT,               -- qwen3-coder-next | …
    generated_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    token_count     INTEGER
);

CREATE INDEX IF NOT EXISTS idx_summary_kind_ref ON summary(kind, ref_id);

-- ------------------------------------------------------------
-- Находки / паттерны / подозрительные места
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS finding (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT    NOT NULL,
    -- circular_dependency | god_class | missing_transaction | deprecated_api |
    -- large_controller | orphan_entity | suspiciously_large_method
    severity        TEXT    NOT NULL DEFAULT 'info',  -- info | warning | error
    class_id        INTEGER REFERENCES class(id) ON DELETE CASCADE,
    method_id       INTEGER REFERENCES method(id) ON DELETE CASCADE,
    module_id       INTEGER REFERENCES module(id) ON DELETE CASCADE,
    description     TEXT    NOT NULL,
    detected_at     TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_finding_kind     ON finding(kind);
CREATE INDEX IF NOT EXISTS idx_finding_severity ON finding(severity);

-- ============================================================
-- FTS5: полнотекстовый поиск по именам, summary, аннотациям
-- (используется find_code_areas)
-- ============================================================
-- Regular (not contentless) FTS5 so columns are retrievable on match.
-- entity_type / entity_id are stored UNINDEXED (returned but not tokenized).
CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5(
    entity_type UNINDEXED,  -- class | method | endpoint
    entity_id   UNINDEXED,
    name,
    fqn,
    annotations,            -- пробел-разделённые имена аннотаций
    summary
);

-- ============================================================
-- Вспомогательные представления
-- ============================================================

-- Все endpoint-ы с контроллером и методом-обработчиком
CREATE VIEW IF NOT EXISTS v_endpoint_full AS
SELECT
    e.id,
    e.http_method,
    e.full_path,
    c.fqn         AS controller_fqn,
    c.file_path   AS controller_file,
    m.name        AS handler_name,
    m.signature   AS handler_signature,
    m.line_start  AS handler_line,
    e.request_dto_fqn,
    e.response_dto_fqn,
    e.deprecated
FROM endpoint e
LEFT JOIN class  c ON c.id = e.controller_class_id
LEFT JOIN method m ON m.id = e.handler_method_id;

-- Классы с их модулями и пакетами
CREATE VIEW IF NOT EXISTS v_class_full AS
SELECT
    cl.id,
    cl.fqn,
    cl.simple_name,
    cl.role,
    cl.kind,
    cl.file_path,
    cl.line_start,
    cl.summary,
    p.fqn  AS package_fqn,
    mo.name AS module_name,
    mo.path AS module_path
FROM class cl
LEFT JOIN package p  ON p.id  = cl.package_id
LEFT JOIN module  mo ON mo.id = cl.module_id;

-- Внешние зависимости с группировкой по модулю
CREATE VIEW IF NOT EXISTS v_module_dependencies AS
SELECT
    mo.name         AS module_name,
    ed.group_id,
    ed.artifact_id,
    ed.version,
    ed.scope
FROM external_dependency ed
JOIN module mo ON mo.id = ed.module_id
ORDER BY mo.name, ed.group_id, ed.artifact_id;
