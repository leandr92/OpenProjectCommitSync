OpenProject Commit Sync
=======================

Сервис для автоматического добавления комментариев в задачи OpenProject по коммитам из GitHub и GitLab. В сообщении коммита указывайте ID задачи через #ID, например: "Fix login #123" — сервис добавит комментарий в задачу 123 с текстом коммита и ссылкой на него.

Требования
- Python 3.13 (в Docker уже используется соответствующий образ)
- Переменные окружения:
  - `OPENPROJECT_URL` — базовый URL OpenProject (без /api/v3 в конце), напр. `http://openproject:8080`
  - `OPENPROJECT_API_KEY` — API key пользователя в OpenProject
  - `GITHUB_WEBHOOK_SECRET` — секрет для GitHub Webhook (X-Hub-Signature-256)
  - `GITLAB_WEBHOOK_SECRET` — секрет для GitLab Webhook (X-Gitlab-Token)
 
Быстрый старт (локально, без Docker)
- Через venv + pip:
  - `python -m venv .venv && source .venv/bin/activate`
  - `pip install fastapi requests uvicorn[standard]`
  - Запуск: `uvicorn app.main:app --host 0.0.0.0 --port 8000 --env-file .env`
- Через Pipenv:
  - `pip install pipenv`
  - `pipenv install` (установит fastapi, requests)
  - `pipenv install "uvicorn[standard]"` (локально добиваем uvicorn)
  - Запуск: `pipenv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --env-file .env`

Запуск в среде разработки
1. Скопируйте `.env.example` в `.env` и укажите реальные значения `OPENPROJECT_URL`, `OPENPROJECT_API_KEY`, `GITHUB_WEBHOOK_SECRET`, `GITLAB_WEBHOOK_SECRET`.
2. Создайте виртуальное окружение: `python -m venv .venv && source .venv/bin/activate` (Windows: `.venv\Scripts\activate`).
3. Установите зависимости: `pip install fastapi requests "uvicorn[standard]"`.
4. Запустите сервис: `uvicorn app.main:app --host 0.0.0.0 --port 8000 --env-file .env`.
5. Убедитесь, что эндпоинты `/github-webhook` и `/gitlab-webhook` отвечают `{"status": "ok"}` на тестовые запросы из GitHub/GitLab (например, через их UI "Test" → "Ping/Pull").

Docker
- Сборка: `docker build -t openproject-commit-sync:latest .`
- Запуск: `docker run --rm -p 8000:8000 --env-file .env openproject-commit-sync:latest`

Docker Compose
- `docker-compose.override.yml` — добавляет сервис `commit-sync` к базовой конфигурации OpenProject и прокидывает порт `8000`. Файл содержит комментарии для настройки с Caddy (caddy-docker-proxy) либо ручного проксирования.
- `docker-compose.traefik.override.yml` — вариант для окружения с Traefik: подключает сервис к сетям `backend` и `traefik-network`, добавляет labels для маршрутизации Traefik.
- После размещения override-файла рядом с основным docker-compose OpenProject запустите: `docker compose up -d`.

Настройка GitHub Webhook
- В репозитории GitHub: Settings → Webhooks → Add webhook
- Payload URL: `https://<ваш-домен>/github-webhook` (или `http://<хост>:8000/github-webhook` при прямом пробросе порта)
- Content type: `application/json`
- Secret: тот же, что и `GITHUB_WEBHOOK_SECRET`
- Events: как минимум "Just the push event"

Настройка GitLab Webhook
- В проекте GitLab: Settings → Webhooks (или Integrations)
- URL: `https://<ваш-домен>/gitlab-webhook`
- Secret Token: значение `GITLAB_WEBHOOK_SECRET`
- Trigger: оставьте включенным только `Push events` (остальные по необходимости)
- При нажатии "Test" → "Push events" сервис вернёт `{"status": "ok"}`

Формат сообщений коммитов
- Указывайте один или несколько ID через `#<число>`. Пример: `Implement validation #101 #202`.
- Сервис добавит комментарии в соответствующие задачи.

Примечания по реализации
- Проверяются подписи/токены GitHub (`X-Hub-Signature-256`) и GitLab (`X-Gitlab-Token`). При неверном секрете вернётся 4xx.
- Для взаимодействия с OpenProject используется таймаут и обработка сетевых ошибок; запросы выполняются неблокирующе относительно event loop.
- GitHub: событие `ping` возвращает `ok`; любые события кроме `push` игнорируются.
- GitLab: обрабатываются только push-hook события (`X-Gitlab-Event: Push Hook` и `object_kind: push`). Остальные события возвращают статус `ignored`.
