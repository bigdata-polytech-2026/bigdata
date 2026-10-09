# Версионирование данных через DVC и Yandex Object Storage

Код и метаданные версий хранятся в GitHub, а содержимое датасетов — в bucket `technical-lag-data-2026`. В репозитории уже настроен default remote:

```text
yandex-s3  s3://technical-lag-data-2026/dvc
```

Endpoint Yandex Object Storage хранится в `.dvc/config`. Ключей там нет. DVC использует локальный AWS-профиль каждого участника из игнорируемого файла `.dvc/config.local`.

## Первый запуск

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dvc.txt

dvc remote modify --local yandex-s3 profile technical-lag
dvc pull
```

Профиль `technical-lag` должен быть заранее настроен в AWS CLI и иметь доступ к bucket. Если у участника профиль называется иначе, в локальной команде нужно указать его имя. Проверить доступ можно так:

```bash
aws --profile technical-lag \
  --endpoint-url https://storage.yandexcloud.net \
  s3api head-bucket \
  --bucket technical-lag-data-2026
```

`access_key_id` и `secret_access_key` нельзя записывать в `.dvc/config`, `.env`, README или commit. Если профиль использовать нельзя, credentials разрешено задавать только командами `dvc remote modify --local ...`; результат останется в `.dvc/config.local`, который игнорируется Git.

Проверить DVC отдельно от будущих больших датасетов можно на маленьком контрольном artifact:

```bash
dvc pull data/artifacts/dvc-smoke-v1.dvc
cat data/artifacts/dvc-smoke-v1/payload.txt
```

Ожидаемое содержимое: `technical-lag dvc remote smoke test v1`.

## Получение версии данных из Git commit

```bash
git pull
dvc pull
```

После `git checkout <commit>` повторный `dvc pull` восстанавливает именно те файлы, хэши которых записаны в выбранном commit.

## Публикация новой immutable-версии

Добавляем в DVC готовые version/date partitions, а не общие изменяемые каталоги и не миллионы отдельных файлов:

Для согласованного pilot команда `collect pilot` уже формирует compacted snapshot, поэтому публикуется только он; рабочий `data/landing/pilot-v1` в DVC не добавляется:

```bash
dvc add data/datasets/pilot-v1
dvc push
git add data/datasets/pilot-v1.dvc data/datasets/.gitignore
git commit -m "data: publish pilot dataset v1"
git push
```

Для отдельных source partitions или будущего full dataset применяется тот же принцип:

```bash
dvc add data/raw/npm/2026-10-09
dvc add data/normalized/v1/package/2026-10-09
dvc add data/normalized/v1/package_version/2026-10-09
dvc add data/normalized/v1/dependency_requirement/2026-10-09
dvc add data/datasets/technical-lag-v1

dvc push
git add .dvc .dvcignore data docs requirements-dvc.txt
git commit -m "data: publish technical lag dataset v1"
git push
```

DVC создаст рядом с каждым output файл `<name>.dvc` и локальный `.gitignore`. В Git отправляются эти метаданные, но не сами data-файлы.

Если версия уже опубликована, ее путь больше не меняем. Следующий snapshot получает новую дату, а новый состав или смысл итогового датасета — `technical-lag-v2`, `technical-lag-v3` и так далее. Так один участник может продолжать анализ старого commit, пока другой готовит новую версию в своей ветке.

## `.dvc` и pipeline-файлы

Сейчас данные версионируются отдельными `.dvc`-файлами, потому что репозиторий еще не содержит законченного pipeline сборки Parquet-датасета. Когда появятся воспроизводимые processing-команды, их нужно описать в `dvc.yaml`; после первого `dvc repro` DVC создаст `dvc.lock` с точными хэшами входов и выходов. Пустой или фиктивный pipeline в репозиторий не добавляем.

## Что физически лежит в Object Storage

Локальные пути `data/raw`, `data/normalized` и `data/datasets` являются понятной человеку структурой проекта. DVC хранит их содержимое под `s3://technical-lag-data-2026/dvc` по content hash. Поэтому в bucket не нужно вручную создавать копии тех же файлов в отдельных `raw/`, `normalized/` и `datasets/` prefix: иначе появятся две несогласованные версии одних данных.
