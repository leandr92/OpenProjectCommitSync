OpenProject Commit Sync
=======================

Сервис дополняет интеграцию OpenProject с GitHub/GitLab: добавляет комментарии к задачам при коммитах и создании веток, чтобы команда видела подробности без перехода в систему контроля версий.

Требования
----------
- Python 3.13 (соответствует образу Docker)
- Переменные окружения:
- `OPENPROJECT_URL` — URL OpenProject без `/api/v3`
- `OPENPROJECT_API_KEY` — персональный API key (используется Basic Auth `apikey:TOKEN`)
- `GITHUB_WEBHOOK_SECRET` — секрет подписи GitHub (X-Hub-Signature-256)
- `GITLAB_WEBHOOK_SECRET` — секрет токена GitLab (X-Gitlab-Token)
- `STATUS_MAPPING_FILE` (опционально) — путь к файлу соответствий статусов (по умолчанию `status_mapping.json`)

Развертывание
-------------

### Локально (venv/pip)
- `python -m venv .venv && source .venv/bin/activate`
- `pip install fastapi requests uvicorn[standard]`
- Запуск: `uvicorn app.main:app --host 0.0.0.0 --port 8088 --env-file .env`

### Локально (Pipenv)
- `pip install pipenv`
- `pipenv install`
- `pipenv install "uvicorn[standard]"`
- Запуск: `pipenv run uvicorn app.main:app --host 0.0.0.0 --port 8088 --env-file .env`

### Подготовка окружения разработчика
1. Скопируйте `.env.example` в `.env` и задайте реальные значения переменных.
2. Активируйте виртуальное окружение (venv или Pipenv).
3. Установите зависимости (см. варианты выше).
4. Запустите сервис (`uvicorn ... --env-file .env`).
5. Проверьте эндпоинты тестовым запросом или через раздел *Test* в GitHub/GitLab — должен вернуться `{"status": "ok"}`.

### Docker
- Сборка: `docker build -t openproject-commit-sync:latest .`
- Запуск: `docker run --rm -p 8088:8088 --env-file .env openproject-commit-sync:latest`

### Docker Compose с OpenProject
- `docker-compose.override.yml` — добавляет сервис `commit-sync` в общий стек, публикует порт `8088` и содержит подсказки по Caddy.
- `docker-compose.traefik.override.yml` — конфигурация для Traefik (подключение к сетям `backend`, `traefik-network`, нужные labels).
- Шаги развёртывания:
  1. Соберите образ из этого репозитория (`docker build -t openproject-commit-sync:latest .`) или используйте `docker compose build commit-sync` в каталоге OpenProject.
  2. Скопируйте выбранный override-файл рядом с основным `docker-compose.yml` OpenProject.
  3. Убедитесь, что путь в директиве `build: .` указывает на корень текущего репо (при необходимости исправьте).
  4. Для прокси на Caddy (`openproject/proxy`): создайте внешнюю сеть один раз `docker network create frontend`, подключите сервис (в override уже описано) и добавьте в Caddyfile маршруты:
     ```
     handle /github-webhook* {
       reverse_proxy commit-sync:8088
     }
     handle /gitlab-webhook* {
       reverse_proxy commit-sync:8088
     }
     reverse_proxy * http://${APP_HOST}:8080 {
       header_up X-Forwarded-Proto {header.X-Forwarded-Proto}
       header_up X-Forwarded-For {header.X-Forwarded-For}
     }
     ```
     Если Caddy берёт конфиг из шаблона (`Caddyfile.template`), пересоберите образ: `docker compose build proxy` и запустите `docker compose up -d proxy`.
  5. Для Traefik используйте `docker-compose.traefik.override.yml` — labels уже настроены на маршруты `/github-webhook` и `/gitlab-webhook`.
  6. Запустите стек: `docker compose up -d` (при необходимости добавьте `--build`).

Настройка вебхуков
-------------------

### GitHub
- Settings → Webhooks → Add webhook
- Payload URL: `https://<домен>/github-webhook` (или `http://<хост>:8088/github-webhook` при прямом доступе)
- Content type: `application/json`
- Secret: `GITHUB_WEBHOOK_SECRET`
- Events: как минимум "Just the push event" (событие `Create` также используется для веток)

### GitLab
- Settings → Webhooks (или Integrations)
- URL: `https://<домен>/gitlab-webhook`
- Secret Token: `GITLAB_WEBHOOK_SECRET`
- Trigger: включите `Push events`
- Кнопка *Test → Push events* должна вернуть `{"status":"ok"}`

Использование
-------------
- **Коммиты**: указывайте ID задачи через `#<число>` в сообщении коммита — сервис добавит комментарий с текстом коммита, ссылкой и кратким списком изменённых файлов (`+` добавлены, `~` изменены, `-` удалены; при более чем 5 файлах появится счётчик).
- **Ветки**: при создании ветки комментарий тоже появится, если ID задачи зашифрован в названии (`feature/#123-login`, `bugfix/123-fix`, `summary-task/3206-organize-open-source-conference`). Поддерживаются шаблоны `#ID` и цифры после `-`, `_` или `/`.
- **Смена статусов**: 
  - создание ветки переводит задачу в статус `in_progress`;
  - push/merge в ветку `dev` устанавливает статус `testing`;
  - push/merge в `main` или `master` устанавливает статус `completed`.
  Реальные статусы задаются в `status_mapping.json`.
- **События без ID**: если коммит/ветка не содержит ID задачи, сервис игнорирует запись.

Настройка статусов
-------------------
- Файл `status_mapping.json` содержит соответствия логических ключей (`in_progress`, `testing`, `completed`) статусам OpenProject.
- Значения указывайте в виде числовых ID статусов (`2`, `3`, ...). Сервис сам преобразует их в нужный API путь.
- Переменная `STATUS_MAPPING_FILE` позволяет указать кастомный путь к файлу.
- Если статус для действия не найден в файле, обновление состояния задачи пропускается.
- Источники статусов:
  - UI: Администрирование → *Work package statuses*. Откройте статус — ID указан в URL `/statuses/<id>/edit`.
  - API: `GET ${OPENPROJECT_URL}/api/v3/statuses` (используйте тот же API key). В ответе `_embedded.elements` содержат `id` и `_links.self.href`.

Логирование
-----------
- По умолчанию сервис выводит ключевые события (ветки, коммиты, смена статусов) в stdout.
- Переменная `LOG_LEVEL` задаёт уровень (`DEBUG`, `INFO`, `WARNING`, `ERROR`). Для детальной отладки установите `LOG_LEVEL=DEBUG`.
- Логи содержат информацию о типе события, ветке, ID задач и результате обновления статусов, что упрощает диагностику интеграции.

Примечания
----------
- Проверка подписи GitHub (`X-Hub-Signature-256`) и токена GitLab (`X-Gitlab-Token`) обязательна — убедитесь, что секреты совпадают.
- Все запросы к OpenProject выполняются с таймаутом и пробрасываются через `requests` в отдельном треде, чтобы не блокировать event loop FastAPI.
- Сервис возвращает `{"status":"ok"}` на `ping`/`Test` события; остальные (например, `pull_request`) игнорируются.
