"""多申报、独立团队与提交后覆盖修改端到端测试。"""
import io
import zipfile
from datetime import timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from apps.accounts.models import User
from apps.activities.models import Activity
from apps.exports.services import build_export_entries
from apps.reviews.services import review_submission
from apps.reviews.views import serialize_submission_for_review
from apps.submissions.models import Submission
from apps.submissions.services import create_draft, save_draft, submit_declaration, submit_for_review
from apps.uploads.models import UploadSession
from apps.uploads.services import (
    delete_uploaded_file,
    direct_upload,
    ensure_standard_material_requirements,
    get_accessible_upload_for_user,
    uploads_for_submission,
)
from tests.fixtures.samples import REAL_JPEG


def make_docx_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        zf.writestr("word/document.xml", '<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body/></w:document>')
    return buf.getvalue()


def make_pdf_bytes() -> bytes:
    return b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n"


def make_jpeg_bytes() -> bytes:
    return REAL_JPEG


def _upload_full_materials(actor, reqs, submission, *, prefix: str):
    direct_upload(
        actor=actor,
        requirement_id=reqs["application_form"].id,
        submission_id=submission.id,
        file_obj=SimpleUploadedFile(f"{prefix}_form.docx", make_docx_bytes(), content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    )
    direct_upload(
        actor=actor,
        requirement_id=reqs["practice_report"].id,
        submission_id=submission.id,
        file_obj=SimpleUploadedFile(f"{prefix}_report.pdf", make_pdf_bytes(), content_type="application/pdf"),
    )
    for idx in range(1, 4):
        direct_upload(
            actor=actor,
            requirement_id=reqs["scene_photos"].id,
            submission_id=submission.id,
            file_obj=SimpleUploadedFile(f"{prefix}_photo_{idx}.jpg", make_jpeg_bytes(), content_type="image/jpeg"),
        )


@pytest.mark.django_db
def test_student_can_submit_multiple_independent_submissions_with_isolated_uploads(client, student, activity):
    reqs = ensure_standard_material_requirements(activity)
    teammate = User.objects.create_user(
        username="2025090101",
        password="StrongPassword!123",
        real_name="队员甲",
        class_name="信安25-1班",
        role=User.Role.STUDENT,
        must_change_password=False,
    )

    # 第一份申报：个人项目
    sub1 = create_draft(actor=student, activity_id=activity.id, force_new=True)
    sub1 = save_draft(
        actor=student,
        submission_id=sub1.id,
        expected_version=sub1.version,
        data={
            "project_name": "个人社会调查项目",
            "category": "社会调查",
            "declaration_type": "individual",
            "team_members": [],
        },
    )
    _upload_full_materials(student, reqs, sub1, prefix="proj1")
    sub1 = submit_declaration(actor=student, submission_id=sub1.id)
    sub1, rev1 = submit_for_review(actor=student, submission_id=sub1.id)

    # 第二份申报：同一学生在同一个活动下再新建团队项目
    sub2 = create_draft(actor=student, activity_id=activity.id, force_new=True)
    assert sub2.id != sub1.id
    sub2 = save_draft(
        actor=student,
        submission_id=sub2.id,
        expected_version=sub2.version,
        data={
            "project_name": "团队科普宣讲项目",
            "category": "科普宣讲",
            "declaration_type": "team",
            "team_members": [
                {
                    "student_number": teammate.username,
                    "real_name": teammate.real_name,
                    "class_name": teammate.class_name,
                }
            ],
        },
    )
    _upload_full_materials(student, reqs, sub2, prefix="proj2")
    sub2 = submit_declaration(actor=student, submission_id=sub2.id)
    sub2, rev2 = submit_for_review(actor=student, submission_id=sub2.id)

    assert Submission.objects.filter(owner=student, activity=activity).count() == 2
    assert rev1.number == 1 and rev2.number == 1

    # 验证附件严格隔离：项目 1 和项目 2 各自 5 份有效附件，互不混杂、互不覆盖
    sub1_names = {up.original_name for up in uploads_for_submission(sub1)}
    sub2_names = {up.original_name for up in uploads_for_submission(sub2)}
    assert sub1_names == {"proj1_form.docx", "proj1_report.pdf", "proj1_photo_1.jpg", "proj1_photo_2.jpg", "proj1_photo_3.jpg"}
    assert sub2_names == {"proj2_form.docx", "proj2_report.pdf", "proj2_photo_1.jpg", "proj2_photo_2.jpg", "proj2_photo_3.jpg"}

    # 审核序列化各自只含对应附件
    s1_view = serialize_submission_for_review(sub1)
    s2_view = serialize_submission_for_review(sub2)
    assert {att["original_name"] for att in s1_view["attachments"]} == sub1_names
    assert {att["original_name"] for att in s2_view["attachments"]} == sub2_names

    # 全院导出目录区分两个项目，互不覆写（按 年级/班级/姓名+学号-项目名/标准材料名）
    entries = build_export_entries(activity=activity)
    assert len(entries) == 10
    arcnames = {e["arcname"] for e in entries}
    assert any("个人社会调查项目/实践报告.pdf" in name for name in arcnames)
    assert any("团队科普宣讲项目/实践报告.pdf" in name for name in arcnames)

    # 页面上“我的申报”展示两份申报且可点击新建与切换
    client.force_login(student)
    resp = client.get(f"/submissions/apply/?activity_id={activity.id}")
    assert resp.status_code == 200
    html = resp.content.decode("utf-8")
    assert "个人社会调查项目" in html
    assert "团队科普宣讲项目" in html
    assert f"submission_id={sub1.id}" in html
    assert f"submission_id={sub2.id}" in html


@pytest.mark.django_db
def test_same_student_can_join_different_teams_independently(student, other_student, activity):
    reqs = ensure_standard_material_requirements(activity)
    member_c = User.objects.create_user(
        username="2025090201",
        password="StrongPassword!123",
        real_name="跨队成员丙",
        class_name="信安25-1班",
        role=User.Role.STUDENT,
        must_change_password=False,
    )
    member_d = User.objects.create_user(
        username="2025090202",
        password="StrongPassword!123",
        real_name="成员丁",
        class_name="信安25-2班",
        role=User.Role.STUDENT,
        must_change_password=False,
    )

    # 队长 1 拉成员 C
    team1 = create_draft(actor=student, activity_id=activity.id, force_new=True)
    team1 = save_draft(
        actor=student,
        submission_id=team1.id,
        expected_version=team1.version,
        data={
            "project_name": "红色寻访团队一",
            "category": "红色寻访",
            "declaration_type": "team",
            "team_members": [
                {"student_number": member_c.username, "real_name": member_c.real_name, "class_name": member_c.class_name}
            ],
        },
    )
    _upload_full_materials(student, reqs, team1, prefix="t1")
    team1 = submit_declaration(actor=student, submission_id=team1.id)

    # 队长 2 也可以拉同一个成员 C 组建另一个完全独立的团队
    team2 = create_draft(actor=other_student, activity_id=activity.id, force_new=True)
    team2 = save_draft(
        actor=other_student,
        submission_id=team2.id,
        expected_version=team2.version,
        data={
            "project_name": "乡村振兴团队二",
            "category": "乡村振兴",
            "declaration_type": "team",
            "team_members": [
                {"student_number": member_c.username, "real_name": member_c.real_name, "class_name": member_c.class_name},
                {"student_number": member_d.username, "real_name": member_d.real_name, "class_name": member_d.class_name},
            ],
        },
    )
    _upload_full_materials(other_student, reqs, team2, prefix="t2")
    team2 = submit_declaration(actor=other_student, submission_id=team2.id)

    # 成员 C 自己也可以独立发起自己的第三个实践项目（个人或新团队）
    own_c = create_draft(actor=member_c, activity_id=activity.id, force_new=True)
    own_c = save_draft(
        actor=member_c,
        submission_id=own_c.id,
        expected_version=own_c.version,
        data={
            "project_name": "成员丙自主实践项目",
            "category": "志愿服务",
            "declaration_type": "team",
            "team_members": [
                {"student_number": member_d.username, "real_name": member_d.real_name, "class_name": member_d.class_name}
            ],
        },
    )
    _upload_full_materials(member_c, reqs, own_c, prefix="tc")
    own_c = submit_declaration(actor=member_c, submission_id=own_c.id)

    # 成员 C 有权访问团队 1 和团队 2 的附件，成员 D 无权访问团队 1 的附件
    t1_upload = uploads_for_submission(team1, requirement_code="application_form")[0]
    t2_upload = uploads_for_submission(team2, requirement_code="application_form")[0]
    assert get_accessible_upload_for_user(actor=member_c, upload_or_asset_id=t1_upload.id)[0].id == t1_upload.id
    assert get_accessible_upload_for_user(actor=member_c, upload_or_asset_id=t2_upload.id)[0].id == t2_upload.id

    from apps.common.errors import Forbidden

    with pytest.raises(Forbidden):
        get_accessible_upload_for_user(actor=member_d, upload_or_asset_id=t1_upload.id)


@pytest.mark.django_db
def test_overwrite_submitted_declaration_and_outcomes_in_place(client, student, activity):
    reqs = ensure_standard_material_requirements(activity)
    admin_user = User.objects.create_user(
        username="admin_overwrite",
        password="StrongPassword!123",
        real_name="终审老师",
        role=User.Role.ADMIN,
        must_change_password=False,
    )

    sub = create_draft(actor=student, activity_id=activity.id, force_new=True)
    sub = save_draft(
        actor=student,
        submission_id=sub.id,
        expected_version=sub.version,
        data={
            "project_name": "初始项目名",
            "category": "社会调查",
            "declaration_type": "individual",
            "team_members": [],
        },
    )
    _upload_full_materials(student, reqs, sub, prefix="v1")
    sub = submit_declaration(actor=student, submission_id=sub.id)
    sub, rev1 = submit_for_review(actor=student, submission_id=sub.id)
    assert sub.status == Submission.Status.PENDING_FIRST_REVIEW

    # 1) 待初审阶段（尚未通过初审）：允许学生自行修改并覆盖原提交
    sub = save_draft(
        actor=student,
        submission_id=sub.id,
        expected_version=sub.version,
        data={
            "project_name": "待初审修改后的项目名",
            "category": "文化传承",
            "declaration_type": "individual",
            "team_members": [],
        },
    )
    direct_upload(
        actor=student,
        requirement_id=reqs["application_form"].id,
        submission_id=sub.id,
        file_obj=SimpleUploadedFile("v2_form.docx", make_docx_bytes(), content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    )
    sub = submit_declaration(actor=student, submission_id=sub.id)
    assert Submission.objects.filter(owner=student, activity=activity).count() == 1
    assert sub.draft_data["project_name"] == "待初审修改后的项目名"
    assert sub.status == Submission.Status.PENDING_FIRST_REVIEW
    assert [u.original_name for u in uploads_for_submission(sub, requirement_code="application_form")] == ["v2_form.docx"]

    # 2) 初审通过后进入待终审：学生不得自行修改草稿、上传或删除附件、重交申报或成果
    from apps.common.errors import Conflict, DomainError
    from apps.reviews.models import ReviewAssignment

    reviewer = User.objects.create_user(
        username="rev_return_check",
        password="StrongPassword!123",
        real_name="团支书",
        role=User.Role.REVIEWER,
        class_name=student.class_name,
        must_change_password=False,
    )
    ReviewAssignment.objects.create(reviewer=reviewer, activity=activity, class_name=student.class_name)

    review_submission(actor=reviewer, activity_id=activity.id, submission_id=sub.id, decision="approved")
    sub.refresh_from_db()
    assert sub.status == Submission.Status.PENDING_FINAL_REVIEW

    with pytest.raises(Conflict):
        save_draft(
            actor=student,
            submission_id=sub.id,
            expected_version=sub.version,
            data={"project_name": "初审通过后强改", "category": "文化传承", "declaration_type": "individual", "team_members": []},
        )
    with pytest.raises(Conflict):
        direct_upload(
            actor=student,
            requirement_id=reqs["practice_report"].id,
            submission_id=sub.id,
            file_obj=SimpleUploadedFile("blocked_report.pdf", make_pdf_bytes(), content_type="application/pdf"),
        )
    with pytest.raises(Conflict):
        delete_uploaded_file(actor=student, upload_id=uploads_for_submission(sub, requirement_code="scene_photos")[0].id)
    with pytest.raises(Conflict):
        submit_for_review(actor=student, submission_id=sub.id)

    # 学生端页面呈现只读锁定
    client.force_login(student)
    assert 'data-readonly-locked="true"' in client.get(f"/?activity_id={activity.id}&submission_id={sub.id}").content.decode("utf-8")
    assert 'data-upload-locked="true"' in client.get(f"/submissions/apply/?activity_id={activity.id}&submission_id={sub.id}").content.decode("utf-8")
    assert 'data-upload-locked="true"' in client.get(f"/submissions/outcomes/?activity_id={activity.id}&submission_id={sub.id}").content.decode("utf-8")

    # 3) 终审通过后依然锁死；未完成二次确认不可打回
    review_submission(actor=admin_user, activity_id=activity.id, submission_id=sub.id, decision="approved")
    sub.refresh_from_db()
    assert sub.status == Submission.Status.FINAL_APPROVED

    with pytest.raises(Conflict):
        save_draft(
            actor=student,
            submission_id=sub.id,
            expected_version=sub.version,
            data={"project_name": "终审通过后强改", "category": "文化传承", "declaration_type": "individual", "team_members": []},
        )

    # 未传二次确认 -> 400 拒绝打回
    with pytest.raises(DomainError):
        review_submission(
            actor=admin_user,
            activity_id=activity.id,
            submission_id=sub.id,
            decision="returned",
            comment="需补充佐证材料",
            confirm_approved_return=False,
        )

    # 拥有初审权限的人员（团支书）经过二次确认也可将终审通过的成果打回
    review_submission(
        actor=reviewer,
        activity_id=activity.id,
        submission_id=sub.id,
        decision="returned",
        comment="经核查需补充佐证材料",
        confirm_approved_return=True,
    )
    sub.refresh_from_db()
    assert sub.status == Submission.Status.RETURNED

    # 4) 被打回后解除锁定：学生恢复修改并替换附件重新提交
    direct_upload(
        actor=student,
        requirement_id=reqs["practice_report"].id,
        submission_id=sub.id,
        file_obj=SimpleUploadedFile("v2_report.pdf", make_pdf_bytes(), content_type="application/pdf"),
    )
    old_photos = uploads_for_submission(sub, requirement_code="scene_photos")
    delete_uploaded_file(actor=student, upload_id=old_photos[0].id)
    direct_upload(
        actor=student,
        requirement_id=reqs["scene_photos"].id,
        submission_id=sub.id,
        file_obj=SimpleUploadedFile("v2_photo_new.jpg", make_jpeg_bytes(), content_type="image/jpeg"),
    )
    sub, rev_latest = submit_for_review(actor=student, submission_id=sub.id)

    assert Submission.objects.filter(owner=student, activity=activity).count() == 1
    assert rev_latest.number > rev1.number
    assert sub.status == Submission.Status.PENDING_FIRST_REVIEW
    assert [u.original_name for u in uploads_for_submission(sub, requirement_code="practice_report")] == ["v2_report.pdf"]
    photo_names = {u.original_name for u in uploads_for_submission(sub, requirement_code="scene_photos")}
    assert "v2_photo_new.jpg" in photo_names
    assert len(photo_names) == 3

    # 5) 验证详情页与表单 POST 二次确认链路（未带 confirm_return -> 400，带 confirm_return=1 -> 302 成功打回）
    review_submission(actor=reviewer, activity_id=activity.id, submission_id=sub.id, decision="approved")
    sub.refresh_from_db()
    assert sub.status == Submission.Status.PENDING_FINAL_REVIEW

    client.force_login(reviewer)
    detail_html = client.get(f"/reviews/submissions/{sub.id}/").content.decode("utf-8")
    assert 'data-requires-confirm="true"' in detail_html
    assert "打回修改" in detail_html
    no_confirm_resp = client.post(
        f"/reviews/submissions/{sub.id}/action/",
        {"decision": "returned", "comment": "未二次确认测试"},
    )
    assert no_confirm_resp.status_code == 400
    confirmed_resp = client.post(
        f"/reviews/submissions/{sub.id}/action/",
        {"decision": "returned", "comment": "二次确认后打回", "confirm_return": "1"},
    )
    assert confirmed_resp.status_code == 302
    sub.refresh_from_db()
    assert sub.status == Submission.Status.RETURNED

    # 页面路由：点击“新建申报”创建独立第 2 条记录
    client.force_login(student)
    post_resp = client.post("/submissions/new/", {"activity_id": str(activity.id)})
    assert post_resp.status_code == 302
    assert Submission.objects.filter(owner=student, activity=activity).count() == 2


@pytest.mark.django_db
def test_closed_activity_still_locks_submission(student):
    closed_activity = Activity.objects.create(
        title="已截止活动",
        opens_at=timezone.now() - timedelta(days=10),
        deadline=timezone.now() - timedelta(days=1),
        is_open=True,
    )
    sub = Submission.objects.create(
        owner=student,
        activity=closed_activity,
        status=Submission.Status.PENDING_FIRST_REVIEW,
        draft_data={"project_name": "已截止项目", "category": "社会调查", "declaration_type": "individual", "team_members": []},
    )
    from apps.common.errors import Conflict

    with pytest.raises(Conflict):
        save_draft(
            actor=student,
            submission_id=sub.id,
            expected_version=sub.version,
            data={"project_name": "企图篡改", "category": "社会调查", "declaration_type": "individual", "team_members": []},
        )


@pytest.mark.django_db
def test_dashboard_renders_independent_progress_card_per_submission(client, student, activity):
    reqs = ensure_standard_material_requirements(activity)

    # 项目 1：已完成申报 + 成果提交 -> 进度 2/2
    sub1 = create_draft(actor=student, activity_id=activity.id, force_new=True)
    sub1 = save_draft(
        actor=student,
        submission_id=sub1.id,
        expected_version=sub1.version,
        data={"project_name": "独立进度项目一", "category": "社会调查", "declaration_type": "individual", "team_members": []},
    )
    _upload_full_materials(student, reqs, sub1, prefix="p1")
    sub1 = submit_declaration(actor=student, submission_id=sub1.id)
    sub1, _ = submit_for_review(actor=student, submission_id=sub1.id)

    client.force_login(student)
    dash1 = client.get(f"/?activity_id={activity.id}").content.decode("utf-8")
    assert dash1.count("data-progress-submission=") == 1
    assert f'data-progress-submission="{sub1.id}"' in dash1
    assert "独立进度项目一" in dash1
    assert "2/2" in dash1

    # 项目 2：仅完成立项申报 -> 进度 1/2
    sub2 = create_draft(actor=student, activity_id=activity.id, force_new=True)
    sub2 = save_draft(
        actor=student,
        submission_id=sub2.id,
        expected_version=sub2.version,
        data={"project_name": "独立进度项目二", "category": "科普宣讲", "declaration_type": "individual", "team_members": []},
    )
    direct_upload(
        actor=student,
        requirement_id=reqs["application_form"].id,
        submission_id=sub2.id,
        file_obj=SimpleUploadedFile("p2_form.docx", make_docx_bytes(), content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    )
    sub2 = submit_declaration(actor=student, submission_id=sub2.id)

    # 项目 3：刚新建草稿 -> 进度 0/2
    sub3 = create_draft(actor=student, activity_id=activity.id, force_new=True)

    dash3 = client.get(f"/?activity_id={activity.id}").content.decode("utf-8")
    assert dash3.count("data-progress-submission=") == 3
    assert f'data-progress-submission="{sub1.id}"' in dash3
    assert f'data-progress-submission="{sub2.id}"' in dash3
    assert f'data-progress-submission="{sub3.id}"' in dash3
    assert "2/2" in dash3
    assert "1/2" in dash3
    assert "0/2" in dash3


@pytest.mark.django_db
def test_delete_submission_allowed_before_first_review_approval_and_blocked_after(
    client, student, other_student, activity, django_capture_on_commit_callbacks
):
    from apps.uploads.models import StorageQuota
    from apps.uploads.services import resolve_file_disk_path

    reqs = ensure_standard_material_requirements(activity)
    admin_user = User.objects.create_user(
        username="admin_del_check",
        password="StrongPassword!123",
        real_name="审核管理员",
        role=User.Role.ADMIN,
        must_change_password=False,
    )

    # 项目 A：已提交待初审（未通过初审 -> 允许删除，且应级联释放附件与配额）
    sub_a = create_draft(actor=student, activity_id=activity.id, force_new=True)
    sub_a = save_draft(
        actor=student,
        submission_id=sub_a.id,
        expected_version=sub_a.version,
        data={"project_name": "待初审可删项目", "category": "社会调查", "declaration_type": "individual", "team_members": []},
    )
    _upload_full_materials(student, reqs, sub_a, prefix="del_a")
    sub_a = submit_declaration(actor=student, submission_id=sub_a.id)
    sub_a, _ = submit_for_review(actor=student, submission_id=sub_a.id)
    sub_a_paths = [resolve_file_disk_path(u.object_key) for u in uploads_for_submission(sub_a)]
    assert all(p.exists() for p in sub_a_paths)

    # 项目 B：提交并初审通过 -> 待终审（已通过初审 -> 不可删除）
    sub_b = create_draft(actor=student, activity_id=activity.id, force_new=True)
    sub_b = save_draft(
        actor=student,
        submission_id=sub_b.id,
        expected_version=sub_b.version,
        data={"project_name": "已过初审不可删项目", "category": "文化传承", "declaration_type": "individual", "team_members": []},
    )
    _upload_full_materials(student, reqs, sub_b, prefix="keep_b")
    sub_b = submit_declaration(actor=student, submission_id=sub_b.id)
    sub_b, _ = submit_for_review(actor=student, submission_id=sub_b.id)
    review_submission(actor=admin_user, activity_id=activity.id, submission_id=sub_b.id, decision="approved")
    sub_b.refresh_from_db()
    assert sub_b.status == Submission.Status.PENDING_FINAL_REVIEW

    # 项目 C：初审被打回（未通过初审 -> 允许删除）
    sub_c = create_draft(actor=student, activity_id=activity.id, force_new=True)
    sub_c = save_draft(
        actor=student,
        submission_id=sub_c.id,
        expected_version=sub_c.version,
        data={"project_name": "初审打回可删项目", "category": "志愿服务", "declaration_type": "individual", "team_members": []},
    )
    _upload_full_materials(student, reqs, sub_c, prefix="del_c")
    sub_c = submit_declaration(actor=student, submission_id=sub_c.id)
    sub_c, _ = submit_for_review(actor=student, submission_id=sub_c.id)
    review_submission(actor=admin_user, activity_id=activity.id, submission_id=sub_c.id, decision="returned", comment="请补充照片")
    sub_c.refresh_from_db()
    assert sub_c.status == Submission.Status.RETURNED

    # 1) 越权检查：他人尝试删除返回 404
    client.force_login(other_student)
    assert client.delete(f"/api/v1/submissions/{sub_a.id}/").status_code == 404
    assert Submission.objects.filter(pk=sub_a.id).exists()

    # 2) 页面渲染检查：A 和 C 有删除按钮，B 没有删除按钮
    client.force_login(student)
    dash_html = client.get(f"/?activity_id={activity.id}").content.decode("utf-8")
    apply_html = client.get(f"/submissions/apply/?activity_id={activity.id}").content.decode("utf-8")
    for html in (dash_html, apply_html):
        assert f'data-delete-submission="{sub_a.id}"' in html
        assert f'data-delete-submission="{sub_c.id}"' in html
        assert f'data-delete-submission="{sub_b.id}"' not in html

    # 3) 通过页面 POST 删除待初审项目 A：记录、附件、磁盘文件全部清理，且不影响项目 B 附件，配额准确扣减
    quota_before = StorageQuota.objects.get(owner=student, activity=activity).used_bytes
    with django_capture_on_commit_callbacks(execute=True):
        del_a_resp = client.post(
            f"/submissions/{sub_a.id}/delete/",
            {"activity_id": str(activity.id), "next": "dashboard"},
        )
    assert del_a_resp.status_code == 302
    assert not Submission.objects.filter(pk=sub_a.id).exists()
    assert all(not p.exists() for p in sub_a_paths)
    assert len(uploads_for_submission(sub_b)) == 5
    quota_after = StorageQuota.objects.get(owner=student, activity=activity).used_bytes
    assert quota_after < quota_before

    # 4) 通过 REST API DELETE 删除初审打回项目 C：成功清理（包括关联 ReviewRecord 与 SubmissionRevision）
    del_c_resp = client.delete(f"/api/v1/submissions/{sub_c.id}/")
    assert del_c_resp.status_code == 200
    assert not Submission.objects.filter(pk=sub_c.id).exists()

    # 5) 尝试删除已通过初审的待终审项目 B -> 409 拒绝
    del_b_resp = client.delete(f"/api/v1/submissions/{sub_b.id}/")
    assert del_b_resp.status_code == 409
    assert Submission.objects.filter(pk=sub_b.id).exists()

    # 6) 即使项目 B 随后在终审经二次确认被打回（仍包含初审通过记录），也不允许删除
    review_submission(
        actor=admin_user,
        activity_id=activity.id,
        submission_id=sub_b.id,
        decision="returned",
        comment="终审意见",
        confirm_approved_return=True,
    )
    sub_b.refresh_from_db()
    assert sub_b.status == Submission.Status.RETURNED
    assert client.delete(f"/api/v1/submissions/{sub_b.id}/").status_code == 409
    assert Submission.objects.filter(pk=sub_b.id).exists()
