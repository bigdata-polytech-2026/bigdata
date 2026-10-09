# npm metadata collector

Collector получает первичные данные npm для последующего построения исторического датасета technical lag. Он не разрешает граф зависимостей и не рассчитывает lag.

## Источник и выборка

Используется публичный npm Registry без токена:

- `GET https://registry.npmjs.org/-/v1/search?text={query}&size={size}&from={offset}` — получение имен пакетами по 250;
- `GET https://registry.npmjs.org/{package}` с `Accept: application/json` — полный package metadata document, или packument.

Формат полного ответа описан в [официальной документации npm Registry](https://github.com/npm/registry/blob/main/docs/responses/package-metadata.md). Сокращенный media type `application/vnd.npm.install-v1+json` не используется, потому что он исключает часть исходных полей.

По умолчанию query равен `keywords:javascript`. Это воспроизводимая процедура получения рабочей выборки, но не случайная и не репрезентативная выборка всей экосистемы. Каждый search response и итоговый порядок результатов сохраняются, поэтому конкретный запуск можно проверить. Для точного повторения фиксированного списка используйте `--packages-file`.

## Запуск

Из корня репозитория:

```bash
./bigdata collect npm --limit 1000
```

`--limit` задает требуемое число успешно доступных пакетов. Ошибочный пакет записывается в лог, после чего collector берет следующего кандидата. Результат запуска выводится в stdout; exit code равен 0, только если достигнут target.

Полезные параметры:

```text
--workers 8              число параллельных package-запросов
--timeout 30             timeout одного HTTP-запроса
--retries 3              retries для network errors, HTTP 429 и HTTP 5xx
--package NAME           явное имя; параметр можно повторять
--packages-file PATH     UTF-8 файл с одним именем на строку
--data-dir PATH          корень данных, по умолчанию data
--refresh                не использовать success markers
```

Переменные `NPM_REGISTRY_URL`, `NPM_SEARCH_QUERY` и `BIGDATA_DATA_DIR` задают значения по умолчанию. Registry credentials collector не принимает и не записывает.

## Выходные данные

Для каждого успешного package response создается новый `snapshot_id`:

```text
data/
  raw/npm/YYYY-MM-DD/{snapshot_id}.json
  raw/npm-search/YYYY-MM-DD/{run_id}-from-{offset}.json
  normalized/v1/package/YYYY-MM-DD/{snapshot_id}.jsonl
  normalized/v1/package_version/YYYY-MM-DD/{snapshot_id}.jsonl
  normalized/v1/dependency_requirement/YYYY-MM-DD/{snapshot_id}.jsonl
  state/npm/{sha256(package_name)}.json
  logs/npm/{run_id}.jsonl
  manifests/npm/{run_id}.json
```

Package raw body записывается байт-в-байт до нормализации. HTTP status, URL, retrieval time, SHA-256 и результат обработки находятся в JSONL-журнале и success marker. Raw содержит все поля источника; normalized records содержат историю версий, publish timestamps, четыре типа dependency requirements, repository metadata и deprecated flag/message согласно Data Contract v1.

`provenance.parser_version` состоит из версии collector и SHA-256 исходного файла нормализатора. Изменение логики автоматически создает новый идентификатор реализации.

## Повторный запуск и ошибки

Success marker считается действительным, только если совпадает parser version, существуют указанные в нем raw-файл и все три normalized-файла, а SHA-256 raw-файла совпадает с marker. Такой пакет учитывается в `--limit`, но не скачивается повторно. `--refresh` создает новый снимок; поврежденный старый raw не перезаписывается.

Записи raw и normalized сначала пишутся во временный файл и публикуются атомарным rename. Ошибка HTTP, JSON или нормализации относится только к одному пакету. Она попадает в stderr, JSONL-журнал и manifest; остальные requests продолжаются.

Большие данные находятся под `data/`, который исключен из Git. В репозиторий входят только код, тесты и маленький пример контракта.
