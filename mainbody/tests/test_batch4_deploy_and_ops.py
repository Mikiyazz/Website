"""批次 4（4.1–4.4）验收测试：矿大服务器部署与抗压配置。

覆盖：
- 4.1 Gunicorn 4x4 + preload_app 扩容、Compose 限额与只读挂载、Nginx X-Accel-Redirect 文件/导出卸载；
- 4.2 Nginx 校内 NAT 限流放宽（login_rate >= 60r/m burst=200、api_rate >= 150r/s burst=400）与 real_ip 配置；
- 4.3 production.py 与 nginx*.conf HTTP/HTTPS 协议开关对齐（消灭无限 301 循环）；
- 4.4 同步班级 ZIP Zip64 + >20MB STORED、异步导出防连点重复/失败清半成品/72h 定时清理、
      Redis Broker/Cache 拆分与 TTL、5GB 磁盘剩余空间硬保护（503 + ERROR 告警）、数据盘与备份脚本校验。
"""
import io
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import zipfile
from collections import namedtuple
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone

from apps.accounts.models import User
from apps.common.errors import InsufficientStorage
from apps.exports.models import ExportJob
from apps.exports.retention import purge_expired_exports
from apps.exports.services import (
    EXPORT_STORED_THRESHOLD_BYTES,
    create_activity_export_job,
    export_class_zip_stream,
    run_activity_export_job,
)
from apps.submissions.services import create_draft, save_draft, submit_for_review
from apps.uploads.chunks import complete_chunked_upload, start_chunked_upload, store_chunk
from apps.uploads.services import (
    direct_upload,
    ensure_disk_space_available,
    ensure_standard_material_requirements,
)
from tests.fixtures.samples import REAL_JPEG

DiskUsage = namedtuple("DiskUsage", ["total", "used", "free"])
BASE_DIR = Path(settings.BASE_DIR)


def _make_admin(username="batch4_admin"):
    return User.objects.create_user(
        username=username,
        password="password123",
        real_name="部署管理员",
        role=User.Role.ADMIN,
        is_staff=True,
        must_change_password=False,
    )


def _make_student(username="20254001", real_name="压测同学", class_name="01班", grade="2025级", major="信息安全"):
    return User.objects.create_user(
        username=username,
        password="password123",
        real_name=real_name,
        class_name=class_name,
        grade=grade,
        major=major,
        must_change_password=False,
    )


def _submit_with_materials(student, activity, *, video_bytes=None):
    draft = create_draft(actor=student, activity_id=activity.pk)
    save_draft(
        actor=student,
        submission_id=draft.pk,
        expected_version=0,
        data={
            "project_name": "矿大抗压测试项目",
            "category": "社会调查",
            "declaration_type": "individual",
            "team_members": [],
        },
    )
    reqs = ensure_standard_material_requirements(activity)
    app_upload, _, _ = direct_upload(
        actor=student,
        requirement_id=reqs["application_form"].id,
        file_obj=SimpleUploadedFile("申报表.docx", b"PK\x03\x04" + b"doc" * 32),
    )
    pdf_upload, _, _ = direct_upload(
        actor=student,
        requirement_id=reqs["practice_report"].id,
        file_obj=SimpleUploadedFile("实践报告.pdf", b"%PDF-1.7\n" + b"pdf" * 32),
    )
    for idx in range(1, 4):
        direct_upload(
            actor=student,
            requirement_id=reqs["scene_photos"].id,
            file_obj=SimpleUploadedFile(f"照片{idx}.jpg", REAL_JPEG),
        )
    if video_bytes is not None:
        direct_upload(
            actor=student,
            requirement_id=reqs["video"].id,
            file_obj=SimpleUploadedFile("宣讲视频.mp4", video_bytes),
        )
    sub, _ = submit_for_review(actor=student, submission_id=draft.pk)
    return sub, app_upload, pdf_upload


# =====================================================================
# 4.1 Gunicorn 扩容 + Compose 资源规格 + X-Accel-Redirect 下载卸载
# =====================================================================

def test_4_1_gunicorn_compose_and_nginx_internal_media_configs():
    gunicorn_text = (BASE_DIR / "deploy" / "gunicorn.conf.py").read_text(encoding="utf-8")
    assert 'os.getenv("GUNICORN_WORKERS", "4")' in gunicorn_text
    assert 'os.getenv("GUNICORN_THREADS", "4")' in gunicorn_text
    assert "preload_app = True" in gunicorn_text
    assert "timeout = 600" in gunicorn_text

    compose_text = (BASE_DIR / "compose.yaml").read_text(encoding="utf-8")
    assert "max_connections=120" in compose_text
    assert "shared_buffers=256MB" in compose_text
    assert "mem_limit: 2g" in compose_text
    assert "media_data:/app/media:ro" in compose_text

    for tpl_name in ("nginx.conf", "nginx.https.conf", "nginx.gateway.conf"):
        text = (BASE_DIR / "deploy" / tpl_name).read_text(encoding="utf-8")
        assert "location /internal-media/" in text, tpl_name
        assert "internal;" in text, tpl_name
        assert "alias /app/media/;" in text, tpl_name


@pytest.mark.django_db
@override_settings(NGINX_ACCEL_REDIRECT_ENABLED=True, NGINX_ACCEL_REDIRECT_PREFIX="/internal-media")
def test_4_1_file_and_export_downloads_use_x_accel_redirect_after_auth(client, activity):
    admin = _make_admin("batch4_accel_admin")
    owner = _make_student("20254011", "周同学")
    outsider = _make_student("20254012", "路人同学")
    _, app_upload, pdf_upload = _submit_with_materials(owner, activity)

    # 1) 本人下载附件 -> 返回空体 + X-Accel-Redirect（不占用 Gunicorn 流式发送）
    client.force_login(owner)
    resp = client.get(f"/api/v1/uploads/{app_upload.id}/file/")
    assert resp.status_code == 200
    assert resp.content == b""
    assert resp["X-Accel-Redirect"].startswith("/internal-media/")
    assert app_upload.object_key in resp["X-Accel-Redirect"]
    assert resp["Content-Disposition"].startswith("attachment;")
    assert resp["X-Content-Type-Options"] == "nosniff"

    # 2) 内联预览 PDF -> inline + X-Accel-Redirect
    inline_resp = client.get(f"/api/v1/uploads/{pdf_upload.id}/file/?inline=1")
    assert inline_resp.status_code == 200
    assert inline_resp.content == b""
    assert inline_resp["X-Accel-Redirect"].startswith("/internal-media/")
    assert inline_resp["Content-Disposition"].startswith("inline;")

    # 3) 越权学生访问 -> 403 且严禁返回 X-Accel-Redirect 头
    client.force_login(outsider)
    denied = client.get(f"/api/v1/uploads/{app_upload.id}/file/")
    assert denied.status_code == 403
    assert "X-Accel-Redirect" not in denied

    # 4) 管理员下载异步导出包 -> X-Accel-Redirect 指向 /internal-media/exports/...
    client.force_login(admin)
    start_resp = client.post(
        "/api/v1/exports/start/",
        data=json.dumps({"activity_id": str(activity.id)}),
        content_type="application/json",
    )
    assert start_resp.status_code == 202
    job_id = start_resp.json()["job_id"]
    job = ExportJob.objects.get(pk=job_id)
    assert job.status == ExportJob.Status.DONE

    dl_resp = client.get(f"/api/v1/exports/{job_id}/download/")
    assert dl_resp.status_code == 200
    assert dl_resp.content == b""
    assert dl_resp["X-Accel-Redirect"] == f"/internal-media/{job.file_path}"
    assert dl_resp["Content-Disposition"].startswith("attachment;")
    assert dl_resp["Content-Type"] == "application/zip"

    # 学生访问导出包下载 -> 403 且无 X-Accel-Redirect
    client.force_login(owner)
    stu_dl = client.get(f"/api/v1/exports/{job_id}/download/")
    assert stu_dl.status_code == 403
    assert "X-Accel-Redirect" not in stu_dl


# =====================================================================
# 4.2 Nginx 校内 NAT 限流放宽与 real_ip 配置
# =====================================================================

def test_4_2_nginx_campus_nat_rate_limits_and_real_ip():
    for tpl_name in ("nginx.conf", "nginx.https.conf", "nginx.gateway.conf"):
        text = (BASE_DIR / "deploy" / tpl_name).read_text(encoding="utf-8")
        assert "set_real_ip_from" in text, tpl_name
        assert "real_ip_header X-Forwarded-For;" in text, tpl_name
        assert "real_ip_recursive on;" in text, tpl_name

        login_zone = re.search(r"zone=login_rate:\d+m\s+rate=(\d+)r/m;", text)
        assert login_zone and int(login_zone.group(1)) >= 60, tpl_name
        login_burst = re.search(r"limit_req\s+zone=login_rate\s+burst=(\d+)\s+nodelay;", text)
        assert login_burst and int(login_burst.group(1)) >= 200, tpl_name

        api_zone = re.search(r"zone=api_rate:\d+m\s+rate=(\d+)r/s;", text)
        assert api_zone and int(api_zone.group(1)) >= 150, tpl_name
        api_burst = re.search(r"limit_req\s+zone=api_rate\s+burst=(\d+)\s+nodelay;", text)
        assert api_burst and int(api_burst.group(1)) >= 400, tpl_name


# =====================================================================
# 4.3 production.py 与 nginx*.conf HTTP/HTTPS 协议对齐（消灭无限 301）
# =====================================================================

def _probe_production_settings(extra_env: dict) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.update(
        {
            "DJANGO_SETTINGS_MODULE": "config.settings.production",
            "DJANGO_SECRET_KEY": "p" * 64,
            "DJANGO_ALLOWED_HOSTS": "practice.cumt.edu.cn,10.10.10.10",
            "DATABASE_URL": "sqlite:///:memory:",
        }
    )
    # 清除可能干扰的宿主环境变量
    for key in (
        "DJANGO_USE_HTTPS",
        "DJANGO_SECURE_SSL_REDIRECT",
        "DJANGO_SESSION_COOKIE_SECURE",
        "DJANGO_CSRF_COOKIE_SECURE",
        "DJANGO_SECURE_HSTS_SECONDS",
        "NGINX_ACCEL_REDIRECT_ENABLED",
    ):
        env.pop(key, None)
    env.update(extra_env)

    script = (
        "import json, django\n"
        "from django.conf import settings\n"
        "django.setup()\n"
        "print(json.dumps({"
        "'ssl_redirect': settings.SECURE_SSL_REDIRECT, "
        "'session_secure': settings.SESSION_COOKIE_SECURE, "
        "'csrf_secure': settings.CSRF_COOKIE_SECURE, "
        "'hsts': settings.SECURE_HSTS_SECONDS, "
        "'proxy_header': list(settings.SECURE_PROXY_SSL_HEADER) if settings.SECURE_PROXY_SSL_HEADER else None, "
        "'accel': settings.NGINX_ACCEL_REDIRECT_ENABLED"
        "}))\n"
    )
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(BASE_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )


def test_4_3_production_https_default_and_campus_http_switch():
    # 1) 默认生产模式：HTTPS 全开启 + X-Accel-Redirect 默认开启
    https_proc = _probe_production_settings(
        {"DJANGO_CSRF_TRUSTED_ORIGINS": "https://practice.cumt.edu.cn"}
    )
    assert https_proc.returncode == 0, https_proc.stderr
    https_cfg = json.loads(https_proc.stdout.strip().splitlines()[-1])
    assert https_cfg["ssl_redirect"] is True
    assert https_cfg["session_secure"] is True
    assert https_cfg["csrf_secure"] is True
    assert https_cfg["hsts"] == 31_536_000
    assert https_cfg["proxy_header"] == ["HTTP_X_FORWARDED_PROTO", "https"]
    assert https_cfg["accel"] is True

    # 2) 默认 HTTPS 模式下配置 http:// CSRF 来源会被拒绝
    bad_https = _probe_production_settings(
        {"DJANGO_CSRF_TRUSTED_ORIGINS": "http://10.10.10.10"}
    )
    assert bad_https.returncode != 0
    assert "HTTPS" in bad_https.stderr

    # 3) 校内纯 HTTP 内网模式（DJANGO_USE_HTTPS=false）：自动关闭 SSL 重定向与安全 Cookie，接受 http:// 来源
    http_proc = _probe_production_settings(
        {
            "DJANGO_USE_HTTPS": "false",
            "DJANGO_CSRF_TRUSTED_ORIGINS": "http://10.10.10.10:8080",
        }
    )
    assert http_proc.returncode == 0, http_proc.stderr
    http_cfg = json.loads(http_proc.stdout.strip().splitlines()[-1])
    assert http_cfg["ssl_redirect"] is False
    assert http_cfg["session_secure"] is False
    assert http_cfg["csrf_secure"] is False
    assert http_cfg["hsts"] == 0
    assert http_cfg["proxy_header"] is None
    assert http_cfg["accel"] is True

    # 4) 验证 HTTP 模式下访问登录页直接 200 无 301 循环；HTTPS 模式下带 X-Forwarded-Proto=https 也直接 200
    http_login_check = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import django\n"
                "from django.conf import settings\n"
                "from django.test import Client, override_settings\n"
                "django.setup()\n"
                "storages = {**settings.STORAGES, 'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}}\n"
                "with override_settings(STORAGES=storages):\n"
                "    c = Client()\n"
                "    r = c.get('/accounts/login/', HTTP_HOST='10.10.10.10')\n"
                "    assert r.status_code == 200, f'unexpected {r.status_code}'\n"
                "print('LOGIN_HTTP_200_OK')\n"
            ),
        ],
        cwd=str(BASE_DIR),
        env={
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "config.settings.production",
            "DJANGO_SECRET_KEY": "p" * 64,
            "DJANGO_ALLOWED_HOSTS": "10.10.10.10",
            "DJANGO_CSRF_TRUSTED_ORIGINS": "http://10.10.10.10",
            "DJANGO_USE_HTTPS": "false",
            "DATABASE_URL": "sqlite:///:memory:",
        },
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert http_login_check.returncode == 0, http_login_check.stderr
    assert "LOGIN_HTTP_200_OK" in http_login_check.stdout

    https_login_check = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import django\n"
                "from django.conf import settings\n"
                "from django.test import Client, override_settings\n"
                "django.setup()\n"
                "storages = {**settings.STORAGES, 'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}}\n"
                "with override_settings(STORAGES=storages):\n"
                "    c = Client()\n"
                "    r_plain = c.get('/accounts/login/', HTTP_HOST='practice.cumt.edu.cn')\n"
                "    assert r_plain.status_code == 301 and r_plain['Location'].startswith('https://')\n"
                "    r_tls = c.get('/accounts/login/', HTTP_HOST='practice.cumt.edu.cn', HTTP_X_FORWARDED_PROTO='https')\n"
                "    assert r_tls.status_code == 200, f'unexpected {r_tls.status_code}'\n"
                "print('LOGIN_HTTPS_200_OK')\n"
            ),
        ],
        cwd=str(BASE_DIR),
        env={
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "config.settings.production",
            "DJANGO_SECRET_KEY": "p" * 64,
            "DJANGO_ALLOWED_HOSTS": "practice.cumt.edu.cn",
            "DJANGO_CSRF_TRUSTED_ORIGINS": "https://practice.cumt.edu.cn",
            "DJANGO_USE_HTTPS": "true",
            "DATABASE_URL": "sqlite:///:memory:",
        },
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert https_login_check.returncode == 0, https_login_check.stderr
    assert "LOGIN_HTTPS_200_OK" in https_login_check.stdout


# =====================================================================
# 4.4 导出防堵/防爆盘 + Redis 拆分 + 5GB 磁盘保护 + 数据盘备份规范
# =====================================================================

@pytest.mark.django_db
def test_4_4_sync_class_zip_uses_zip64_and_stored_for_large_files(activity):
    admin = _make_admin("batch4_sync_zip_admin")
    student = _make_student("20254021", "大文件同学", class_name="01班", grade="2025级", major="信息安全")
    big_video = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * (EXPORT_STORED_THRESHOLD_BYTES + 1024)
    _submit_with_materials(student, activity, video_bytes=big_video)

    zip_calls = []
    real_zipfile = zipfile.ZipFile

    def spy_zipfile(*args, **kwargs):
        zip_calls.append(dict(kwargs))
        return real_zipfile(*args, **kwargs)

    with patch("apps.exports.services.zipfile.ZipFile", side_effect=spy_zipfile):
        tmp_file, _ = export_class_zip_stream(
            actor=admin,
            activity_id=activity.pk,
            class_name="01班",
            grade="2025级",
            major="信息安全",
        )

    try:
        assert zip_calls and zip_calls[0].get("allowZip64") is True
        with zipfile.ZipFile(tmp_file, "r") as zf:
            video_entry = next(name for name in zf.namelist() if name.endswith("宣讲视频.mp4"))
            report_entry = next(name for name in zf.namelist() if name.endswith("实践报告.pdf"))
            assert zf.getinfo(video_entry).compress_type == zipfile.ZIP_STORED
            assert zf.getinfo(report_entry).compress_type == zipfile.ZIP_DEFLATED
            assert zf.getinfo(video_entry).file_size == len(big_video)
    finally:
        tmp_file.close()


@pytest.mark.django_db
def test_4_4_start_export_five_rapid_clicks_create_only_one_job(client, activity):
    admin = _make_admin("batch4_dedup_admin")
    student = _make_student("20254031", "连点测试同学")
    _submit_with_materials(student, activity)
    client.force_login(admin)

    # 模拟真实异步队列（不立即 eager 执行完），连点 5 次批量导出应只创建 1 个任务且只派发 1 次
    with patch("apps.exports.tasks.export_activity_submissions.delay") as mock_delay:
        job_ids = []
        reused_flags = []
        for _ in range(5):
            resp = client.post(
                "/api/v1/exports/start/",
                data=json.dumps({"activity_id": str(activity.id), "mode": "declaration_and_outcomes"}),
                content_type="application/json",
            )
            assert resp.status_code == 202
            body = resp.json()
            job_ids.append(body["job_id"])
            reused_flags.append(body["reused"])

    assert len(set(job_ids)) == 1
    assert reused_flags == [False, True, True, True, True]
    assert mock_delay.call_count == 1
    assert ExportJob.objects.filter(activity=activity, requested_by=admin).count() == 1


@pytest.mark.django_db
def test_4_4_failed_export_cleans_up_partial_zip(activity):
    admin = _make_admin("batch4_fail_admin")
    student = _make_student("20254041", "失败清理同学")
    _submit_with_materials(student, activity)

    job = create_activity_export_job(actor=admin, activity_id=activity.pk)
    export_dir = Path(settings.MEDIA_ROOT) / "exports" / str(activity.pk)

    with patch("apps.exports.services._write_zip_entry", side_effect=OSError("simulated disk write error")):
        failed_job = run_activity_export_job(job.pk)

    assert failed_job.status == ExportJob.Status.FAILED
    assert failed_job.file_path == ""
    assert failed_job.error_code == "OSError"
    leftover = list(export_dir.glob("*")) if export_dir.exists() else []
    assert leftover == []


@pytest.mark.django_db
def test_4_4_purge_expired_exports_removes_older_than_72h_and_orphans(activity):
    admin = _make_admin("batch4_purge_admin")
    student = _make_student("20254051", "过期清理同学")
    _submit_with_materials(student, activity)

    # 创建一个已完成导出包并改写为 75 小时前过期
    old_job = create_activity_export_job(actor=admin, activity_id=activity.pk, mode="declaration_only")
    old_job = run_activity_export_job(old_job.pk)
    assert old_job.status == ExportJob.Status.DONE
    assert old_job.expires_at is not None
    old_file = Path(settings.MEDIA_ROOT) / old_job.file_path
    assert old_file.is_file()

    # 再创建一个新鲜的导出包（不应被删）
    fresh_job = create_activity_export_job(actor=admin, activity_id=activity.pk, mode="declaration_and_outcomes")
    fresh_job = run_activity_export_job(fresh_job.pk)
    fresh_file = Path(settings.MEDIA_ROOT) / fresh_job.file_path
    assert fresh_file.is_file()

    # 将 old_job 拨回 75 小时前，并放一个 80 小时前的孤儿半成品 .part
    expired_time = timezone.now() - timedelta(hours=75)
    ExportJob.objects.filter(pk=old_job.pk).update(expires_at=expired_time, finished_at=expired_time)
    orphan_part = old_file.parent / ".orphan_half.zip.part"
    orphan_part.write_bytes(b"half-zip-bytes")
    old_ts = (timezone.now() - timedelta(hours=10)).timestamp()
    os.utime(orphan_part, (old_ts, old_ts))

    # 1) dry-run 不删文件不改库
    dry = purge_expired_exports(dry_run=True)
    assert dry.purged == 1
    assert dry.orphans_removed == 1
    assert old_file.is_file()
    assert orphan_part.is_file()

    # 2) 正式清理：删过期包与孤儿 .part，保留未过期包
    out = io.StringIO()
    call_command("purge_expired_exports", "--json", stdout=out)
    payload = json.loads(out.getvalue())
    assert payload["purged"] == 1
    assert payload["orphans_removed"] == 1
    assert not old_file.exists()
    assert not orphan_part.exists()
    assert fresh_file.is_file()

    old_job.refresh_from_db()
    assert old_job.file_path == ""

    # 3) 幂等复跑为 0；同时验证 dispatch_outbox --once 会自动拉起过期导出清理钩子
    second = purge_expired_exports()
    assert second.purged == 0
    assert second.orphans_removed == 0

    with patch("apps.jobs.management.commands.dispatch_outbox.purge_expired_exports") as mock_purge:
        mock_purge.return_value = second
        call_command("dispatch_outbox", "--once")
    assert mock_purge.call_count == 1


@pytest.mark.django_db
def test_4_4_disk_space_guard_rejects_below_5gb_with_503_and_error_log(client, activity, caplog):
    student = _make_student("20254061", "磁盘守卫同学")
    reqs = ensure_standard_material_requirements(activity)
    low_disk = DiskUsage(total=500 * 1024**3, used=498 * 1024**3, free=2 * 1024**3)

    with patch("apps.uploads.services.shutil.disk_usage", return_value=low_disk):
        with caplog.at_level(logging.ERROR):
            with pytest.raises(InsufficientStorage):
                ensure_disk_space_available(required_bytes=1024)
        assert any("disk_space_low" in rec.getMessage() for rec in caplog.records)

        # 直传 HTTP API 返回 503 insufficient_storage
        client.force_login(student)
        resp = client.post(
            "/api/v1/uploads/direct-upload/",
            {
                "requirement_id": str(reqs["application_form"].id),
                "file": SimpleUploadedFile("申报表.docx", b"PK\x03\x04" + b"x" * 64),
            },
        )
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "insufficient_storage"

        # 分片初始化同样被 503 拦截
        chunk_resp = client.post(
            "/api/v1/uploads/chunk-start/",
            data=json.dumps(
                {
                    "material_type": "video",
                    "filename": "宣讲视频.mp4",
                    "file_size": 1024 * 1024,
                    "total_chunks": 1,
                    "activity_id": str(activity.id),
                }
            ),
            content_type="application/json",
        )
        assert chunk_resp.status_code == 503
        assert chunk_resp.json()["error"]["code"] == "insufficient_storage"


def test_4_4_redis_split_cache_ttl_and_prod_backup_artifacts():
    compose_text = (BASE_DIR / "compose.yaml").read_text(encoding="utf-8")
    assert "redis-cache:" in compose_text
    assert "--maxmemory\", \"512mb\", \"--maxmemory-policy\", \"noeviction\"" in compose_text
    assert "--maxmemory\", \"512mb\", \"--maxmemory-policy\", \"allkeys-lru\"" in compose_text
    assert "CACHE_URL: redis://redis-cache:6379/0" in compose_text

    base_settings_text = (BASE_DIR / "config" / "settings" / "base.py").read_text(encoding="utf-8")
    assert "CACHE_DEFAULT_TIMEOUT" in base_settings_text
    assert '"TIMEOUT": CACHE_DEFAULT_TIMEOUT' in base_settings_text

    prod_compose = (BASE_DIR / "compose.prod.yaml").read_text(encoding="utf-8")
    assert "postgres_data:" in prod_compose
    assert "media_data:" in prod_compose
    assert "o: bind" in prod_compose

    backup_script = (BASE_DIR / "deploy" / "backup_daily.sh").read_text(encoding="utf-8")
    assert "pg_dump" in backup_script
    assert "pg_restore --list" in backup_script
    assert "rsync" in backup_script
    assert "--link-dest" in backup_script
