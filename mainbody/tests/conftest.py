from datetime import timedelta
import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from apps.accounts.models import User
from apps.activities.models import Activity, MaterialRequirement
from apps.uploads.services import direct_upload, ensure_standard_material_requirements


@pytest.fixture(autouse=True)
def isolated_media_root(settings, tmp_path):
    media_dir = tmp_path / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    settings.MEDIA_ROOT = media_dir
    return media_dir


@pytest.fixture
def student(db):
    return User.objects.create_user(username="001234", password="test-password-123", class_name="计算机一班")


@pytest.fixture
def other_student(db):
    return User.objects.create_user(username="005678", password="test-password-456", class_name="计算机二班")


@pytest.fixture
def activity(db):
    now = timezone.now()
    return Activity.objects.create(
        title="虚构测试活动",
        opens_at=now - timedelta(days=1),
        deadline=now + timedelta(days=1),
        is_open=True,
    )


@pytest.fixture
def rule(activity):
    return MaterialRequirement.objects.create(
        activity=activity,
        code="outcome",
        title="测试成果",
        allowed_extensions=[".zip"],
        max_count=10,
    )


def seed_mandatory_materials(actor, activity):
    """Upload valid application_form (1 .docx), practice_report (1 .pdf), and scene_photos (3 .jpg/.png)."""
    reqs = ensure_standard_material_requirements(activity)
    direct_upload(
        actor=actor,
        requirement_id=reqs["application_form"].id,
        file_obj=SimpleUploadedFile("申报表.docx", b"PK\x03\x04" + b"\x00" * 64),
    )
    direct_upload(
        actor=actor,
        requirement_id=reqs["practice_report"].id,
        file_obj=SimpleUploadedFile("实践报告.pdf", b"%PDF-1.7\n" + b"0" * 64),
    )
    for idx in range(1, 4):
        direct_upload(
            actor=actor,
            requirement_id=reqs["scene_photos"].id,
            file_obj=SimpleUploadedFile(f"现场照片{idx}.jpg", b"\xff\xd8\xff\xe0" + b"\x00" * 64),
        )
    return reqs
