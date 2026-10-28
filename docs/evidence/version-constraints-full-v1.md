# Version constraints и technical lag на full-v1

Анализ выполнен на тех же 76 871 904 dependency requirements
dataset `full-v1`. Каждое требование отнесено ровно к одной категории:
`exact`, `caret`, `tilde`, `wildcard`, `range`, `prerelease`, `unsupported` или
`invalid`. Классификация выполнена strict `semver@7.8.4`.

SHA-256 normalization manifest:
`b4560e99e8a3a4e83e3799fea3989d5de175d08d64766cdc4dd626c1909254b9`;
SHA-256 technical lag manifest:
`27a024bb8c588a30a73630cdef777cbd55dba307803e7c738497bb98b9f4a096`.

Воспроизводимый запуск из корня репозитория:

```bash
analysis_output_root="$(mktemp -d)"
.venv/bin/python -m analysis.version_constraints \
  --dataset-id full-v1 \
  --normalized-dir data/datasets/full-v1-normalized/normalized \
  --lag-dir data/datasets/full-v1-technical-lag/technical_lag \
  --output-dir "$analysis_output_root/version-constraints"
```

Параметры статистики по умолчанию: 2 000 bootstrap-повторов, seed `20261027`,
reference category — `caret`.

## Распределение constraints

| Категория | Requirements | Доля | Technical lag рассчитан |
| --- | ---: | ---: | ---: |
| caret | 40 057 783 | 52,11% | 7 784 413 (19,43%) |
| exact | 18 809 723 | 24,47% | 3 384 633 (17,99%) |
| prerelease | 11 355 980 | 14,77% | 0 |
| tilde | 2 467 745 | 3,21% | 387 610 (15,71%) |
| range | 1 918 322 | 2,50% | 83 663 (4,36%) |
| wildcard | 1 634 277 | 2,13% | 166 573 (10,19%) |
| unsupported | 622 276 | 0,81% | 0 |
| invalid | 5 798 | 0,008% | 0 |

Technical lag рассчитан для 11 806 892 строк. Исключённые строки не трактуются
как отсутствие lag. Основное статистическое сравнение ограничено пятью категориями
с поддерживаемой stable SemVer-семантикой: exact, caret, tilde, wildcard и range.

Constraint category и причина исключения — разные признаки. Например, 11 355 980
требований классифицированы как prerelease syntax, но `prerelease_requirement`
является основной причиной исключения только у 92 767 строк: у остальных раньше
сработали причины, связанные с consumer version/time или dependency type.
Аналогично, `unsupported_dependency_type` описывает вид зависимости, тогда как
constraint category `unsupported` — синтаксис requirement.

## Частота и величина lag

| Constraint | Пригодных строк | С version lag > 0 | Медиана version lag при version lag > 0 | Медиана time lag при version lag > 0 |
| --- | ---: | ---: | ---: | ---: |
| exact | 3 384 633 | 24,69% | 5 | 281,72 дня |
| tilde | 387 610 | 22,82% | 4 | 197,04 дня |
| caret | 7 784 413 | 12,52% | 4 | 268,41 дня |
| wildcard | 166 573 | 8,70% | 5 | 210,36 дня |
| range | 83 663 | 7,59% | 9 | 117,18 дня |

По сравнению с caret абсолютная вероятность lag выше на 12,16 процентного пункта
для exact (95% package-cluster bootstrap CI: от 8,06 до 17,12 п. п.) и на
10,29 п. п. для tilde (от 6,47 до 14,89 п. п.). Для wildcard она ниже на
3,82 п. п. (от -5,65 до -1,99), для range — на 4,93 п. п.
(от -6,88 до -2,94).

Omnibus-сравнение частоты lag дало chi-square = 280 236,70 и Cramer's V = 0,154.
Вычисленное p-value оказалось ниже диапазона представления floating point и
сохранено как `0.0`; это означает крайне малое значение, а не буквально нулевую
вероятность. При таком объёме данных практический вывод основан прежде всего на
Cramer's V и разностях рисков, а не только на p-value.

Величина положительного version lag также различается между группами
(Kruskal–Wallis H = 8 169,52; p-value ниже численной точности). Однако большинство
парных эффектов относительно caret малы: Cliff's delta равен 0,071 для exact,
0,012 для tilde и 0,055 для wildcard. Range встречается с lag реже, но условный
lag у него больше: медиана 9 против 4 версий и Cliff's delta = 0,265.

## Наблюдаемый возрастной градиент lag

Модель возрастного градиента построена на 2 128 471 package-version/category
ячейке для 36 376 пакетов, с package-clustered standard errors и поправками на
release cadence и число dependencies. Outcome каждой ячейки — среднее
`ln(1 + version lag)` по requirements; наблюдения взвешены по их количеству.
Коэффициент interaction показывает изменение этого преобразованного outcome на
один дополнительный год возраста относительно caret. Только exact показал
статистически различимый дополнительный градиент: `age × exact` = 0,0239,
95% CI [0,0041; 0,0436], p = 0,018.

Для tilde, wildcard и range доверительные интервалы взаимодействия с возрастом
включают ноль. Следовательно, exact демонстрирует немного более крутой наблюдаемый
возрастной градиент lag, чем caret; различий градиента для остальных типов не
обнаружено. Это сравнение исторических package-version observations, а не
продольное наблюдение одних и тех же dependency relations, поэтому его нельзя
интерпретировать как непосредственно измеренную скорость накопления.

## Артефакты и проверки

Результат содержит четыре графика, четыре итоговые таблицы, четыре набора
статистических результатов, краткий `findings.md` и manifest с SHA-256 каждого
файла. Все 545 477 уникальных текстов requirement получили категорию, неизвестных
категорий нет. Входные manifests проверяются до анализа, результат записывается
атомарно.

DVC output: `8460b2cb88de299848c989d4495f0ec1.dir`, 553 687 874 bytes,
15 файлов. Синхронизация cache с remote `yandex-s3` повторно проверена 29.10.2026.

## Вывод

Тип version constraint связан прежде всего с вероятностью появления technical
lag. Exact и tilde сопровождаются lag заметно чаще caret, а wildcard и range —
реже. При уже возникшем lag различия его величины в основном невелики; исключение —
редкие lagged range, у которых отставание по числу версий больше. Для exact также
наблюдается немного более крутой возрастной градиент lag относительно caret.

Результаты наблюдательные и не доказывают, что синтаксис constraint вызывает lag.
Категории различаются по package age, cohort, назначению и полноте исторической
реконструкции. Особенно различается доля строк, пригодных для расчёта: от 4,36%
для range до 19,43% для caret. Dataset описывает декларации npm-манифестов и
surviving releases текущего Registry snapshot, но не lock-файлы, фактически
установленные версии или resolved graph deps.dev.
