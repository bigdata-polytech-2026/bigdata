# Data Contract v2: OSV Vulnerability

Это отдельная несовместимая версия нормализованной сущности `Vulnerability` для OSV collector. Она не изменяет npm и deps.dev записи схемы 1.0.0: они продолжают храниться в `normalized/v1/`. OSV collector записывает v2 в `normalized/v2/vulnerability/`, поэтому processing не должен смешивать v1 и v2 в одном чтении.

## Общие поля

Каждая запись содержит:

| Поле | Тип | Смысл |
| --- | --- | --- |
| `schema_version` | string, `2.0.0` | Версия контракта. |
| `source` | string, `osv` | Источник данных. |
| `provenance` | object | `snapshot_id`, `request_url`, `retrieved_at`, `raw_path`, `raw_sha256`, `parser_version`. |

`raw_path` относителен к корню `data-dir`; raw-ответ не изменяется. `retrieved_at` — UTC timestamp, а `raw_sha256` — SHA-256 его байтов.

## Vulnerability

Одна запись описывает один элемент `affected[]` advisory, у которого `affected[].package.ecosystem = npm`. Логический ключ снимка: `(osv_id, provenance.snapshot_id, provenance.parser_version, affected_index)`.

| Поле | Тип | Смысл |
| --- | --- | --- |
| `osv_id` | string | Идентификатор OSV advisory. |
| `aliases` | string[] | Aliases advisory, например CVE; пустой список, если их нет. |
| `affected_index` | integer ≥ 0 | Позиция блока в исходном `affected[]`. |
| `package_name` | string | `affected[].package.name`. |
| `ecosystem` | string, `npm` | Явно сохранённая экосистема затронутого пакета. |
| `package_purl` | string или `null` | PURL пакета, если указан OSV. |
| `affected` | object | `{versions, ranges, ecosystem_specific, database_specific}` из одного affected-блока. |
| `fixed_versions` | string[] | Уникальные события `fixed` из ranges типов `SEMVER` и `ECOSYSTEM`. |
| `published_at` | timestamp или `null` | Дата публикации advisory. |
| `modified_at` | timestamp | Дата изменения advisory. |
| `severity` | object[] | Исходные `{type, score, source}` из OSV. |
| `withdrawn_at` | timestamp или `null` | Дата отзыва advisory. |

В `affected.ranges[]` сохраняются `type`, `repo`, `events` и `database_specific`. Диапазоны не разворачиваются в версии. Полный advisory всегда остаётся в raw.

## Результат запроса без уязвимостей

Для пустого `vulns` normalized `Vulnerability` не создаётся. Manifest и JSONL event `query_completed` содержат `status="no_vulnerabilities"` и provenance результата: `snapshot_id`, `request_url`, `retrieved_at`, `http_status`, `raw_path`, `raw_sha256`. Это положительное доказательство проверенного отсутствия результатов, а не API-ошибка.
