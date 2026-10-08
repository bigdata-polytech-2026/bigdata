# deps.dev dependency collector

## Источник и endpoint

MVP использует стабильный deps.dev API v3, метод [`GetDependencies`](https://docs.deps.dev/api/v3/#getdependencies):

```text
GET https://api.deps.dev/v3/systems/npm/packages/{name}/versions/{version}:dependencies
```

Имя npm-пакета и версия кодируются как отдельные path segments. Авторизация не нужна. deps.dev разрешает кешировать ответы. Полученные sample-файлы сохраняются с указанием источника и используются в рамках условий API и лицензии CC-BY 4.0 для сгенерированных deps.dev данных.

`GetVersion` в MVP не вызывается: имя, publish time и npm metadata уже собираются из npm Registry. `GetRequirements` тоже не используется как основной источник, потому что объявленные `dependencies`, `devDependencies`, `optionalDependencies` и `peerDependencies` уже сохраняет npm collector. Здесь нужен другой факт — разрешенный граф.

## Что используем

- корень графа — `nodes[0].versionKey`, он связывает ответ с запрошенным package/version;
- direct dependencies — исходящие ребра корня (`edges[].fromNode == 0`), а `nodes[].relation = DIRECT` служит дополнительной проверкой;
- resolved graph — все `nodes[]` и `edges[]`, включая транзитивные связи;
- resolved versions — `nodes[].versionKey.version`;
- dependency requirement — `edges[].requirement`, сохраняется в raw и сравнивается с npm metadata;
- dependency depth — минимальное число ребер от `nodes[0]`, вычисляется BFS при нормализации;
- `bundled`, `relation`, `nodes[].errors` и верхнеуровневый `error` сохраняются в raw и отражаются в метриках качества.

Normalized `DependencyRelation` следует [Data Contract v1](../data-contract-v1.md). Если граф содержит общий `error`, normalized edges не создаются. Ребра, затрагивающие bundled/error nodes, некорректные индексы или недостижимые nodes, тоже остаются только в raw и считаются как `skipped_edges`.

## Запуск

Файл входа содержит по одной точной версии на строку в формате `PACKAGE@VERSION`; комментарии начинаются с `#`:

```bash
./bigdata collect depsdev \
  --input-file docs/samples/depsdev-package-versions.txt \
  --workers 8
```

Для большого batch:

```bash
./bigdata collect depsdev \
  --input-file package-versions.txt \
  --limit 1000 \
  --workers 8
```

Можно повторять `--package-version`. `--refresh` игнорирует success markers и загружает новый snapshot. Без него повторный запуск использует marker только при совпадении parser version, наличии raw/normalized файлов и совпадении SHA-256 raw-файла.

## Файлы результата

```text
data/
├── raw/depsdev/YYYY-MM-DD/<snapshot_id>.json
├── normalized/v1/dependency_relation/YYYY-MM-DD/<snapshot_id>.jsonl
├── state/depsdev/<sha256(package + version)>.json
├── logs/depsdev/<run_id>.jsonl
└── manifests/depsdev/<run_id>.json
```

Raw HTTP body записывается до разбора JSON и не переписывается нормализатором. Manifest хранит исходный package/version, канонический корень ответа, количество nodes/edges, bytes, ошибки и итоговые метрики batch.

## Retry и изоляция ошибок

Collector повторяет HTTP 429, 5xx и сетевые ошибки. Задержка — exponential backoff с jitter; числовой `Retry-After` имеет приоритет и ограничивается 60 секундами. 4xx, кроме 429, не повторяются. Ошибка одной версии попадает в event log и manifest, остальные futures продолжают выполняться. Команда завершает полный batch и возвращает ненулевой exit code, если остались API failures.

## Ограничения источника

- Graph соответствует установке на generic 64-bit Linux без заранее установленных пакетов. Это не lockfile конкретного проекта и не историческое состояние на дату публикации.
- В ответе нет времени расчета графа. Повторный запрос той же package version позднее может дать другой resolved graph.
- `GetDependencies` не помечает edge как `dependencies`, `optionalDependencies` или `peerDependencies`. Тип декларации остается в npm metadata; join делается позже.
- Dev dependencies в install graph обычно отсутствуют. Optional/platform-specific зависимости зависят от модельного окружения deps.dev.
- Один package/version может встречаться в нескольких nodes, поэтому node index и edge index нельзя заменять парой имен.
- Bundled package names могут быть локальными составными именами и не обязаны совпадать с глобальным npm package.
- `error` и `nodes[].errors` — человекочитаемые строки без стабильного машинного формата.
- В официальной документации v3 не указан числовой rate limit. Параллелизм поэтому задается явно, а 429 обрабатывается retry/backoff.

## Ручное сравнение с npm metadata

Сравнение сделано 2026-10-08 по точным npm version documents и сохраненным [sample raw responses](../samples/depsdev/raw/).

| Package version | npm runtime dependencies | deps.dev direct edges | Наблюдение |
| --- | ---: | ---: | --- |
| `react@18.2.0` | 1 | 1 | Requirement `loose-envify ^1.1.0` совпал; deps.dev дополнительно разрешил `1.4.0` и транзитивный `js-tokens@4.0.0`. |
| `lodash@4.17.21` | 0 | 0 | Пустой install graph совпал с npm metadata. |
| `is-odd@3.0.1` | 1 | 1 | Requirement `is-number ^6.0.0` совпал и разрешен в `6.0.0`; npm devDependencies в граф не попали. |
| `chalk@4.1.2` | 2 | 2 | Оба requirements совпали; 3 транзитивных edges есть только в resolved graph, npm version document хранит их не как прямые зависимости chalk. |
| `@colors/colors@1.5.0` | 0 | 0 | Runtime dependencies нет; npm devDependencies присутствуют, но deps.dev install graph содержит только root. |

Для этих пяти версий противоречий в именах и direct runtime requirements не найдено. Наблюдаемые расхождения объясняются разными сущностями: npm хранит декларации, включая dev dependencies, а deps.dev дает выбранные resolved versions и транзитивный install graph. Поэтому эти источники нельзя взаимозаменять.

## MVP batch на 1000 версий

Основной сетевой batch обработал 1000 свежих npm `latest` версий за 31.460 сек: 958 ответов получены успешно, 42 вернули 404, success rate — 95.8%, скорость — 31.786 versions/sec при 8 workers. После top-up более старыми версиями доступно 1058 успешных графов. Всего сохранено 50 523 raw edges и 6 409 319 raw bytes.

Полные метрики, аудит файлов, точный состав выборки и наблюдения о 404 зафиксированы в [`docs/evidence/depsdev-collector-1000.md`](../evidence/depsdev-collector-1000.md).
