# Temporal analysis

Модуль анализирует опубликованный `temporal_pilot` и не выполняет запросов к npm.
Единица анализа для гипотез — package-level среднее terminal lag, поэтому пакеты с
большим числом dependency relations не доминируют в p-значениях.

```bash
.venv/bin/python -m analysis.temporal \
  --dataset-id pilot-v1 \
  --temporal-dir data/datasets/pilot-v1-temporal/temporal \
  --normalized-dir data/datasets/pilot-v1-normalized/normalized \
  --output-dir artifacts/analysis/temporal-pilot/pilot-v1
```

Сценарии определяются по последовательности `version_lag`: `always_current`,
`accumulation`, `catch_up`, `persistent_lag` и `other`. Все признаки вычисляются
на terminal observation и используют только версии с публикацией не позднее этого
момента. `conclusions.md` содержит три заранее сформулированные гипотезы, тесты,
p-значения, размеры эффекта и ограничения интерпретации.
