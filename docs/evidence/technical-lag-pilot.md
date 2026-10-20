# Technical lag pilot: проверки и статистика

Расчёт выполнен 20.10.2026 из опубликованного normalized output DVC
`cb9262db42bbc1160e4a4385de1b42f3.dir`. Input manifest SHA-256:
`b0fe2e1a2775fded953ef0a11fe899341f860710b78617977cf338a35184cca0`.

Воспроизводимый stage:

```bash
npm ci --prefix processing/technical_lag --ignore-scripts
.venv/bin/python -m pip install -r processing/technical_lag/requirements.txt
.venv/bin/dvc repro technical_lag_pilot
```

Stage сохранил отдельный output
`data/datasets/pilot-v1-technical-lag/technical_lag` и не изменил immutable
`pilot-v1` или опубликованный normalized snapshot.
DVC output hash: `e7bcccaf6736f4d44c8f1b19db26c3a9.dir`, 27 815 311 bytes,
4 файла. `.venv/bin/dvc push dvc.yaml:technical_lag_pilot -j4` отправил 5 cache
objects; последующий cloud status подтвердил синхронизацию `yandex-s3`.

## Результат полного pilot

| Показатель | Строки |
| --- | ---: |
| Все dependency requirements | 8 957 576 |
| Успешно рассчитаны | 236 719 |
| Исключены с причиной | 8 720 857 |
| Переданы в SemVer-ядро | 1 424 826 |

Доля успешных расчётов — 2,6427%. Низкая доля ожидаема для pilot: 5 186 332
требования принадлежат невалидным/prerelease consumer versions, 2 346 418 относятся
к типам вне основного анализа, а для 1 107 955 нет dependency packument в
ограниченной, не dependency-closed npm-выборке.

Остальные причины: `no_satisfying_version_at_t` — 58 447,
`unsupported_requirement` — 13 804, `prerelease_requirement` — 7 770,
`invalid_requirement` — 88, `no_available_stable_version_at_t` — 37,
`snapshot_before_t` — 6. Ни одна успешная строка не получена из неполной истории
дат: среди использованных stable versions пропусков publication time нет.

Классы доступного обновления среди 236 719 успешных строк:

| Класс | Строки |
| --- | ---: |
| none | 222 336 |
| patch | 1 336 |
| minor | 3 193 |
| major | 9 854 |

`nonmonotone_publish_order=true` у 3 443 успешных строк. Подготовлены 926 историй
dependency packages, содержащие 316 514 версий: 91 828 stable, 224 681 prerelease
и 5 невалидных SemVer.

## Проверки

Ядро прошло 61 Node unit-тест без ошибок и пропусков. Полный Python suite прошёл
36 тестов, 2 live-интеграционных теста штатно пропущены без opt-in. Покрыты exact, caret (включая
0.x), tilde, comparators, hyphen ranges, OR, wildcard, момент публикации, UTC и
наносекунды, все причины исключения, отрицательный time lag и неоднозначный SemVer
precedence. Parquet adapter отдельно проверен на fixture, повторный запуск и
повреждённый output.

На полном output выполнены 13 integrity checks, все с нулём нарушений:
уникальность natural keys, cardinality, отсутствие orphan/missing calculator rows,
согласованность status/reason, полнота успешных метрик, NULL для исключённых строк
и допустимость metric values.

Фактический machine-readable отчёт, build identity и SHA-256 каждого файла находятся
в `technical-lag-manifest.json`; человекочитаемая копия — в `report.md` внутри DVC output.
