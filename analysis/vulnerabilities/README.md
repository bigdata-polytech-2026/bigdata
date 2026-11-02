# Vulnerability and technical-lag analysis

`v_req` из temporal dataset сопоставляется с OSV-enriched npm version. Это
реконструируемая допустимая версия dependency, а не подтверждённая версия из
lock-файла. Исправление считается доступным, если OSV явно указывает fixed
version, она есть в сохранённой npm history и опубликована к моменту снимка.

```bash
.venv/bin/python -m analysis.vulnerabilities --dataset-id pilot-v1 \
  --temporal-dir data/datasets/pilot-v1-temporal/temporal \
  --security-dir data/datasets/pilot-v1-security/security \
  --normalized-dir data/datasets/pilot-v1-normalized/normalized \
  --pilot-dir data/datasets/pilot-v1 \
  --output-dir artifacts/analysis/vulnerabilities-pilot/pilot-v1
```
