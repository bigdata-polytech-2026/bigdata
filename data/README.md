# Данные проекта

В Git лежат только этот файл, `.gitkeep`, `.dvc`-метаданные и создаваемые DVC файлы `.gitignore`. Содержимое датасетов хранится в Yandex Object Storage через DVC.

Рабочая структура:

```text
data/
├── raw/
│   ├── npm/YYYY-MM-DD/
│   ├── depsdev/YYYY-MM-DD/
│   └── osv/YYYY-MM-DD/
├── normalized/
│   └── v1/<entity>/YYYY-MM-DD/
├── datasets/
│   └── technical-lag-vN/
└── artifacts/
    └── dvc-smoke-v1/
```

Каталоги с датой и каталоги `technical-lag-vN` считаются immutable: готовую версию не перезаписываем. Новая выгрузка получает новую дату, а изменение логики итогового датасета — новый номер версии.

Коллекторы пока сохраняют аудируемые raw-ответы и normalized JSONL. Перед публикацией очень крупной выгрузки ее нужно собрать в разумное число partition-файлов (`*.jsonl.zst` для raw и Parquet для таблиц). Миллионы мелких JSON-файлов в DVC/S3 не публикуем.

`artifacts/dvc-smoke-v1.dvc` описывает маленький контрольный файл. Он нужен, чтобы новый участник мог сразу проверить доступ к remote командой `dvc pull` без загрузки большого датасета.

Полный рабочий процесс описан в [`docs/data-versioning.md`](../docs/data-versioning.md).
