# Technical Lag in npm

Анализ technical lag зависимостей npm: расчёт исторических метрик, A6/A7,
B4/B5 и прогноз сохранения major lag через 180 суток. Источники — npm Registry,
deps.dev и OSV. Код и DVC pointers хранятся в Git, payloads — в Yandex Object
Storage. Основной способ запуска — **Docker Compose + Make**.

## Installation

Требуются Git, Make, Docker Engine/Desktop и Compose v2 с Buildx
(проверенная версия Compose: 2.39.2). На macOS запустите Docker Desktop;
на Linux Docker daemon должен быть доступен текущему пользователю.
Host Python, Node и AWS CLI не требуются.

```bash
git clone https://github.com/bigdata-polytech-2026/bigdata.git
cd bigdata
cp .env.example .env
make setup
```

`make setup` строит образ и проверяет runtime. Базовые образы закреплены digest,
Python — **3.14.8**, Node — **22.17.0**, `semver` — **7.8.4**. Все Python
зависимости, включая DVC, зафиксированы с SHA-256 в
`infra/distribution/requirements.lock`; npm использует два `package-lock.json`.
Debian packages устанавливаются из dated snapshot, включая security repository.
Credentials не входят в образ; `.env`, `.dvc/config.local`, данные и host `.venv`
исключены из build context. Чистая проверка и ограничения описаны в
[reproducibility-check](docs/reproducibility-check.md).

Ресурсы:

| Режим | Рекомендуется |
| --- | --- |
| Demo / tests / frozen ML inference | Ноутбук с 8 ГБ RAM; Docker limit 4 ГБ, 2 CPU; 3 ГБ свободного диска для image/build |
| Финальные статистические анализы | 8–16 ГБ RAM, 15 ГБ свободного диска с учётом DVC cache и scratch |
| Полный пересчёт technical lag | 16 ГБ RAM и 30+ ГБ диска; увеличить `BIGDATA_MEMORY` в `.env` до 8g |

Загрузка образов и dependencies требует сети и выполняется при setup.
Demo после `make data` не обращается к npm/OSV APIs или S3.

## Data

В проекте разделены:

- immutable npm/pilot snapshots: `data/datasets/full-v1`, `pilot-v1`;
- normalized datasets, lag, temporal и security: отдельные `data/datasets/*`;
- statistical artifacts: опубликованные `artifacts/analysis/*`;
- ML artifacts: `artifacts/ml/full-v1-corrected-v2`;
- новые результаты воспроизведения: `artifacts/demo/*` и `artifacts/reproduced/*`.

DVC remote: `s3://technical-lag-data-2026/dvc`,
endpoint: `https://storage.yandexcloud.net`. Нужен AWS-compatible profile
с правами чтения/list этого bucket. **DVC pointers не дают доступ к приватному
bucket.** Профиль и ключи выдаются владельцем хранилища; собственный пустой bucket
не содержит опубликованных данных.

Создайте профиль `technical-lag` в стандартных AWS files на host. Например,
`~/.aws/credentials` (подставить собственные ключи, не коммитить):

```ini
[technical-lag]
aws_access_key_id = YOUR_ACCESS_KEY
aws_secret_access_key = YOUR_SECRET_KEY
```

`~/.aws/config`:

```ini
[profile technical-lag]
region = ru-central1
```

В `.env` задайте **абсолютный** путь к этой директории и имя своего профиля:

```dotenv
AWS_DIR=/home/alice/.aws
AWS_PROFILE=technical-lag
```

Для macOS путь обычно `/Users/alice/.aws`. Каталог монтируется read-only.
По умолчанию `.env.example` указывает на пустой каталог: setup/tests работают
без ключей, а data-команды сообщат, что profile отсутствует. Команда data
автоматически настраивает local DVC profile и hardlink/copy cache, не меняя
общую `.dvc/config`. Секреты не передаются через аргументы команд.

```bash
make data          # только demo-v1 + неизменённые OSV shards: менее 1 МБ
make data-ml       # только финальный ML dataset + saved models: около 11 МБ
make data-full     # full normalized + lag, pilot normalized/temporal/security, OSV и ML
make data-raw      # опциональный raw full-v1: около 3,5 GB; не нужен для analyze-full
```

`data-full` восстанавливает полный **аналитический** набор: все 76 871 904
requirements и соответствующий lag, плюс входы опубликованных B4/B5 и ML.
Он не скачивает повторно raw npm snapshot. Уникальные full inputs занимают
примерно 2 ГБ; без hardlinks cache и checkout могут потребовать вдвое больше.
`make data-raw` отдельно восстанавливает весь исходный immutable full-v1.
Повторный сбор Registry не нужен. Версии зафиксированы pointers и `dvc.lock`;
`dvc pull` вызывается для конкретных targets, без repro upstream collectors.

При собственной установке DVC вне Docker эквивалентная local настройка:

```bash
dvc remote modify --local yandex-s3 profile YOUR_PROFILE
dvc pull data/datasets/full-v1-normalized.dvc data/datasets/full-v1-technical-lag.dvc
```

Подробнее о происхождении данных: [data versioning](docs/data-versioning.md),
[Data Contract](docs/data-contract-v1.md), [определение lag](docs/technical-lag-definition.md).

## Demo

После настройки доступа к S3:

```bash
make data
make test
make demo
make demo   # тот же результат переиспользуется после проверки checksum
```

Demo — **проекция реальных immutable данных**, не синтетические observations:
25 consumer packages, все их 7 901 requirements, 54 packages с полными историями
20 739 версий, сохранённые OSV records. Срез содержит контекст dependency
histories, но не добавляет их собственные requirements в consumer sample.
Manifest содержит исходные hashes, правило выбора и provenance.

Команда заново рассчитывает lag, строит A6/A7, temporal и security inputs,
выполняет B4/B5 и inference двух frozen ML-моделей на фиксированной проекции
500 test rows. В statistical demo используется 100 bootstrap repetitions,
в temporal — до 1 000 relations и 12 состояний, поэтому это проверка всего
workflow, **не замена финальных оценок**. Demo ML не обучается и не меняет
thresholds; полные сохранённые показатели не выдаются за метрики маленького среза.

`artifacts/demo/latest.json` перечисляет результаты. Для каждого этапа создаётся
`artifacts/demo/<stage>/<fingerprint>/` с таблицами/графиками и `execution.json`.
Ожидаются technical-lag, A6, A7, temporal, security, B4, B5 и ML.
[Distribution exporter](infra/distribution/export_demo.py) — maintainer tool:
срез опубликован через DVC, при обычном запуске пересобирать его не требуется.

## Full analysis

```bash
make data-full
make analyze-full    # A6 + A7 на full-v1; B4 + B5 на их финальном pilot-v1
make ml-full         # inference/evaluation saved моделей на всех 20 549 test rows
make artifacts       # единая команда для analyze-full + ml-full
```

Full mode воспроизводит **опубликованный масштаб каждого блока**. B4/B5 в проекте
выполнены на temporal pilot (100 000 relations), а не на всей npm collection;
A11 не превращает их в новый эксперимент. A6/A7 используют 2 000 bootstrap
repetitions и прежние seed/методику.

Отдельные команды:

```bash
make a6-full
make a7-full
make b4-full
make b5-full
make technical-lag-full   # полный пересчёт из full normalized, долгий и требует scratch
make ml-train-full        # опциональный retrain прежних logistic/tree из готовой матрицы
```

`ml-full` загружает только сохранённые модели и проверяет совпадение AP/ROC-AUC/
precision/recall/F1 с опубликованными значениями (tolerance 1e-8, confusion matrix
должна совпасть точно). **Fit не вызывается.** `ml-train-full` отдельно повторяет
утверждённый fit/evaluation protocol, без новых типов моделей или поиска по test.
Результаты и ограничения ML: [ml-results](docs/ml-results.md).

Результаты: `artifacts/reproduced/<stage>/<fingerprint>/`, общий index —
`artifacts/reproduced/latest.json`. Итоговые DVC artifacts не переписываются.
Параметры, hashes входов, кода и dependency lock определяют fingerprint.
Нормальный повторный запуск проверяет результаты и переиспользует их;
изменение входов/кода создаёт новый run. Неудавшийся этап строится во временном
каталоге, поэтому повтор не требует ручного удаления промежуточных файлов.
Повреждение checksum диагностируется явно и не маскируется cache hit.

## Troubleshooting

- **Docker недоступен:** запустите Docker Desktop/daemon, проверьте `docker info`.
  `make setup` использует builder текущего Docker context, не чужой выбранный
  multi-platform builder; при необходимости задайте `BUILDER=<name>`.
- **Credentials unavailable / profile not found:** проверьте абсолютный AWS_DIR,
  наличие файлов credentials/config и совпадение AWS_PROFILE с заголовком profile.
  Не вставляйте ключи в Dockerfile или source. `make doctor` не требует S3.
- **Access denied / data missing in remote:** нужны права на опубликованный bucket;
  пустой fork remote не заменяет источник. При сетевых proxy проверьте настройки
  Docker, не исправляйте ошибки сменой дат/версий данных.
- **Missing data:** выполните соответствующий `make data`, `data-full` или `data-ml`.
- **Missing dependencies / semver:** выполните `make setup`. Host `.venv` и Node
  не используются. После обновления npm lock можно удалить только project node
  volumes через `docker compose down -v`, затем setup; datasets при этом не удаляются.
- **Out of memory:** увеличьте лимит Docker Desktop и BIGDATA_MEMORY в `.env`
  или запускайте этапы отдельно. Для demo достаточно default 4g / 2 CPU.
- **No space left:** cache, immutable inputs и generated runs имеют разные роли.
  Не удаляйте исходные snapshots ради повторного запуска. Уберите только свои
  ненужные generated runs либо используйте общий DVC cache на том же filesystem.
  Никакой автоматический Docker/DVC prune не выполняется.
- **Checksum mismatch:** не изменяйте immutable files. Повторно восстановите input
  через DVC либо сохраните испорченный generated run отдельно и повторите этап.
  Испорченный output никогда не используется молча.

Опциональный общий cache в нескольких clean checkouts при малом свободном диске:

```dotenv
PROJECT_STORAGE_ROOT=/absolute/common-parent
PROJECT_SUBDIR=relative/path/to/this-clone
DVC_CACHE_DIR=/workspace/.dvc/cache
```

Здесь cache расположен в `<common-parent>/.dvc/cache`, clone — внутри того же
common-parent. Это один bind mount, что позволяет hardlinks без второй копии
больших immutable файлов. Paths container-visible; Python/Node всё равно
из image, а cwd — чистый clone. Обычно эти параметры оставляют default.
Подробности фактической проверки и того, где cache был холодным/общим, см.
[reproducibility-check](docs/reproducibility-check.md).

## Development

Основные collector и pipeline модули остаются доступны; A11 не собирает npm заново.
Документация: [npm](docs/npm-collector.md), [deps.dev](docs/data-sources/depsdev.md),
[OSV](docs/data-sources/osv.md), [pilot](docs/pilot-dataset.md),
[normalization](processing/normalization/README.md), [technical lag](processing/technical_lag/README.md).
Обновление dependency lock: `uv pip compile infra/distribution/requirements.in
--python-version 3.14.8 --universal --generate-hashes
--output-file infra/distribution/requirements.lock` (зафиксировать новый lock в Git).
