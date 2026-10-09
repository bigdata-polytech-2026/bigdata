# Pilot dataset: npm + deps.dev + OSV

## Цель

Pilot проверяет реальный join трёх источников до full collection. Он сохраняет полную историю npm-версий и dependency requirements для замороженной выборки пакетов, но ограничивает дорогие внешние enrichment-запросы несколькими историческими точками на пакет. Это позволяет измерить match rate, объем, скорость и ошибки, не отправляя сотни тысяч запросов без предварительной оценки.

Рекомендуемый первый запуск на текущей машине:

```bash
./bigdata collect pilot \
  --dataset-id pilot-v1 \
  --package-limit 3000 \
  --versions-per-package 5 \
  --workers 8 \
  --osv-workers 8
```

Команда не требует сторонних Python-библиотек и не загружает весь dataset в RAM.

## Этапы

1. npm Search фиксирует выборку из 3000 успешно полученных пакетов.
2. Полные npm packuments сохраняются byte-for-byte; создаются `Package`, `PackageVersion` и `DependencyRequirement`.
3. Все версии индексируются во временной SQLite-базе на диске.
4. Для каждого пакета выбираются до пяти версий, равномерно распределённых по истории. При наличии нескольких версий выборка всегда включает самую старую и самую новую.
5. Выбранные пары обрабатываются deps.dev и OSV частями по 500, поэтому в памяти нет списка полного dataset и десятков тысяч результатов одного batch.
6. При выполнении acceptance thresholds рабочие файлы собираются в immutable `data/datasets/pilot-v1`.

По умолчанию ожидается не менее 90% успешных deps.dev graphs и 99% успешных OSV queries. Все версии обрабатываются даже при отдельных API errors. Порог можно изменить флагами `--min-depsdev-success-rate` и `--min-osv-success-rate`; фактические ошибки остаются в отчёте.

## Resume

Если процесс, сеть или ноутбук прервали сбор, нужно повторить ту же команду. Повторный запуск:

- использует замороженный `landing/pilot-v1/inputs/packages.txt`;
- проверяет parser version, пути и SHA-256 raw-файлов npm, deps.dev и OSV;
- не повторяет валидные запросы;
- снова пытается получить только отсутствующие или поврежденные результаты;
- не меняет уже созданный финальный snapshot.

Если `data/datasets/pilot-v1` уже существует с той же конфигурацией, команда только возвращает его summary без API-запросов. Другая конфигурация должна получить новый `--dataset-id`.

## Физическая структура

```text
data/
├── landing/pilot-v1/
│   ├── raw/                  # рабочие immutable API responses
│   ├── normalized/           # рабочие normalized records
│   ├── state/                # resume markers
│   ├── manifests/            # manifests sub-runs
│   ├── logs/
│   ├── inputs/
│   └── reports/
└── datasets/pilot-v1/
    ├── inputs/
    ├── raw/<source>/part-*.tar.gz
    ├── normalized/<entity>/part-*.jsonl.gz
    ├── audit/<source>/part-*.tar.gz
    ├── validation/
    ├── manifest.json
    └── report.md
```

Raw archives сохраняют исходные bytes и относительные пути файлов. Audit archives содержат активные state markers и manifests, по которым проверяются identity, parser version и SHA-256. Normalized shards записываются потоково; целевой compressed size по умолчанию 128 MiB.

`jsonl.gz` выбран для pilot, потому что он доступен в стандартной библиотеке Python и позволяет проверить join без установки тяжелого стека. После измерения pilot full pipeline сможет добавить Parquet с размером row group, выбранным по реальным данным.

## Результат и публикация

Финальная строка stdout — JSON summary. Основной человекочитаемый результат находится в `data/datasets/pilot-v1/report.md`, полный список shards и checksums — в `manifest.json`.

Команда намеренно не выполняет `dvc add`, `dvc push`, Git commit или upload в S3. Сначала snapshot валидируется локально; после подтверждения публикуется только compacted `data/datasets/pilot-v1`, а рабочий `landing` остается локальным и может быть удален отдельно после согласования.
