# OSV security enrichment

Этот offline-процесс сопоставляет npm versions из нормализованного pilot dataset с OSV `Vulnerability` records схемы 2.0.0. Он не меняет immutable pilot snapshot, raw OSV-ответы или основной normalized dataset.

Нужна зависимость обработки Parquet:

```bash
.venv/bin/python -m pip install -r processing/security/requirements.txt
```

```bash
.venv/bin/python -m processing.security \
  --dataset-dir data/datasets/pilot-v1 \
  --versions-dir data/datasets/pilot-v1-normalized/normalized \
  --output-dir data/datasets/pilot-v1-security/security
```

Сопоставление package names — только точное. Для `affected.versions` используется точное равенство; `SEMVER` и `ECOSYSTEM` ranges интерпретируются по событиям OSV: `introduced` включает границу, `fixed`/`limit` исключают её, `last_affected` включает её. Git ranges не используются для npm version matching.

Выходы:

| Файл | Содержание |
| --- | --- |
| `security_features.parquet` | Одна строка на `(package_name, version)`: `is_vulnerable`, JSON-массивы `osv_ids`/`severity`, `has_fixed_version`, количества сопоставленных и неразрешённых records. |
| `mapping_errors.parquet` | Некорректные, неоднозначные или несопоставленные OSV records. |
| `mapping-statistics.json` | Match rate, число input records, vulnerable package versions и причины ошибок. |
| `security-manifest.json` | SHA-256 входов и outputs для повторного запуска. |

Одинаковые OSV records, полученные при запросах нескольких версий, дедуплицируются. `mapping_success_fraction` считается среди уникальных OSV records; число повторов записывается отдельно как `osv_records_deduplicated`. Конфликтующие records с одним `(osv_id, affected_index, package_name)` сохраняются как `ambiguous_duplicate_osv_record`. Пустой OSV input даёт валидную таблицу, в которой все npm versions неуязвимы.
