import pytest
from apps.accounts.admin import PlatformUserAdmin
from apps.accounts.models import User, StudentRoster
from apps.accounts.services import register_student
from apps.common.errors import Conflict, DomainError, Forbidden
from apps.reviews.models import ReviewAssignment, ReviewRecord
from apps.reviews.services import review_submission
from apps.submissions.models import Submission, SubmissionRevision
from apps.submissions.services import create_draft, save_draft, submit_for_review
from tests.conftest import seed_mandatory_materials
from django.contrib.admin.sites import AdminSite


# =====================================================================
# 1. 账号注册与鉴权逻辑测试 (apps/accounts/)
# =====================================================================

def test_student_registration_success(db):
    """测试学生自主注册成功，密码哈希加密，角色强制写死为 student。"""
    user = register_student(
        student_number="20250101",
        real_name="张三",
        class_name="计算机25-1班",
        password="plain_password_123",
    )
    assert user.username == "20250101"
    assert user.real_name == "张三"
    assert user.class_name == "计算机25-1班"
    assert user.role == User.Role.STUDENT
    # 验证密码经过哈希加密
    assert user.password != "plain_password_123"
    assert user.check_password("plain_password_123") is True


def test_student_registration_forces_student_role_ignoring_frontend_tamper(client, db):
    """测试通过 API 注册时，即使前端传了 role='college_admin'，后端也强制写死为 student。"""
    payload = {
        "student_number": "20250102",
        "real_name": "李四",
        "class_name": "计算机25-2班",
        "password": "mypassword888",
        "role": "college_admin",  # 试图提权
        "is_staff": True,
        "is_superuser": True,
    }
    response = client.post("/api/v1/accounts/register/", payload, content_type="application/json")
    assert response.status_code == 201
    user = User.objects.get(username="20250102")
    assert user.role == User.Role.STUDENT
    assert user.is_staff is False
    assert user.is_superuser is False


def test_registration_duplicate_student_number_fails(db):
    """测试学号查重逻辑：重复学号注册抛出 409 Conflict。"""
    register_student(
        student_number="20250103",
        real_name="王五",
        class_name="信安25-1班",
        password="password1",
    )
    with pytest.raises(Conflict):
        register_student(
            student_number="20250103",
            real_name="王五重名",
            class_name="信安25-2班",
            password="password2",
        )


def test_registration_validation_rules(db):
    """测试注册字段非空和格式校验。"""
    with pytest.raises(DomainError):
        register_student(student_number="", real_name="测试", class_name="班级", password="123456")
    with pytest.raises(DomainError):
        register_student(student_number="20259999", real_name="", class_name="班级", password="123456")
    with pytest.raises(DomainError):
        register_student(student_number="20259999", real_name="测试", class_name="", password="123456")
    with pytest.raises(DomainError):
        register_student(student_number="20259999", real_name="测试", class_name="班级", password="123")


def test_admin_actions_and_search_fields(db):
    """测试 Django Admin 后台搜索学号及一键提权 Action。"""
    admin = PlatformUserAdmin(User, AdminSite())
    assert "username" in admin.search_fields

    u1 = User.objects.create_user(username="20250110", password="pwd", role=User.Role.STUDENT)
    u2 = User.objects.create_user(username="20250111", password="pwd", role=User.Role.STUDENT)

    # 1. 一键提权为审核人员（团支书）
    admin.promote_to_reviewer(None, User.objects.filter(pk=u1.pk))
    u1.refresh_from_db()
    assert u1.role == User.Role.REVIEWER

    # 2. 一键提权为学院管理员
    admin.promote_to_admin(None, User.objects.filter(pk=u2.pk))
    u2.refresh_from_db()
    assert u2.role == User.Role.ADMIN
    assert u2.is_staff is True

    # 3. 一键重置为普通学生
    admin.demote_to_student(None, User.objects.filter(pk=u2.pk))
    u2.refresh_from_db()
    assert u2.role == User.Role.STUDENT
    assert u2.is_staff is False


# =====================================================================
# 2. 材料提交与原草稿覆盖机制测试 (apps/submissions/)
# =====================================================================

def test_submission_overwrites_same_row_and_creates_snapshot(student, activity):
    """学生多次重复保存直接更新原数据行，点击正式提交时生成不可变快照并将状态变更为待初审。"""
    draft = create_draft(actor=student, activity_id=activity.pk)
    assert draft.status == Submission.Status.DRAFT
    assert draft.version == 0

    # 第一次编辑草稿
    save_draft(actor=student, submission_id=draft.pk, expected_version=0, data={"title": "草稿V1", "content": "初版内容"})
    draft.refresh_from_db()
    assert draft.version == 1
    assert draft.draft_data["title"] == "草稿V1"

    # 第二次编辑草稿（覆盖原数据行，记录总数仍为 1）
    save_draft(actor=student, submission_id=draft.pk, expected_version=1, data={"title": "草稿V2", "content": "新版覆盖"})
    draft.refresh_from_db()
    assert draft.version == 2
    assert draft.draft_data["title"] == "草稿V2"
    assert Submission.objects.filter(owner=student, activity=activity).count() == 1

    # 点击正式提交（先上传齐必交材料）
    seed_mandatory_materials(student, activity)
    sub, rev = submit_for_review(actor=student, submission_id=draft.pk)
    assert sub.status == Submission.Status.PENDING_FIRST_REVIEW
    assert sub.version == 3
    assert rev.number == 1
    assert rev.snapshot == {"title": "草稿V2", "content": "新版覆盖"}
    assert sub.revisions.count() == 1

    # 待初审状态下，不允许再次保存草稿或再次提交
    with pytest.raises(Conflict):
        save_draft(actor=student, submission_id=sub.pk, expected_version=3, data={"title": "篡改"})
    with pytest.raises(Conflict):
        submit_for_review(actor=student, submission_id=sub.pk)


def test_cannot_submit_empty_draft(student, activity):
    """空草稿点击正式提交报错。"""
    draft = create_draft(actor=student, activity_id=activity.pk)
    with pytest.raises(DomainError):
        submit_for_review(actor=student, submission_id=draft.pk)


# =====================================================================
# 3. 两级审核状态机流转与打回修改测试 (apps/reviews/)
# =====================================================================

def test_two_stage_review_state_machine_full_happy_path(student, activity):
    """
    状态机正常流转：
    草稿 (draft) -> 待初审 (pending_first_review) -> 初审通过 (pending_final_review) -> 终审通过 (final_approved)
    """
    draft = create_draft(actor=student, activity_id=activity.pk)
    save_draft(actor=student, submission_id=draft.pk, expected_version=0, data={"title": "实践报告"})
    seed_mandatory_materials(student, activity)
    sub, rev = submit_for_review(actor=student, submission_id=draft.pk)
    assert sub.status == Submission.Status.PENDING_FIRST_REVIEW

    # 团支书（初审人员）
    reviewer = User.objects.create_user(username="reviewer01", password="pwd", role=User.Role.REVIEWER)
    ReviewAssignment.objects.create(reviewer=reviewer, activity=activity, class_name=student.class_name)

    # 学院老师（管理员，终审人员）
    teacher = User.objects.create_user(username="teacher01", password="pwd", role=User.Role.ADMIN, is_staff=True)

    # 1. 团支书初审通过
    rec1, sub1 = review_submission(
        actor=reviewer,
        activity_id=activity.pk,
        submission_id=sub.pk,
        decision="approved",
        comment="团支部初审符合规范",
    )
    assert sub1.status == Submission.Status.PENDING_FINAL_REVIEW
    assert rec1.stage == ReviewRecord.Stage.FIRST
    assert rec1.decision == "approved"

    # 团支书无权进行终审
    with pytest.raises(Forbidden):
        review_submission(
            actor=reviewer,
            activity_id=activity.pk,
            submission_id=sub.pk,
            decision="approved",
        )

    # 2. 老师终审通过
    rec2, sub2 = review_submission(
        actor=teacher,
        activity_id=activity.pk,
        submission_id=sub.pk,
        decision="approved",
        comment="学院终审通过",
    )
    assert sub2.status == Submission.Status.FINAL_APPROVED
    assert rec2.stage == ReviewRecord.Stage.FINAL
    assert rec2.decision == "approved"

    # 终审通过后不能再次审核
    with pytest.raises(Conflict):
        review_submission(actor=teacher, activity_id=activity.pk, submission_id=sub.pk, decision="approved")


def test_review_rejection_requires_comment_and_allows_student_resubmit(student, activity):
    """
    测试强制校验：
    打回时必须强制填写打回意见，原因不能为空；
    打回后自动将材料状态变更为打回修改，允许学生重新编辑原草稿并再次正式提交。
    """
    draft = create_draft(actor=student, activity_id=activity.pk)
    save_draft(actor=student, submission_id=draft.pk, expected_version=0, data={"title": "初版报告"})
    seed_mandatory_materials(student, activity)
    sub, _ = submit_for_review(actor=student, submission_id=draft.pk)

    reviewer = User.objects.create_user(username="reviewer02", password="pwd", role=User.Role.REVIEWER)
    ReviewAssignment.objects.create(reviewer=reviewer, activity=activity, class_name=student.class_name)

    # 1. 打回时原因未填写 -> 强制校验报错
    with pytest.raises(DomainError, match="初审打回时必须填写打回意见"):
        review_submission(
            actor=reviewer,
            activity_id=activity.pk,
            submission_id=sub.pk,
            decision="returned",
            comment="   ",  # 空格无效
        )

    # 2. 打回并填写具体意见 -> 状态变更为打回修改 (returned)
    rec, sub = review_submission(
        actor=reviewer,
        activity_id=activity.pk,
        submission_id=sub.pk,
        decision="returned",
        comment="缺少实践活动现场高清照片",
    )
    assert sub.status == Submission.Status.RETURNED
    assert rec.decision == "returned"
    assert rec.comment == "缺少实践活动现场高清照片"

    # 3. 此时学生可以重新编辑原草稿（覆盖原数据行）
    sub.refresh_from_db()
    save_draft(
        actor=student,
        submission_id=sub.pk,
        expected_version=sub.version,
        data={"title": "初版报告", "photos": ["photo1.jpg", "photo2.jpg"]},
    )
    sub.refresh_from_db()
    assert sub.draft_data["photos"] == ["photo1.jpg", "photo2.jpg"]
    assert Submission.objects.filter(owner=student, activity=activity).count() == 1

    # 4. 学生再次点击正式提交 -> 生成第二版快照，状态重新变更为待初审
    sub, rev2 = submit_for_review(actor=student, submission_id=sub.pk)
    assert sub.status == Submission.Status.PENDING_FIRST_REVIEW
    assert rev2.number == 2
    assert rev2.snapshot["photos"] == ["photo1.jpg", "photo2.jpg"]
    assert sub.revisions.count() == 2
