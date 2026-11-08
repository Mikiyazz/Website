"""pytest 专用样本加载器。

fixtures 目录里的文件分两类：

* 真实可解析的最小图片（walkthrough.jpg / walkthrough.png，ffmpeg 编码，2x2 像素）：
  图片结构校验（SOI/SOF/EOI、IHDR/IEND+CRC）与 Pillow 解码都能通过。
* 攻击向量样本：
  - fake_docx.exe    可执行文件改名成 .docx（魔数不符）
  - photohorse.jpg   真实 JPEG 尾部拼 PHP 一句话（图片马）
  - zipslip.zip      条目名带 ../ 的 ZipSlip 包
  - nested.zip       包内再套 zip（嵌套深度 2）
  - bomb.zip         3 个 80MB 全零条目（解压炸弹，解压后 240MB）
  - material_pack.zip 合法压缩包，用于"正常包必须放行"的对照
"""
from pathlib import Path

FIXTURE_DIR = Path(__file__).resolve().parent


def fixture_path(name: str) -> Path:
    return FIXTURE_DIR / name


def read_fixture(name: str) -> bytes:
    return fixture_path(name).read_bytes()


REAL_JPEG = read_fixture("walkthrough.jpg")
REAL_PNG = read_fixture("walkthrough.png")
FAKE_DOCX_EXE = read_fixture("fake_docx.exe")
PHOTO_HORSE_JPEG = read_fixture("photohorse.jpg")


def jpeg_with_trailing(payload: bytes) -> bytes:
    """真实 JPEG + 追加字节，用于构造各种"图片 + 附加载荷"变体。"""
    return REAL_JPEG + payload
