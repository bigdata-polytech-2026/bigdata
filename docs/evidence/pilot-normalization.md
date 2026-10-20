# Pilot normalization: фактический запуск

Проверено 19.10.2026 на локальном snapshot `data/datasets/pilot-v1`.
Сборка offline из raw npm/deps.dev; метаданные provenance взяты из audit states,
а для старых deps.dev states — из проверенных collector JSONL. Сущности JSONL
не копировались: поля таблиц пересчитаны из raw.

Команда после установки зависимостей:

```bash
.venv/bin/python bigdata normalize pilot
```

Результат: `data/datasets/pilot-v1-normalized/normalized/`.

| Таблица | Строк |
| --- | ---: |
| dependency_relations | 995,928 |
| dependency_requirements | 8,957,576 |
| packages | 3,000 |
| versions | 546,882 |

Количества всех четырёх сущностей совпадают с результатом исходного collector snapshot.
У всех 8,957,576 requirements найдена исходная npm version;
у всех versions найден Package. У всех relations найдена npm version корня графа.
Пропущенных дат публикации версий: 0.

| Сопоставление relations с npm versions | Строк |
| --- | ---: |
| Оба конца сопоставлены | 77,189 |
| Нет исходной версии | 92,045 |
| Нет разрешённой версии назначения | 132,757 |
| Нет обеих версий | 693,937 |
| Итого unmatched | 918,739 / 995,928 (92.2495%) |

Итого без source version: 785,982, без resolved version:
826,694; эти две величины пересекаются.
Все relations сохранены. В `relation_matches.parquet` есть два флага и причина
несопоставления; для выборки с обоими npm endpoints используются оба true-флага.

Для requirements сопоставление dependency package по точному имени декларации:
7,101,513 из 8,957,576 без соответствия (79.2794%).
Это отдельный знаменатель; npm aliases не переписываются в предполагаемые target names.
Выборка из 3000 пакетов не замкнута по зависимостям, поэтому эти пропуски нельзя
выдать за потерю строк при join или заменить синтетическими PackageVersion.

Для technical lag подготовлено 297,471 кандидатов после проверки версий потребителей.
В `version_quality.parquet` помечены 5 invalid SemVer, 332,878 prerelease
и 213,999 stable dated versions. Строки версий и исходные ключи сохранены.
`technical_lag_inputs.parquet` содержит все требования, дату публикации потребителя,
provenance обоих npm-снимков, optional overrides и причины предварительного исключения:

| Причина | Строк |
| --- | ---: |
| invalid_consumer_version_or_time | 5,186,332 |
| missing_dependency_snapshot | 1,126,983 |
| optional_dependency_override | 5,468 |
| unsupported_dependency_type | 2,340,950 |
| unsupported_requirement | 372 |

Это входы для следующего этапа, а не посчитанные метрики lag. Кандидатам ещё нужны
проверки диапазонов и истории dependency versions через strict `semver@7.8.4`,
времени снимков и полноты истории
по [определению lag](../technical-lag-definition.md). Из графа deps.dev нельзя
получить исторический `v_req`. Для отсутствующих dependency packuments история недоступна.

Raw audit: 3,000 npm responses,
13,953 deps.dev responses,
1,019,482 raw edges = 995,928 normalized +
23,554 skipped. Причины фильтрации графа определены
контрактом: errors, bundled/non-NPM nodes, malformed/unreachable edges.
Parser warnings: 1; предупреждение с raw_path сохранено в `warnings.jsonl`.
У 5,469 старых графов без normalized rows нет
точного retrieval timestamp. Время не подставлялось: эти графы не создают relations.
Точное время непустых графов восстановлено из сохранённого provenance с проверкой
его полей против audit state. Новые deps.dev state markers сохраняют `retrieved_at`.

Проверки: 20 ограничений целостности, все с нулём нарушений.
Проверены natural/physical keys, NOT NULL keys, schema/source, FK versions→packages,
requirements→source versions, relations→root versions, типы требований, индексы/depth,
кардинальность lag inputs. Отдельно проверены archive/raw SHA-256, identity ответов
и accounting всех raw edges.

```bash
.venv/bin/python -m unittest discover -s tests -q
# Ran 33 tests; OK (skipped=2)
```

31 тест прошёл, 2 live API-теста пропущены. Normalization покрыта 15 тестами:
raw→Parquet и joins; precision дат; optional priority; legacy provenance;
пустые графы; нарушения FK; дубликаты; неверная identity; повреждённые raw/outputs;
повторный запуск и независимая пересборка fixture с одинаковыми строками Parquet;
неизменность input snapshot; strict SemVer без coerce; запрет NULL в обязательном provenance.
Повторный запуск на полном pilot завершился с exit code 0, проверил checksums
входных архивов и выходов, вернул ту же статистику.

Build identity:

- Input manifest SHA-256: `3540600379f8a362cc0c22ac4cdcf7989859a19d82822accddfc9eeb9734d3d5`.
- Parser: `pilot-normalization@1.0.0+source.be623bb8be22752622cc0c66a25d4b0f2b2119f21746971418611aa0f62df051`.
- PyArrow 23.0.1; DuckDB 1.4.4; semver 7.8.4 (npm lockfile).
- Размер файлов производного output: 65,439,642 bytes без собственного manifest.

Checksums каждого output находятся в `normalization-manifest.json` рядом с Parquet.
Контракт физических типов и инструкция запуска: [normalization README](../../processing/normalization/README.md).
Исходный snapshot и `data/datasets/pilot-v1.dvc` не изменены: DVC hash остаётся
`4732d00dbe9aaa96a016c65c9e28eab8.dir`, 19 файлов. Первоначальный локальный Parquet
перенесён в резервный tmp-каталог; в immutable snapshot он больше не находится.
Реальный stage `normalize_pilot` в `dvc.yaml` успешно построил отдельный output;
`dvc.lock` содержит точные hashes исходников, входного snapshot и результата.
Публикация `.venv/bin/dvc push dvc.yaml:normalize_pilot` завершилась успешно:
`11 files pushed` (10 output files и directory object).
Output DVC hash: `cb9262db42bbc1160e4a4385de1b42f3.dir`, 65,444,028 bytes.
`dvc status --cloud dvc.yaml:normalize_pilot` подтвердил:
`Cache and remote 'yandex-s3' are in sync.`
Повторный `dvc repro normalize_pilot` пропускает неизменённый stage.
Это проверяет синхронизацию объектов с remote; отдельное восстановление в пустой
cache в рамках этого запуска не выполнялось.
