# Data Contract v1

Этот документ задает формат данных, которые три collector передают в `processing/`. Пока это JSON Lines: одна нормализованная запись на строку, отдельный файл для каждого типа сущности. [Файл с примерами](samples/data-contract-v1.json) собран в один JSON-объект для удобства чтения; в рабочем потоке каждая вложенная запись идет в свой JSONL-файл. Выбор базы данных или формата итогового датасета здесь не нужен.

Контракт согласован с [определением technical lag](technical-lag-definition.md): объявленное требование npm и разрешенная связь deps.dev не считаются одним и тем же наблюдением. Первое берется из манифеста, второе — из графа зависимостей. Основной анализ lag позже отберет только `dependencies`, но collector сохраняет и остальные типы.

## Общие правила

- Контракт v1 относится только к npm, поэтому поля `ecosystem` в normalized записях нет. При чтении deps.dev принимаем только систему `NPM`, при чтении OSV — только блоки `affected[].package.ecosystem = npm`; исходные значения остаются в raw. Имена npm-пакетов храним как `name` из Registry, включая `@scope/name`. URL-кодирование из API-пути убираем. Произвольно менять регистр или переписывать имя нельзя. Если имя deps.dev/OSV не удается сопоставить с npm, запись остается для сверки, а не соединяется по похожему названию.
- Логический ключ пакета — `package_name`, версии — пара `(package_name, version)`. Отдельный числовой ID не нужен. `version` — строка из источника, без автоматического SemVer `coerce`; в случае некорректного номера processing выставит причину ошибки. Для запросов к API имена и версии кодируются в URL, но это не часть ключа.
- Все timestamps в normalized данных — строки RFC 3339 в UTC с `Z`: `YYYY-MM-DDTHH:mm:ss[.fraction]Z`, например `2026-01-21T10:15:00.000Z`. Дробная часть необязательна, от 1 до 9 цифр; исходную точность не округляем и не обрезаем, чтобы не менять порядок публикаций. Входное смещение приводим к UTC. Сравниваем разобранные моменты времени, а не строки. Неизвестная дата — JSON `null`, не пустая строка. `retrieved_at` всегда известен. Событийные даты источника нельзя заменять временем загрузки.
- Колонка `Обязательно` описывает наличие свойства в normalized JSON. Каждое поле, отмеченное `да`, присутствует в записи; если его тип допускает `null`, отсутствие значения в источнике представляется именно `null`. `null` допустим только для полей, в типе которых он указан явно. Массив без элементов записывается `[]`, а не `null`. Поля, не перечисленные в v1, collector может сохранить в raw, но не добавляет в normalized без изменения контракта.
- Логические ключи `Package`, `PackageVersion` и `DependencyRequirement` не включают `snapshot_id`: повторная загрузка создает новый снимок той же сущности. Физическая уникальность их normalized-записей: `(логический ключ, provenance.snapshot_id, provenance.parser_version)`. `DependencyRelation` и `Vulnerability` представляют элементы массивов внутри конкретного raw-ответа; их snapshot-scoped ключи определены в соответствующих разделах. Новая версия парсера не перезаписывает результат старой: processing выбирает одну `parser_version` явно и не смешивает варианты нормализации одного снимка.

Общие поля каждой записи:

| Поле | Тип | Обязательно | Смысл |
| --- | --- | --- | --- |
| `schema_version` | string, `1.0.0` | да | Версия этого контракта; значение задаем при нормализации. |
| `source` | string enum: `npm`, `depsdev`, `osv` | да | Источник записи. Вычисленные внутри записи поля отдельно отмечены в таблицах ниже. |
| `provenance` | object | да | Ссылка на один raw-ответ, из которого получена запись. |
| `provenance.snapshot_id` | UUID string | да | Идентификатор одной загрузки ответа. |
| `provenance.request_url` | string | да | URL запроса без токенов и других секретов. |
| `provenance.retrieved_at` | timestamp | да | Когда ответ был получен. |
| `provenance.raw_path` | string | да | Относительный путь к сохраненному raw JSON. |
| `provenance.raw_sha256` | string, 64 hex символа | да | SHA-256 байтов raw-файла. |
| `provenance.parser_version` | non-empty string | да | Неизменяемый идентификатор сборки парсера/нормализатора, например версия артефакта вместе с Git revision. Меняется при любом изменении логики разбора или вычисляемых полей. |

В v1 нет отдельной сущности `source=calculated`: расчеты над несколькими ответами делает processing и для них понадобится собственный список входов. Поля `depth` и `fixed_versions` вычисляются из одного raw-ответа, поэтому сама запись сохраняет источник `depsdev` или `osv`. `schema_version` отвечает за форму и смысл контракта, а `provenance.parser_version` — за конкретную реализацию, которая получила normalized-запись. Повторная нормализация тех же raw-байтов той же версией парсера идемпотентна; другая версия создает отдельный физический вариант записи.

`raw/` и `normalized/` — разные каталоги в локальном или объектном хранилище; оба исключены из Git. В `raw/` кладем ответ источника без изменения смысла, вместе с HTTP-статусом и временем получения в журнале загрузок. В `normalized/` кладем только записи этого контракта после разбора и проверки типов. Рекомендуемые пути: `data/raw/{source}/{YYYY-MM-DD}/{snapshot_id}.json` и `data/normalized/v1/{entity}/{YYYY-MM-DD}/part-*.jsonl`. В Git лежат только маленькие примеры. Если API вернул ошибку, она остается в журнале/raw; не создаем пустую сущность.

## Package

Одна запись на имя пакета из полного npm packument. Логический ключ: `package_name`. Пакет может встретиться в deps.dev или OSV раньше, чем будет собран его packument; наличие строки `Package` не требуется для сохранения их сырых данных.

| Поле | Тип | Обязательно | Смысл и источник |
| --- | --- | --- | --- |
| `package_name` | string | да | `name` пакета в npm Registry. |
| `repository` | JSON value или `null` | да | Верхнеуровневое `repository` полного packument без преобразования; исторически npm допускает object и string. Если поля нет, записываем `null`. |
| `dist_tags` | object | да | Верхнеуровневый `dist-tags` без интерпретации; если объект отсутствует или имеет неверный тип, записываем `{}` и сохраняем исходное значение только в raw. |
| `created_at` | timestamp или `null` | да | `time.created`, либо `null`, если корректной даты нет. |
| `modified_at` | timestamp или `null` | да | `time.modified`, либо `null`, если корректной даты нет. |

`schema_version`, `source=npm` и `provenance` добавляются по общему правилу. Пример есть в файле samples под `Package`.

## PackageVersion

Одна запись на опубликованную версию из `versions` полного npm packument. Логический ключ: `(package_name, version)`.

| Поле | Тип | Обязательно | Смысл и источник |
| --- | --- | --- | --- |
| `package_name` | string | да | Имя пакета из npm Registry. |
| `version` | string | да | Ключ в `versions`; не приводим к числу. |
| `published_at` | timestamp или `null` | да | `time[version]` из полного npm packument. Если его нет, записываем `null`; расчет lag сам исключит такой случай. |
| `repository` | JSON value или `null` | да | `versions[version].repository` без подстановки верхнеуровневого значения; `null`, если поля нет. |
| `is_deprecated` | boolean | да | `true`, если `versions[version].deprecated` — непустая строка; иначе `false`. |
| `deprecated_message` | string или `null` | да | Исходная строка `versions[version].deprecated`, включая пустую, либо `null`, если строкового значения нет. |

`source=npm`. Отсутствие `published_at` не мешает collector сохранить версию, но запрещает выдавать ее за исторически датированную. deps.dev тоже сообщает `publishedAt`, однако в v1 дата для анализа берется из npm Registry; расхождения фиксируем отдельно, не перезаписываем молча. Прочие поля packument и version document, включая авторов, license, engines, dist и произвольные поля publisher, полностью остаются в raw; normalized v1 не копирует их выборочно без изменения контракта.

## DependencyRequirement

Одна запись на пару «имя зависимости — строка требования» в одном из четырех полей `package.json` конкретной версии пакета. Это то, что автор объявил, а не результат установки. Логический ключ: `(source_package, source_version, dependency_type, dependency_name)`; в одном снимке один ключ встречается не более одного раза.

| Поле | Тип | Обязательно | Смысл и источник |
| --- | --- | --- | --- |
| `source_package` | string | да | Имя пакета, в манифесте которого объявлена зависимость. |
| `source_version` | string | да | Версия этого пакета. |
| `dependency_name` | string | да | Ключ в поле зависимостей манифеста; для `npm:` alias это имя alias, не обязательно имя целевого пакета. |
| `requirement` | string | да | Значение из манифеста без интерпретации; пустая строка допустима и означает `*` по правилам npm. |
| `dependency_type` | string enum | да | Одно из `dependencies`, `devDependencies`, `peerDependencies`, `optionalDependencies`. |

`source=npm`; извлекаем из `versions[source_version]` полного packument. При совпадении имени в `dependencies` и `optionalDependencies` сохраняем обе исходные строки: правило приоритета optional применяется позже в анализе A1. `bundleDependencies`, `overrides` и транзитивные связи здесь не превращаем в требования.

## DependencyRelation

Одна запись на ребро графа `GetDependencies` deps.dev для выбранного корня. `source_package/source_version` здесь означают узел, который объявляет ребро (`fromNode`), а `dependency_package/resolved_version` — узел назначения (`toNode`). Поэтому в транзитивном ребре `source_package` не совпадает с корнем графа. Номер версии уже разрешен deps.dev для своего сценария установки; его нельзя подставлять вместо `v_req(t)` из A1. [Описание графа deps.dev](https://docs.deps.dev/api/v3alpha/#getdependencies) прямо разделяет `nodes`, `edges` и их индексы.

Физический ключ ребра: `(root_package, root_version, provenance.snapshot_id, provenance.parser_version, edge_index)`. Один и тот же пакет/версия может быть несколькими узлами графа, поэтому пара имен не является ключом ребра. В новом снимке или при другой `parser_version` `edge_index` нельзя использовать для связи с прежней записью.

| Поле | Тип | Обязательно | Смысл и источник |
| --- | --- | --- | --- |
| `root_package` | string | да | `nodes[0].versionKey.name`; каноническое имя корня в ответе. Имя из запроса остается в `request_url`. |
| `root_version` | string | да | `nodes[0].versionKey.version`. |
| `edge_index` | integer ≥ 0 | да | Позиция ребра в `edges[]` ответа, с нуля. |
| `from_node` | integer ≥ 0 | да | `edges[].fromNode`; индекс в `nodes[]`. |
| `to_node` | integer ≥ 0 | да | `edges[].toNode`; индекс в `nodes[]`. |
| `source_package` | string | да | `nodes[from_node].versionKey.name`. |
| `source_version` | string | да | `nodes[from_node].versionKey.version`. |
| `dependency_package` | string | да | `nodes[to_node].versionKey.name`. |
| `resolved_version` | string | да | `nodes[to_node].versionKey.version`; не объявленный range. |
| `depth` | integer ≥ 1 | да | Кратчайшее число ребер от корневого узла `nodes[0]` до `to_node`, вычисляет collector по всему графу. Для прямой зависимости 1. |
| `source` | string, `depsdev` | да | Источник разрешенного графа. |

Граф с общей ошибкой `error`, узлы с `errors` и bundled-узлы с локальными именами сохраняем в raw, но затронутые ребра не выдаем как обычные npm `DependencyRelation`; считаем пропуски. Если до `to_node` нет пути от корня, это поврежденный граф и такое ребро тоже пропускаем. При нескольких путях `depth` — минимум. В v1 relation не включает дату разрешения версии в прошлом: deps.dev дает граф на момент своего расчета, его нельзя считать историческим lockfile. Условия окружения deps.dev описаны в [API](https://docs.deps.dev/api/v3alpha/#getdependencies).

## Vulnerability

Одна запись на один исходный элемент `affected[]` OSV с `affected[].package.ecosystem = npm`. Даже если advisory повторяет тот же `package_name`, блоки не объединяем: их `ranges`, `severity`, `ecosystem_specific` и `database_specific` могут описывать разные условия применимости. `affected_index` сохраняет позицию блока в исходном массиве. Физический ключ записи: `(osv_id, provenance.snapshot_id, provenance.parser_version, affected_index)`; индекс имеет смысл только внутри этого raw-снимка и не используется для связи блоков между снимками. Оригинальный advisory полностью сохраняется в raw. Формат полей описан в [OSV schema](https://ossf.github.io/osv-schema/).

| Поле | Тип | Обязательно | Смысл и источник |
| --- | --- | --- | --- |
| `osv_id` | string | да | `id` из OSV, без замены на CVE alias. |
| `affected_index` | integer ≥ 0 | да | Индекс исходного блока в `affected[]`, с нуля. |
| `package_name` | string | да | `affected[].package.name`; целевой пакет. |
| `package_purl` | string или `null` | да | `affected[].package.purl`, либо `null`, если OSV его не указал. |
| `affected` | object | да | Данные ровно одного блока: `{versions, ranges, ecosystem_specific, database_specific}`. Отсутствующие `versions`/`ranges` становятся `[]`, отсутствующие qualifier-объекты — `null`; содержимое объектов сохраняется без интерпретации. Достаточно непустого `versions` или `ranges`. |
| `fixed_versions` | string[] | да | Значения `{fixed: value}` только из ranges типов `SEMVER` и `ECOSYSTEM` этого блока, в исходном порядке, без точных повторов. Значения из `GIT` и других типов сюда не попадают; они остаются типизированными событиями в `affected.ranges`. `[]` значит «нет подходящего fixed event», а не «исправления нет». Рассчитывается при нормализации. |
| `published_at` | timestamp или `null` | да | `published` advisory, либо `null`, если OSV его не указал. |
| `modified_at` | timestamp | да | `modified` advisory; обязательно в OSV. |
| `severity` | object[] | да | Severity этого блока: его `affected[].severity`, если оно присутствует; иначе верхнеуровневое `severity`, если ни один affected-блок advisory не задает package-level severity; иначе `[]`. Каждый объект имеет `{type, score, source}`, где `source` nullable. |
| `withdrawn_at` | timestamp или `null` | да | `withdrawn` advisory, либо `null`, если его нет. Не удаляем запись, чтобы не потерять историю. |

Внутри `affected.ranges[]` `type` — обязательная строка OSV (`SEMVER`, `ECOSYSTEM`, `GIT` и т. п.), `repo` — строка или `null`, `events` — непустой массив объектов с ровно одним из ключей `introduced`, `fixed`, `last_affected`, `limit`, а `database_specific` — исходный объект или `null`. Значение события — строка и интерпретируется только вместе с `type`: в `GIT` это commit hash, а не версия npm-пакета. Не сводим `last_affected` к `fixed`: это разные утверждения. В `severity[]` `type` и `score` — обязательные строки, `source` — строка или `null`, если OSV ее не дал. Если advisory содержит блок npm-пакета без `versions` и `ranges`, оставляем его в raw и считаем пропуском, а не заявляем, что уязвимы все версии.

Например, если `affected[2]` и `affected[5]` относятся к одному npm-пакету, collector выдает две записи с `affected_index=2` и `affected_index=5`. Processing проверяет каждую запись с ее собственными qualifier-полями и объединяет результаты только после такой проверки.

## Кто что выдает

| Collector | Raw | Нормализованный результат v1 | Чего он не решает |
| --- | --- | --- | --- |
| npm Registry | Полный packument и журнал ответа | `Package`, `PackageVersion`, `DependencyRequirement` | Не выбирает установленную/разрешенную версию и не считает lag. |
| deps.dev | Ответ `GetDependencies` и журнал ответа | `DependencyRelation` | Не заменяет декларации npm и не утверждает историческую доступность ребра. |
| OSV | Полный advisory и журнал ответа | `Vulnerability` | Не вычисляет, была ли конкретная версия установлена или уязвима на дату `t`. |

Processing читает эти записи, связывает их по пакетам и версиям, учитывает пропуски и только затем строит исторический датасет. Успешный сбор raw без normalized записи допустим, если источник не подходит контракту; число таких пропусков и причины должны быть видны в журнале нормализации. Значения `raw_sha256` и UUID в примерах условные: они показывают форму записи, а не результат реальной загрузки.

## Изменения и review

`schema_version` имеет вид `MAJOR.MINOR.PATCH`. Исправление описания без изменения данных — patch. Добавление необязательного поля — minor. Переименование поля, смена типа/ключа, изменение смысла или превращение nullable в обязательное — major. После merge любой breaking change оформляем отдельным PR с новой схемой и правилами миграции; старые записи не переписываем молча.

Если проект когда-нибудь начнет обрабатывать несколько экосистем, добавление `ecosystem` в записи и ключи будет отдельным breaking change, а не неявным расширением v1.

Перед merge оба участника должны проверить: свои поля, один sample record своего collector, ключи join, nullable-значения и происхождение raw. Замечания review исправляются в этом PR или фиксируются здесь как открытые вопросы. Пока второй участник не оставил review, документ нельзя помечать как прошедший review обоих.

Открытые вопросы для review:

1. Достаточно ли хранить severity как исходный OSV score/vector, или в следующей версии нужен рассчитанный числовой CVSS?
2. Нужно ли позже добавить отдельные normalized records для ошибок графа deps.dev и для OSV advisory без `affected` npm?
