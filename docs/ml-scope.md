# ML scope v1: прогноз сохранения major technical lag

Все вычисления горизонтов выполняются в UTC; сутки = 86 400 секунд.

Дата фиксации: 09.11.2026. Модели не обучались. Input: immutable full-v1,
его normalization и technical-lag manifests. Исходные A6/A7/B4/B5 не изменены.

## Практический вопрос и область применения

Основная задача: при первом наблюдаемом major lag пары consumer–dependency
оценить вероятность, что через **180 суток** в последнем опубликованном stable
манифесте consumer для этой зависимости всё ещё будет major lag.
Это conditional incident-risk model; возникновение lag у сейчас актуальных
зависимостей в основной target не входит. 90 суток — заранее зафиксированный
secondary sensitivity endpoint; выбирать между горизонтами по качеству модели нельзя.

Population: stable consumer releases с известной датой, production dependencies
без optional override и с корректно определимым lag по A1. Дополнительный risk-set
фильтр: current update_type = major. Значительный lag определяется **наличием
major update**: major(v_latest(t)) > major(v_req(t)). Это SemVer-категория A1,
а не произвольный порог дней/числа версий. В A6 major составляет 1 286 896 строк,
67,04% случаев положительного version lag. Это обосновывает отдельную задачу
для структурно крупных обновлений; major не гарантирует реальной несовместимости.
Time lag и version lag остаются признаками; порог по ним не подбирается по test.

## Единица, anchor и построение метки

Одна строка — первый наблюдаемый major-lag incident пары
(source_package, dependency_name) в сохранённой истории. Среди пригодных stable
production requirements с major lag выбирается минимальное время публикации;
при равенстве anchor выбирается лексикографически минимальный source_version.
Включается только один incident на пару. Это исключает повтор одного процесса
между train/test; новые эпизоды после устранения lag в v1 не добавляются.
Первый наблюдаемый incident не обязательно является первым в реальной истории npm.

На endpoint u=t+180×86400 секунд:

1. Проверить u <= min(consumer_retrieved_at, dependency_retrieved_at). Иначе
   right-censored, label=NULL. Это только контроль наблюдаемости outcome,
   retrieval ceiling и факт цензурирования не становятся predictors.
2. Выбрать stable consumer version с максимальной датой публикации <=u.
   Это latest-by-publication, не максимум SemVer: backport может сменить ветку.
   Несколько stable версий в один момент дают unknown, не произвольную метку.
3. В её сохранённом манифесте найти исходное dependency_name. Если имени нет
   ни в одном из четырёх dependency fields: label=0, outcome_reason=dependency_removed.
   Это структурный negative, не присвоение нулевого lag исключённой строке.
4. Если имя осталось только в dev/peer/optional, либо production shadowed by
   optional: label=NULL, future_out_of_scope. Alias/Git/tag/prerelease и любые
   другие исключения A1 на endpoint также дают NULL, не label=0.
5. Для production requirement выбранной версии повторить A1 на точном u,
   используя только dependency releases с publication_time<=u. label=1 при
   update_type=major; label=0 при ok и update_type in {minor,patch,none}.

Положительный класс означает major lag **на endpoint**, не доказанное
непрерывное отставание весь интервал: устранение и повторное появление возможно.
Изменение requirement consumer учитывается. Фиксированный старый package.json
из B4 не используется как proxy обновлений проекта. Отсутствие новых релизов
consumer не означает отсутствие наблюдения: при достаточной истории действует
последний опубликованный stable manifest. Измеряются потенциально разрешаемые
версии, не lockfile, не установленная версия и не действия пользователей.

## Population funnel и class balance

Все числа ниже реально пересчитаны; sampling не сократил набор incidents:
технический лимит audit=60 000 выше всех 58 399 найденных пар.

| Последовательный фильтр | Наблюдений |
|---|---:|
| all | 76,871,904 |
| stable_consumer | 32,870,558 |
| production | 12,954,617 |
| calculable | 11,806,892 |
| current_major | 1,286,896 |
| unique_first_major_incidents | 58,399 |
| deterministic_scope_sample | 58,399 |

| Горизонт | Label 1 | Label 0 | Unknown/censored | Доля 1 среди известных |
|---|---:|---:|---:|---:|
| 90 суток | 38,184 | 17,437 | 2,778 | 68.65% |
| 180 суток | 31,987 | 20,563 | 5,849 | 60.87% |

180 суток выбраны до обучения: outcome измеряется на более длинном интервале,
negative outcomes чаще, чем на 90 сутках, при сохранении десятков тысяч известных
меток. Это не доказательство оптимальности горизонта для бизнеса. Конкретные
причины unknown и удаления сохранены в horizons.csv; class balance относится
к описанному incident risk set, не ко всем 76,87 млн requirements.
Endpoint coverage и censoring обязаны повторно публиковаться для каждой версии
ML dataset. Полные результаты scope audit не являются готовой feature matrix.

## Temporal split v1

Фактические anchors: 2011-03-02 — 2026-10-27. Для известных 180-day test outcomes
последние anchors достигают 2026-04-30. Индивидуальный retrieval ceiling важнее
глобальной даты окончания. Границы округлены до календарных эпох; они не
подбирались по качеству модели. Технический output dates.csv содержит также
будущие endpoints цензурированных строк, это не доступные outcome observations.

```text
train:       t < 2019-07-05 UTC
purge:       2019-07-05 <= t < 2020-01-01
validation:  2020-01-01 <= t < 2022-07-05
purge:       2022-07-05 <= t < 2023-01-01
test:        t >= 2023-01-01, только при наблюдаемом endpoint
```

Purge исключает строки из обучения/оценки. Для train требуется t+180d <
2020-01-01, для validation — t+180d < 2023-01-01; дополнительные assertions
обязательны при построении dataset. Нет random split. Даты также используются
для secondary 90-day endpoint, чтобы comparison не менял эпохи.

| Split, 180 суток | Label 1 | Label 0 | Unknown | Доля 1 среди известных |
|---|---:|---:|---:|---:|
| train | 8,117 | 5,826 | 375 | 58.22% |
| validation | 8,319 | 4,831 | 494 | 63.26% |
| test | 12,720 | 7,829 | 4,889 | 61.90% |
| purged | 2,831 | 2,077 | 91 | 57.68% |

Один consumer может иметь разные dependency incidents в разных эпохах; это
оценка будущих incidents для встречавшихся consumer packages, не cold-package
transfer. В отчёте отдельно показать test по seen/unseen consumers относительно
train. Имена и версии не predictors. Доверительные интервалы считать cluster
bootstrap по consumer package (2 000 повторов, seed=20261109), сохраняя зависимости
внутри пакета вместе; underlying dependency packages также создают корреляцию.

## Признаки и временная доступность

Нормативная [таблица признаков и provenance](ml-features.csv) задаёт определения,
источник, cutoff и статус каждого поля. Разрешены только allowed/allowed_derived;
derived означает однозначно заданное вычисление, а не уже готовую колонку.

Все даты release/creation берутся из сохранённых npm event timestamps.
Историческая доступность реконструируется с отсечением на t; метаданные,
которые могли измениться и не имеют истории, исключены. Для cadence учитывать
только stable releases в соответствующем прошлом окне. Для prior lag на t−30d
и t−90d заново выбирать опубликованный тогда stable consumer manifest;
нельзя применять текущий manifest до его публикации. Tie, отсутствующая
история и исключение A1 дают missing, а не 0. Не использовать 12 разреженных
снимков B4 для интерполяции на произвольную дату.

OSV и deps.dev graph predictors в v1 **исключены**: у full-v1 нет graph rows,
а текущие OSV annotations не доказывают, что сведения были доступны на t.
Одного advisory.published_at недостаточно: нужны point-in-time advisory versions,
включая affected ranges, modified и withdrawn. Lifetime releases, current
popularity, нынешний dist-tags.latest и факт будущего удаления запрещены.

Набор матрицы фиксирован всеми allowed полями CSV. Категории update_type и
constraint_type one-hot, неизвестная category — отдельное unknown. Числа:
median imputation с missing indicator, StandardScaler; все параметры fit только
на train. Для истории без измеримого прошлого оставлять missing. Удалить
константные predictors, включая current major flag, по train. Не использовать
current_requirement как свободный текст или source/dependency IDs. Фичи
не могут содержать horizon outcome, future_version, endpoint reason, retrieval
ceilings, период split или class label.

## Baseline, обучение и метрики

Обязательные baselines без обучения итоговой модели:

- majority: класс по train majority, probability = train positive prevalence;
- persistence: всегда label=1 (в risk set уже есть major), score=1;
- severity rule: score=current_version_lag, cutoff выбирается на validation
  по max F1, при равенстве — больший precision, затем больший cutoff.

Logistic regression — первый обучаемый интерпретируемый baseline, после фиксации
этого документа и отдельной сборки ML dataset: C=1, penalty=L2, solver=lbfgs,
max_iter=2000, class_weight=None, random_state=20261109. В v1 hyperparameter
search не проводится. Threshold — max validation F1; ties: выше precision,
затем выше threshold. Тест применяется единожды после фиксации preprocessing,
feature list, коэффициентов и cutoff. Не балансировать validation/test;
ресемплинг train в v1 не применяется.

Primary ranking metric — **average precision** (sklearn average_precision_score,
а не trapezoidal PR area). Публиковать PR curve и baseline prevalence для
каждого split. Обязательны precision, recall, F1, confusion matrix и support
при зафиксированном validation threshold. ROC-AUC — secondary; при одном классе
метрика undefined, не 0. Accuracy можно показать дополнительно. Отдельно
публиковать coverage и unknown rate: исключённые outcomes не участвуют ни
в положительном, ни в отрицательном классе. Помимо общей оценки — constraint
cohorts, seen/unseen consumer и reason=removed в отрицательном классе.

## Leakage controls и build contract

Builder обязан сохранить keys, anchor t, endpoint, horizon, raw provenance,
calculation/parser versions, target_status, outcome_reason и feature cutoffs
отдельно от feature matrix. Эти служебные поля не predictors. Перед training:

1. Проверить input hashes, cardinality, уникальность consumer/dependency incident.
2. Проверить stable production eligibility и current_major=1 для каждого anchor.
3. Для каждой feature dependency/event assertion publication_time<=feature_cutoff<=t.
4. Для labels повторить A1 именно на endpoint, проверив snapshot coverage.
   Нет ближайшего snapshot, переноса endpoint или forward fill из будущего.
5. Проверить endpoint purge, отсутствие одной пары в нескольких splits,
   отсутствие fitting preprocessing на validation/test.
6. Проверить label=NULL для unknown, отдельно structural removal=0, class counts
   и cohort coverage. Test label balance уже исследован только для фиксации scope;
   качество моделей и thresholds по test не выбирались.

Некоторые future outcomes не наблюдаемы из-за типа/синтаксиса или retrieval date.
Отбор known outcomes может быть информативным и смещать оценку. Выводы модели
относятся к reconstructed survivors и выбранной последней опубликованной
stable-ветке; они не описывают фактически используемые версии, реальную
совместимость, causal effect constraints или гарантированный security risk.
Неизвестные outcomes должны оставаться отдельной группой и в production.

## Артефакты и воспроизведение

`ml/scope/audit.py` и `evaluate.cjs` выполняют только population/label audit.
Ни fit, ни model training не вызываются. Основной горизонт и split закреплены
этим документом; endpoint row audit остаётся локальным ignored payload.
Агрегаты — `artifacts/ml-scope/full-v1/{population,horizons,split,yearly,dates}.csv`,
checksums и source fingerprints — `analysis-manifest.json`.

```bash
.venv/bin/python -m ml.scope.audit --limit 60000 \
  --output-dir artifacts/ml-scope/full-v1
.venv/bin/python -m unittest tests.test_ml_scope
```

Output directory должна отсутствовать; повторный запуск — в новый каталог.
Проверены все 116 798 endpoints: длительность в UTC, уникальность ключей,
цензурирование, выбор consumer не позже endpoint и временной purge.
Ни один endpoint не затронут семью warnings исходной нормализации.

Четыре unit-теста проверяют inclusive endpoint, отсутствие будущих релизов
в lag, снятие major после обновления constraint, unknown и removal.
До выполнения отдельной задачи построения/проверки feature matrix и review
этого scope обучение финальной модели не начинается.
