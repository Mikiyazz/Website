"""新闻稿的授权范围查询：所有可见性判断都在服务端完成。

口径与 apps.reviews.selectors 保持一致
------------------------------------
* 学生：只看到挂在自己申报上的那一篇（OneToOne 反查，不信任前端传的 uuid）。
* 团支书（reviewer）：只看到被 ReviewAssignment 覆盖的班级；没有分配记录时视为全院。
* 学院管理员 / 超管：全院可见。
"""
from django.db.models import Q, QuerySet

from apps.accounts.models import User
from apps.accounts.permissions import is_college_admin
from apps.activities.models import Activity
from apps.common.errors import DomainError
from apps.reviews.models import ReviewAssignment
from apps.submissions.models import Submission

from .models import NewsDraft


def _as_student(user) -> bool:
    return bool(
        getattr(user, "is_authenticated", False)
        and getattr(user, "is_active", False)
        and getattr(user, "role", "") in (User.Role.STUDENT, "grad_student")
    )


def _as_reviewer(user) -> bool:
    return bool(
        getattr(user, "is_authenticated", False)
        and getattr(user, "is_active", False)
        and getattr(user, "role", "") == User.Role.REVIEWER
    )


def my_news_queryset(user) -> QuerySet:
    """当前学生自己的新闻稿（未创建时为空集，不自动建行）。"""
    if not _as_student(user):
        return NewsDraft.objects.none()
    return NewsDraft.objects.filter(submission__owner=user).select_related("submission", "submission__activity")


def my_news(user, news_id=None) -> NewsDraft:
    """按 id 取下属于当前学生的新闻稿；他人 id 一律 404，不泄露是否存在。"""
    queryset = my_news_queryset(user)
    if news_id is not None:
        queryset = queryset.filter(pk=news_id)
    return queryset.get()


def news_for_submission(user, submission_id) -> NewsDraft | None:
    if not _as_student(user):
        return None
    return NewsDraft.objects.filter(pk__isnull=False, submission_id=submission_id, submission__owner=user).first()


def reviewer_class_scope(user, activity_id) -> list[str] | None:
    """返回该活动下 reviewer 被分配的班级列表；None 表示不受班级限制（全院）。"""
    assignments = list(
        ReviewAssignment.objects.filter(reviewer=user, activity_id=activity_id).values_list(
            "class_name", flat=True
        )
    )
    if not assignments or "" in assignments:
        return None
    allowed = [str(item).strip() for item in assignments if item and str(item).strip()]
    return allowed or None


def review_queue_queryset(user, *, activity_id=None, class_name="", status="") -> QuerySet:
    """独立审核页 /reviews/news/ 的数据源：只返回当前审核人有权处置的新闻稿。"""
    queryset = NewsDraft.objects.select_related(
        "submission", "submission__owner", "submission__activity"
    ).exclude(status=NewsDraft.Status.DRAFT)

    if is_college_admin(user):
        pass
    elif _as_reviewer(user):
        assignments = ReviewAssignment.objects.filter(reviewer=user)
        activity_ids = [str(value) for value in assignments.values_list("activity_id", flat=True)]
        if not activity_ids:
            pass  # 没有任何分配记录 = 全院可见（与 reviews.workbench_activities 同口径）
        else:
            clause = Q(pk__in=[])
            for assignment in assignments.values_list("activity_id", "class_name"):
                target_activity, target_class = assignment
                clean = str(target_class or "").strip()
                if clean:
                    clause |= Q(submission__activity_id=target_activity, submission__owner__class_name=clean)
                else:
                    clause |= Q(submission__activity_id=target_activity)
            queryset = queryset.filter(clause)
    else:
        return NewsDraft.objects.none()

    if activity_id:
        queryset = queryset.filter(submission__activity_id=activity_id)
    clean_class = str(class_name or "").strip()
    if clean_class:
        queryset = queryset.filter(submission__owner__class_name=clean_class)
    clean_status = str(status or "").strip()
    if clean_status:
        if clean_status not in NewsDraft.Status.values:
            raise DomainError(f"无效的新闻稿状态筛选参数：{clean_status}")
        queryset = queryset.filter(status=clean_status)

    return queryset.order_by("-submitted_at", "-updated_at")


def review_queue_activities(user) -> QuerySet:
    """审核页的活动下拉：只列出该审核人接触得到的活动。"""
    if is_college_admin(user):
        return Activity.objects.all().order_by("-created_at")
    if not _as_reviewer(user):
        return Activity.objects.none()
    assignments = ReviewAssignment.objects.filter(reviewer=user)
    if not assignments.exists():
        return Activity.objects.all().order_by("-created_at")
    return Activity.objects.filter(id__in=assignments.values("activity_id")).distinct().order_by("-created_at")


def news_belongs_to_reviewer(user, news: NewsDraft) -> bool:
    """单篇审核权限：管理员全院通吃；团支书按自己的分配范围判定。"""
    if is_college_admin(user):
        return True
    if not _as_reviewer(user):
        return False
    scope = reviewer_class_scope(user, news.submission.activity_id)
    if scope is None:
        return True
    student_class = str(news.submission.owner.class_name or "").strip()
    return bool(student_class) and student_class in scope


def excel_label_for_submission(submission: Submission) -> str:
    """Excel 第 7 列取值：没有新闻稿时返回「未提交」，不凭空建行。"""
    news = getattr(submission, "news_draft", None)
    return news.excel_label if news is not None else NewsDraft.NO_NEWS_LABEL
