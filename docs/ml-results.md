# ML2: исправленный прогноз сохранения major technical lag

Дата пересчёта: 12.11.2026. Версия результата: `full-v1-corrected-v2`.
Исходный результат PR #32 (`29db905`, `artifacts/ml/full-v1.dvc`) сохранён.
Реализованы ранее утверждённые требования [ML scope](ml-scope.md), без изменения
population, target, горизонта или временного разбиения. Обе модели заново обучены.

Основной результат: логистическая регрессия даёт **Average Precision (AP)
0,7985** на temporal test против 0,6190 у константного baseline и 0,6912 у
правила по текущему version lag. Дерево имеет AP 0,7349. Это оценка incident risk:
сохранится ли major lag через 180 суток после первого наблюдаемого major-lag
incident consumer–dependency, с учётом новых stable-манифестов consumer.

## Данные и признаки

| Split | Label 0 | Label 1 | Всего |
| --- | ---: | ---: | ---: |
| Train | 5 826 | 8 117 | 13 943 |
| Validation | 4 831 | 8 319 | 13 150 |
| Test | 7 829 | 12 720 | 20 549 |

Все 47 642 labels и split назначения совпали с исходным ML2. Повторных пар нет.
Положительная доля test — 61,9008%. Сохранены ML1 purge-эпохи; train endpoints
предшествуют 2020-01-01 UTC, validation endpoints — 2023-01-01 UTC. Горизонт
равен ровно 180×86400 секунд, все окна рассчитаны в UTC.

Матрица содержит все allowed/allowed_derived поля `ml-features.csv`: 16 числовых
колонок (включая три update flags), constraint category и missing indicators
для каждого числового признака до imputation. Добавлены consumer stable age,
prior version lag на t−30 и t−90, prior major на t−30, nonmonotone flag.
Update flags константны в incident risk set и удаляются после train-only
VarianceThreshold; список остальных удалённых констант сохранён для каждой модели.
Все числовые признаки стандартизируются после train median imputation;
категории проходят one-hot, с явной unknown category до удаления train-констант.
У моделей отдельные preprocessing instances. Source/dependency IDs не predictors.

Historical features реконструируются из последнего stable consumer manifest,
опубликованного не позже t−30/t−90, и dependency releases не позже этой же даты.
Отсутствие/исключение prior state — missing, а не нулевой lag. На t−30 известен
lag у 29 351 строки, missing у 18 291; на t−90 — 25 971 и 21 671 соответственно.
Десять consumer ages, не определимых корректно, оставлены NULL вместо прежнего
обнуления. Возраст теперь выражен дробными UTC-сутками t−created_at.

Проверочный пример: `2d-polygon-boolean` → `point-in-big-polygon`, prior cutoff
2015-05-06T01:55:16.739Z. Consumer manifest `1.0.0` опубликован
2014-12-18T03:17:42.478Z и задаёт `~1.0.0`. На cutoff стабильная максимальная
разрешённая версия dependency — `1.0.0`, максимальная доступная — `2.0.0`:
version lag=1, major=1. Релиз `2.0.1` от 2021-06-03 исключён из этого расчёта.
Сам manifest выбирается на prior cutoff, а не переносится назад с anchor.

## Baselines и фиксированные правила обучения

Majority использует train prevalence и threshold=0,5. Train majority — label 1,
поэтому бинарные predictions совпадают с persistence (всегда major сохраняется).
Прежний threshold=1,1 ошибочно давал только 0; опубликованный ранее F1=0
не является результатом корректного majority classifier.

Severity-rule ранжирует по current_version_lag; validation max-F1 threshold
оказался 1. Поэтому бинарные predictions тоже совпали с persistence, хотя
ранжирование и AP отличаются. Severity score не является вероятностью;
calibration для него помечена not applicable, без искусственной нормализации.

Logistic: C=1, L2, lbfgs, max_iter=2000, class_weight=None. Tree: max_depth=6,
min_samples_leaf=50. Seed=20261109. Параметры зафиксированы до пересчёта,
по test не подбирались. Порог каждой модели — validation max F1, при равенстве
выше precision, затем выше threshold. Logistic threshold=0,498704,
tree=0,411765. Hyperparameter search не проводился. Logistic — заранее
заданный primary ranking estimator; tree — фиксированный exploratory comparator.

## Test metrics

AP вычисляется как `average_precision_score`, не trapezoidal area under PR curve.
95% CI ниже — percentile consumer-cluster bootstrap: 2 000 повторов, seed=20261109,
6 521 consumer clusters. В каждом повторе все модели получают одни и те же
resampled consumers с сохранением всех их observations; модели и пороги не refit.

| Модель | AP | 95% CI AP | ROC-AUC | Precision | Recall | F1 |
| --- | ---: | --- | ---: | ---: | ---: | ---: |
| majority | 0.6190 | [0.5823; 0.6541] | 0.5000 | 0.6190 | 1.0000 | 0.7647 |
| persistence | 0.6190 | [0.5823; 0.6541] | 0.5000 | 0.6190 | 1.0000 | 0.7647 |
| severity_rule | 0.6912 | [0.6522; 0.7270] | 0.5821 | 0.6190 | 1.0000 | 0.7647 |
| logistic | 0.7985 | [0.7793; 0.8157] | 0.7226 | 0.7123 | 0.8417 | 0.7716 |
| tree | 0.7349 | [0.6626; 0.8031] | 0.6812 | 0.6655 | 0.9435 | 0.7805 |

У logistic TN=3504, FP=4325, FN=2013, TP=10707. У дерева TN=1797,
FP=6032, FN=719, TP=12001. Более высокий F1 дерева не означает лучшего ranking.
Brier score logistic=0,21146; calibration curve и её bin values сохранены отдельно.

Paired AP differences (те же resampled clusters):

| Сравнение | Разность AP | 95% CI |
| --- | ---: | --- |
| Logistic − constant baseline | +0,1795 | [0,1426; 0,2193] |
| Logistic − severity rule | +0,1073 | [0,0702; 0,1459] |
| Logistic − tree | +0,0636 | [−0,0033; 0,1370] |

Преимущество logistic по AP над двумя baseline сохраняется в этих интервалах.
Интервал logistic−tree включает ноль: точечная оценка выше, но уверенного
разделения моделей этот bootstrap не даёт. Интервалы разности F1 также включают
ноль: logistic−persistence [−0,0341; 0,0458], tree−persistence
[−0,0022; 0,0296]. Нельзя объявлять уверенный выигрыш F1 по одной точечной оценке.
Интервалы условны на обученных моделях и зафиксированном protocol, не включают
неопределённость fit, выбора scope или нового исходного snapshot; они не
скорректированы на множественные сравнения. Кластеры по общему dependency могут
создавать дополнительную корреляцию, которую consumer bootstrap не устраняет.

## Cohorts и интерпретация

| Consumer cohort | Test rows | Positive rate | Logistic AP | Logistic F1 |
| --- | ---: | ---: | ---: | ---: |
| Seen в train | 2 198 | 0,6001 | 0,7713 | 0,7626 |
| Unseen в train | 18 351 | 0,6213 | 0,8011 | 0,7727 |

Это описательная разбивка того же temporal holdout; case mix и prevalence разные.
Одна пара consumer–dependency не встречается в нескольких splits, но consumer
с другой зависимостью может встречаться. Predictor IDs исключены. Unseen-cohort
не является отдельным проспективным экспериментом или гарантийной оценкой transfer.

Для logistic AP по constraints: caret 0,8047 (n=16068), exact 0,7720 (3978),
range 0,9422 (118), tilde 0,8050 (298), wildcard 0,8620 (87). Малые cohorts
и различная prevalence ограничивают сравнение. F1 tilde=0,6425 указывает, что
общий validation threshold не даёт одинакового поведения по всем категориям.
Cohort-specific thresholds по test не настраивались.

Обе модели и все baseline имеют собственные PR/confusion/cohort/error artifacts.
Calibration доступна для probability outputs; severity rule явно исключена.
Ошибки содержат model identity, все test scores сохранены в Parquet.
Отрицательные outcomes с удалением dependency выделены отдельно; это structural
negative, не обнуление excluded lag. Коэффициенты logistic стандартизованы и
сохранены с именами признаков; они не доказывают причинное влияние факторов.

## Сравнение с первоначальным ML2 и ограничения

| Модель | Исходный AP | Исправленный AP | Исходный F1 | Исправленный F1 |
| --- | ---: | ---: | ---: | ---: |
| Logistic | 0,7865 | 0,7985 | 0,7566 | 0,7716 |
| Tree | 0,7274 | 0,7349 | 0,7860 | 0,7805 |

Точный повторный подсчёт: заменены 1 051 constraint categories из 47 642:
148 caret→range, 766 other→wildcard, 122 range→exact, 15 tilde→range.
Предварительное число 1 031 было арифметической ошибкой. Используется общий A7
classifier и pinned semver@7.8.4. UTC-исправление нижней границы cadence windows
изменило 1 dependency 90-day count, 4 dependency annual counts и 8 consumer
annual counts. Одновременно исправлены ages, добавлены historical features,
missing indicators и threshold tie rule. Это совокупная коррекция protocol;
рост AP нельзя приписывать одному признаку без отдельной ablation.

Temporal holdout уже изучался в исходном ML2. Исправления следуют ранее
зафиксированному scope, а не поиску по test, но пересчёт не является новым
независимым внешним подтверждением. Для такого подтверждения нужен следующий
независимый snapshot/период. Unknown/censored outcomes исключены и могут
отличаться систематически. Reconstructed survivors не восстанавливает удалённые
версии. Прогноз относится к разрешаемым версиям в сохранённых manifest и
endpoint status, не непрерывному lag, lockfiles, установленным версиям или
security risk. OSV и graph predictors по-прежнему исключены.

## Проверки и артефакты

Полный artifact: `artifacts/ml/full-v1-corrected-v2.dvc`. В dataset сохранены
feature coverage, причины отсутствия prior state и 95 284 prior audit rows.
В evaluation — модели, thresholds, validation/test metrics, paired bootstrap
intervals, per-model diagnostics и source/runtime fingerprints. В корне —
`verification.json` и `comparison.json`. Исходный DVC pointer не переписан.

`ml.verify` проверяет output/source hashes, train-only imputation/scaling,
перезагрузку сериализованных моделей, совпадение predictions и метрик sklearn,
validation thresholds, canonical A7 categories, labels с ML1, временные cutoffs,
purge, missingness и bootstrap metadata. Docker/k3d продолжал работать;
расчёт ограничен 1 ГБ DuckDB, двумя SQL threads и одним BLAS thread.

## Воспроизведение

Установить `analysis/requirements.txt`, `npm ci --prefix processing/technical_lag`
и восстановить full-v1 inputs через DVC. Использовать новые output directories;
если scope cache отсутствует, он автоматически строится и проверяется.
Не направлять scope build в уже существующий Git-only каталог без row payload.

```bash
export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 VECLIB_MAXIMUM_THREADS=1
.venv/bin/python -m ml build \
  --dataset-dir artifacts/ml/full-v1-corrected-v2/dataset \
  --scope-dir /tmp/ml-scope-corrected-v2
.venv/bin/python -m ml train \
  --dataset-dir artifacts/ml/full-v1-corrected-v2/dataset \
  --output-dir artifacts/ml/full-v1-corrected-v2/evaluation
.venv/bin/python -m ml.verify \
  --dataset-dir artifacts/ml/full-v1-corrected-v2/dataset \
  --evaluation-dir artifacts/ml/full-v1-corrected-v2/evaluation \
  --scope-dir /tmp/ml-scope-corrected-v2 \
  --output artifacts/ml/full-v1-corrected-v2/verification.json
.venv/bin/python -m unittest tests.test_ml_corrections tests.test_ml_pipeline tests.test_ml_scope
```

Главный научный вывод: доступные на anchor признаки содержат предиктивную
информацию о сохранении major lag через 180 суток. В зафиксированном incident
risk set logistic даёт AP 0,7985 против 0,6190 у константного baseline и 0,6912
у текущего version lag. Это подтверждённое улучшение ranking в пределах данного
holdout и описанных ограничений; универсальное превосходство над деревом или
практический эффект intervention из этих результатов не следуют.
