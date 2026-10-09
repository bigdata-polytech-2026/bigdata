# OSV: известные уязвимости npm

Сборщик обращается к `POST /v1/query` OSV с точной парой `package.name`, `package.ecosystem="npm"` и `version`. Это ответ на вопрос, известны ли OSV уязвимости для запрошенной версии на момент сбора, а не историческое утверждение о состоянии базы в прошлом.

Запуск из корня репозитория:

```bash
./bigdata collect osv --package-version lodash@4.17.20 --package-version lodash@4.17.21
```

Для списка подходит текстовый файл, по одной паре `PACKAGE@VERSION` на строку; scoped-пакеты поддерживаются, например `@babel/core@7.20.0`:

```bash
./bigdata collect osv --input-file docs/samples/osv-package-versions.txt --workers 8
```

Каждый ответ, включая `{}` для версии без найденных уязвимостей, сохраняется в `data/raw/osv/<date>/<snapshot>.json`. Если OSV возвращает `next_page_token`, collector следует [официальному протоколу pagination](https://google.github.io/osv.dev/post-v1-query/): передает token как `page_token` до исчерпания страниц и сохраняет каждую страницу отдельно. Нормализованные строки `Vulnerability` схемы 2.0.0 сохраняются в `data/normalized/v2/vulnerability/<date>/<snapshot>.jsonl`; их формат задан в [OSV contract v2](../data-contract-v2.md).

Успешный положительный или отрицательный результат получает marker в `data/state/osv/`. Повторный запуск проверяет package/version, parser version, наличие normalized-файла и SHA-256 всех raw-страниц, после чего использует результат без сетевого запроса. `--refresh` отключает reuse. В manifest статус `no_vulnerabilities` — нормальный исход, а `api_error` означает изолированную ошибку одного запроса.

Итоговый JSON содержит метрики запуска: `checked`, `downloaded`, `skipped`, `versions_with_vulnerabilities`, `unique_osv_records`, `vulnerability_records`, `api_errors` и `elapsed_seconds`.

## Ограничения

- OSV — агрегатор advisory, поэтому его покрытие и точность зависят от подключенных баз и могут меняться между запусками.
- Запрос версионный; он не определяет, была ли версия фактически установлена в проекте и не вычисляет время появления либо исправления уязвимости.
- В ответе могут быть диапазоны, а не исчерпывающий список версий. `fixed_versions` включает только события `fixed` диапазонов `SEMVER` и `ECOSYSTEM`; пустой список не означает отсутствия исправления.
- Уровень серьёзности не всегда указан и хранится как исходный тип/score OSV, без пересчёта в число. Канонический ключ — `osv_id`; CVE и прочие aliases также сохраняются отдельным полем `aliases`.
- Одна OSV advisory может содержать несколько блоков `affected`; сборщик сохраняет отдельную нормализованную строку для каждого подходящего npm-блока. Некорректные или неполные блоки не выдумываются и фиксируются предупреждением в журнале.
