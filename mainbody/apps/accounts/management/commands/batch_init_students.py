"""命令行批量初始化学生账号：支持从 StudentRoster 或 CSV 分批导入，避免 Admin HTTP 超时。"""
import csv
from pathlib import Path
from django.core.management.base import BaseCommand, CommandError
from apps.accounts.models import StudentRoster
from apps.accounts.services import register_student
from apps.common.errors import DomainError


class Command(BaseCommand):
    help = "分批初始化学生账号（默认初始密码为学号后 6 位），支持从 StudentRoster 或 CSV 导入。"

    def add_arguments(self, parser):
        parser.add_argument(
            "--all-roster",
            action="store_true",
            help="为所有 active=True 且尚未绑定账号的 StudentRoster 记录批量创建账号。",
        )
        parser.add_argument(
            "--csv",
            type=str,
            default="",
            help="从 UTF-8 CSV 文件导入（列：student_number,real_name,grade,major,class_name,phone）。",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=100,
            help="每批汇报进度的记录数（默认 100）。",
        )

    def handle(self, *args, **options):
        all_roster = options["all_roster"]
        csv_path = (options["csv"] or "").strip()
        batch_size = max(1, int(options["batch_size"] or 100))

        if not all_roster and not csv_path:
            raise CommandError("请至少指定 --all-roster 或 --csv <文件路径>。")

        created = 0
        skipped = 0

        if csv_path:
            path = Path(csv_path)
            if not path.is_file():
                raise CommandError(f"CSV 文件不存在：{path}")
            with path.open("r", encoding="utf-8-sig", newline="") as fh:
                reader = csv.DictReader(fh)
                for idx, row in enumerate(reader, start=1):
                    sn = str(row.get("student_number") or row.get("username") or "").strip()
                    rn = str(row.get("real_name") or "").strip()
                    grade = str(row.get("grade") or "").strip()
                    major = str(row.get("major") or "").strip()
                    class_name = str(row.get("class_name") or "").strip()
                    phone = str(row.get("phone") or "").strip()
                    if not sn:
                        skipped += 1
                        continue
                    StudentRoster.objects.update_or_create(
                        student_number=sn,
                        defaults={
                            "real_name": rn,
                            "grade": grade,
                            "major": major,
                            "class_name": class_name,
                            "phone": phone,
                            "active": True,
                        },
                    )
                    try:
                        register_student(
                            student_number=sn,
                            real_name=rn,
                            class_name=class_name,
                            password=None,
                            grade=grade,
                            major=major,
                            phone=phone,
                        )
                        created += 1
                    except DomainError as exc:
                        skipped += 1
                        self.stderr.write(f"[跳过] {sn}: {exc}")
                    if idx % batch_size == 0:
                        self.stdout.write(f"已处理 {idx} 行（新增 {created}，跳过 {skipped}）...")

        if all_roster:
            qs = StudentRoster.objects.filter(active=True, user__isnull=True).order_by("student_number")
            total = qs.count()
            self.stdout.write(f"待初始化花名册记录共 {total} 条...")
            for idx, roster in enumerate(qs.iterator(chunk_size=batch_size), start=1):
                try:
                    register_student(
                        student_number=roster.student_number,
                        real_name=roster.real_name,
                        class_name=roster.class_name,
                        password=None,
                        grade=roster.grade,
                        major=roster.major,
                        phone=roster.phone,
                    )
                    created += 1
                except DomainError as exc:
                    skipped += 1
                    self.stderr.write(f"[跳过] {roster.student_number}: {exc}")
                if idx % batch_size == 0:
                    self.stdout.write(f"进度 {idx}/{total}（新增 {created}，跳过 {skipped}）...")

        self.stdout.write(self.style.SUCCESS(f"批量初始化完成：新增 {created} 个账号，跳过 {skipped} 条。"))
