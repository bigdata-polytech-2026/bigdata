# Technical Lag in npm

Исследовательский monorepo проекта «Анализ технического отставания программных зависимостей в экосистеме npm и факторов несвоевременного обновления».

Источники данных: npm Registry, deps.dev и OSV. Рабочий процесс: отдельная ветка на задачу, Pull Request, review и CI. Объемные данные хранятся в Yandex Object Storage и версионируются через DVC; секреты в Git не хранятся.

## Структура

- `collector/npm/`, `collector/depsdev/`, `collector/osv/` — сбор исходных данных;
- `collector/pilot/` — возобновляемая сборка согласованного pilot snapshot;
- `processing/` — нормализация и исторические срезы;
- `analysis/` — статистический и security-анализ;
- `ml/` — прогнозирование;
- `infra/` — воспроизводимый запуск;
- `docs/` — определения, data contract и описание источников;
- `tests/` — проверки.

## Данные и DVC

После clone или переключения на другой commit нужная версия данных восстанавливается так:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dvc.txt
dvc remote modify --local yandex-s3 profile technical-lag
dvc pull
```

Remote `s3://technical-lag-data-2026/dvc` и endpoint Yandex уже находятся в общей конфигурации. Имя AWS-профиля и credentials остаются только локально. Правила публикации новых immutable snapshots, структура `data/` и команды для второго участника описаны в [документации по версионированию данных](docs/data-versioning.md).

## npm metadata collector

Нужен Python 3.9 или новее; сторонних библиотек нет. Из корня репозитория:

```bash
./bigdata collect npm --limit 1000
```

Команда выбирает пакеты запросом `keywords:javascript` к npm Search API, затем получает полный packument каждого пакета. В stdout выводится JSON summary; прогресс и ошибки идут в stderr. Сырые ответы, normalized JSONL, журнал, manifest запуска и markers для повторного запуска создаются в `data/`. Каталог уже исключен из Git.

Для точечного сбора имена можно передать явно:

```bash
./bigdata collect npm --limit 3 \
  --package express \
  --package lodash \
  --package request
```

Unit и live integration test:

```bash
python3 -m unittest tests.test_npm_collector
RUN_NPM_INTEGRATION=1 python3 -m unittest tests.test_npm_integration
```

Endpoint, формат файлов, правила повторного запуска и ограничения выборки описаны в [документации collector](docs/npm-collector.md). Формат normalized records задан в [Data Contract v1](docs/data-contract-v1.md).

## deps.dev dependency collector

Collector получает resolved dependency graph для точных npm package versions:

```bash
./bigdata collect depsdev \
  --input-file docs/samples/depsdev-package-versions.txt
```

Для acceptance batch с ожидаемыми пропусками API можно задать минимальное число успешных графов; весь вход при этом всё равно будет обработан:

```bash
./bigdata collect depsdev \
  --input-file docs/samples/depsdev-acceptance-package-versions.txt \
  --success-limit 1000
```

Можно передать версии прямо в команде:

```bash
./bigdata collect depsdev \
  --package-version react@18.2.0 \
  --package-version @colors/colors@1.5.0
```

Raw-ответы, normalized `DependencyRelation`, manifest, event log и markers повторного запуска сохраняются в `data/`. Формат API, используемые поля, ограничения и результаты MVP-сбора описаны в [документации deps.dev](docs/data-sources/depsdev.md).

```bash
python3 -m unittest tests.test_depsdev_collector
RUN_DEPSDEV_INTEGRATION=1 python3 -m unittest tests.test_depsdev_integration
```

## OSV vulnerability collector

Для запроса известных уязвимостей точных npm-версий:

```bash
./bigdata collect osv --package-version lodash@4.17.20 --package-version lodash@4.17.21
```

Сырые ответы OSV сохраняются отдельно; отсутствие найденных уязвимостей записывается как штатный результат. Полный пример, формат результатов и ограничения источника — в [документации OSV](docs/data-sources/osv.md).

```bash
python3 -m unittest tests.test_osv_collector
```

## Согласованный pilot dataset

Одна команда собирает полную npm-историю для замороженной выборки из 3000 пакетов, выбирает до пяти равномерно распределённых исторических версий каждого пакета, обогащает их через deps.dev и OSV и формирует immutable snapshot с compressed shards:

```bash
./bigdata collect pilot \
  --dataset-id pilot-v1 \
  --package-limit 3000 \
  --versions-per-package 5 \
  --workers 8 \
  --osv-workers 8
```

После прерывания запускается та же команда: npm, deps.dev и OSV используют проверяемые success markers, поэтому уже сохранённые ответы не скачиваются повторно. Готовый snapshot и отчёт находятся в `data/datasets/pilot-v1/`. Подробное описание этапов, критериев и структуры файлов — в [документации pilot](docs/pilot-dataset.md).
