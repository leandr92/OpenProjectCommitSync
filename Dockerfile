FROM python:3.13-slim

WORKDIR /app

# Установим pipenv
RUN pip install --no-cache-dir pipenv

COPY Pipfile Pipfile.lock* /app/

# Установим зависимости в system site-packages
RUN pipenv install --system --deploy

COPY ./app /app/app

# Uvicorn не зафиксирован в Pipfile.lock — установим отдельно
RUN pip install --no-cache-dir "uvicorn[standard]"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8088"]
