# Агент-разработчик и наблюдение за ОС

Сервис не имеет shell и не развёртывает код. Он читает разрешённые исходники, возвращает их SHA-256, сохраняет новое содержимое и diff как предложение в отдельном каталоге. Доступ к исходникам смонтирован только для чтения. ROOT, Docker socket и `.env` не передаются.

1. Создайте `.env.extras` скриптом `scripts/configure-extras.py`, если файла ещё нет. Укажите настоящий MCP-домен с DNS на VPS.
2. Запустите из корня проекта:

```bash
docker compose --env-file .env --env-file .env.extras -f compose.yaml -f deployment/maintenance/compose.maintenance.yaml config --quiet
docker compose --env-file .env --env-file .env.extras -f compose.yaml -f deployment/maintenance/compose.maintenance.yaml up -d --build
```

3. В панели MCP добавьте `https://ВАШ-MCP-ДОМЕН/mcp`. В поле токена вставьте `MAINTENANCE_TOKEN` из локального `.env.extras`. Это отдельный токен, не API-key модели.
4. Загрузите каталог. Создайте агента «Разработчик ОС». Назначьте `source_list`, `source_read`, `propose_change`, `service_status`.
5. Пример инструкции агента: «Изучи относящиеся к задаче исходники. Объясни причину проблемы. Подготовь минимальное изменение, сохрани через propose_change с expected_sha256 прочитанного файла. Не утверждай, что тесты пройдены или код развёрнут. Рабочие файлы менять напрямую нельзя».
6. Привяжите агента к Telegram-боту в режиме чата. Через `/approvals` найдите запрос, откройте `/tool ID`, проверьте аргументы и подтвердите `/approve ID`. Веб-панель показывает ту же очередь.

Предложения лежат в volume `proposals`. Можно извлечь нужный файл через Docker Compose `cp` из `/proposals/ID.json`, проверить diff и применить в отдельной рабочей ветке вручную. Здесь нет автоматического применения: сначала тесты и отдельное решение владельца.

Сервис ограничивает размер исходника/предложения 100 КБ и число JSON-предложений 200; значения заданы в `services/maintenance/app.py`. После разбора предложений оператор освобождает очередь. Наблюдение относится к контейнеру сервиса; `/status` Telegram и экран «CRM и система» показывают состояние основного приложения. Это не полный мониторинг VPS.

## Совместно с Twenty

Не накладывайте оба Caddyfile вслепую: последняя volume-запись заменит предыдущую. Для всех модулей используйте дополнительный готовый файл `deployment/compose.full.yaml` и следующие флаги после `--env-file`:

```bash
-f compose.yaml -f deployment/twenty/compose.twenty.yaml -f deployment/maintenance/compose.maintenance.yaml -f deployment/compose.full.yaml
```

Файл `deployment/Caddyfile.full` содержит три домена. Первоначальную Basic-защиту CRM снимают после закрытия регистрации, как описано в инструкции Twenty.
