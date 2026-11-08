# 安全相关外部依赖说明（阶段 2 安全加固）

本阶段新增/明确了两个安全校验依赖。两者都属于"有则更强、无则降级"的设计，
不会让业务功能依赖它们才能跑通，但生产部署应当装上。

## Pillow（pip 包，已写入 base.in / base.txt）

- 用途：现场照片与其他证明材料的图片在落盘后执行 Image.open().verify()，真解码一次，
  拦下"改扩展名""结构伪造"与像素炸弹。
- 降级：未安装时 apps.uploads.policies 走纯标准库的容器结构校验
  （PNG 的 IHDR/IEND + CRC、JPEG 的 SOI/SOF/EOI），并打 image_verify_degraded warning 日志。
- 范围说明：按运维要求已关闭图片尾部载荷（图片马）扫描，图片只验"能否真实解码"；
  对外访问由 Content-Disposition 与受限 inline 白名单兜底，不会按脚本解析。
- 无 Pillow 时 pytest 依然全绿：tests/fixtures 里的样本是真实最小图片，
  结构校验与 Pillow 解码都能通过。

## ffprobe（系统二进制，不是 pip 包）

- 用途：.mp4 / .mov / .avi / .mkv 落盘后做容器解析，确认里面真的有视频流，
  防止"ftyp 头 + 任意垃圾"伪装成宣讲视频。
- 配置：settings.UPLOAD_FFPROBE_BIN，默认 "ffprobe"，可写绝对路径。
- 降级：找不到二进制（或执行失败）时，按"扩展名 + 大小 + 32 字节魔数"放行并打
  video_probe_degraded warning，部署侧可用日志告警确认 ffmpeg 是否缺失。
- 测试：config/settings/test.py 把 UPLOAD_FFPROBE_BIN 置空，避免测试结果依赖
  跑测试的机器；test_step8 用假 ffprobe 脚本注入，覆盖 probe 成功 / probe 失败 /
  二进制缺失三条分支。
