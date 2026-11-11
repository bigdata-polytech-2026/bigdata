# ML2: прогноз сохранения major technical lag

ML dataset построен из full-v1 согласно ML1: одна строка — первый major-lag
incident пары consumer–dependency, target — наличие major lag на endpoint через
180 суток. Для признаков используется только anchor time; OSV, current
popularity, graph rows, identifiers и endpoint-служебные поля не являются
predictors.

## Temporal holdout

| Split | Наблюдений |
| --- | ---: |
| Train | 13 943 |
| Validation | 13 150 |
| Test | 20 549 |

Test содержит 61,90% положительных outcomes. Purge-эпохи и unknown/censored
outcomes не участвуют в обучении или оценке.

## Test metrics

| Model | PR-AUC | ROC-AUC | Precision | Recall | F1 |
| --- | ---: | ---: | ---: | ---: | ---: |
| Majority | 0,619 | 0,500 | 0,000 | 0,000 | 0,000 |
| Persistence | 0,619 | 0,500 | 0,619 | 1,000 | 0,765 |
| Logistic regression | **0,787** | **0,707** | 0,685 | 0,846 | 0,757 |
| Decision tree | 0,727 | 0,671 | 0,650 | **0,994** | **0,786** |

Основная ranking-метрика ML1 — average precision. Поэтому финальной моделью для
ranking считается logistic regression: сложное дерево улучшает F1 за счёт почти
полного recall, но не превосходит логистическую регрессию по PR-AUC.

## Ограничения и интерпретация

Модель применима к уже возникшим major-lag incidents, а не к актуальным
dependencies. Она оценивает реконструируемую допустимую версию, не lock-file и
не фактическое действие разработчика. Коэффициенты и importance описывают
предиктивный вклад и не являются причинной интерпретацией. Test относится к
reconstructed survivors full-v1 и к зафиксированному temporal split.

## Воспроизведение

```bash
.venv/bin/python -m ml build --dataset-dir artifacts/ml/full-v1/dataset \
  --scope-dir /tmp/ml-scope-full-v1
.venv/bin/python -m ml train --dataset-dir artifacts/ml/full-v1/dataset \
  --output-dir artifacts/ml/full-v1/evaluation
```

Dataset, модели, calibration, PR curve, confusion matrix, cohort table,
feature importance и error analysis хранятся в DVC artifact
`artifacts/ml/full-v1.dvc`.
