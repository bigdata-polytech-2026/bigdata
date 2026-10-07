# npm collector: acceptance run for 1000 packages

Проверка выполнена 07.10.2026 по московскому времени из корня репозитория на публичном npm Registry.

## Первый запуск

Команда:

```bash
./bigdata collect npm --limit 1000
```

- Run ID: `20261006T231526548299Z-a7209fff`
- UTC interval: `2026-10-06T23:15:26.548Z` — `2026-10-06T23:17:01.002Z`
- Query: `keywords:javascript`
- Parser: `npm-collector@1.0.0+source.0d176ab13f830b4eadbd2306ebe9fc32df00141a4eb61f89963facb6c48990bb`

Summary:

```json
{"available":1000,"downloaded":1000,"failed":0,"skipped":0,"target":1000}
```

Локальный dataset занимает 2.4 GiB и находится в исключенном из Git каталоге `data/`. Созданы 1000 package raw responses, 4 search responses, 1000 success markers и по 1000 файлов каждого normalized entity.

Полная потоковая проверка всех markers, raw SHA-256 и normalized JSONL дала:

```json
{
  "deprecated_versions": 7626,
  "packages": 1000,
  "packages_with_repository": 979,
  "raw_sha256_verified": 1000,
  "requirements": 2749695,
  "versions": 187333,
  "versions_with_repository": 181750,
  "versions_with_timestamp": 187333,
  "versions_without_timestamp": 0
}
```

## Повторный запуск

Run ID: `20261006T231810660446Z-8f5ab544`

```json
{"available":1000,"downloaded":0,"failed":0,"skipped":1000,"target":1000}
```

Повторный запуск выполнил search-запросы для актуального списка кандидатов, проверил локальные markers и не скачал package metadata повторно.

## Тесты

```text
python3 -m unittest
Ran 3 tests: OK (live test skipped by default)

RUN_NPM_INTEGRATION=1 python3 -m unittest tests.test_npm_integration
Ran 1 test: OK
```

Live integration test получил `express`, `lodash` и `request`, проверил raw responses, normalized package/version records, repository metadata, publish timestamps, deprecated versions и dependency requirements.
