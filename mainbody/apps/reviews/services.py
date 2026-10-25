from apps.common.errors import FeatureUnavailable
from .selectors import reviewable_submissions


def review_submission(*, actor, activity_id, submission_id, revision_id, decision, comment):
    reviewable_submissions(actor, activity_id).get(pk=submission_id)
    raise FeatureUnavailable("审核写入尚未实现；需锁定提交并核对 revision_id 后记录结论。")
