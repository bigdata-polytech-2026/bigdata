# Pilot normalization

Из корня репозитория, Python 3.10+ и Node.js 22+:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r processing/normalization/requirements.txt
npm ci --prefix processing/normalization --ignore-scripts
.venv/bin/python bigdata normalize pilot
```

После установки зависимостей pipeline запускается одной командой:

```bash
.venv/bin/python bigdata normalize pilot --dataset-dir data/datasets/pilot-v1
```

Pipeline работает offline с компактным snapshot: `manifest.json`, `raw/npm`,
`raw/depsdev`, `audit/npm`, `audit/depsdev`. Landing и API не нужны.
В старом pilot-v1 deps.dev state markers не содержат `retrieved_at`: для таких
снимков pipeline дополнительно читает только provenance из archived
`normalized/dependency_relation/*.jsonl.gz`, проверяя hash, число строк и совпадение
всех полей со state. Поля relations заново рассчитываются из raw. Время пустых
графов без normalized rows не восстанавливается и не выдумывается: они не создают
строк в таблице, их число записывается в raw statistics. Непустой граф без точного
времени приводит к ошибке сборки. Collector теперь сохраняет время в state marker,
поэтому для новых снимков этот дополнительный вход не требуется.
Проверяются SHA-256 архивов и каждого активного raw-ответа, число файлов,
наличие state marker, HTTP status и соответствие identity ответа запросу.
npm search archives учитываются при проверке архива, но не становятся пакетами.
Повторно используются функции разбора collectors; fingerprint нормализатора
включает исходники обоих парсеров и pipeline. Старый parser_version не выдается
за версию новой обработки.

Результат: `data/datasets/pilot-v1-normalized/normalized/`.
Это отдельный производный dataset: опубликованный immutable `pilot-v1` не меняется.
Для другого источника default — соседний `<dataset-id>-normalized/normalized`;
свой путь задаётся `--output-dir`. Запись внутрь или поверх входного dataset запрещена.

| Путь | Содержание |
| --- | --- |
| `packages/part-00000.parquet` | Package, ключ `package_name` |
| `versions/part-00000.parquet` | PackageVersion, ключ `(package_name, version)` |
| `dependency_requirements/part-00000.parquet` | DependencyRequirement, ключ `(source_package, source_version, dependency_type, dependency_name)` |
| `dependency_relations/part-00000.parquet` | DependencyRelation, ключ `(root_package, root_version, provenance.snapshot_id, provenance.parser_version, edge_index)` |
| `relation_matches.parquet` | Все relations, флаги сопоставления обоих концов и причина пропуска |
| `version_quality.parquet` | Все npm versions, strict SemVer validity, prerelease flag и причина исключения версии |
| `technical_lag_inputs.parquet` | Все requirements, source publication time, provenance двух npm-снимков, optional override и причины предварительного исключения |
| `warnings.jsonl` | Предупреждения parser с raw_path |
| `report.md` | Количества, match rates, ограничения и проверки |
| `normalization-manifest.json` | Полная статистика, fingerprint, hash исходного manifest и checksums выходов |

Четыре основные таблицы сохраняют поля и смысл Data Contract v1. Имена и версии
сопоставляются точным равенством, без lower-case, fuzzy matching, SemVer coercion
и искусственных package/version IDs. Разные node/edge indices графа не схлопываются.
`repository` (произвольный JSON) и `dist_tags` хранятся в Parquet как canonical JSON
strings; `NULL` repository сохраняется SQL NULL. Декодирование: `json.loads(value)`.
`provenance` — struct; даты — RFC3339 UTC strings, чтобы сохранить до девяти знаков
дробной секунды, а не обрезать их до микросекунд. Остальные поля имеют явную Arrow schema.

Записи с несопоставленными концами сохраняются. Для analysis с полной npm-историей
обоих концов выбирайте `source_version_matched AND resolved_version_matched` из
`relation_matches.parquet`. Доля unmatched равна числу relations с хотя бы одним
несопоставленным концом, деленному на все normalized relations; skipped raw edges
считаются отдельно. Для requirements отдельная доля использует точное имя декларации:
это не подтверждение разрешения alias или корректности range.

`technical_lag_inputs` готовит связи и даты для следующего этапа, но не считает lag.
`preparation_exclusion_reason IS NULL` означает кандидата. Версии потребителей уже
проверены strict `semver@7.8.4` без coerce, prerelease и версии без даты помечены.
Следующему этапу ещё нужны проверки диапазонов, времени обоих снимков и полноты
истории по `docs/technical-lag-definition.md`. Для отсутствующих packuments истории
нет. npm aliases исключаются из основного анализа; другие non-SemVer требования
классифицирует следующий этап. Граф deps.dev не подставляется вместо исторического
`v_req`. Snapshot имеет ограниченную enrichment-выборку; это не полный граф npm.

Обработка потоковая по raw-ответам; Arrow batches по 16 384 строки. В памяти
находится один packument/graph, его нормализованные записи и audit states.
DuckDB выполняет join с memory limit 1 GiB и spill в staging-каталог. Выходы Zstandard,
явные schemas даже для пустой таблицы. После проверки ключей, FK и accounting всех
ребер staging атомарно переименовывается в отдельный output. При ошибке
незавершенный output удаляется. Повторный запуск сверяет fingerprint, версии библиотек,
hash manifest и все outputs и возвращает ту же статистику. Смена исходников/входов
требует нового output path; существующий результат автоматически не перезаписывается.

Исходные raw, audit, JSONL shards и `manifest.json` collector не изменяются.
Производный артефакт имеет собственный manifest. `dvc.yaml` описывает реальный
stage `normalize_pilot`: immutable `pilot-v1` — dependency, отдельный
`pilot-v1-normalized/normalized` — output. `dvc.lock` фиксирует hashes inputs/outputs.
Восстановление и воспроизводимая сборка:

```bash
.venv/bin/dvc pull data/datasets/pilot-v1.dvc
.venv/bin/dvc repro normalize_pilot
```

Для получения готового результата: `.venv/bin/dvc pull dvc.yaml:normalize_pilot`.
После проверки новый output публикуется `.venv/bin/dvc push dvc.yaml:normalize_pilot`;
descriptor `data/datasets/pilot-v1.dvc` при этом не изменяется. Git хранит
`dvc.yaml` и `dvc.lock`, а Parquet — DVC cache/remote. Новая опубликованная версия
результата получает новый output path и соответствующее изменение stage.
В Git payload не включается; отчет фактического запуска хранится также в
`docs/evidence/pilot-normalization.md`.

Проверки:

```bash
.venv/bin/python -m unittest tests.test_normalization -v
.venv/bin/python -m unittest discover -s tests -v
```
