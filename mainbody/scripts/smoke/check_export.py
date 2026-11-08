import io
import os
import sys
import zipfile
from pathlib import Path

import requests

# mainbody/scripts/smoke/check_export.py -> 项目根 mainbody/
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.development")
import django

django.setup()

from apps.activities.models import Activity
from apps.uploads.services import ensure_standard_material_requirements
from tests.fixtures.samples import REAL_JPEG

BASE = "http://127.0.0.1:8010"
DOCX = b"PK\x03\x04" + b"docx" * 200
PDF = b"%PDF-1.7\n" + b"pdf" * 200


def login(username, password):
    s = requests.Session()
    s.get(BASE + "/accounts/login/", timeout=20)
    token = s.cookies.get("csrftoken", "")
    r = s.post(
        BASE + "/accounts/login/",
        data={"username": username, "password": password, "csrfmiddlewaretoken": token},
        headers={"Referer": BASE + "/accounts/login/"},
        allow_redirects=False,
        timeout=20,
    )
    assert r.status_code in (302, 303), (r.status_code, r.text[:200])
    return s, s.cookies.get("csrftoken", token)


activity = Activity.objects.get(title__contains="暑期")
reqs = ensure_standard_material_requirements(activity)
student, token = login("2025080902", "080902")
headers = {"X-CSRFToken": token, "Referer": BASE + "/"}

for code_name, filename, payload, content_type in [
    ("application_form", "社会实践申报表.docx", DOCX, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    ("practice_report", "社会实践报告.pdf", PDF, "application/pdf"),
    ("scene_photos", "现场照片1.jpg", REAL_JPEG, "image/jpeg"),
    ("scene_photos", "现场照片2.jpg", REAL_JPEG, "image/jpeg"),
    ("scene_photos", "现场照片3.jpg", REAL_JPEG, "image/jpeg"),
]:
    r = student.post(
        BASE + "/api/v1/uploads/direct-upload/",
        data={"requirement_id": str(reqs[code_name].id)},
        files={"file": (filename, payload, content_type)},
        headers=headers,
        timeout=60,
    )
    assert r.status_code == 201, (code_name, r.status_code, r.text[:300])
print("PASS uploads for returned submission")

admin, admin_token = login("admin", "Admin123456")
started = admin.post(
    BASE + "/api/v1/exports/start/",
    json={"activity_id": str(activity.id)},
    headers={"X-CSRFToken": admin_token, "Referer": BASE + "/"},
    timeout=120,
)
assert started.status_code == 202, started.text[:300]
job = started.json()["job"]
assert job["status"] == "done", job
download = admin.get(BASE + job["download_url"], timeout=120)
assert download.status_code == 200, download.text[:300]
archive = zipfile.ZipFile(io.BytesIO(download.content))
names = archive.namelist()
print("PASS export entries:", len(names))
for name in names:
    print("   ", name)
assert any(name.startswith("2025080902_") for name in names)
assert any(name.endswith("社会实践申报表.docx") for name in names)
