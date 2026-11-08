import io
import zipfile
import openpyxl
import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from apps.accounts.models import User
from apps.exports.services import sanitize_filename
from apps.reviews.models import ReviewAssignment
from apps.reviews.services import review_submission
from apps.submissions.models import Submission
from apps.submissions.services import create_draft, save_draft, submit_for_review
from apps.uploads.services import direct_upload, ensure_standard_material_requirements
from tests.fixtures.samples import REAL_JPEG, REAL_PNG


def _upload_full_student_materials(student, activity, reqs, *, include_optional=True):
    """为指定学生上传全套标准材料并返回各文件原始字节字典。"""
    docx_bytes = b"PK\x03\x04" + f"docx-for-{student.username}".encode("utf-8") * 8
    pdf_bytes = b"%PDF-1.7\n" + f"report-for-{student.username}".encode("utf-8") * 8
    # 现场照片必须是真实可解析图片：图片结构校验会拒绝伪造样本；用 NUL 填充保证三份内容互不相同。
    jpg1_bytes = REAL_JPEG
    jpg2_bytes = REAL_JPEG + bytes([0]) * 4
    png3_bytes = REAL_PNG

    direct_upload(
        actor=student,
        requirement_id=reqs["application_form"].id,
        file_obj=SimpleUploadedFile("我的申请表_初稿.docx", docx_bytes),
    )
    direct_upload(
        actor=student,
        requirement_id=reqs["practice_report"].id,
        file_obj=SimpleUploadedFile("社会实践总结.pdf", pdf_bytes),
    )
    direct_upload(
        actor=student,
        requirement_id=reqs["scene_photos"].id,
        file_obj=SimpleUploadedFile("合影A.jpg", jpg1_bytes),
    )
    direct_upload(
        actor=student,
        requirement_id=reqs["scene_photos"].id,
        file_obj=SimpleUploadedFile("合影B.jpg", jpg2_bytes),
    )
    direct_upload(
        actor=student,
        requirement_id=reqs["scene_photos"].id,
        file_obj=SimpleUploadedFile("现场C.png", png3_bytes),
    )

    payloads = {
        "申报表.docx": docx_bytes,
        "实践报告.pdf": pdf_bytes,
        "现场照片_1.jpg": jpg1_bytes,
        "现场照片_2.jpg": jpg2_bytes,
        "现场照片_3.png": png3_bytes,
    }

    if include_optional:
        mp4_bytes = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + f"video-{student.username}".encode("utf-8") * 16
        other1_bytes = b"%PDF-1.7\n" + f"cert1-{student.username}".encode("utf-8") * 8
        other2_bytes = REAL_JPEG + bytes([0]) * 8

        direct_upload(
            actor=student,
            requirement_id=reqs["video"].id,
            file_obj=SimpleUploadedFile("宣讲录像.mp4", mp4_bytes),
        )
        direct_upload(
            actor=student,
            requirement_id=reqs["other_materials"].id,
            file_obj=SimpleUploadedFile("接收证明.pdf", other1_bytes),
        )
        direct_upload(
            actor=student,
            requirement_id=reqs["other_materials"].id,
            file_obj=SimpleUploadedFile("感谢信扫描件.jpg", other2_bytes),
        )
        payloads["宣讲视频.mp4"] = mp4_bytes
        payloads["其他证明材料_1.pdf"] = other1_bytes
        payloads["其他证明材料_2.jpg"] = other2_bytes

    return payloads


# =====================================================================
# 1. Excel 汇总表导出接口与 6 列表头/中文状态校验
# =====================================================================

def test_export_excel_summary_headers_rows_and_filters(client, activity, db):
    """构造多班级、多审核状态的学生申报，验证 Excel 导出的 6 列表头、行数据、中文审核状态及按班级/状态筛选。"""
    reqs = ensure_standard_material_requirements(activity)

    admin_user = User.objects.create_user(
        username="admin_export",
        password="password123",
        real_name="学院管理员",
        role=User.Role.ADMIN,
        is_staff=True,
    )
    reviewer_c1 = User.objects.create_user(
        username="rev_c1",
        password="password123",
        real_name="信安一班团支书",
        role=User.Role.REVIEWER,
    )
    ReviewAssignment.objects.create(reviewer=reviewer_c1, activity=activity, class_name="信安25-1班")

    # 构造 5 名学生，分别覆盖：终审通过、待终审、打回修改、待初审、草稿
    s1 = User.objects.create_user(username="20250101", password="pwd", real_name="张三", class_name="信安25-1班")
    s2 = User.objects.create_user(username="20250102", password="pwd", real_name="李四", class_name="信安25-1班")
    s3 = User.objects.create_user(username="20250103", password="pwd", real_name="王五", class_name="信安25-1班")
    s4 = User.objects.create_user(username="20250201", password="pwd", real_name="赵六", class_name="计算机25-2班")
    s5 = User.objects.create_user(username="20250202", password="pwd", real_name="钱七", class_name="计算机25-2班")

    # s1: 终审通过
    d1 = create_draft(actor=s1, activity_id=activity.pk)
    save_draft(
        actor=s1,
        submission_id=d1.pk,
        expected_version=0,
        data={"project_name": "红色基地调研", "category": "红色寻访", "declaration_type": "individual", "team_members": []},
    )
    _upload_full_student_materials(s1, activity, reqs, include_optional=False)
    sub1, _ = submit_for_review(actor=s1, submission_id=d1.pk)
    review_submission(actor=reviewer_c1, activity_id=activity.pk, submission_id=sub1.pk, decision="approved")
    review_submission(actor=admin_user, activity_id=activity.pk, submission_id=sub1.pk, decision="approved")

    # s2: 待终审
    d2 = create_draft(actor=s2, activity_id=activity.pk)
    save_draft(
        actor=s2,
        submission_id=d2.pk,
        expected_version=0,
        data={"project_name": "网络安全科普进社区", "category": "科普宣讲", "declaration_type": "individual", "team_members": []},
    )
    _upload_full_student_materials(s2, activity, reqs, include_optional=False)
    sub2, _ = submit_for_review(actor=s2, submission_id=d2.pk)
    review_submission(actor=reviewer_c1, activity_id=activity.pk, submission_id=sub2.pk, decision="approved")

    # s3: 打回修改
    d3 = create_draft(actor=s3, activity_id=activity.pk)
    save_draft(
        actor=s3,
        submission_id=d3.pk,
        expected_version=0,
        data={"project_name": "反诈宣传实践", "category": "社会调查", "declaration_type": "individual", "team_members": []},
    )
    _upload_full_student_materials(s3, activity, reqs, include_optional=False)
    sub3, _ = submit_for_review(actor=s3, submission_id=d3.pk)
    review_submission(
        actor=reviewer_c1,
        activity_id=activity.pk,
        submission_id=sub3.pk,
        decision="returned",
        comment="照片清晰度不足",
    )

    # s4: 待初审
    d4 = create_draft(actor=s4, activity_id=activity.pk)
    save_draft(
        actor=s4,
        submission_id=d4.pk,
        expected_version=0,
        data={"project_name": "乡村数字治理调研", "category": "乡村振兴", "declaration_type": "individual", "team_members": []},
    )
    _upload_full_student_materials(s4, activity, reqs, include_optional=False)
    submit_for_review(actor=s4, submission_id=d4.pk)

    # s5: 草稿
    d5 = create_draft(actor=s5, activity_id=activity.pk)
    save_draft(
        actor=s5,
        submission_id=d5.pk,
        expected_version=0,
        data={"project_name": "非遗数字化保护", "category": "文化传承", "declaration_type": "individual", "team_members": []},
    )

    # 1) 权限校验：普通学生禁止访问 Excel 导出接口
    client.force_login(s1)
    assert client.get(f"/api/v1/exports/activities/{activity.id}/excel/").status_code == 403

    # 2) 管理员导出全部汇总表并用 openpyxl 校验 6 列表头与全部行数据
    client.force_login(admin_user)
    resp_all = client.get(f"/api/v1/exports/activities/{activity.id}/excel/")
    assert resp_all.status_code == 200
    assert resp_all["Content-Type"] == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

    wb = openpyxl.load_workbook(io.BytesIO(resp_all.content))
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))

    assert list(rows[0]) == ["学号", "姓名", "班级", "项目名", "类别", "审核状态"]
    expected_rows = [
        ("20250101", "张三", "信安25-1班", "红色基地调研", "红色寻访", "终审通过"),
        ("20250102", "李四", "信安25-1班", "网络安全科普进社区", "科普宣讲", "待终审"),
        ("20250103", "王五", "信安25-1班", "反诈宣传实践", "社会调查", "打回修改"),
        ("20250201", "赵六", "计算机25-2班", "乡村数字治理调研", "乡村振兴", "待初审"),
        ("20250202", "钱七", "计算机25-2班", "非遗数字化保护", "文化传承", "草稿"),
    ]
    assert rows[1:] == expected_rows

    # 3) 按班级筛选 (?class_name=信安25-1班)
    resp_cls = client.get(f"/api/v1/exports/activities/{activity.id}/excel/", {"class_name": "信安25-1班"})
    assert resp_cls.status_code == 200
    ws_cls = openpyxl.load_workbook(io.BytesIO(resp_cls.content)).active
    rows_cls = list(ws_cls.iter_rows(values_only=True))
    assert list(rows_cls[0]) == ["学号", "姓名", "班级", "项目名", "类别", "审核状态"]
    assert rows_cls[1:] == expected_rows[:3]

    # 4) 按状态筛选 (?status=final_approved 及中文 ?status=终审通过)
    for status_query in (Submission.Status.FINAL_APPROVED, "终审通过"):
        resp_st = client.get(f"/api/v1/exports/activities/{activity.id}/excel/", {"status": status_query})
        assert resp_st.status_code == 200
        ws_st = openpyxl.load_workbook(io.BytesIO(resp_st.content)).active
        rows_st = list(ws_st.iter_rows(values_only=True))
        assert rows_st[1:] == [expected_rows[0]]


# =====================================================================
# 2. 按班级 ZIP 归档打包接口、班级列表接口与非法文件名清洗测试
# =====================================================================

def test_export_class_zip_structure_and_filename_sanitization(client, activity, tmp_path, db):
    """验证班级列表接口、按班级打包 ZIP 的目录结构、多图自动加序号、非法字符清洗以及流式临时文件读取。"""
    reqs = ensure_standard_material_requirements(activity)
    admin_user = User.objects.create_user(
        username="admin_zip",
        password="password123",
        real_name="归档管理员",
        role=User.Role.ADMIN,
        is_staff=True,
    )

    # 信安25-1班：学生 A（正常项目名称，包含全部 5 类材料）
    stu_a = User.objects.create_user(username="20250801", password="pwd", real_name="周达", class_name="信安25-1班")
    draft_a = create_draft(actor=stu_a, activity_id=activity.pk)
    save_draft(
        actor=stu_a,
        submission_id=draft_a.pk,
        expected_version=0,
        data={"project_name": "矿区网络安全实训", "category": "科技发明制作", "declaration_type": "individual", "team_members": []},
    )
    payloads_a = _upload_full_student_materials(stu_a, activity, reqs, include_optional=True)
    submit_for_review(actor=stu_a, submission_id=draft_a.pk)

    # 信安25-1班：学生 B（项目名包含 /  : * ? " < > | 等非法文件名字符）
    stu_b = User.objects.create_user(username="20250802", password="pwd", real_name="林溪", class_name="信安25-1班")
    dirty_project_name = 'AI/大模型:安全*防御?研究"靶场<一期>|测试\\终版'
    cleaned_project_name = sanitize_filename(dirty_project_name)
    assert cleaned_project_name == "AI_大模型_安全_防御_研究_靶场_一期__测试_终版"
    for bad_ch in ('/', '\\', ':', '*', '?', '"', '<', '>', '|'):
        assert bad_ch not in cleaned_project_name

    draft_b = create_draft(actor=stu_b, activity_id=activity.pk)
    save_draft(
        actor=stu_b,
        submission_id=draft_b.pk,
        expected_version=0,
        data={"project_name": dirty_project_name, "category": "学术论文", "declaration_type": "individual", "team_members": []},
    )
    payloads_b = _upload_full_student_materials(stu_b, activity, reqs, include_optional=False)
    submit_for_review(actor=stu_b, submission_id=draft_b.pk)

    # 计算机25-2班：学生 C（用于验证按班级打包不会混入其他班级材料）
    stu_c = User.objects.create_user(username="20250901", password="pwd", real_name="陈默", class_name="计算机25-2班")
    draft_c = create_draft(actor=stu_c, activity_id=activity.pk)
    save_draft(
        actor=stu_c,
        submission_id=draft_c.pk,
        expected_version=0,
        data={"project_name": "智慧矿山巡检系统", "category": "科技发明制作", "declaration_type": "individual", "team_members": []},
    )
    _upload_full_student_materials(stu_c, activity, reqs, include_optional=False)
    submit_for_review(actor=stu_c, submission_id=draft_c.pk)

    # 1) 验证获取某活动下已有申报的班级列表接口
    client.force_login(admin_user)
    classes_resp = client.get(f"/api/v1/exports/activities/{activity.id}/classes/")
    assert classes_resp.status_code == 200
    assert classes_resp.json()["classes"] == ["信安25-1班", "计算机25-2班"]

    # 2) 缺少 class_name 参数时返回 400
    missing_cls_resp = client.get(f"/api/v1/exports/activities/{activity.id}/zip/")
    assert missing_cls_resp.status_code == 400

    # 3) 按班级下载“信安25-1班”的 ZIP 包并验证内部目录和文件内容
    zip_resp = client.get(
        f"/api/v1/exports/activities/{activity.id}/zip/",
        {"class_name": "信安25-1班"},
    )
    assert zip_resp.status_code == 200
    assert zip_resp["Content-Type"] == "application/zip"
    assert zip_resp.streaming is True

    zip_bytes = b"".join(zip_resp.streaming_content)
    with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
        namelist = set(zf.namelist())

        prefix_a = "信安25-1班/周达-20250801-矿区网络安全实训"
        expected_a_files = {
            f"{prefix_a}/申报表.docx",
            f"{prefix_a}/实践报告.pdf",
            f"{prefix_a}/现场照片_1.jpg",
            f"{prefix_a}/现场照片_2.jpg",
            f"{prefix_a}/现场照片_3.png",
            f"{prefix_a}/宣讲视频.mp4",
            f"{prefix_a}/其他证明材料_1.pdf",
            f"{prefix_a}/其他证明材料_2.jpg",
        }

        prefix_b = f"信安25-1班/林溪-20250802-{cleaned_project_name}"
        expected_b_files = {
            f"{prefix_b}/申报表.docx",
            f"{prefix_b}/实践报告.pdf",
            f"{prefix_b}/现场照片_1.jpg",
            f"{prefix_b}/现场照片_2.jpg",
            f"{prefix_b}/现场照片_3.png",
        }

        assert namelist == expected_a_files | expected_b_files
        # 确认不包含计算机25-2班的文件
        assert not any(name.startswith("计算机25-2班/") for name in namelist)

        # 验证压缩包内各文件字节与上传源文件完全一致
        for filename, expected_bytes in payloads_a.items():
            assert zf.read(f"{prefix_a}/{filename}") == expected_bytes
        for filename, expected_bytes in payloads_b.items():
            assert zf.read(f"{prefix_b}/{filename}") == expected_bytes

        # 实际解压到临时目录，确保清洗后的路径在 Windows/Linux 文件系统下均不会解压失败
        extract_dir = tmp_path / "unzipped"
        zf.extractall(extract_dir)
        assert (extract_dir / prefix_a / "申报表.docx").is_file()
        assert (extract_dir / prefix_b / "申报表.docx").is_file()

# =====================================================================
# 3. 审核人员（团支书）导出权限边界测试
# =====================================================================

def test_reviewer_export_permission_boundaries(client, activity, db):
    """验证审核人员仅可导出所负责班级的 Excel 汇总表与 ZIP 压缩包，跨班访问返回 403。"""
    reqs = ensure_standard_material_requirements(activity)
    reviewer = User.objects.create_user(
        username="rev_bound",
        password="pwd",
        real_name="信安团支书",
        role=User.Role.REVIEWER,
    )
    ReviewAssignment.objects.create(reviewer=reviewer, activity=activity, class_name="信安25-1班")

    stu1 = User.objects.create_user(username="20250301", password="pwd", real_name="甲同学", class_name="信安25-1班")
    stu2 = User.objects.create_user(username="20250302", password="pwd", real_name="乙同学", class_name="计算机25-2班")

    for stu, proj in ((stu1, "信安实践项目"), (stu2, "计科实践项目")):
        draft = create_draft(actor=stu, activity_id=activity.pk)
        save_draft(
            actor=stu,
            submission_id=draft.pk,
            expected_version=0,
            data={"project_name": proj, "category": "社会调查", "declaration_type": "individual", "team_members": []},
        )
        _upload_full_student_materials(stu, activity, reqs, include_optional=False)
        submit_for_review(actor=stu, submission_id=draft.pk)

    client.force_login(reviewer)

    # 1) 班级列表仅返回该审核员有权限的班级
    cls_resp = client.get(f"/api/v1/exports/activities/{activity.id}/classes/")
    assert cls_resp.status_code == 200
    assert cls_resp.json()["classes"] == ["信安25-1班"]

    # 2) 导出本班 ZIP 成功，导出非负责班级 ZIP 返回 403
    ok_zip = client.get(f"/api/v1/exports/activities/{activity.id}/zip/", {"class_name": "信安25-1班"})
    assert ok_zip.status_code == 200
    forbidden_zip = client.get(f"/api/v1/exports/activities/{activity.id}/zip/", {"class_name": "计算机25-2班"})
    assert forbidden_zip.status_code == 403

    # 3) 导出非负责班级 Excel 返回 403；不带参数导出 Excel 自动限定在所负责班级
    forbidden_excel = client.get(f"/api/v1/exports/activities/{activity.id}/excel/", {"class_name": "计算机25-2班"})
    assert forbidden_excel.status_code == 403

    scoped_excel = client.get(f"/api/v1/exports/activities/{activity.id}/excel/")
    assert scoped_excel.status_code == 200
    ws = openpyxl.load_workbook(io.BytesIO(scoped_excel.content)).active
    rows = list(ws.iter_rows(values_only=True))
    assert len(rows) == 2
    assert rows[1] == ("20250301", "甲同学", "信安25-1班", "信安实践项目", "社会调查", "待初审")
