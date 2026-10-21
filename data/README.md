# Данные проекта

В Git лежат только этот файл, `.gitkeep`, `.dvc`-метаданные и создаваемые DVC файлы `.gitignore`. Содержимое датасетов хранится в Yandex Object Storage через DVC.

Рабочая структура:

```text
data/
├── landing/
│   └── pilot-v1/             # resumable рабочее состояние collectors
├── raw/
│   ├── npm/YYYY-MM-DD/
│   ├── depsdev/YYYY-MM-DD/
│   └── osv/YYYY-MM-DD/
├── normalized/
│   └── v1/<entity>/YYYY-MM-DD/
├── datasets/
│   ├── pilot-v1/             # immutable compacted pilot snapshot
│   ├── pilot-v1-normalized/  # отдельный производный Parquet dataset
│   ├── pilot-v1-technical-lag/ # рассчитанные lag-признаки pilot
│   └── technical-lag-vN/      # будущие версии итоговых datasets
└── artifacts/
    └── dvc-smoke-v1/
```

Каталоги с датой и каталоги `technical-lag-vN` считаются immutable: готовую версию не перезаписываем. Новая выгрузка получает новую дату, а изменение логики итогового датасета — новый номер версии.

Отдельные collectors сохраняют аудируемые raw-ответы и normalized JSONL. Команда `collect pilot` использует `landing/<dataset-id>` как возобновляемое рабочее состояние, а затем формирует immutable `datasets/<dataset-id>`: byte-exact raw-файлы и audit metadata упаковываются в `tar.gz`, normalized entities — в `jsonl.gz` shards. Миллионы мелких рабочих файлов из `landing` в DVC/S3 не публикуем.

`artifacts/dvc-smoke-v1.dvc` описывает маленький контрольный файл. Он нужен, чтобы новый участник мог сразу проверить доступ к remote командой `dvc pull` без загрузки большого датасета.

Stage `normalize_pilot` в `dvc.yaml` пересчитывает npm/deps.dev из `pilot-v1` и
сохраняет Parquet отдельно в `pilot-v1-normalized/normalized`. Старый snapshot
и его `.dvc` descriptor не меняются; производный output версионируется `dvc.lock`.
Следующий stage `technical_lag_pilot` читает этот точный DVC output и сохраняет
расчёт отдельно в `pilot-v1-technical-lag/technical_lag`.

Полный рабочий процесс описан в [`docs/data-versioning.md`](../docs/data-versioning.md).
