from datetime import timedelta
import pytest
from django.utils import timezone
from apps.accounts.models import User
from apps.activities.models import Activity, MaterialRequirement


@pytest.fixture
def student(db):
    return User.objects.create_user(username="001234", password="test-password-123", class_name="计算机一班")


@pytest.fixture
def other_student(db):
    return User.objects.create_user(username="005678", password="test-password-456", class_name="计算机二班")


@pytest.fixture
def activity(db):
    now = timezone.now()
    return Activity.objects.create(title="虚构测试活动", opens_at=now - timedelta(days=1),
                                   deadline=now + timedelta(days=1), is_open=True)


@pytest.fixture
def rule(activity):
    return MaterialRequirement.objects.create(activity=activity, code="outcome", title="测试成果",
                                               allowed_extensions=[".zip"], max_count=10)
