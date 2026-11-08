"""Export Celery tasks: thin wrappers so the web request never builds a ZIP inline."""
from celery import shared_task

from .services import create_activity_export_job, run_activity_export_job


@shared_task(
    name="apps.exports.export_activity_submissions",
    queue="exports",
    # 导出是长任务：大活动含视频时可能远超全局 300s 硬超时，单独放宽到 1 小时。
    soft_time_limit=3600,
    time_limit=3660,
)
def export_activity_submissions(activity_id, requested_by, job_id=None):
    """Build one activity archive.

    Signature mirrors the requested contract export_activity_submissions(activity_id, requested_by);
    job_id is optional so a job can be created by the task itself when it is triggered elsewhere.
    """
    if job_id is None:
        from apps.accounts.models import User

        actor = User.objects.get(pk=requested_by)
        job = create_activity_export_job(actor=actor, activity_id=activity_id)
        job_id = job.pk
    job = run_activity_export_job(job_id)
    return {
        "job_id": str(job.id),
        "status": job.status,
        "total": job.total,
        "current": job.current,
        "file_path": job.file_path,
    }
