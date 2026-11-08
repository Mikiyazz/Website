import io
import json
import re
import sys
import zipfile

import requests

BASE = "http://127.0.0.1:8010"


def login(username, password):
    session = requests.Session()
    session.get(BASE + "/accounts/login/", timeout=20)
    token = session.cookies.get("csrftoken", "")
    response = session.post(
        BASE + "/accounts/login/",
        data={"username": username, "password": password, "csrfmiddlewaretoken": token},
        headers={"Referer": BASE + "/accounts/login/"},
        allow_redirects=False,
        timeout=20,
    )
    assert response.status_code in (302, 303), (username, response.status_code, response.text[:200])
    return session, session.cookies.get("csrftoken", token)


def main():
    student, _ = login("2025080901", "080901")
    dashboard = student.get(BASE + "/", timeout=30)
    assert dashboard.status_code == 200
    activity_id = sys.argv[1]
    dashboard = student.get(BASE + "/", params={"activity_id": activity_id}, timeout=30)
    assert "计算机科普与乡村教育帮扶实践" in dashboard.text, dashboard.text[:300]
    assert "已提交" in dashboard.text
    assert 'id="must-change-password-banner"' in dashboard.text
    print("PASS dashboard: submission card + status badge + force-change banner")

    apply_page = student.get(BASE + "/submissions/apply/", params={"activity_id": activity_id}, timeout=30)
    assert apply_page.status_code == 200
    assert 'id="wizard-steps"' in apply_page.text
    for step in (1, 2, 3, 4):
        assert 'data-step="%d"' % step in apply_page.text
    assert "只读锁定" in apply_page.text
    print("PASS apply: four wizard steps + locked banner for submitted draft")

    reviewer, _ = login("tzs2025", "Admin123456")
    workbench = reviewer.get(BASE + "/reviews/workbench/", params={"activity_id": activity_id}, timeout=30)
    assert workbench.status_code == 200
    assert "批量导出" in workbench.text
    assert "2025080901" in workbench.text
    assert 'data-bs-target="#history-' in workbench.text
    filtered = reviewer.get(
        BASE + "/reviews/workbench/",
        params={"activity_id": activity_id, "class_name": "信安25-1班"},
        timeout=30,
    )
    assert "2025080901" in filtered.text and "2025080902" in filtered.text
    assert "现场照片材料目前仅有 1 张" in filtered.text
    print("PASS workbench: class filter + history timeline + rejection comment")

    admin, token = login("admin", "Admin123456")
    panel = admin.get(BASE + "/admin-panel/", timeout=30)
    assert panel.status_code == 200
    assert "全院汇总表导出" in panel.text

    started = admin.post(
        BASE + "/api/v1/exports/start/",
        json={"activity_id": activity_id},
        headers={"X-CSRFToken": token, "Referer": BASE + "/"},
        timeout=120,
    )
    assert started.status_code == 202, started.text[:300]
    job = started.json()["job"]
    assert job["status"] == "done", job
    download = admin.get(BASE + job["download_url"], timeout=120)
    assert download.status_code == 200, download.text[:300]
    archive = zipfile.ZipFile(io.BytesIO(download.content))
    names = archive.namelist()
    print("PASS export: job=%s status=%s entries=%d sample=%s" % (job["job_id"], job["status"], len(names), names[:3]))


if __name__ == "__main__":
    main()
