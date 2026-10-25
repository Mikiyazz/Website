FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements/base.txt /app/requirements/base.txt
RUN pip install --no-cache-dir -r requirements/base.txt && useradd --create-home --uid 10001 app
COPY --chown=app:app . /app
RUN DJANGO_SETTINGS_MODULE=config.settings.base python manage.py collectstatic --noinput
USER app
EXPOSE 8000
CMD ["gunicorn", "-c", "deploy/gunicorn.conf.py", "config.wsgi:application"]
