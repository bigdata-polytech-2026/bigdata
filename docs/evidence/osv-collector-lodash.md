# Проверочный запуск OSV collector

Дата запуска: 2026-10-07. Команда:

```bash
./bigdata collect osv \
  --package-version lodash@4.17.20 \
  --package-version lodash@4.17.21 \
  --data-dir data-test
```

Результат успешного запуска:

```json
{
  "api_errors": 0,
  "checked": 2,
  "elapsed_seconds": 0.819,
  "target": 2,
  "unique_osv_records": 5,
  "versions_with_vulnerabilities": 2
}
```

Обе проверенные версии lodash имели связанные записи OSV. Полные raw-ответы и нормализованные данные намеренно не добавляются в Git: они создаются в указанном `--data-dir` и исключены из репозитория. Малый воспроизводимый входной набор находится в [osv-package-versions.txt](../samples/osv-package-versions.txt).
