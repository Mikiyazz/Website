"""命令行分批重置用户密码为学号后 6 位，避免 Admin 勾选上千人同步跑 PBKDF2 超时。"""
import re
from django.core.management.base import BaseCommand, CommandError
from apps.accounts.models import User
from apps.accounts.services import reset_password_to_default
from apps.common.errors import DomainError


class Command(BaseCommand):
    help = "分批将指定范围内的学生账号密码重置为学号后 6 位（must_change_password=True）。"

    def add_arguments(self, parser):
        parser.add_argument("--all-students", action="store_true", help="重置全部 student 角色账号密码。")
        parser.add_argument("--grade", type=str, default="", help="按年级筛选（如 2025级）。")
        parser.add_argument("--class-name", type=str, default="", help="按班级筛选（如 01班）。")
        parser.add_argument(
            "--student-numbers",
            type=str,
            default="",
            help="逗号或空格分隔的指定学号列表。",
        )
        parser.add_argument("--batch-size", type=int, default=100, help="每批进度输出步长（默认 100）。")

    def handle(self, *args, **options):
        all_students = options["all_students"]
        grade = (options["grade"] or "").strip()
        class_name = (options["class_name"] or "").strip()
        raw_sns = (options["student_numbers"] or "").strip()
        batch_size = max(1, int(options["batch_size"] or 100))

        if not any((all_students, grade, class_name, raw_sns)):
            raise CommandError("请指定筛选范围：--all-students / --grade / --class-name / --student-numbers。")

        qs = User.objects.filter(is_active=True)
        if all_students:
            qs = qs.filter(role=User.Role.STUDENT)
        if grade:
            qs = qs.filter(grade=grade)
        if class_name:
            qs = qs.filter(class_name=class_name)
        if raw_sns:
            sns = [x.strip() for x in re.split(r"[,，;；\s]+", raw_sns) if x.strip()]
            qs = qs.filter(username__in=sns)

        qs = qs.order_by("username")
        total = qs.count()
        self.stdout.write(f"命中待重置密码账号共 {total} 个...")

        reset_count = 0
        skipped = 0
        for idx, user in enumerate(qs.iterator(chunk_size=batch_size), start=1):
            try:
                reset_password_to_default(user=user)
                reset_count += 1
            except DomainError as exc:
                skipped += 1
                self.stderr.write(f"[跳过] {user.username}: {exc}")
            if idx % batch_size == 0:
                self.stdout.write(f"进度 {idx}/{total}（已重置 {reset_count}，跳过 {skipped}）...")

        self.stdout.write(self.style.SUCCESS(f"密码重置完成：成功 {reset_count} 个，跳过 {skipped} 个。"))
