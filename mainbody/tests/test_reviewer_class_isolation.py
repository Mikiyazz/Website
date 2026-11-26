"""团支书（初审人员）本班严格隔离测试：注册信息、成果审核、附件访问与导出。"""
import io
import json
import zipfile

import openpyxl
import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.accounts.models import User
from apps.common.errors import Forbidden
from apps.exports.models import ExportJob
from apps.reviews.selectors import reviewable_submissions, workbench_class_options
from apps.reviews.services import review_submission
from apps.submissions.models import Submission
from apps.submissions.services import create_draft, save_draft, submit_for_review
from apps.uploads.models import UploadSession
from apps.uploads.services import direct_upload, ensure_standard_material_requirements
from tests.conftest import seed_mandatory_materials


def _make_student(username, real_name, *, grade, major, class_name, phone):
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


def _create_submitted_project(student, activity, project_name, *, team_members=None):
    sub = create_draft(actor=student, activity_id=activity.id)
    save_draft(
        actor=student,
        submission_id=sub.id,
        expected_version=sub.version,
        data={
            "project_name": project_name,
            "category": "社会调查",
            "declaration_type": "team" if team_members else "individual",
            "team_members": list(team_members or []),
        },
    )
    seed_mandatory_materials(student, activity)
    submit_for_review(actor=student, submission_id=sub.id)
    sub.refresh_from_db()
    return sub


@pytest.mark.django_db
def test_reviewer_only_sees_own_class_registrations_and_approves_own_class_outcomes(client, activity):
    """团支书按 年级+专业+班级 严格隔离：仅可见本班注册信息与成果，同班号跨年级/跨专业均拦截。"""
    reviewer = User.objects.create_user(
        username="tzs_is25_01",
        password="password123",
        real_name="李团支书",
        role=User.Role.REVIEWER,
        grade="2025级",
        major="信息安全",
        class_name="01班",
        phone="13800001000",
        must_change_password=False,
    )

    own_stu_submitted = _make_student(
        "08251001", "本班已交同学", grade="2025级", major="信息安全", class_name="01班", phone="13800001001"
    )
    own_stu_unsubmitted = _make_student(
        "08251002", "本班未交同学", grade="2025级", major="信息安全", class_name="01班", phone="13800001002"
    )
    other_class_stu = _make_student(
        "08251003", "同专业二班同学", grade="2025级", major="信息安全", class_name="02班", phone="13800001003"
    )
    other_grade_same_cls_stu = _make_student(
        "08261001", "跨年级一班同学", grade="2026级", major="信息安全", class_name="01班", phone="13800001004"
    )
    other_major_same_cls_stu = _make_student(
        "08252001", "跨专业一班同学", grade="2025级", major="软件工程", class_name="01班", phone="13800001005"
    )

    own_sub = _create_submitted_project(own_stu_submitted, activity, "本班安全科普项目")
    other_class_sub = _create_submitted_project(other_class_stu, activity, "二班实践项目")
    other_grade_sub = _create_submitted_project(other_grade_same_cls_stu, activity, "26级一班实践项目")
    other_major_sub = _create_submitted_project(other_major_same_cls_stu, activity, "软工一班实践项目")

    # 1. Selector 层面仅返回本班成果
    visible_ids = set(reviewable_submissions(reviewer, activity.id).values_list("id", flat=True))
    assert visible_ids == {own_sub.id}
    assert workbench_class_options(reviewer) == ["01班"]

    # 2. 仪表盘：同时看到本班成果列表 + 本班注册同学信息（不含跨班/跨年级/跨专业）
    client.force_login(reviewer)
    dash_resp = client.get(f"/?activity_id={activity.id}")
    assert dash_resp.status_code == 200
    dash_html = dash_resp.content.decode("utf-8")

    assert 'id="account-progress-section"' in dash_html
    assert 'data-account-row="08251001"' in dash_html
    assert 'data-account-row="08251002"' in dash_html
    assert "13800001001" in dash_html
    assert "13800001002" in dash_html
    assert "本班安全科普项目" in dash_html

    for outsider_sn in ("08251003", "08261001", "08252001"):
        assert f'data-account-row="{outsider_sn}"' not in dash_html
        assert outsider_sn not in dash_html
    for outsider_proj in ("二班实践项目", "26级一班实践项目", "软工一班实践项目"):
        assert outsider_proj not in dash_html

    # 尝试通过查询参数绕过查看外班注册信息
    bypass_resp = client.get(f"/?activity_id={activity.id}&acct_class=02班&acct_q=08251003")
    assert bypass_resp.status_code == 200
    bypass_html = bypass_resp.content.decode("utf-8")
    assert 'data-account-row="08251003"' not in bypass_html
    assert "同专业二班同学" not in bypass_html
    assert "13800001003" not in bypass_html

    # 3. 审核工作台与 JSON 列表接口仅返回本班成果
    wb_html = client.get(f"/reviews/workbench/?activity_id={activity.id}").content.decode("utf-8")
    assert "本班安全科普项目" in wb_html
    assert "二班实践项目" not in wb_html
    assert "26级一班实践项目" not in wb_html
    assert "软工一班实践项目" not in wb_html

    api_items = client.get(f"/api/v1/reviews/?activity_id={activity.id}").json()["items"]
    assert [item["id"] for item in api_items] == [str(own_sub.id)]

    # 4. 详情页 / 详情 API / 审批表单 / 审批 API：本班成功，跨班/跨年级/跨专业全部 403
    assert client.get(f"/reviews/submissions/{own_sub.id}/").status_code == 200
    assert client.get(f"/api/v1/reviews/{own_sub.id}/detail/").status_code == 200

    for forbidden_sub in (other_class_sub, other_grade_sub, other_major_sub):
        assert client.get(f"/reviews/submissions/{forbidden_sub.id}/").status_code == 403
        assert client.get(f"/api/v1/reviews/{forbidden_sub.id}/detail/").status_code == 403
        assert (
            client.post(
                f"/reviews/submissions/{forbidden_sub.id}/action/",
                {"decision": "approved", "comment": "越权尝试"},
            ).status_code
            == 403
        )
        assert (
            client.post(
                f"/api/v1/reviews/{forbidden_sub.id}/",
                data=json.dumps({"activity_id": str(activity.id), "decision": "approved"}),
                content_type="application/json",
            ).status_code
            == 403
        )
        with pytest.raises(Forbidden):
            review_submission(
                actor=reviewer,
                activity_id=activity.id,
                submission_id=forbidden_sub.id,
                decision="approved",
            )
        forbidden_sub.refresh_from_db()
        assert forbidden_sub.status == Submission.Status.PENDING_FIRST_REVIEW

    # 本班成果审批通过 -> 进入待终审
    ok_review = client.post(
        f"/reviews/submissions/{own_sub.id}/action/",
        {"decision": "approved", "comment": "本班材料齐全，初审通过"},
    )
    assert ok_review.status_code == 302
    own_sub.refresh_from_db()
    assert own_sub.status == Submission.Status.PENDING_FINAL_REVIEW

    # 5. 附件下载与删除权限：本班可下载，跨班/跨年级/跨专业一律 403
    own_upload = UploadSession.objects.filter(owner=own_stu_submitted, status=UploadSession.Status.COMPLETE).first()
    assert client.get(f"/api/v1/uploads/{own_upload.id}/file/").status_code == 200

    for outsider in (other_class_stu, other_grade_same_cls_stu, other_major_same_cls_stu):
        outsider_upload = UploadSession.objects.filter(owner=outsider, status=UploadSession.Status.COMPLETE).first()
        assert outsider_upload is not None
        assert client.get(f"/api/v1/uploads/{outsider_upload.id}/file/").status_code == 403
        assert client.post(f"/api/v1/uploads/{outsider_upload.id}/delete/").status_code == 403

    # 6. 同步导出 Excel、班级 ZIP 与异步整包 ZIP 均严格只含本班
    excel_resp = client.get(f"/api/v1/exports/activities/{activity.id}/excel/")
    assert excel_resp.status_code == 200
    wb = openpyxl.load_workbook(io.BytesIO(excel_resp.content))
    exported_sns = [row[0] for row in list(wb.active.iter_rows(values_only=True))[1:]]
    assert exported_sns == ["08251001"]

    assert client.get(f"/api/v1/exports/activities/{activity.id}/zip/", {"class_name": "02班"}).status_code == 403
    ok_zip = client.get(f"/api/v1/exports/activities/{activity.id}/zip/", {"class_name": "01班"})
    assert ok_zip.status_code == 200
    with zipfile.ZipFile(io.BytesIO(ok_zip.content)) as zf:
        names = zf.namelist()
        assert names
        assert all("08251001" in n for n in names)
        assert all("08261001" not in n and "08252001" not in n for n in names)

    start_resp = client.post(
        "/api/v1/exports/start/",
        data=json.dumps({"activity_id": str(activity.id), "mode": "declaration_and_outcomes"}),
        content_type="application/json",
    )
    assert start_resp.status_code == 202
    job_id = start_resp.json()["job_id"]
    job = ExportJob.objects.get(pk=job_id)
    assert job.status == ExportJob.Status.DONE
    dl_resp = client.get(f"/api/v1/exports/{job_id}/download/")
    assert dl_resp.status_code == 200
    with zipfile.ZipFile(io.BytesIO(b"".join(dl_resp.streaming_content))) as zf:
        async_names = zf.namelist()
        assert async_names
        assert all("08251001" in n for n in async_names)
        assert all(sn not in n for n in async_names for sn in ("08251003", "08261001", "08252001"))


@pytest.mark.django_db
def test_unassigned_reviewer_fails_closed(client, activity):
    """未绑定班级且无显式分配的团支书账号默认拒绝：看零条记录，审批与导出均返回 403。"""
    unassigned_reviewer = User.objects.create_user(
        username="tzs_unassigned",
        password="password123",
        real_name="未分班审核员",
        role=User.Role.REVIEWER,
        grade="",
        major="",
        class_name="",
        must_change_password=False,
    )
    stu = _make_student(
        "08259001", "某班同学", grade="2025级", major="信息安全", class_name="01班", phone="13800009001"
    )
    sub = _create_submitted_project(stu, activity, "不应泄露的项目")

    client.force_login(unassigned_reviewer)
    dash = client.get("/")
    assert dash.status_code == 200
    html = dash.content.decode("utf-8")
    assert "08259001" not in html
    assert "不应泄露的项目" not in html

    assert client.get(f"/reviews/submissions/{sub.id}/").status_code == 403
    assert (
        client.post(f"/reviews/submissions/{sub.id}/action/", {"decision": "approved"}).status_code == 403
    )
    assert client.get(f"/api/v1/exports/activities/{activity.id}/excel/").status_code == 403
    assert (
        client.post(
            "/api/v1/exports/start/",
            data=json.dumps({"activity_id": str(activity.id)}),
            content_type="application/json",
        ).status_code
        == 403
    )


@pytest.mark.django_db
def test_cross_class_team_member_does_not_leak_other_class_submission_link(client, activity):
    """本班同学作为外班队长团队队员时：本班团支书能看到其参报状态，但不暴露外班申报详情链接。"""
    reviewer = User.objects.create_user(
        username="tzs_cls02",
        password="password123",
        real_name="二班团支书",
        role=User.Role.REVIEWER,
        grade="2025级",
        major="信息安全",
        class_name="02班",
        must_change_password=False,
    )
    leader_cls01 = _make_student(
        "08253001", "一班队长", grade="2025级", major="信息安全", class_name="01班", phone="13800003001"
    )
    member_cls02 = _make_student(
        "08253002", "二班队员", grade="2025级", major="信息安全", class_name="02班", phone="13800003002"
    )

    leader_sub = _create_submitted_project(
        leader_cls01,
        activity,
        "跨班联合实践项目",
        team_members=[{"student_number": "08253002", "real_name": "二班队员", "class_name": "02班"}],
    )

    client.force_login(reviewer)
    dash_html = client.get(f"/?activity_id={activity.id}").content.decode("utf-8")
    assert 'data-account-row="08253002"' in dash_html
    assert "团队申报（队员）" in dash_html
    assert 'data-account-row="08253001"' not in dash_html
    assert str(leader_sub.id) not in dash_html
    assert client.get(f"/reviews/submissions/{leader_sub.id}/").status_code == 403
