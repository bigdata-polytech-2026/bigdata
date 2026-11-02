# Temporal analysis: pilot-v1

Анализ выполнен только на ранее опубликованных DVC-артефактах
`pilot-v1-temporal` и `pilot-v1-normalized`; новых запросов к npm Registry не
было. Перед вычислениями проверяются SHA-256 файлов из обоих manifests.

Воспроизводимый запуск:

```bash
.venv/bin/python -m pip install -r analysis/requirements.txt
.venv/bin/python -m analysis.temporal \
  --dataset-id pilot-v1 \
  --temporal-dir data/datasets/pilot-v1-temporal/temporal \
  --normalized-dir data/datasets/pilot-v1-normalized/normalized \
  --output-dir artifacts/analysis/temporal-pilot/pilot-v1
```

## Покрытие и сценарии

В temporal input содержатся 1 096 406 снимков 100 000 dependency relations.
Для 99 230 relations найден хотя бы один пригодный снимок; в проверку гипотез
вошли 1 183 consumer packages с полным набором package-level признаков.

| Сценарий | Relations | Смысл |
| --- | ---: | --- |
| `accumulation` | 64 764 | От нулевого lag к положительному terminal lag |
| `persistent_lag` | 21 284 | Lag есть в первом и последнем пригодном снимке |
| `always_current` | 13 182 | Lag отсутствует во всех пригодных снимках |

Сценарий `catch_up` формально предусмотрен, но в этом pilot не встретился. Это
наблюдение не означает, что обновления не происходят в npm: temporal набор
фиксирует состояния только на дискретных событиях публикации dependency.

## Гипотезы

Все тесты — двусторонняя корреляция Спирмена на package-level средних terminal
`version_lag`; это снижает влияние пакетов с большим числом relations.

| Гипотеза | rho | p-value | Практический размер эффекта |
| --- | ---: | ---: | --- |
| H1: более высокая частота релизов dependency связана с большим lag | 0,634 | 3,02e-134 | заметный |
| H2: больший возраст consumer package связан с большим lag | 0,342 | 7,73e-34 | заметный |
| H3: большее число зависимостей consumer package связано с большим lag | 0,278 | 2,07e-22 | умеренный |

При α=0,05 для всех трёх гипотез нулевая гипотеза об отсутствии монотонной
связи отвергается. Однако p-значение зависит от размера выборки и не является
мерой практической важности: её отражает rho. Результаты описательные, не
доказывают причинного влияния факторов и не относятся к lock-файлам или реально
установленным версиям.

## Артефакты

DVC output `e777af5ae3a6f3ef33193c604732afac.dir` (310 009 bytes, 11 files)
содержит четыре графика, четыре итоговые таблицы, JSON с тестами и
`conclusions.md`. Ограничения данных и полная интерпретация находятся в этом
`conclusions.md`.
