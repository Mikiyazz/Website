from datetime import timedelta
import pytest
from django.contrib.admin.sites import AdminSite
from django.utils import timezone
from apps.accounts.admin import PlatformUserAdmin
from apps.accounts.models import StudentRoster, User
from apps.accounts.services import (
    batch_initialize_students,
    register_student,
)
from apps.activities.admin import ActivityAdmin
from apps.activities.models import Activity
from apps.activities.selectors import ensure_activity_open
from apps.common.errors import Conflict
from apps.submissions.models import Submission
from apps.submissions.services import create_draft, save_draft, submit_for_review
from tests.conftest import seed_mandatory_materials


# =====================================================================
# 1. 账号体系完善：扩充字段、默认密码后6位、改密闭环与 Admin 重置密码
# =====================================================================

def test_register_student_default_password_last_six_and_extended_fields(db):
    """未传自定义密码时，默认初始密码为学号后 6 位，must_change_password=True，且支持年级/专业/手机号。"""
    user = register_student(
        student_number="2025080901",
        real_name="周同学",
        class_name="信安25-1班",
        grade=User.Grade.SOPHOMORE,
        major="信息安全",
        phone="13800138000",
    )
    assert user.username == "2025080901"
    assert user.grade == "大二"
    assert user.major == "信息安全"
    assert user.phone == "13800138000"
    assert user.must_change_password is True
    assert user.check_password("080901") is True


def test_batch_initialize_students_and_all_grades(db):
    """验证大一至大四及研究生枚举覆盖，以及后台批量初始化默认密码为学号后 6 位。"""
    expected_grades = ["大一", "大二", "大三", "大四", "研究生"]
    assert list(User.Grade.values) == expected_grades

    r1 = StudentRoster.objects.create(
        student_number="2024123456",
        real_name="王研",
        class_name="计科研24-1班",
        grade=User.Grade.GRADUATE,
        major="计算机科学与技术",
        phone="13900001111",
    )
    created = batch_initialize_students([r1])
    assert len(created) == 1
    u = created[0]
    assert u.username == "2024123456"
    assert u.grade == "研究生"
    assert u.major == "计算机科学与技术"
    assert u.phone == "13900001111"
    assert u.must_change_password is True
    assert u.check_password("123456") is True


def test_change_password_api_and_admin_reset_action(client, db):
    """验证修改密码接口将 must_change_password 置为 False，以及 Admin 一键重置密码为学号后 6 位。"""
    user = register_student(
        student_number="2025112233",
        real_name="林同学",
        class_name="计算机25-1班",
        grade=User.Grade.FRESHMAN,
        major="计算机科学与技术",
        phone="13711112222",
    )
    assert user.must_change_password is True
    assert user.check_password("112233") is True

    client.force_login(user)
    response = client.post(
        "/api/v1/accounts/change-password/",
        {"old_password": "112233", "new_password": "NewSecure#2026"},
        content_type="application/json",
    )
    assert response.status_code == 200
    payload = response.json()["user"]
    assert payload["must_change_password"] is False

    user.refresh_from_db()
    assert user.must_change_password is False
    assert user.check_password("NewSecure#2026") is True

    # 原密码错误时应拦截
    bad_resp = client.post(
        "/api/v1/accounts/change-password/",
        {"old_password": "wrong-password", "new_password": "AnotherPassword#1"},
        content_type="application/json",
    )
    assert bad_resp.status_code == 400

    # Admin Action：一键重置密码为学号后 6 位
    admin_instance = PlatformUserAdmin(User, AdminSite())
    assert "reset_password_to_last_six" in admin_instance.actions
    admin_instance.reset_password_to_last_six(None, User.objects.filter(pk=user.pk))

    user.refresh_from_db()
    assert user.must_change_password is True
    assert user.check_password("112233") is True


# =====================================================================
# 2. 活动时间动态控制与补交白名单 (apps/activities/)
# =====================================================================

def test_activity_deadline_blocks_regular_student_but_allows_whitelisted_student(db):
    """活动截止后普通学生拦截，而补交白名单内的学生可正常创建/保存草稿并提交审核。"""
    normal_student = User.objects.create_user(
        username="2025001001",
        password="password123",
        real_name="普通学生",
        class_name="信安25-1班",
    )
    whitelist_student = User.objects.create_user(
        username="2025001002",
        password="password123",
        real_name="白名单补交生",
        class_name="信安25-1班",
    )

    now = timezone.now()
    # 先在开放期内创建活动
    act = Activity.objects.create(
        title="暑期社会实践申报",
        opens_at=now - timedelta(days=5),
        deadline=now + timedelta(days=1),
        is_open=True,
        late_whitelist_student_numbers=["2025001002"],
    )

    # Admin 配置检查
    act_admin = ActivityAdmin(Activity, AdminSite())
    assert "late_whitelist_student_numbers" in act_admin.fields

    # 开放期内两名学生均可创建草稿
    normal_draft = create_draft(actor=normal_student, activity_id=act.pk)
    whitelist_draft = create_draft(actor=whitelist_student, activity_id=act.pk)

    # 管理员后台将 deadline 调整为已过期
    act.deadline = now - timedelta(hours=1)
    act.save(update_fields=["deadline", "updated_at"])

    # 普通学生在截止后保存/提交均被拦截
    with pytest.raises(Conflict, match="活动尚未开放或已截止"):
        ensure_activity_open(act, normal_student)
    with pytest.raises(Conflict, match="活动尚未开放或已截止"):
        save_draft(
            actor=normal_student,
            submission_id=normal_draft.pk,
            expected_version=0,
            data={
                "project_name": "逾期项目",
                "category": "社会调查",
                "declaration_type": "individual",
                "team_members": [],
            },
        )

    # 白名单学生在截止后可正常保存草稿并正式提交
    ensure_activity_open(act, whitelist_student)
    saved = save_draft(
        actor=whitelist_student,
        submission_id=whitelist_draft.pk,
        expected_version=0,
        data={
            "project_name": "白名单补交项目",
            "category": "社会调查",
            "declaration_type": "individual",
            "team_members": [],
        },
    )
    assert saved.version == 1
    seed_mandatory_materials(whitelist_student, act)
    sub, rev = submit_for_review(actor=whitelist_student, submission_id=whitelist_draft.pk)
    assert sub.status == Submission.Status.PENDING_FIRST_REVIEW
    assert rev.number == 1


# =====================================================================
# 3. 个人/团队申报兼容与同活动跨项目防重复进队校验 (apps/submissions/)
# =====================================================================

def test_duplicate_student_number_across_submissions_in_same_activity_blocked(activity, db):
    """同一活动内，无论作为申报人 owner 还是作为其他项目 team_members，均不可重复出现在多个有效申报中。"""
    leader_a = User.objects.create_user(
        username="2025002001",
        password="password123",
        real_name="队长A",
        class_name="计算机一班",
    )
    leader_b = User.objects.create_user(
        username="2025002002",
        password="password123",
        real_name="队长B",
        class_name="计算机一班",
    )
    member_c = User.objects.create_user(
        username="2025002003",
        password="password123",
        real_name="队员C",
        class_name="计算机二班",
    )

    # 队长 A 创建并保存团队项目，包含队员 C (2025002003)
    draft_a = create_draft(actor=leader_a, activity_id=activity.pk)
    save_draft(
        actor=leader_a,
        submission_id=draft_a.pk,
        expected_version=0,
        data={
            "project_name": "AI安全靶场构建",
            "category": "科技发明制作",
            "declaration_type": "team",
            "team_members": [
                {
                    "student_number": "2025002003",
                    "real_name": "队员C",
                    "class_name": "计算机二班",
                }
            ],
        },
    )
    seed_mandatory_materials(leader_a, activity)
    submit_for_review(actor=leader_a, submission_id=draft_a.pk)

    # 场景 1：队长 B 试图在另一团队项目中再次拉入队员 C (2025002003) -> 立即抛出 Conflict 并指明冲突学号
    draft_b = create_draft(actor=leader_b, activity_id=activity.pk)
    with pytest.raises(Conflict, match="2025002003"):
        save_draft(
            actor=leader_b,
            submission_id=draft_b.pk,
            expected_version=0,
            data={
                "project_name": "区块链存证系统",
                "category": "科技发明制作",
                "declaration_type": "team",
                "team_members": [
                    {
                        "student_number": "2025002003",
                        "real_name": "队员C",
                        "class_name": "计算机二班",
                    }
                ],
            },
        )

    # 场景 2：队长 B 试图把已作为申报人 owner 的队长 A (2025002001) 拉入自己团队 -> 立即拦截并指明 2025002001
    with pytest.raises(Conflict, match="2025002001"):
        save_draft(
            actor=leader_b,
            submission_id=draft_b.pk,
            expected_version=0,
            data={
                "project_name": "区块链存证系统",
                "category": "科技发明制作",
                "declaration_type": "team",
                "team_members": [
                    {
                        "student_number": "2025002001",
                        "real_name": "队长A",
                        "class_name": "计算机一班",
                    }
                ],
            },
        )

    # 场景 3：已在项目 A 中作为队员的 C (2025002003) 试图自己作为 owner 保存/提交个人项目 -> 立即拦截并指明 2025002003
    draft_c = create_draft(actor=member_c, activity_id=activity.pk)
    with pytest.raises(Conflict, match="2025002003"):
        save_draft(
            actor=member_c,
            submission_id=draft_c.pk,
            expected_version=0,
            data={
                "project_name": "队员C的个人申报",
                "category": "学术论文",
                "declaration_type": "individual",
                "team_members": [],
            },
        )

    # 场景 4：若绕过 save_draft 直接构造冲突草稿并调用 submit_for_review，同样被硬性校验拦截
    Submission.objects.filter(pk=draft_c.pk).update(
        draft_data={
            "project_name": "绕过尝试",
            "category": "学术论文",
            "declaration_type": "individual",
            "team_members": [],
        }
    )
    with pytest.raises(Conflict, match="2025002003"):
        submit_for_review(actor=member_c, submission_id=draft_c.pk)

    # 场景 5：队长 B 使用无冲突队员正常申报个人或团队项目可通过
    save_draft(
        actor=leader_b,
        submission_id=draft_b.pk,
        expected_version=0,
        data={
            "project_name": "区块链存证系统",
            "category": "科技发明制作",
            "declaration_type": "team",
            "team_members": [
                {
                    "student_number": "2025002099",
                    "real_name": "新队员D",
                    "class_name": "计算机二班",
                }
            ],
        },
    )
    seed_mandatory_materials(leader_b, activity)
    sub_b, rev_b = submit_for_review(actor=leader_b, submission_id=draft_b.pk)
    assert sub_b.status == Submission.Status.PENDING_FIRST_REVIEW
    assert rev_b.snapshot["declaration_type"] == "team"
    assert rev_b.snapshot["project_name"] == "区块链存证系统"
