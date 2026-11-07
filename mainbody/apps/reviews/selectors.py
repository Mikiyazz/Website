from apps.accounts.models import User
from apps.accounts.permissions import is_college_admin
from apps.submissions.models import Submission
from .models import ReviewAssignment


def reviewable_submissions(user, activity_id):
    if is_college_admin(user):
        return Submission.objects.filter(activity_id=activity_id)
    if not user.is_authenticated or not user.is_active or user.role != User.Role.REVIEWER:
        return Submission.objects.none()
    assignments = list(
        ReviewAssignment.objects.filter(reviewer=user, activity_id=activity_id).values_list("class_name", flat=True)
    )
    if not assignments or "" in assignments:
        return Submission.objects.filter(activity_id=activity_id)
    allowed_classes = [c.strip() for c in assignments if c and c.strip()]
    return Submission.objects.filter(activity_id=activity_id, owner__class_name__in=allowed_classes)
