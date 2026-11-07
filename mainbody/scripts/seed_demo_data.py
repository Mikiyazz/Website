"""Seed initial demo data for local testing."""
import copy
import os
import sys
import uuid
from datetime import timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.development")
import django
django.setup()

from django.utils import timezone
from apps.accounts.models import User
from apps.activities.models import Activity
from apps.uploads.services import ensure_standard_material_requirements
from apps.submissions.models import Submission, SubmissionRevision
from apps.reviews.models import ReviewRecord

def seed():
    print("Seeding initial local demo accounts and activity...")
    
    # 1. Admin (学院管理员 / 终审)
    admin_user, _ = User.objects.get_or_create(
        username="admin",
        defaults={
            "real_name": "张老师（团委）",
            "role": User.Role.ADMIN,
            "is_staff": True,
            "is_superuser": True,
            "must_change_password": False,
        }
    )
    admin_user.set_password("Admin123456")
    admin_user.role = User.Role.ADMIN
    admin_user.is_staff = True
    admin_user.is_superuser = True
    admin_user.save()
    print("  [+] Admin user: admin / Admin123456")

    # 2. Reviewer (团支书 / 初审)
    reviewer_user, _ = User.objects.get_or_create(
        username="tzs2025",
        defaults={
            "real_name": "李团支书",
            "role": User.Role.REVIEWER,
            "class_name": "信安25-1班",
            "is_staff": False,
            "is_superuser": False,
            "must_change_password": False,
        }
    )
    reviewer_user.set_password("Admin123456")
    reviewer_user.role = User.Role.REVIEWER
    reviewer_user.save()
    print("  [+] Reviewer user: tzs2025 / Admin123456")

    # 3. Students
    s1, _ = User.objects.get_or_create(
        username="2025080901",
        defaults={
            "real_name": "王同学",
            "role": User.Role.STUDENT,
            "grade": "大二",
            "major": "信息安全",
            "class_name": "信安25-1班",
            "phone": "13800000001",
            "must_change_password": True,
        }
    )
    s1.set_password("080901")
    s1.save()

    s2, _ = User.objects.get_or_create(
        username="2025080902",
        defaults={
            "real_name": "刘同学",
            "role": User.Role.STUDENT,
            "grade": "大二",
            "major": "信息安全",
            "class_name": "信安25-1班",
            "phone": "13800000002",
            "must_change_password": False,
        }
    )
    s2.set_password("080902")
    s2.save()

    s3, _ = User.objects.get_or_create(
        username="2025080903",
        defaults={
            "real_name": "陈同学",
            "role": User.Role.STUDENT,
            "grade": "大二",
            "major": "信息安全",
            "class_name": "信安25-2班",
            "phone": "13800000003",
            "must_change_password": True,
        }
    )
    s3.set_password("080903")
    s3.save()
    print("  [+] Student accounts (password = 学号后6位): 2025080901, 2025080902, 2025080903")

    # 4. Activity
    now = timezone.now()
    activity, _ = Activity.objects.get_or_create(
        title="2026年学院暑期社会实践材料申报与结项评定",
        defaults={
            "opens_at": now - timedelta(days=3),
            "deadline": now + timedelta(days=14),
            "is_open": True,
            "allowed_classes": [],
            "late_whitelist_student_numbers": ["2025080903"],
        }
    )
    activity.is_open = True
    activity.save()
    reqs = ensure_standard_material_requirements(activity)
    print(f"  [+] Activity created: '{activity.title}' with {len(reqs)} standard requirements")

    # 5. Seed sample submissions
    # Submission 1 for s1: pending_first_review
    sub1, _ = Submission.objects.get_or_create(
        activity=activity,
        owner=s1,
        defaults={
            "status": Submission.Status.PENDING_FIRST_REVIEW,
            "draft_data": {
                "project_name": "计算机科普与乡村教育帮扶实践",
                "category": "乡村振兴",
                "declaration_type": "individual",
                "team_members": [],
            }
        }
    )
    sub1.status = Submission.Status.PENDING_FIRST_REVIEW
    sub1.draft_data = {
        "project_name": "计算机科普与乡村教育帮扶实践",
        "category": "乡村振兴",
        "declaration_type": "individual",
        "team_members": [],
    }
    sub1.save()
    SubmissionRevision.objects.get_or_create(
        submission=sub1,
        number=1,
        defaults={
            "snapshot": copy.deepcopy(sub1.draft_data),
            "request_key": uuid.uuid4(),
        }
    )

    # Submission 2 for s2: returned (打回修改)
    sub2, _ = Submission.objects.get_or_create(
        activity=activity,
        owner=s2,
        defaults={
            "status": Submission.Status.RETURNED,
            "draft_data": {
                "project_name": "红色科技寻访与宣讲团",
                "category": "红色传承",
                "declaration_type": "team",
                "team_members": [
                    {"student_number": "2025080903", "real_name": "陈同学", "class_name": "信安25-2班"}
                ],
            }
        }
    )
    sub2.status = Submission.Status.RETURNED
    sub2.draft_data = {
        "project_name": "红色科技寻访与宣讲团",
        "category": "红色传承",
        "declaration_type": "team",
        "team_members": [
            {"student_number": "2025080903", "real_name": "陈同学", "class_name": "信安25-2班"}
        ],
    }
    sub2.save()
    rev2, _ = SubmissionRevision.objects.get_or_create(
        submission=sub2,
        number=1,
        defaults={
            "snapshot": copy.deepcopy(sub2.draft_data),
            "request_key": uuid.uuid4(),
        }
    )

    # Record return log
    ReviewRecord.objects.get_or_create(
        submission=sub2,
        revision=rev2,
        reviewer=reviewer_user,
        stage=ReviewRecord.Stage.FIRST,
        decision=ReviewRecord.Decision.RETURNED,
        defaults={
            "comment": "现场照片材料目前仅有 1 张，未满足 3～5 张的必交要求；请补全合影后重新提交。",
        }
    )
    print("  [+] Sample submissions seeded: 1 待初审, 1 打回修改")
    print("Seeding finished successfully.")

if __name__ == "__main__":
    seed()
