# Temporal pilot

`python -m processing.temporal` строит ограниченный исторический набор для
анализа динамики technical lag. В pilot отбираются детерминированно первые
100 000 прямых dependency relations и не более 12 состояний на relation.

## Единица наблюдения

Одна строка `temporal_technical_lag.parquet` — состояние отношения
`source_package@source_version -> dependency_name` в `observation_time`.
Первое состояние соответствует публикации consumer; следующие — моментам
публикации dependency. Для каждого момента повторно применяется общий
калькулятор technical lag.

В расчет попадает только dependency version с `published_at <= observation_time`.
Момент наблюдения не может быть позже минимума из `consumer_retrieved_at` и
`dependency_retrieved_at`. Поэтому версия, опубликованная после наблюдения,
не становится ни разрешенной, ни последней доступной версией.

## Запуск

```bash
.venv/bin/dvc repro temporal_pilot
```

Результат сохраняется в `data/datasets/pilot-v1-temporal/temporal/`:

- `temporal_technical_lag.parquet` — признаки lag для каждого снимка;
- `temporal-statistics.json` — покрытие во времени и количество снимков;
- `temporal-manifest.json` — хеши входов и результатов;
- `report.md` — краткая сводка.

Проверка границ включает 10 вручную составленных случаев в
`docs/evidence/temporal-pilot-boundaries.md`; они автоматизированы в
`tests/test_temporal_pipeline.py`. Результат запуска на pilot зафиксирован в
`docs/evidence/temporal-pilot-run.md`.

## Ограничения

История восстанавливается по опубликованным временам версий, доступным в
сохраненном Registry snapshot. Удаленные, не попавшие в snapshot или позже
исправленные метаданные Registry восстановить нельзя. Это представление
показывает состояние по известной истории релизов и не является независимым
архивом npm Registry.
