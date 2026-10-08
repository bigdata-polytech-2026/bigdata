# Проверка deps.dev collector на 1000+ npm package versions

Проверка проведена 2026-10-08 с deps.dev API v3. Полные данные лежат в исключенном из Git каталоге `data/acceptance/depsdev-1000-final/`; в Git добавлены точный [список входных версий](../samples/depsdev-acceptance-package-versions.txt), небольшой raw sample и эта сводка.

## Выборка

Исходный npm snapshot: `20261006T231526548299Z-a7209fff`, 1000 пакетов. Для каждого пакета выбрана версия из `dist-tags.latest`, существующая среди его нормализованных `PackageVersion`. Получилось ровно 1000 уникальных package versions без пропусков.

Для подтверждения минимум 1000 успешных ответов к ним добавлены 100 точных более старых версий, опубликованных до 2025-01-01, из того же npm snapshot. Итоговый файл содержит 1100 строк, SHA-256: `8ba2903040bc038f1c2da85ba9361529eaf2139e0af9c4165bbcd69115a80992`.

## Основной batch: 1000 свежих latest versions

Команда:

```bash
./bigdata collect depsdev \
  --input-file docs/samples/depsdev-acceptance-package-versions.txt \
  --limit 1000 \
  --workers 8 \
  --timeout 30 \
  --retries 3 \
  --data-dir data/acceptance/depsdev-1000-final
```

Run ID: `20261007T223829830956Z-0fc5c02d`. Parser version: `depsdev-collector@1.0.0+source.3f482ca0b8c430859727c9cf91708099ac2548c54562c511515115fc26f5e486`.

| Метрика | Значение |
| --- | ---: |
| Обработано версий | 1000 |
| Успешных API responses | 958 |
| API failures | 42 |
| Success rate | 95.8% |
| Время | 31.460 сек |
| Средняя скорость | 31.786 versions/sec |
| Raw dependency edges | 41 298 |
| Direct edges | 2 779 |
| Normalized relations | 41 097 |
| Пропущено edges по правилам качества | 201 |
| Raw HTTP bytes | 5 225 074 |
| Normalized JSONL bytes | 32 085 734 |
| Bundled nodes | 123 |
| Nodes with errors | 2 |
| Graph-level errors | 0 |

Все 42 ошибки были HTTP 404. Все отсутствующие версии опубликованы в 2026 году, 40 из 42 — менее чем за месяц до npm snapshot. 429, 5xx и сетевых failures не наблюдалось. Это похоже на задержку ingestion для свежих релизов, но два апрельских 2026 релиза показывают, что возраст версии сам по себе не гарантирует наличие в deps.dev. Дополнительное подтверждение: `scorm-again@3.4.5` вернул 404 в предыдущем прогоне, но появился в API к финальному прогону; 404 нельзя кешировать как постоянный результат.

## Top-up и критерий 1000 успешных версий

Повторный запуск по 1100 строкам использовал 958 валидных markers, повторно показал 42 явных 404 и загрузил еще 100 графов. Итог:

- доступно 1058 успешно обработанных package versions;
- success rate по 1100 входам — 96.1818%;
- 50 523 raw edges, из них 3 297 direct;
- 50 322 normalized relations и 201 осознанно пропущенное edge;
- 6 409 319 raw bytes и 39 509 384 normalized bytes;
- полный каталог вместе с manifests, logs и state занимает 58 668 KiB.

Top-up run ID: `20261007T223914395818Z-e841f6da`. Его 5.055 сек нельзя использовать как чистую скорость API: 958 результатов были восстановлены через local markers. Скорость API берется из первого полного сетевого batch.

## Аудит файлов

Потоковая проверка 1058 успешных результатов подтвердила:

- 1058 raw JSON и 1058 normalized JSONL;
- SHA-256 каждого raw-файла совпадает с marker;
- requested package/version каждого графа совпадает с canonical root и сохраненной identity;
- сумма `edges` в markers равна 50 523;
- число JSONL relations равно 50 322;
- самый большой граф содержит 2823 edges, медиана — 1, p95 — 319.

Восемь версий содержали bundled nodes, node errors или связанные с ними пропуски. Самые заметные случаи: три пакета Ethereum Waffle по 45 пропущенных bundled edges, `storyblok@4.23.4` с 22 edges около error node и `@strapi/strapi@5.56.0` с графом на 2823 edges.

## Вывод

deps.dev пригоден как основной источник resolved dependency graph: API быстро отдает nodes, edges, requirements и resolved versions, а на тесте не было throttling или временных ошибок. При масштабировании нельзя считать 404 окончательным отсутствием версии: свежие npm releases нужно ставить в отложенную очередь и пробовать снова. Графы сильно различаются по размеру, поэтому полный dataset нужно сохранять partition-файлами, а concurrency и объем ответа — измерять на каждом batch.
