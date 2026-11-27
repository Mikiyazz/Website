from django.db import migrations


def backfill_submission_team_members(apps, schema_editor):
    Submission = apps.get_model("submissions", "Submission")
    SubmissionTeamMember = apps.get_model("submissions", "SubmissionTeamMember")

    batch = []
    qs = (
        Submission.objects.select_related("owner")
        .exclude(draft_data={})
        .only("id", "activity_id", "status", "declaration_submitted", "draft_data", "owner__username")
    )
    for sub in qs.iterator(chunk_size=500):
        data = sub.draft_data if isinstance(sub.draft_data, dict) else {}
        if not data and sub.status == "draft" and not sub.declaration_submitted:
            continue
        team_members = data.get("team_members")
        if not isinstance(team_members, list) or not team_members:
            continue
        decl_type = str(data.get("declaration_type") or "").strip()
        if decl_type not in ("", "team"):
            continue
        owner_sn = str(getattr(sub.owner, "username", "") or "").strip()
        seen = set()
        for item in team_members:
            if not isinstance(item, dict):
                continue
            sn = str(item.get("student_number") or "").strip()[:64]
            if not sn or sn == owner_sn or sn in seen:
                continue
            seen.add(sn)
            batch.append(
                SubmissionTeamMember(
                    submission_id=sub.id,
                    activity_id=sub.activity_id,
                    student_number=sn,
                    real_name=str(item.get("real_name") or "").strip()[:100],
                    class_name=str(item.get("class_name") or "").strip()[:100],
                )
            )
            if len(batch) >= 500:
                SubmissionTeamMember.objects.bulk_create(batch, batch_size=500, ignore_conflicts=True)
                batch.clear()
    if batch:
        SubmissionTeamMember.objects.bulk_create(batch, batch_size=500, ignore_conflicts=True)


def reverse_noop(apps, schema_editor):
    SubmissionTeamMember = apps.get_model("submissions", "SubmissionTeamMember")
    SubmissionTeamMember.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("submissions", "0006_submissionteammember"),
    ]

    operations = [
        migrations.RunPython(backfill_submission_team_members, reverse_noop),
    ]
