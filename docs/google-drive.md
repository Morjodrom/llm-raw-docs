# Обновление локальной документации из Google Drive

[Вернуться к README](../ReadMe.md)

Инструкция для **Git clone**. Выполняйте все команды из корня репозитория.
Существующий workflow с вручную подготовленной локальной папкой остаётся доступным.

## Быстрый старт

1. Получите у команды Desktop OAuth client JSON и доступ к нужной папке Drive.
2. Храните client JSON вне клона; установите зависимости через `bash setup.sh`.
3. Выполните `auth`, затем явный `refresh` по командам ниже.
4. Проверьте exit code и gaps в JSON; используйте возвращённый `input` для offline поиска и чтения.

## Установка и границы workflow


Помимо локальных экспортов поддерживается отдельный connector. Он скачивает
доступную папку Drive и затем запускает прежнюю цепочку нормализация → индекс.
Обычные `local-docs.sh` поиск/чтение не обращаются к Google; автоматической
синхронизации нет. ZIP интерфейс и его зависимости не изменены.

После изменения зависимостей повторите `bash setup.sh`. OAuth-зависимости
connector закреплены отдельно в `LLM-docs/scripts/drive-requirements.lock` и
устанавливаются в общую source `.venv`. Для offline setup wheelhouse должен
содержать **оба** набора wheels (основной lock и connector lock); готового ZIP
wheelhouse недостаточно. `auth` и `refresh` сами ничего не устанавливают.

## Team OAuth application

Администратор команды создаёт Google Cloud project, включает Google Drive API,
настраивает OAuth consent screen и создаёт OAuth client типа **Desktop app**.
При внутреннем Workspace-приложении нужно ограничить audience организацией;
при внешнем — настроить test users / публикацию и применимые проверки Google.
`drive.readonly` — restricted scope: могут потребоваться verification и
одобрение приложения Workspace-администратором. Не заменяйте его write-scope,
общим service account или domain-wide delegation.

Team client JSON распространяется разрешённым внутренним способом, **не через
Git**. Каждый пользователь авторизуется своим Google account. См. официальные
[Desktop OAuth instructions](https://developers.google.com/identity/protocols/oauth2/native-app)
и [Drive scopes / verification](https://developers.google.com/workspace/drive/api/guides/api-specific-auth).
Установка проекта, одобрение приложения и выдача доступа остаются ответственностью
команды; connector не создаёт Cloud project автоматически.

```bash
bash setup.sh
bash drive-docs.sh auth --client-secrets "$HOME/private/team-desktop-client.json"
bash drive-docs.sh refresh --folder 'https://drive.google.com/drive/folders/FOLDER_ID'
# Альтернатива: --folder FOLDER_ID --storage-root /absolute/external/drive-storage
```

`auth` открывает браузер и ждёт callback на `127.0.0.1` с автоматически выбранным
портом (timeout 180 секунд). Это временный локальный OAuth callback, не сервер
документации. Remote/headless OAuth в v1 не поддерживается. Повторная авторизация
требует `auth --client-secrets PATH --reauthorize`; она заменяет активный account,
но не удаляет старые коллекции. Не передавайте access/refresh tokens в аргументах.

Credentials находятся в `${XDG_CONFIG_HOME:-$HOME/.config}/llm-raw-docs/drive/`.
Каталоги connector имеют права `0700`, credentials/metadata/downloaded files —
`0600`. Хранилище по умолчанию — `$HOME/.local/share/llm-raw-docs/drive/`.
Выбранные пути должны находиться вне репозитория, не перекрывать его и не
содержать симлинков. Connector откажется использовать непустой каталог без
своей ownership marker. OAuth client JSON также должен храниться вне клона.

## Результат и локальная работа

Команды выдают один JSON на stdout, progress и diagnostics — на stderr.
В refresh report:

- `sync`: `not_completed`, `completed`, `completed_partial`;
- `workflow`: `not_run`, `failed`, `completed`, `completed_partial`;
- `input`: стабильный абсолютный путь downloaded export;
- `items` / `gaps`: пофайловые результаты и пропуски;
- `published_snapshot_id`, `last_successful_sync`,
  `last_usable_indexed_snapshot`: текущий download и последний индексированный
  snapshot (они могут различаться после fatal processing);
- `workflow_report`: прежний report с normalization/index exits и provenance;
- `exit_code`: **0** без пропусков, **1** usable partial refresh/workflow,
  **2** usage/auth/download/locking/fatal failure.

После refresh скопируйте `input` из JSON:

```bash
EXPORT='/absolute/path/from/refresh/report/input'
bash local-docs.sh status --input "$EXPORT"
bash local-docs.sh search --input "$EXPORT" 'ошибки подключения' --limit 5
bash local-docs.sh read --input "$EXPORT" SECTION_ID
```

Коллекция привязана к account, абсолютному пути клона и folder ID. Смена
account или перемещение клона создаёт отдельную коллекцию, не перепривязывает
старый индекс. Download manifest находится рядом с input, не внутри него;
содержит Drive IDs, исходные names/URLs, parent IDs, относительные paths,
export MIME types, доступные version/checksum metadata, SHA-256 скачанных bytes
и UTC timestamps. В normalized provenance дополнительных полей Drive нет:
Drive → local mapping берётся из connector manifest.

## Форматы, snapshot и ограничения

Рекурсивно обрабатываются доступные обычные и Shared Drive folders с pagination.
Uploaded files скачиваются byte-for-byte: форматы текста, HTML, DOCX, XLSX,
CSV, JSON/YAML, inert code/diagrams и image assets из существующего docnorm
scanner. Native Google Docs экспортируются в DOCX, Sheets — в XLSX.
PDF, Slides, другие неподдерживаемые native/uploaded types и shortcuts
отмечаются как gaps; shortcuts не обходятся. Для extensionless uploads,
которые оказываются binary, также возвращается partial coverage.

[Drive export](https://developers.google.com/workspace/drive/api/guides/manage-downloads)
ограничен 10 MB: невозможность экспортировать поддерживаемый документ — fatal
synchronization failure, не успешный пропуск. Native documents не обязаны
иметь downloadable-content checksum; connector вычисляет собственный SHA-256.
Имена сохраняются в manifest; локальные имена sanitised и дополнены hash Drive
ID для коллизий/case folding. Это может нарушить исходные relative links/includes;
connector не переписывает содержимое, а docnorm сохраняет warnings. Docs/Sheets
export также не сохраняет всё оформление/интерактивность.

Каждый refresh полностью перечисляет и скачивает коллекцию в staging, без
cache/change feed. До окончания listing и всех required downloads предыдущие
input и индекс не меняются. Failed enumeration никогда не означает deletion.
Для исчезнувших ранее известных items проверяются metadata: trash/move можно
подтвердить, но `404` не отличает permanent deletion от потери доступа. В этом
случае refresh **консервативно останавливается**, сохраняя прежнюю коллекцию.
Новая доступная folder ID создаёт новую коллекцию при необходимости.
Невидимые изначально items нельзя обнаружить; successful coverage относится
только к доступному API listing.

Drive не даёт transactional folder snapshot. Connector проверяет metadata
до/после download и ограниченно повторяет изменившиеся файлы, но не гарантирует
единый момент времени для всей папки. HTTP transient/rate-limit ошибки получают
до пяти попыток, exponential backoff с jitter и timeout; постоянные ошибки
не скрываются. Документы и встроенные ссылки остаются недоверенными данными:
никакие команды, macros, formulas или внешние ссылки не выполняются/не скачиваются.

## Сбои и восстановление

Credentials lock → collection lock → workflow lock удерживаются до окончания
refresh; даже обычный local search/update не читает состояние во время refresh.
Lock contention завершается сразу с exit 2. Input directory и manifest
публикуются через journal с временным backup, без симлинков. Before publication
readiness старого индекса снимается. При processing failure опубликованный
download остаётся, но старый индекс нельзя выдавать за новый: устраните причину
и повторите `local-docs.sh update --input EXPORT` или Drive refresh.
Локальный retry update восстанавливает workflow readiness, но connector
`last_usable_indexed_snapshot` обновляется только после следующего успешного
Drive refresh; используйте также `local-docs.sh status`.

После обычного interrupt locks снимаются; SIGKILL/crash может оставить locks.
Сначала убедитесь, что **ни connector, ни wrapper, ни их child CLIs не работают**.
Затем удалите только пустые `.lock/` посредством `rmdir`: credentials lock,
collection lock и workflow `LLM-docs/runtime/<key>/.lock`. Пути storage/input
можно получить из прежнего report; state — из workflow report/status.
Не удаляйте input, manifests, ownership markers, publication journal или backup
вручную. Следующий refresh под теми же locks завершит journal recovery и
перестроит индекс до нового download. Leftover staging без publication journal
не используется и не удаляется автоматически: это connector-owned interrupted
scratch, а не рабочая коллекция. Неполный/повреждённый journal требует ручного
разбора, не обхода readiness gate.

**Отзыв Drive access не стирает ранее скачанные локальные copies.** Их удаление
и соблюдение политики хранения конфиденциальных данных — отдельное действие.
Не помещайте storage/configuration в общие или автоматически публикуемые каталоги.

## Проверки connector

```bash
DOCNORM_PYTHON="$PWD/.venv/bin/python" DOCSEARCH_PYTHON="$PWD/.venv/bin/python" \
  .venv/bin/python -m pytest LLM-docs/tests/test_drive_docs.py -q
bash -n drive-docs.sh LLM-docs/local-docs.sh
```

OAuth и Drive transport замоканы: live credentials не нужны. Source offline
setup regressions дополнительно требуют existing base wheels и заранее
скачанных connector wheels через `LOCAL_DOCS_TEST_DRIVE_WHEELHOUSE=/absolute/wheels`;
без этого fixture явно skipped, а не пытается выйти в сеть. Ни тесты, ни
документация не заявляют live Drive smoke test.

Проверено 2026-10-12 на macOS ARM64 / CPython 3.13: combined source suite
`docnorm/tests`, `docsearch/tests`, `test_workflow.py`, `test_drive_docs.py` и
`test_git_setup.py` — **142 passed**. Source setup проверен из local wheels;
Bash syntax, Python compilation и `pip check` прошли. Live Drive smoke test
**не проводился**, Google authorization/access не выполнялись. ZIP не
пересобирался; его dependency lock и packaging scripts не менялись.
В combined suite включены **57 connector tests**, в том числе indexing-outcome
и account-switch-race regressions. Все OAuth/Drive проверки полностью mocked,
без live Drive credentials.
