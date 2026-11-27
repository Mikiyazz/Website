"""批次 3（师生正常使用不翻车 · 5 项）专项验收测试。

覆盖：
- 3.1 (FUNC-03 + PERF-04): 审核工作台、管理面板审核列表与学生进度表、/api/v1/reviews/ 全面分页与真实全量 KPI 聚合，1600 条数据单页 HTML < 150 KB，团支书绝不拉取全院申报
- 3.2 (FUNC-04): ReviewAssignment 支持 grade/major，跨年级/跨专业同名 01班 审核/筛选/导出严格隔离并兼容无年级/专业旧数据
- 3.3 (SEC-01): apply.html / outcomes.html 使用 |json_script 防特殊字符与 </script> 弄崩 DOM
- 3.4 (FUNC-06): 审核台切活动更新补交白名单表单与顶部 KPI，direct_upload 幂等 (owner, request_key) 查重与配额复核，非法 UUID 参数返回 400 而非 500
- 3.5 (FUNC-05 + FUNC-07): store_chunk / complete_chunked_upload 校验 expires_at 与 ensure_activity_open，eligible_activities PG 分支补齐 late_whitelist_student_numbers__contains，apply-wizard.js 校验失败自动跳回首个有错步骤并回收 URL.createObjectURL
"""
from datetime import timedelta
import io
import json
from pathlib import Path
import re
import uuid
import zipfile

import openpyxl
import pytest
from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.activities.models import Activity
from apps.activities.selectors import (
    eligible_activities,
    ensure_activity_open,
    is_activity_open_for_user,
)
from apps.common.errors import Conflict, QuotaExceeded
from apps.exports.services import list_activity_declared_classes
from apps.reviews.models import ReviewAssignment
from apps.reviews.selectors import reviewable_submissions, workbench_class_options
from apps.submissions.models import Submission
from apps.submissions.services import create_draft, save_draft, submit_for_review
from apps.uploads.chunks import complete_chunked_upload, start_chunked_upload, store_chunk
from apps.uploads.models import FileAsset, StorageQuota, UploadSession
from apps.uploads.services import direct_upload, ensure_standard_material_requirements
from tests.conftest import seed_mandatory_materials


def _make_valid_docx_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "<Types></Types>")
        zf.writestr("word/document.xml", "<document><body>测试申报表</body></document>")
    return buf.getvalue()


def _make_admin(username="admin_b3"):
    return User.objects.create_user(
        username=username,
        password="password123",
        real_name="学院管理员",
        role=User.Role.ADMIN,
        is_staff=True,
        must_change_password=False,
    )


def _make_student(username, real_name, *, grade="", major="", class_name="01班", phone="13800000001"):
    return User.objects.create_user(
        username=username,
        password="password123",
        real_name=real_name,
        role=User.Role.STUDENT,
        grade=grade,
        major=major,
        class_name=class_name,
        phone=phone,
        must_change_password=False,
    )


# =====================================================================
# 3.1 1600 条数据：KPI 真实统计、三端分页、单页 HTML < 150 KB、团支书禁拉全院
# =====================================================================

@pytest.mark.django_db
def test_batch3_1_workbench_dashboard_and_api_pagination_1600_records(client, activity):
    ensure_standard_material_requirements(activity)
    admin = _make_admin("admin_b3_1600")
    pw_hash = make_password("123456")

    total_count = 1600
    statuses = (
        Submission.Status.PENDING_FIRST_REVIEW,
        Submission.Status.PENDING_FINAL_REVIEW,
        Submission.Status.FINAL_APPROVED,
        Submission.Status.RETURNED,
    )
    users = [
        User(
            username=f"2531{i:04d}",
            password=pw_hash,
            real_name=f"学生{i:04d}",
            grade="2025级",
            major="信息安全" if i % 2 == 0 else "软件工程",
            class_name="01班" if (i // 2) % 2 == 0 else "02班",
            role=User.Role.STUDENT,
            is_active=True,
            must_change_password=False,
        )
        for i in range(total_count)
    ]
    User.objects.bulk_create(users)
    created_users = list(User.objects.filter(username__startswith="2531").order_by("username"))

    subs = [
        Submission(
            owner=stu,
            activity=activity,
            status=statuses[idx % len(statuses)],
            draft_data={
                "project_name": f"社会实践项目_{idx:04d}",
                "category": "社会调查",
                "declaration_type": "individual",
                "team_members": [],
            },
            declaration_submitted=True,
            is_late_submission=(idx % 20 == 0),
            version=1,
        )
        for idx, stu in enumerate(created_users)
    ]
    Submission.objects.bulk_create(subs)

    client.force_login(admin)

    # 1) 审核工作台：KPI 真实为 1600，分页每页 25 条，可翻至第 64 页，单页 HTML < 150 KB
    wb_p1 = client.get(f"/reviews/workbench/?activity_id={activity.id}")
    assert wb_p1.status_code == 200
    assert len(wb_p1.content) < 150 * 1024, f"工作台单页 HTML 过大: {len(wb_p1.content)} bytes"
    assert wb_p1.context["counts"]["total"] == 1600
    assert wb_p1.context["counts"]["pending_first_review"] == 400
    assert wb_p1.context["counts"]["pending_final_review"] == 400
    assert wb_p1.context["counts"]["final_approved"] == 400
    assert wb_p1.context["counts"]["returned"] == 400
    assert wb_p1.context["counts"]["late"] == 80
    assert len(wb_p1.context["rows"]) == 25
    assert wb_p1.context["page_obj"].paginator.num_pages == 64
    wb_p1_html = wb_p1.content.decode("utf-8")
    assert "共 1600 条" in wb_p1_html
    assert 'id="workbench-pagination"' in wb_p1_html

    wb_p64 = client.get(f"/reviews/workbench/?activity_id={activity.id}&page=64")
    assert wb_p64.status_code == 200
    assert len(wb_p64.context["rows"]) == 25
    assert wb_p64.context["page_obj"].number == 64
    assert wb_p64.context["counts"]["total"] == 1600

    # 2) 管理面板：审核列表 & 学生进度表全面分页，顶部统计真实 1600，单页 HTML < 150 KB
    panel_p1 = client.get(f"/admin-panel/?activity_id={activity.id}")
    assert panel_p1.status_code == 200
    assert len(panel_p1.content) < 150 * 1024, f"管理面板单页 HTML 过大: {len(panel_p1.content)} bytes"
    assert panel_p1.context["stats"]["total"] == 1600
    assert panel_p1.context["stats"]["final_approved"] == 400
    assert len(panel_p1.context["submission_items"]) == 25
    assert panel_p1.context["submission_page_obj"].paginator.num_pages == 64
    assert panel_p1.context["account_progress_summary"]["total"] == 1600
    assert len(panel_p1.context["account_progress_rows"]) == 25
    assert panel_p1.context["account_progress_page_obj"].paginator.num_pages == 64
    panel_html = panel_p1.content.decode("utf-8")
    assert 'id="review-submissions-pagination"' in panel_html
    assert 'id="account-progress-pagination"' in panel_html

    panel_last = client.get(f"/admin-panel/?activity_id={activity.id}&sub_page=64&acct_page=64")
    assert panel_last.status_code == 200
    assert panel_last.context["submission_page_obj"].number == 64
    assert len(panel_last.context["submission_items"]) == 25
    assert panel_last.context["account_progress_page_obj"].number == 64
    assert len(panel_last.context["account_progress_rows"]) == 25

    # 3) /api/v1/reviews/ 分页（每页最多 50 条，total 准确为 1600，可翻至末页）
    api_p1 = client.get(f"/api/v1/reviews/?activity_id={activity.id}&page=1&page_size=50")
    assert api_p1.status_code == 200
    p1_data = api_p1.json()
    assert p1_data["total"] == 1600
    assert p1_data["total_pages"] == 32
    assert p1_data["page"] == 1
    assert p1_data["page_size"] == 50
    assert p1_data["has_next"] is True
    assert len(p1_data["items"]) == 50

    api_p32 = client.get(f"/api/v1/reviews/?activity_id={activity.id}&page=32&page_size=50")
    assert api_p32.status_code == 200
    p32_data = api_p32.json()
    assert p32_data["page"] == 32
    assert p32_data["has_next"] is False
    assert len(p32_data["items"]) == 50


@pytest.mark.django_db
def test_batch3_1_reviewer_account_progress_does_not_query_whole_college_submissions(client, activity):
    """团支书查看本班进度时仅查询本班学生及其团队关联，绝不拉取全院申报。"""
    ensure_standard_material_requirements(activity)
    reviewer = User.objects.create_user(
        username="tzs_b3_scope",
        password="password123",
        real_name="一班团支书",
        role=User.Role.REVIEWER,
        grade="2025级",
        major="信息安全",
        class_name="01班",
        must_change_password=False,
    )
    own_stu = _make_student("25320001", "本班同学", grade="2025级", major="信息安全", class_name="01班")
    own_member = _make_student("25320002", "本班队员", grade="2025级", major="信息安全", class_name="01班")
    outsider_leader = _make_student("25320099", "外班队长", grade="2025级", major="软件工程", class_name="02班")

    # 本班个人申报 + 外班队长带本班队员的跨班团队申报
    sub_own = create_draft(actor=own_stu, activity_id=activity.id)
    save_draft(
        actor=own_stu,
        submission_id=sub_own.id,
        expected_version=0,
        data={"project_name": "本班独立项目", "category": "社会调查", "declaration_type": "individual", "team_members": []},
    )
    sub_cross = create_draft(actor=outsider_leader, activity_id=activity.id)
    save_draft(
        actor=outsider_leader,
        submission_id=sub_cross.id,
        expected_version=0,
        data={
            "project_name": "跨班团队项目",
            "category": "社会调查",
            "declaration_type": "team",
            "team_members": [{"student_number": own_member.username, "real_name": own_member.real_name, "class_name": "01班"}],
        },
    )

    # 额外造 25 名无关外班学生和申报
    for i in range(25):
        other = _make_student(f"253210{i:02d}", f"无关同学{i}", grade="2024级", major="计算机科学与技术", class_name="03班")
        Submission.objects.create(
            owner=other,
            activity=activity,
            status=Submission.Status.PENDING_FIRST_REVIEW,
            draft_data={"project_name": f"无关外班项目_{i}", "category": "社会调查", "declaration_type": "individual", "team_members": []},
            declaration_submitted=True,
        )

    client.force_login(reviewer)
    with CaptureQueriesContext(connection) as ctx:
        resp = client.get(f"/?activity_id={activity.id}")
    assert resp.status_code == 200
    html = resp.content.decode("utf-8")
    assert 'data-account-row="25320001"' in html
    assert 'data-account-row="25320002"' in html
    assert "团队申报（队员）" in html
    assert "无关外班项目_0" not in html
    assert "25321000" not in html

    # 验证所有针对 submissions_submission 的主查询都带了 owner_id / class_name / id 限定（绝无无条件全院扫描）
    unscoped_sub_queries = []
    for entry in ctx.captured_queries:
        sql = entry["sql"]
        if 'FROM "submissions_submission"' in sql and "EXISTS" not in sql.upper() and "GROUP BY" not in sql.upper():
            # 排除 redundant_member_draft_ids 子查询外，普通拉取必须有 owner/class 过滤
            if '"draft_data" = \'{}\'' in sql:
                continue
            if "owner_id" not in sql and "class_name" not in sql and "submissions_submission\".\"id\" IN" not in sql:
                unscoped_sub_queries.append(sql)
    assert unscoped_sub_queries == [], f"团支书请求触发了全院申报查询: {unscoped_sub_queries}"


# =====================================================================
# 3.2 跨专业 / 跨年级同名班级（01班）串班隔离 + 兼容旧无年级/专业记录
# =====================================================================

@pytest.mark.django_db
def test_batch3_2_same_class_name_isolation_across_grades_and_majors(client, activity):
    ensure_standard_material_requirements(activity)
    admin = _make_admin("admin_b3_cls")

    # 3 个同名 01班 + 1 个无年级/专业历史格式班级
    stu_target = _make_student("25330001", "信安25一班甲", grade="2025级", major="信息安全", class_name="01班")
    stu_other_grade = _make_student("26330001", "信安26一班乙", grade="2026级", major="信息安全", class_name="01班")
    stu_other_major = _make_student("25330002", "软工25一班丙", grade="2025级", major="软件工程", class_name="01班")
    stu_legacy = _make_student("24330001", "历史班丁", grade="", major="", class_name="信安24-1班")

    subs = {}
    for stu, proj in (
        (stu_target, "信安25一班项目"),
        (stu_other_grade, "信安26一班项目"),
        (stu_other_major, "软工25一班项目"),
        (stu_legacy, "历史无年级专业项目"),
    ):
        d = create_draft(actor=stu, activity_id=activity.id)
        save_draft(
            actor=stu,
            submission_id=d.id,
            expected_version=0,
            data={"project_name": proj, "category": "社会调查", "declaration_type": "individual", "team_members": []},
        )
        seed_mandatory_materials(stu, activity)
        sub, _ = submit_for_review(actor=stu, submission_id=d.id)
        subs[stu.username] = sub

    # 1) ReviewAssignment 带 grade + major 精确隔离同名 01班
    assigned_reviewer = User.objects.create_user(
        username="rev_assigned_exact",
        password="password123",
        real_name="显式分配审核员",
        role=User.Role.REVIEWER,
        grade="",
        major="",
        class_name="",
        must_change_password=False,
    )
    ReviewAssignment.objects.create(
        reviewer=assigned_reviewer,
        activity=activity,
        grade="2025级",
        major="信息安全",
        class_name="01班",
    )
    assert set(reviewable_submissions(assigned_reviewer, activity.id).values_list("id", flat=True)) == {
        subs["25330001"].id
    }
    assert workbench_class_options(assigned_reviewer) == ["2025级 信息安全 01班"]

    client.force_login(assigned_reviewer)
    assert client.get(f"/reviews/submissions/{subs['25330001'].id}/").status_code == 200
    assert client.get(f"/reviews/submissions/{subs['26330001'].id}/").status_code == 403
    assert client.get(f"/reviews/submissions/{subs['25330002'].id}/").status_code == 403
    assert (
        client.get(
            f"/api/v1/exports/activities/{activity.id}/zip/",
            {"class_name": "2025级 软件工程 01班"},
        ).status_code
        == 403
    )

    # 2) 管理员筛选/导出「2025级 信息安全 01班」不混入其他年级/专业 01班
    client.force_login(admin)
    declared = list_activity_declared_classes(actor=admin, activity_id=activity.id)
    assert "2025级 信息安全 01班" in declared
    assert "2026级 信息安全 01班" in declared
    assert "2025级 软件工程 01班" in declared
    assert "信安24-1班" in declared

    # 审核工作台筛选
    wb_resp = client.get("/reviews/workbench/", {"activity_id": str(activity.id), "class_name": "2025级 信息安全 01班"})
    assert wb_resp.status_code == 200
    wb_html = wb_resp.content.decode("utf-8")
    assert "信安25一班项目" in wb_html
    assert "信安26一班项目" not in wb_html
    assert "软工25一班项目" not in wb_html

    # 管理面板审核列表 + 学生进度表筛选
    dash_resp = client.get(
        "/admin-panel/",
        {
            "activity_id": str(activity.id),
            "class_name": "2025级 信息安全 01班",
            "acct_class": "2025级 信息安全 01班",
        },
    )
    assert dash_resp.status_code == 200
    assert [it["student_number"] for it in dash_resp.context["submission_items"]] == ["25330001"]
    assert [r["student_number"] for r in dash_resp.context["account_progress_rows"]] == ["25330001"]

    # /api/v1/reviews/ 筛选
    api_resp = client.get("/api/v1/reviews/", {"activity_id": str(activity.id), "class_name": "2025级 信息安全 01班"})
    assert api_resp.status_code == 200
    assert [it["student_number"] for it in api_resp.json()["items"]] == ["25330001"]

    # Excel 按班导出精确匹配
    excel_resp = client.get(
        f"/api/v1/exports/activities/{activity.id}/excel/",
        {"class_name": "2025级 信息安全 01班"},
    )
    assert excel_resp.status_code == 200
    ws = openpyxl.load_workbook(io.BytesIO(excel_resp.content)).active
    excel_rows = list(ws.iter_rows(values_only=True))
    assert [r[0] for r in excel_rows[1:]] == ["25330001"]

    # ZIP 按班导出精确匹配
    zip_resp = client.get(
        f"/api/v1/exports/activities/{activity.id}/zip/",
        {"class_name": "2025级 信息安全 01班"},
    )
    assert zip_resp.status_code == 200
    with zipfile.ZipFile(io.BytesIO(b"".join(zip_resp.streaming_content))) as zf:
        names = zf.namelist()
        assert names
        assert all("25330001" in n for n in names)
        assert all("26330001" not in n and "25330002" not in n for n in names)

    # 兼容旧数据无年级/专业筛选与导出
    legacy_resp = client.get(
        f"/api/v1/exports/activities/{activity.id}/excel/",
        {"class_name": "信安24-1班"},
    )
    assert legacy_resp.status_code == 200
    legacy_ws = openpyxl.load_workbook(io.BytesIO(legacy_resp.content)).active
    assert [r[0] for r in list(legacy_ws.iter_rows(values_only=True))[1:]] == ["24330001"]


# =====================================================================
# 3.3 模板 JSON 改用 |json_script 防特殊字符与 </script> 破坏 DOM
# =====================================================================

@pytest.mark.django_db
def test_batch3_3_json_script_escapes_script_tag_and_quotes(client, activity):
    ensure_standard_material_requirements(activity)
    stu = _make_student("25340001", '周"引号"达', grade="2025级", major="信息安全", class_name="01班")
    sub = create_draft(actor=stu, activity_id=activity.id)
    evil_name = '项目</script><script>window.__xss=1</script>"测试\''
    evil_member = '队员</script>"甲'
    save_draft(
        actor=stu,
        submission_id=sub.id,
        expected_version=0,
        data={
            "project_name": evil_name,
            "category": "社会调查",
            "declaration_type": "team",
            "team_members": [
                {"student_number": "25340002", "real_name": evil_member, "class_name": "01班"},
            ],
        },
    )

    client.force_login(stu)
    for url in (
        f"/submissions/apply/?activity_id={activity.id}&submission_id={sub.id}",
        f"/submissions/outcomes/?activity_id={activity.id}&submission_id={sub.id}",
    ):
        resp = client.get(url)
        assert resp.status_code == 200
        html = resp.content.decode("utf-8")
        assert "</script><script>window.__xss=1</script>" not in html
        # 解析由 json_script 生成的 <script id="wizard-team-json" type="application/json">...</script>
        match = re.search(
            r'<script id="wizard-team-json" type="application/json">(.*?)</script>',
            html,
            re.DOTALL,
        )
        assert match is not None, f"{url} 未找到 json_script 生成的 wizard-team-json"
        assert "</script>" not in match.group(1)
        parsed_team = json.loads(match.group(1))
        assert parsed_team[0]["real_name"] == evil_member

        mat_match = re.search(
            r'<script id="wizard-materials-json" type="application/json">(.*?)</script>',
            html,
            re.DOTALL,
        )
        assert mat_match is not None
        assert isinstance(json.loads(mat_match.group(1)), list)


# =====================================================================
# 3.4 审核台切活动更新补交表单 + 重复 request_key 幂等 + 非法 UUID 400
# =====================================================================

@pytest.mark.django_db
def test_batch3_4_workbench_activity_switch_and_invalid_uuid_returns_400(client, activity):
    now = timezone.now()
    act_b = Activity.objects.create(
        title="2026年秋季社会实践B",
        opens_at=now - timedelta(days=1),
        deadline=now + timedelta(days=5),
        is_open=True,
        late_submission_enabled=True,
        late_whitelist_student_numbers=["20259999"],
    )
    admin = _make_admin("admin_b3_34")
    stu = _make_student("25350001", "普通学生", grade="2025级", major="信息安全", class_name="01班")

    client.force_login(admin)
    resp_b = client.get(f"/reviews/workbench/?activity_id={act_b.id}")
    assert resp_b.status_code == 200
    html_b = resp_b.content.decode("utf-8")
    assert f'/admin-panel/activities/{act_b.id}/late-settings/' in html_b
    assert "20259999" in html_b
    # 确认切活动脚本不再跳过 #late-settings-card，而是整页跳转携带新 activity_id
    assert 'target.id === "filter_activity_id"' in html_b
    assert "window.location.href =" in html_b

    # 非法 UUID 参数：API 与 HTML POST 表单均返回 400 而非 500
    assert client.get("/api/v1/reviews/?activity_id=not-a-uuid").status_code == 400
    assert client.post(
        reverse("admin-activity-save"),
        {
            "activity_id": "invalid-uuid-123",
            "title": "坏活动",
            "opens_at": (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M"),
            "deadline": (now + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M"),
        },
    ).status_code == 400

    client.force_login(stu)
    assert client.post(reverse("student-save-draft"), {"submission_id": "not-a-uuid", "project_name": "测试"}).status_code == 400
    assert client.post(reverse("student-save-draft"), {"activity_id": "not-a-uuid", "project_name": "测试"}).status_code == 400


@pytest.mark.django_db
def test_batch3_4_direct_upload_idempotent_request_key_and_written_bytes_quota(client, activity):
    reqs = ensure_standard_material_requirements(activity)
    req = reqs["application_form"]
    stu = _make_student("25350002", "重试同学", grade="2025级", major="信息安全", class_name="01班")
    sub = create_draft(actor=stu, activity_id=activity.id)
    docx_bytes = _make_valid_docx_bytes()
    req_key = str(uuid.uuid4())

    client.force_login(stu)
    r1 = client.post(
        "/api/v1/uploads/direct-upload/",
        {
            "requirement_id": str(req.id),
            "submission_id": str(sub.id),
            "request_key": req_key,
            "file": SimpleUploadedFile("申报表.docx", docx_bytes, content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        },
    )
    assert r1.status_code == 201
    first_id = r1.json()["id"]
    quota_after_first = StorageQuota.objects.get(owner=stu, activity=activity).used_bytes
    assert quota_after_first == len(docx_bytes)

    # 相同 request_key 重复提交：幂等返回同一记录、绝不报 500、配额不双扣
    r2 = client.post(
        "/api/v1/uploads/direct-upload/",
        {
            "requirement_id": str(req.id),
            "submission_id": str(sub.id),
            "request_key": req_key,
            "file": SimpleUploadedFile("申报表.docx", docx_bytes, content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        },
    )
    assert r2.status_code == 201
    assert r2.json()["id"] == first_id
    assert StorageQuota.objects.get(owner=stu, activity=activity).used_bytes == quota_after_first
    assert UploadSession.objects.filter(owner=stu, request_key=req_key).count() == 1

    # 同 request_key 换用不同文件名 -> 409 Conflict
    r3 = client.post(
        "/api/v1/uploads/direct-upload/",
        {
            "requirement_id": str(req.id),
            "submission_id": str(sub.id),
            "request_key": req_key,
            "file": SimpleUploadedFile("另一个文件.docx", docx_bytes, content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        },
    )
    assert r3.status_code == 409

    # 写入后用 written_bytes 复核配额：将限额设为当前已用 + 10 字节，再上传更大的实践报告触发 QuotaExceeded
    quota = StorageQuota.objects.get(owner=stu, activity=activity)
    quota.limit_bytes = quota.used_bytes + 10
    quota.save(update_fields=["limit_bytes"])
    report_req = reqs["practice_report"]
    with pytest.raises(QuotaExceeded):
        direct_upload(
            actor=stu,
            requirement_id=report_req.id,
            submission_id=sub.id,
            file_obj=SimpleUploadedFile("超额报告.pdf", b"%PDF-1.7\n" + b"0" * 512),
        )


# =====================================================================
# 3.5 截止后分片校验 + PostgreSQL 白名单对齐 + 前端错误步骤回跳与 ObjectURL 回收
# =====================================================================

@pytest.mark.django_db
def test_batch3_5_chunk_upload_and_complete_enforce_deadline_and_expiry(activity):
    reqs = ensure_standard_material_requirements(activity)
    stu = _make_student("25360001", "分片同学", grade="2025级", major="信息安全", class_name="01班")
    create_draft(actor=stu, activity_id=activity.id)

    mp4_header = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"
    part0 = mp4_header + b"A" * (5 * 1024 * 1024 - len(mp4_header))
    part1 = b"B" * 1024
    total_size = len(part0) + len(part1)

    upload, resumed = start_chunked_upload(
        actor=stu,
        material_type="video",
        filename="demo.mp4",
        file_size=total_size,
        total_chunks=2,
        requirement_id=reqs["video"].id,
    )
    assert resumed is False
    store_chunk(actor=stu, upload_id=upload.id, chunk_index=0, chunk_file=SimpleUploadedFile("p0", part0))

    # 1) 活动截止后（非白名单）：继续上传分片与合并分片均被拒绝
    activity.deadline = timezone.now() - timedelta(minutes=1)
    activity.save(update_fields=["deadline"])

    with pytest.raises(Conflict):
        store_chunk(actor=stu, upload_id=upload.id, chunk_index=1, chunk_file=SimpleUploadedFile("p1", part1))
    with pytest.raises(Conflict):
        complete_chunked_upload(actor=stu, upload_id=upload.id)

    # 2) 将该学生加入补交白名单并开启补交 -> 允许继续上传第 1 片，但会话 expires_at 过期仍拒绝
    activity.late_submission_enabled = True
    activity.late_whitelist_student_numbers = [stu.username]
    activity.save(update_fields=["late_submission_enabled", "late_whitelist_student_numbers"])

    upload.expires_at = timezone.now() - timedelta(seconds=5)
    upload.save(update_fields=["expires_at"])
    with pytest.raises(Conflict):
        store_chunk(actor=stu, upload_id=upload.id, chunk_index=1, chunk_file=SimpleUploadedFile("p1", part1))
    with pytest.raises(Conflict):
        complete_chunked_upload(actor=stu, upload_id=upload.id)

    # 3) 恢复有效 expires_at -> 白名单学生可顺利传完并合并
    upload.expires_at = timezone.now() + timedelta(hours=1)
    upload.save(update_fields=["expires_at"])
    store_chunk(actor=stu, upload_id=upload.id, chunk_index=1, chunk_file=SimpleUploadedFile("p1", part1))
    done_upload, asset, _ = complete_chunked_upload(actor=stu, upload_id=upload.id)
    assert done_upload.status == UploadSession.Status.COMPLETE
    assert asset.actual_bytes == total_size


@pytest.mark.django_db
def test_batch3_5_eligible_activities_postgresql_whitelist_and_open_alignment(monkeypatch):
    now = timezone.now()
    act = Activity.objects.create(
        title="限定班级补交活动",
        opens_at=now - timedelta(days=5),
        deadline=now - timedelta(hours=1),
        is_open=True,
        allowed_classes=["02班"],
        late_submission_enabled=True,
        late_whitelist_student_numbers=["25360009"],
    )
    # 跨班白名单学生（01班，不在 allowed_classes=["02班"]，但在补交白名单内）
    stu_wl = _make_student("25360009", "白名单跨班同学", grade="2025级", major="信息安全", class_name="01班")

    # 1) SQLite 分支可见且可打开/提交
    assert act.id in set(eligible_activities(stu_wl).values_list("id", flat=True))
    is_open, label = is_activity_open_for_user(act, stu_wl)
    assert is_open is True and "白名单" in label
    ensure_activity_open(act, stu_wl)  # 不抛异常

    # 2) 验证 PostgreSQL 分支构造的 Q 包含 late_whitelist_student_numbers__contains=[student_number]
    captured_q = []

    def _fake_filter(q_obj):
        captured_q.append(q_obj)
        return Activity.objects.none()

    monkeypatch.setattr("apps.activities.selectors.connection.vendor", "postgresql", raising=False)
    monkeypatch.setattr(Activity.objects, "filter", _fake_filter)
    eligible_activities(stu_wl)
    assert len(captured_q) == 1
    q_repr = repr(captured_q[0])
    assert "late_whitelist_student_numbers__contains" in q_repr
    assert "25360009" in q_repr


def test_batch3_5_apply_wizard_js_jumps_to_first_error_and_revokes_object_urls():
    js_path = Path(settings.BASE_DIR) / "static" / "js" / "apply-wizard.js"
    source = js_path.read_text(encoding="utf-8")

    # 回收 URL.createObjectURL
    assert "revokeSummaryObjectUrls" in source
    assert "window.URL.revokeObjectURL" in source
    assert "beforeunload" in source

    # 提交校验失败依次回跳至 Step 1 / 2 / 3 并聚焦首个错误字段
    assert "focusFirstInvalidInStep(1)" in source
    assert "focusFirstInvalidInStep(2)" in source
    assert "focusFirstInvalidInStep(3)" in source
    assert "showStep(1, { skipValidation: true })" in source
    assert "showStep(2, { skipValidation: true })" in source
    assert "showStep(3, { skipValidation: true })" in source
