# Technical lag A1

Реализация определения из [docs/technical-lag-definition.md](../../docs/technical-lag-definition.md).
Чистое JavaScript-ядро получает обычные объекты и не зависит от файлового формата;
Python adapter потоково связывает его с опубликованным normalized Parquet.

Python 3.10+, Node.js 22+, `pyarrow==23.0.1`, `duckdb==1.4.4` и закреплённый
`semver@7.8.4`:

```bash
npm ci --prefix processing/technical_lag --ignore-scripts
.venv/bin/python -m pip install -r processing/technical_lag/requirements.txt
```

Полный pilot воспроизводимо строится одной командой:

```bash
.venv/bin/dvc repro technical_lag_pilot
```

Прямой запуск того же pipeline без DVC:

```bash
.venv/bin/python -m processing.technical_lag pilot
```

Unit- и adapter-тесты:

```bash
npm test --prefix processing/technical_lag
.venv/bin/python -m unittest tests.test_technical_lag_pipeline -v
```

Вход: `data/datasets/pilot-v1-normalized/normalized/`. Выход хранится отдельно в
`data/datasets/pilot-v1-technical-lag/technical_lag/`; опубликованный normalized
dataset не изменяется.

| Файл | Содержание |
| --- | --- |
| `technical_lag.parquet` | Все строки `dependency_requirements`, исходные ключи/constraint/provenance, статус, причина исключения и lag-метрики |
| `dependency_history_diagnostics.parquet` | Одна строка на использованную историю пакета и количества stable/prerelease/invalid версий |
| `report.md` | Успешные/неуспешные строки, причины исключений и классы обновлений |
| `technical-lag-manifest.json` | Fingerprint кода, hash normalized manifest, версии runtime и SHA-256 выходов |

Единица расчёта — объявленное прямое требование из `dependency_requirements`, а не
ребро графа deps.dev: только манифест содержит constraint. Все 8 957 576 требований
pilot остаются в выходе. Для непригодных наблюдений `status=excluded`, метрики
равны `NULL`, причина находится в `exclusion_reason`. Natural key не меняется:
`(source_package, source_version, dependency_type, dependency_name)`.

Pipeline подготавливает историю каждого фактически использованного dependency package
один раз, передаёт пригодные строки в Node-процесс потоково и формирует Parquet через
DuckDB. Проверяются cardinality, уникальность ключей, отсутствие orphan results,
полнота метрик успешных строк, NULL-метрики исключённых строк и допустимые значения.
Повторный прямой запуск сверяет build identity и SHA-256 уже построенного output.

## API

```js
const {calculateLag, prepareDependencyHistory, summarizeResults}
  = require('./processing/technical_lag/core.cjs');

// При обработке нескольких требований история зависимости готовится один раз.
const history = prepareDependencyHistory({
  package_name: 'lib',
  provenance: dependencyPackage.provenance,
  versions: dependencyVersions, // Полная история [{version, published_at, ...}].
});

const result = calculateLag({
  consumer: consumerVersion, // {package_name, version, published_at, provenance}
  dependency: history,       // Допустим также исходный объект истории или null.
  requirement: '^1.0.0',
  dependency_type: 'dependencies',
  shadowed_by_optional: false,
});
```

`consumerVersion`, `dependencyPackage` и `dependencyVersions` здесь — объекты,
которые передаёт Parquet adapter, а не имена файлов или скрытые зависимости API.
`provenance` передаётся из Data Contract без подстановки текущего времени;
`retrieved_at` обязателен для пригодного к вычислению снимка.
Исходный объект истории можно передать прямо в `dependency`; подготовленный вариант
избавляет от повторного разбора и сортировки всех релизов при каждом требовании.
История изолирована от последующих изменений исходного массива.

По умолчанию `t = consumer.published_at`. Необязательный `observation_time` позволяет
использовать более позднее наблюдение того же манифеста; дата до публикации или
невалидный явный timestamp — ошибка аргумента.
`shadowed_by_optional` вычисляет adapter по наличию одноимённой optional-декларации.
Исходный `dependency_type` сохраняется, эффективный становится `optionalDependencies`,
а причина исключения — `unsupported_dependency_type`, как в A1.

Результат содержит исходные ключи, требование, `observation_time`, provenance обоих
снимков и `historical_mode = reconstructed_survivors`.
При `status = ok`:

| Поле | Смысл |
| --- | --- |
| `v_latest` | Наибольшая по SemVer стабильная версия, опубликованная не позже t |
| `v_req` | Наибольшая версия из того же набора, удовлетворяющая constraint |
| `available_versions` | Число версий в C(t) |
| `version_lag` | Число версий C(t), больших v_req |
| `time_lag_days` | Разность дат v_latest и v_req в сутках, без округления |
| `time_lag_nanoseconds` | Та же длительность, точная целая строка, включая знак |
| `update_type` | `patch`, `minor`, `major` либо `none` |
| `nonmonotone_publish_order` | Дата v_latest раньше даты v_req |

`status = excluded` содержит `exclusion_reason`; все перечисленные поля равны
`null`, включая флаги. Нулевой lag выдаётся только при успешном вычислении.
Причины и их приоритет совпадают с A1: consumer → эффективный тип → constraint →
наличие dependency snapshot → время снимков → полнота истории → C(t) →
неоднозначность precedence → наличие удовлетворяющей версии.

`dependency_diagnostics` содержит количества всех, некорректных, prerelease,
стабильных и стабильных недатированных dependency versions. Если история ещё не
просматривалась из-за более раннего исключения, diagnostics равен `null`.
Повреждение структуры входа (например, нестроковое требование, дубликат ключа версии,
отсутствующий retrieval timestamp) вызывает `TypeError`, а не считается нулевым lag.
Невалидные строки версий учитываются в diagnostics; даты не восстанавливаются из
retrieval time, dist-tags или дат соседних версий.

Сравнения дат выполняются в целых наносекундах. Принимаются RFC3339 timestamps с
точностью до девяти знаков и явным UTC offset; адаптер контракта передаёт UTC `Z`.
`time_lag_days` — JavaScript Number, точная длительность дополнительно сохраняется
в `time_lag_nanoseconds`. Невалидные календарные даты не нормализуются молча.
SemVer и ranges разбирает только node-semver, `loose: false`, `includePrerelease: false`,
без coerce. В строковых идентификаторах сохраняются исходные имена и версии.

`summarizeResults(iterable)` считает `total`, `successful`, `unsuccessful` и
`exclusion_reasons`, не накапливая все строки. Diagnostics истории относятся к
одному снимку; для dataset-статистики их следует агрегировать один раз на snapshot,
а не складывать повторно для каждого требования к тому же пакету.

## Связь с normalized dataset

Adapter связывает `dependency_requirements` с consumer version и полной историей
dependency package и сохраняет результаты/исключения в новый immutable dataset.
Имена aliases не переписываются на предполагаемые пакеты. Разрешённые версии из
deps.dev не подставляются вместо исторического `v_req`:
строка A1 описывает объявленное прямое требование, а не установленную версию.
Фактическая статистика полного запуска находится в
[docs/evidence/technical-lag-pilot.md](../../docs/evidence/technical-lag-pilot.md).
