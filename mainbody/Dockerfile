FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements/base.txt /app/requirements/base.txt
RUN pip install --no-cache-dir -r requirements/base.txt && useradd --create-home --uid 10001 app
COPY --chown=app:app . /app
# media 现在是命名卷的挂载点：镜像里先建好并交给 app 用户，否则卷初始化后属主是 root，上传会 EACCES。
RUN mkdir -p /app/media && chown -R app:app /app/media
RUN DJANGO_SETTINGS_MODULE=config.settings.base python manage.py collectstatic --noinput
USER app
EXPOSE 8000
CMD ["gunicorn", "-c", "deploy/gunicorn.conf.py", "config.wsgi:application"]
