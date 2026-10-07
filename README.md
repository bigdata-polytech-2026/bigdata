# Technical Lag in npm

Исследовательский monorepo проекта «Анализ технического отставания программных зависимостей в экосистеме npm и факторов несвоевременного обновления».

Источники данных: npm Registry, deps.dev и OSV. Рабочий процесс: отдельная ветка на задачу, Pull Request, review и CI. Объемные данные и секреты в Git не хранятся.

## Структура

- `collector/npm/`, `collector/depsdev/`, `collector/osv/` — сбор исходных данных;
- `processing/` — нормализация и исторические срезы;
- `analysis/` — статистический и security-анализ;
- `ml/` — прогнозирование;
- `infra/` — воспроизводимый запуск;
- `docs/` — определения, data contract и описание источников;
- `tests/` — проверки.

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
