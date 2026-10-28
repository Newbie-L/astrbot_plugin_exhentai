"""ExHentai / E-Hentai 画廊下载插件 for AstrBot.

工作方式（v0.2.8）：
1. 私聊里发一条带 exhentai.org / e-hentai.org 画廊链接的消息，
   或手动发 `/ex_dl <链接>`，插件把该画廊加入下载队列。
2. 后台按画廊页 -> 图片页 -> 原图的顺序逐张下载到服务器本地。
   单张失败重试 3 次，整遍下完后缺的序号再自动重下最多 2 轮，
   残文件写 .part、完整才重命名，不会把残文件当成功。
   2 轮后还缺的标 partial，不发不完整包，再跑 /ex_dl 只补缺的序号。
3. 下载完且无缺图后把图片打成加密 zip：密码是随机 6 位数字，
   压缩包名就是画廊标题（去掉文件名非法字符、截断到 120 字）。
4. 压缩完后把压缩包和密码发到触发会话：
   - 优先发到配置里的 `target_group_umo`；
   - 没配的话就发回触发会话：群触发发回群，私聊触发发回私聊。
     v0.2.1 之前这里只认群，私聊触发会 no_target、哪里都不发，
     这是 bug，v0.2.2 已修。
   注意：密码和压缩包发在同一条消息里，收到的人都能打开，
   加密主要防的是平台扫描 / 非接收者直接拿到文件，6 位密码
   本身也不抗暴力破解，别把它当成强保密手段。
5. 用 `/ex_status` 查进度、压缩包路径、密码和缺图序号，
   `/ex_list` 看已完成的，`/ex_send <gid>` 在当前会话手动补发。

关键前提，实话先说在前面：
- exhentai.org 不是公开站：必须登录，且浏览器里要有 `igneous` cookie
  才能进站（否则只会看到 sad panda）。这三个 cookie 请在 AstrBot
  后台的插件配置里填你自己的，不要发给任何人、不要写进代码仓库：
  `ipb_member_id`、`ipb_pass_hash`、`igneous`。
  没有这三个 cookie，exhentai 大概率直接失败；e-hentai.org 的公开
  画廊一般不需要。
- 不要用多并发猛刷：ExHentai 有图片流量配额，刷爆会返回 509。
  默认单并发 + 每张间隔 1 秒，宁可慢一点。
- 只下载你自己有权保存、仅供个人使用的内容，下载目录不要对外公开。

AstrBot 版本差异说明：
- 本插件用 `@filter.regex` 做监听，这是 AstrBot 3.x 的标准写法。
  如果你的版本没有这个装饰器，把 `on_gallery_link` 上面的装饰器换成
  `@filter.event_message_type(EventMessageType.ALL)` 即可，
  函数体里本来就会再用正则提取链接，不依赖装饰器过滤。
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

PLUGIN_NAME = "astrbot_plugin_exhentai"
PLUGIN_VERSION = "0.2.8"
PLUGIN_REPO = "https://github.com/Newbie-L/astrbot_plugin_exhentai"

# https://exhentai.org/g/1234567/abcdef1234/  /  https://e-hentai.org/g/...
GALLERY_RE = re.compile(
    r"https?://(?:exhentai\.org|e-hentai\.org)/g/(\d+)/([0-9a-fA-F]+)/?"
)
# 画廊页里指向图片页的链接：/s/<hash>/<gid>-<序号>
IMAGE_PAGE_RE = re.compile(r"/s/[0-9a-fA-F]+/(\d+)-(\d+)")
# 图片页里的原图：<img id="img" src="...">
IMG_SRC_RE = re.compile(r'<img[^>]+id="img"[^>]+src="([^"]+)"')
IMG_SRC_FALLBACK_RE = re.compile(r'id="img".{0,300}?src="([^"]+)"')

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


def extract_gallery_links(text: str) -> list[dict]:
    """从一段文本里提取去重后的画廊链接，保留第一次出现的顺序。"""
    seen: set[str] = set()
    out: list[dict] = []
    for m in GALLERY_RE.finditer(text or ""):
        url = m.group(0)
        host = urlparse(url).netloc
        gid, token = m.group(1), m.group(2).lower()
        key = f"{host}|{gid}|{token}"
        if key in seen:
            continue
        seen.add(key)
        out.append({"url": url, "host": host, "gid": gid, "token": token})
    return out


def sanitize_filename(name: str, limit: int = 80) -> str:
    """标题转文件夹名：去掉文件系统非法字符，截断，空标题兜底。"""
    cleaned = re.sub(r'[\\/:*?"<>|]', "_", (name or "").strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned[:limit] or "untitled"


def parse_image_page_links(html: str, gid: str) -> list[int]:
    """从画廊页 HTML 里解析属于该 gid 的图片序号，去重并排序。"""
    indexes: set[int] = set()
    for page_gid, idx in IMAGE_PAGE_RE.findall(html or ""):
        if page_gid == gid:
            indexes.add(int(idx))
    return sorted(indexes)


def parse_image_src(html: str) -> str | None:
    """从图片页 HTML 里解析原图地址，找不到返回 None。"""
    m = IMG_SRC_RE.search(html or "") or IMG_SRC_FALLBACK_RE.search(html or "")
    return m.group(1) if m else None


def generate_zip_password() -> str:
    """随机 6 位数字密码，保留前导 0（用 secrets，不用 random）。"""
    return f"{secrets.randbelow(1_000_000):06d}"


def create_encrypted_zip(folder: Path, zip_path: Path, password: str) -> str:
    """把 folder 里的图片打成加密 zip，返回加密方式。

    首选 pyzipper（AES-256，7-Zip / WinZip 都能打开）；
    没装 pyzipper 时退到系统 `zip -P`（传统 ZipCrypto，较弱）；
    两者都没有就抛错，绝不悄悄发一个没加密的包。
    这是同步阻塞函数，调用方请用 asyncio.to_thread 跑。
    """
    images = sorted(
        p for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in
        (".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif")
        and p.stat().st_size > 0
    )
    if not images:
        raise RuntimeError("图片文件夹里没有可压缩的图片")
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    if zip_path.exists():
        zip_path.unlink()
    try:
        import pyzipper

        with pyzipper.AESZipFile(
            zip_path, "w", compression=pyzipper.ZIP_DEFLATED,
            encryption=pyzipper.WZ_AES,
        ) as zf:
            zf.setpassword(password.encode("utf-8"))
            zf.setencryption(pyzipper.WZ_AES, nbits=256)
            for img in images:
                zf.write(img, arcname=img.name)
        return "AES-256 (pyzipper)"
    except ImportError:
        pass
    if shutil.which("zip") is None:
        raise RuntimeError(
            "未安装 pyzipper 且系统没有 zip 命令，无法生成加密压缩包，"
            "请先 pip install pyzipper"
        )
    result = subprocess.run(
        ["zip", "-q", "-P", password, "-j", str(zip_path)]
        + [str(p) for p in images],
        capture_output=True, text=True, timeout=3600,
    )
    if result.returncode != 0 or not zip_path.exists():
        raise RuntimeError(f"zip 命令压缩失败：{result.stderr.strip()[:200]}")
    return "ZipCrypto (zip -P)"


def _message_text(event: AstrMessageEvent) -> str:
    text = getattr(event, "message_str", "")
    if isinstance(text, str) and text.strip():
        return text
    raw = getattr(getattr(event, "message_obj", None), "message_str", "")
    return raw if isinstance(raw, str) else ""


def _origin_umo(event: AstrMessageEvent) -> str:
    """触发会话的 unified_msg_origin，格式如 aiocqhttp:GroupMessage:123456。"""
    for attr in ("unified_msg_origin", "umo"):
        value = getattr(event, attr, "")
        if isinstance(value, str) and value:
            return value
    return ""


def _is_valid_image_file(path: Path) -> bool:
    """检查一个已落盘的图片是不是真的完整图片，而不是残文件/错误页。

    旧版只看 size > 0 就算成功：中途截断但没抛异常、或服务器返回了
    HTML 错误页时，文件有大小却打不开/空白，之后重跑又会因为
    size > 0 直接跳过，残图就这样进了压缩包。
    检查分三层，越往后越严，缺 PIL 时只做前两层：
    1. 文件头 magic：JPEG 以 FF D8 开头、PNG 以 PNG 头开头、
       WebP 以 RIFF....WEBP 开头且 RIFF 长度字段要等于实际大小、
       GIF 以 GIF87a/89a 开头，HTML（<html / <!DOCTYPE）直接判坏。
    2. 文件尾：JPEG 必须以 FF D9 结尾、PNG 必须以 IEND 结尾、
       WebP 的 RIFF 长度必须对得上，截断的文件尾一定对不上。
    3. PIL 可用时：能完整打开并 load，且不是纯色空白图
       （全像素同一个颜色，常见于占位/空白图）。
    """
    try:
        size = path.stat().st_size
        if size <= 0:
            return False
        with path.open("rb") as fh:
            head = fh.read(16)
            fh.seek(-16, 2) if size >= 16 else fh.seek(0)
            tail = fh.read()
        if head.lstrip()[:1] == b"<":
            return False
        if head.startswith(b"\xff\xd8"):
            if not tail.endswith(b"\xff\xd9"):
                return False
        elif head.startswith(b"\x89PNG"):
            if not tail.endswith(b"IEND\xaeB`\x82"):
                return False
        elif head.startswith(b"RIFF") and head[8:12] == b"WEBP":
            import struct

            riff_size = struct.unpack("<I", head[4:8])[0]
            if riff_size + 8 != size:
                return False
        elif head.startswith(b"GIF87a") or head.startswith(b"GIF89a"):
            pass
        else:
            return False
        try:
            from PIL import Image

            with Image.open(path) as im:
                im.load()
                # 纯色图：灰度极值相等，基本是空白占位图
                try:
                    gray = im.convert("L")
                    if gray.getextrema()[0] == gray.getextrema()[1]:
                        return False
                except Exception:
                    pass
        except ImportError:
            pass
        except Exception:
            return False
        return True
    except Exception:
        return False


def _build_components(text: str, zip_path: Path | None = None):
    """只拼组件列表，不包 MessageChain。

    返回 (components, has_file)。
    File 构造：name 必填、file 为本地路径（无 file_path 参数的版本
    会在 file_path 尝试时失败，自动退到 file 形式）。
    """
    from astrbot.api.message_components import File, Plain

    components = [Plain(text=text)]
    if zip_path is None:
        return components, False
    file_comp = None
    for kwargs in (
        {"file": str(zip_path), "name": zip_path.name},
        {"name": zip_path.name, "file": str(zip_path)},
        {"file": str(zip_path)},
    ):
        try:
            file_comp = File(**kwargs)
            break
        except Exception:
            continue
    if file_comp is None:
        return components, False
    components.append(file_comp)
    return components, True


def _build_file_chain(text: str, zip_path: Path | None = None):
    """给 context.send_message 用的 MessageChain。

    v0.2.5 的教训：chain_result 和 send_message 要的东西不一样。
    chain_result 内部会对结果做 len(result.chain)，传 MessageChain
    对象过去会报 object of type 'MessageChain' has no len()；
    而 send_message 要的是 MessageChain 对象（定义在
    astrbot.core.message.message_event_result，不在 message_components）。
    所以这里专门包一层 MessageChain，chain_result 那边直接用列表。
    找不到 MessageChain 时退回列表本身。
    """
    components, has_file = _build_components(text, zip_path)
    for mod_name in (
        "astrbot.core.message.message_event_result",
        "astrbot.api.event",
        "astrbot.api.message_components",
    ):
        try:
            import importlib

            mod = importlib.import_module(mod_name)
            chain_cls = getattr(mod, "MessageChain", None)
            if chain_cls is None:
                continue
            try:
                return chain_cls(chain=components), has_file
            except Exception:
                continue
        except Exception:
            continue
    return components, has_file


@register(
    PLUGIN_NAME,
    "Newbie-L",
    "监听 ExHentai/E-Hentai 画廊链接，下载后压缩成加密包发到群聊",
    PLUGIN_VERSION,
    PLUGIN_REPO,
)
class ExHentaiDownloader(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self._config = config
        self.data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.data_dir / "downloads.json"
        self.state: dict[str, dict] = self._load_state()
        self._semaphore = asyncio.Semaphore(self._cfg("max_concurrent", 1))
        self._tasks: dict[str, asyncio.Task] = {}

    # ------------------------------------------------------------ 配置
    def _cfg(self, key: str, default):
        """兼容 AstrBotConfig（类 dict）和缺省值；布尔/数字做一次容错转换。"""
        value = default
        try:
            if self._config is not None:
                value = self._config.get(key, default)
        except Exception:
            value = default
        if isinstance(default, bool) and isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        if isinstance(default, int) and isinstance(value, str):
            try:
                return int(value)
            except ValueError:
                return default
        if isinstance(default, float) and isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                return default
        return value

    def _download_dir(self) -> Path:
        raw = str(self._cfg("download_dir", "") or "").strip()
        path = Path(raw).expanduser() if raw else self.data_dir / "downloads"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _cookies(self) -> dict[str, str]:
        cookies = {}
        for key in ("ipb_member_id", "ipb_pass_hash", "igneous"):
            value = str(self._cfg(key, "") or "").strip()
            if value:
                cookies[key] = value
        return cookies

    def _is_private(self, event: AstrMessageEvent) -> bool:
        try:
            if hasattr(event, "is_private_chat"):
                return bool(event.is_private_chat())
        except Exception:
            pass
        # 兜底：按消息类型字符串判断，判断不出来时按私聊处理（宁可少触发）
        msg_type = str(getattr(event, "message_type", "") or "")
        return "private" in msg_type.lower() or "friend" in msg_type.lower()

    # ------------------------------------------------------------ 状态
    def _load_state(self) -> dict[str, dict]:
        try:
            if self.state_file.exists():
                data = json.loads(self.state_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
        except Exception as exc:
            logger.warning(f"[{PLUGIN_NAME}] 读取下载状态失败：{exc}")
        return {}

    def _save_state(self) -> None:
        try:
            self.state_file.write_text(
                json.dumps(self.state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning(f"[{PLUGIN_NAME}] 保存下载状态失败：{exc}")

    def _set_state(self, gid: str, **fields) -> None:
        item = self.state.setdefault(gid, {})
        item.update(fields)
        item["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self._save_state()

    # ------------------------------------------------------------ 入口
    @filter.regex(r"https?://(?:exhentai\.org|e-hentai\.org)/g/\d+/[0-9a-fA-F]+")
    async def on_gallery_link(self, event: AstrMessageEvent):
        """自动监听：私聊里的画廊链接直接进队列，群聊默认不触发。"""
        if self._cfg("only_private", True) and not self._is_private(event):
            return
        links = extract_gallery_links(_message_text(event))
        if not links:
            return
        lines = []
        for link in links:
            lines.append(self._enqueue(link, origin_umo=_origin_umo(event)))
        yield event.plain_result("\n".join(lines))

    @filter.command("ex_dl")
    async def manual_download(self, event: AstrMessageEvent):
        """/ex_dl <画廊链接>：手动加入下载队列（链接没被自动监听到时用它）。"""
        links = extract_gallery_links(_message_text(event))
        if not links:
            yield event.plain_result(
                "用法：/ex_dl https://exhentai.org/g/1234567/abcdef1234/\n"
                "私聊直接发链接也可以自动触发，不用加命令。"
            )
            return
        lines = [self._enqueue(link, origin_umo=_origin_umo(event))
                 for link in links]
        yield event.plain_result("\n".join(lines))

    @filter.command("ex_send")
    async def resend_zip(self, event: AstrMessageEvent):
        """/ex_send <gid>：把已压缩好的包和密码发到当前会话（群/私聊都行）。"""
        gids = re.findall(r"\b\d{5,}\b", _message_text(event))
        if not gids:
            yield event.plain_result("用法：/ex_send <gid>，例如 /ex_send 1456937")
            return
        item = self.state.get(gids[0])
        if not item or not item.get("zip_path"):
            yield event.plain_result(
                f"gid={gids[0]} 还没有压缩包，先等下载完成或查 /ex_status {gids[0]}。"
            )
            return
        zip_path = Path(item["zip_path"])
        if not zip_path.exists():
            yield event.plain_result(f"压缩包找不到了：{zip_path}")
            return
        text = (
            f"📦 {item.get('title') or gids[0]}\n"
            f"压缩包：{zip_path.name}\n"
            f"解压密码：{item.get('zip_password', '')}"
        )
        try:
            # chain_result 要的是组件列表，不是 MessageChain 对象。
            # 传 MessageChain 过去会在 result_decorate 里 len(result.chain)
            # 报错：object of type 'MessageChain' has no len()。
            chain, has_file = _build_components(text, zip_path)
            if hasattr(event, "chain_result"):
                self._set_state(gids[0], send_status="sent_manually")
                yield event.chain_result(chain)
                return
        except Exception as exc:
            logger.warning(f"[{PLUGIN_NAME}] 构造文件消息链失败：{exc}")
        yield event.plain_result(
            f"当前版本/平台发文件失败，包在服务器上：{zip_path}\n{text}"
        )

    @filter.command("ex_verify")
    async def verify_images(self, event: AstrMessageEvent):
        """/ex_verify <gid>：校验已下载的图片，把残图/空白图删掉并重下。

        只看 size > 0 会漏掉残图：截断的文件、HTML 错误页、空白占位图
        都有大小，打开却是空白的，而且重跑 /ex_dl 时又会因为已有文件
        直接跳过，残图就一直留在包里。这个命令先逐张验 magic/尾部/
        PIL（有 PIL 时还验纯色空白），把坏的删掉，再按原链接重跑，
        只补坏的那几张，补完重新压缩发送。
        """
        gids = re.findall(r"\b\d{5,}\b", _message_text(event))
        if not gids:
            yield event.plain_result("用法：/ex_verify <gid>，例如 /ex_verify 4285225")
            return
        gid = gids[0]
        item = self.state.get(gid)
        if not item or not item.get("folder"):
            yield event.plain_result(f"没找到 gid={gid} 的图片目录，先查 /ex_status {gid}。")
            return
        folder = Path(item["folder"])
        if not folder.exists():
            yield event.plain_result(f"图片目录找不到了：{folder}")
            return
        bad: list[int] = []
        for p in sorted(folder.iterdir()):
            m = re.match(r"(\d{4})\.(.+)$", p.name)
            if not m or p.suffix == ".part":
                continue
            idx = int(m.group(1))
            if not _is_valid_image_file(p):
                bad.append(idx)
                try:
                    p.unlink()
                except Exception:
                    pass
        if not bad:
            yield event.plain_result(f"gid={gid} 校验通过：图片目录里没有残图/空白图。")
            return
        # 把状态改回 partial，让 _enqueue 走补下分支，只补坏的序号
        total = item.get("total") or 0
        downloaded = max(0, (item.get("downloaded") or 0) - len(bad))
        self._set_state(gid, status="partial", missing=bad,
                        downloaded=downloaded, total=total,
                        send_status="not_sent_missing_images")
        link = {"gid": gid, "token": item.get("token", ""),
                "host": "exhentai.org", "url": item.get("url", "")}
        # host 从原链接里恢复，避免默认 exhentai 误判 e-hentai 画廊
        url = item.get("url") or ""
        if "e-hentai.org" in url:
            link["host"] = "e-hentai.org"
        msg = self._enqueue(link, origin_umo=_origin_umo(event))
        yield event.plain_result(
            f"gid={gid} 校验发现坏图：{bad}，已删掉并开始重下，"
            f"补完会重新压缩发送。\n{msg}"
        )

    @filter.command("ex_help")
    async def show_help(self, event: AstrMessageEvent):
        """/ex_help：列出本插件所有命令。"""
        yield event.plain_result(
            "astrbot_plugin_exhentai 命令：\n"
            "• 私聊发画廊链接：自动下载（默认只在私聊自动触发，群里不自动下）\n"
            "• /ex_dl <链接>：手动加入下载队列\n"
            "• /ex_status [gid]：查进度、压缩包路径、密码、缺图序号\n"
            "• /ex_list：列出已完成的画廊\n"
            "• /ex_send <gid>：把已压缩好的包和密码发到当前会话\n"
            "• /ex_verify <gid>：校验图片，删掉残图/空白图并重下，补完重新压缩发送\n"
            "• /ex_help：显示这份帮助\n"
            "下载完且无缺图时会自动压缩成加密 zip（随机 6 位密码，包名=画廊标题）"
            "并按触发会话发送：群触发发回群，私聊触发发回私聊；"
            "没配 target_group_umo 时也一样，配了就优先发到配置的群。"
            "缺图时会标 partial、不发不完整包，再跑 /ex_dl 只补缺图。"
        )

    @filter.command("ex_status")
    async def download_status(self, event: AstrMessageEvent):
        """/ex_status [gid]：查某个画廊的进度，不带 gid 则列出最近的任务。"""
        text = _message_text(event)
        gids = re.findall(r"\b\d{5,}\b", text)
        if gids:
            item = self.state.get(gids[0])
            if not item:
                yield event.plain_result(f"没找到 gid={gids[0]} 的任务，先发链接或 /ex_dl。")
                return
            yield event.plain_result(self._format_item(gids[0], item))
            return
        if not self.state:
            yield event.plain_result("还没有下载任务。私聊发一条画廊链接试试。")
            return
        lines = ["下载任务："]
        for gid, item in list(self.state.items())[-10:]:
            lines.append(self._format_item(gid, item))
        yield event.plain_result("\n".join(lines))

    @filter.command("ex_list")
    async def downloaded_list(self, event: AstrMessageEvent):
        """/ex_list：列出已下载完成（含压缩包）的画廊。"""
        done = [(g, i) for g, i in self.state.items()
                if i.get("status") in ("done", "sent", "zip_ready", "partial")]
        if not done:
            yield event.plain_result("还没有下载完成的画廊。")
            return
        lines = ["已下载完成："]
        for gid, item in done:
            lines.append(
                f"- {gid} {item.get('title', '')} -> "
                f"{item.get('zip_path') or item.get('folder', '')}"
            )
        yield event.plain_result("\n".join(lines))

    async def terminate(self):
        for task in list(self._tasks.values()):
            if not task.done():
                task.cancel()
        logger.info(f"[{PLUGIN_NAME}] 插件已卸载")

    # ------------------------------------------------------------ 队列
    def _enqueue(self, link: dict, origin_umo: str = "") -> str:
        gid = link["gid"]
        item = self.state.get(gid, {})
        # 旧版已完成但实际缺图（downloaded < total）也视为未完成，允许补下。
        # 新版缺图状态是 partial，同样允许重进队列只补缺的序号。
        already_done = (
            item.get("status") in ("done", "sent", "zip_ready")
            and not (
                (item.get("total") or 0) > 0
                and (item.get("downloaded") or 0) < item["total"]
            )
        )
        if already_done:
            return (
                f"✅ gid={gid} 已经下载过："
                f"{item.get('zip_path') or item.get('folder', '')}"
            )
        # 正在跑的任务才算在队列里。重启插件后 _tasks 是空的，
        # 旧状态还停在 downloading 时不能直接说“已在队列”，否则永远卡住。
        _live = self._tasks.get(gid)
        if item.get("status") in ("queued", "downloading", "zipping") and _live is not None and not _live.done():
            return f"⏳ gid={gid} 已在队列中：{item.get('title') or link['url']}"
        if item.get("status") == "partial" or (
            item.get("status") in ("done", "sent", "zip_ready")
            and (item.get("total") or 0) > 0
            and (item.get("downloaded") or 0) < item["total"]
        ):
            # 补下：保留已下载的图片和原压缩包路径，只重跑下载循环，
            # _download_one 里已存在的文件会跳过，只补缺的序号。
            task = asyncio.create_task(self._download_one(link))
            self._tasks[gid] = task
            self._set_state(gid, status="queued", origin_umo=origin_umo,
                            error="")
            missing = item.get("missing") or []
            return (
                f"🔁 gid={gid} 上次缺图，正在补下"
                f"{f'（缺：{missing}）' if missing else ''}，"
                f"补完会重新压缩并按触发会话发送。"
            )
        self._set_state(gid, url=link["url"], host=link["host"],
                       token=link["token"], status="queued",
                       title="", downloaded=0, total=0, error="",
                       origin_umo=origin_umo, zip_path="", zip_password="",
                       send_status="", missing=[])
        task = asyncio.create_task(self._download_one(link))
        self._tasks[gid] = task
        if link["host"] == "exhentai.org" and not self._cookies():
            return (
                f"⏳ gid={gid} 已加入队列，但未填 exhentai 登录 cookie，"
                f"很可能下载失败。请先在插件配置里填 ipb_member_id / "
                f"ipb_pass_hash / igneous，再 /ex_dl 重试。"
            )
        return f"⏳ gid={gid} 已加入下载队列，下载完会压缩成加密包发到群里，完成后用 /ex_status {gid} 查看。"

    @staticmethod
    def _format_item(gid: str, item: dict) -> str:
        status = item.get("status", "?")
        total = item.get("total") or 0
        done = item.get("downloaded") or 0
        title = item.get("title") or item.get("url", "")
        line = f"- {gid} [{status}] {done}/{total} {title}"
        if item.get("current_idx"):
            line += f"\n  当前正在下第 {item['current_idx']} 张"
        if item.get("missing"):
            line += f"\n  缺图序号：{item['missing']}"
        if item.get("folder"):
            line += f"\n  图片目录：{item['folder']}"
        if item.get("zip_path"):
            line += f"\n  压缩包：{item['zip_path']}"
            if item.get("zip_password"):
                line += f"\n  解压密码：{item['zip_password']}"
            if item.get("send_status"):
                line += f"\n  发送状态：{item['send_status']}"
        if item.get("error"):
            line += f"\n  错误：{item['error']}"
        return line

    # ------------------------------------------------------------ 发群
    def _send_target(self, gid: str) -> str:
        """决定发到哪个会话：配置的群优先，其次是触发时的会话。

        之前这里只认 GroupMessage，私聊触发会返回空、导致 no_target、
        压缩包明明生成了却哪里都不发——这是 v0.2.1 的 bug。
        改成：没配目标群时就发回触发会话，群触发发回群，私聊触发发回私聊。
        """
        configured = str(self._cfg("target_group_umo", "") or "").strip()
        if configured:
            return configured
        origin = str(self.state.get(gid, {}).get("origin_umo") or "")
        return origin

    async def _send_zip_to_target(self, gid: str, title: str,
                                  zip_path: Path, password: str) -> str:
        """后台任务里主动发到目标群，返回 send_status。"""
        if not self._cfg("send_zip_to_group", True):
            return "disabled_by_config"
        target = self._send_target(gid)
        if not target:
            return "no_target"
        text = (
            f"📦 {title}\n"
            f"压缩包：{zip_path.name}\n"
            f"解压密码：{password}"
        )
        try:
            chain, has_file = _build_file_chain(text, zip_path)
            await self.context.send_message(target, chain)
            return "sent" if has_file else "text_only_file_failed"
        except Exception as exc:
            logger.warning(f"[{PLUGIN_NAME}] 发到 {target} 失败：{exc}")
            # 降级：只发文本，至少让对方拿到密码和包名
            try:
                chain, _ = _build_file_chain(
                    text + "\n（文件发送失败，包在服务器上）", None
                )
                await self.context.send_message(target, chain)
                return "text_only_file_failed"
            except Exception as exc2:
                logger.warning(f"[{PLUGIN_NAME}] 降级发文本也失败：{exc2}")
                return f"failed: {exc}"[:200]

    # ------------------------------------------------------------ 下载
    async def _download_one(self, link: dict) -> None:
        gid, token, host = link["gid"], link["token"], link["host"]
        delay = float(self._cfg("download_delay", 1.0))
        async with self._semaphore:
            try:
                self._set_state(gid, status="downloading")
                async with aiohttp.ClientSession(
                    headers={"User-Agent": UA}, cookies=self._cookies()
                ) as session:
                    meta = await self._fetch_metadata(session, host, gid, token)
                    title = (meta or {}).get("title") or f"gallery_{gid}"
                    total = 0
                    try:
                        total = int((meta or {}).get("filecount") or 0)
                    except (TypeError, ValueError):
                        total = 0
                    folder = self._download_dir() / f"{gid}_{sanitize_filename(title)}"
                    folder.mkdir(parents=True, exist_ok=True)
                    self._set_state(gid, title=title, total=total,
                                    folder=str(folder))
                    (folder / "metadata.json").write_text(
                        json.dumps(meta or {"gid": gid, "token": token, "title": title},
                                   ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    image_pages = await self._collect_image_pages(
                        session, host, gid, token, total
                    )
                    if not image_pages:
                        raise RuntimeError(
                            "没解析到任何图片页：可能是 cookie 失效、画廊被删、"
                            "或被限流（509），请先检查 exhentai 登录状态"
                        )
                    # 总数以 metadata 为准：如果解析到的图片页少于总数，
                    # 超出的序号也算缺图，不能悄悄截断成“已完成”。
                    final_total = total if total else len(image_pages)
                    if total and len(image_pages) < total:
                        logger.warning(
                            f"[{PLUGIN_NAME}] gid={gid} 只解析到 "
                            f"{len(image_pages)}/{total} 张图片页"
                        )
                    downloaded = 0
                    missing: list[int] = []
                    # 先把未解析到的尾部序号记为缺图，下载循环只跑已解析的页
                    unparsed_missing = (
                        list(range(len(image_pages) + 1, total + 1))
                        if total and len(image_pages) < total else []
                    )
                    for idx, page_url in enumerate(image_pages, start=1):
                        # 状态每张都更新，之前只每 10 张更新一次，
                        # 查状态时会误以为卡在旧数字上不动。
                        self._set_state(gid, current_idx=idx,
                                        downloaded=downloaded,
                                        total=final_total,
                                        missing=missing + unparsed_missing)
                        ok = await self._download_one_image(
                            session, page_url, folder, idx
                        )
                        if ok:
                            downloaded += 1
                        else:
                            missing.append(idx)
                        self._set_state(gid, current_idx=idx,
                                        downloaded=downloaded,
                                        total=final_total,
                                        missing=missing + unparsed_missing)
                        await asyncio.sleep(delay)
                    missing = missing + unparsed_missing
                    # 缺图自动重下：第一遍没下到的序号再单独跑最多 2 轮，
                    # 每轮间隔拉长一点，避免 509 时硬刷。2 轮后还缺的，
                    # 才标 partial，等用户再跑 /ex_dl 补下，不发不完整包。
                    for retry_round in range(2):
                        if not missing:
                            break
                        logger.warning(
                            f"[{PLUGIN_NAME}] gid={gid} 第 {retry_round + 1} 轮"
                            f"重下缺图：{missing}"
                        )
                        await asyncio.sleep(delay * 2)
                        still_missing: list[int] = []
                        for idx in list(missing):
                            if idx > len(image_pages):
                                # 未解析到的尾部序号没有图片页可重试，
                                # 留到下次重跑时重新收集画廊分页再补。
                                still_missing.append(idx)
                                continue
                            page_url = image_pages[idx - 1]
                            ok = await self._download_one_image(
                                session, page_url, folder, idx
                            )
                            if ok:
                                downloaded += 1
                            else:
                                still_missing.append(idx)
                            await asyncio.sleep(delay)
                        missing = still_missing
                        self._set_state(gid, downloaded=downloaded,
                                        total=final_total,
                                        missing=missing)
                    self._set_state(gid, status="done", downloaded=downloaded,
                                    total=final_total, missing=missing,
                                    current_idx=0)
                    logger.info(
                        f"[{PLUGIN_NAME}] gid={gid} 下载完成 "
                        f"{downloaded}/{final_total} -> {folder} "
                        f"缺图：{missing}"
                    )
                    if downloaded == 0:
                        # 一张都没下成功就不是 done，避免误导
                        self._set_state(
                            gid, status="failed",
                            error="一张图都没下成功，请检查 cookie / 509 限流后重试",
                        )
                        return
                    if missing:
                        # 缺图时先不发群，避免把不完整的包当成完整包发出。
                        # 状态标 partial，再跑 /ex_dl 会只补缺的序号。
                        self._set_state(gid, status="partial")
                        logger.warning(
                            f"[{PLUGIN_NAME}] gid={gid} 缺图 {missing}，"
                            f"暂不发群，等补下完成后再压缩发送"
                        )
                        # 仍先打一个现有图片的包备查，但不自动发送
                        self._set_state(gid, status="zipping")
                        zip_name = f"{sanitize_filename(title, 120)}.zip"
                        zip_path = self._download_dir() / zip_name
                        password = generate_zip_password()
                        try:
                            method = await asyncio.to_thread(
                                create_encrypted_zip, folder, zip_path, password
                            )
                        except Exception as exc:
                            logger.warning(
                                f"[{PLUGIN_NAME}] gid={gid} 压缩失败：{exc}"
                            )
                            self._set_state(
                                gid, status="partial", error=str(exc)[:300]
                            )
                            return
                        self._set_state(
                            gid, status="partial", zip_path=str(zip_path),
                            zip_password=password, zip_method=method,
                            send_status="not_sent_missing_images",
                        )
                        return
                    # ---- 下载完且无缺图：压缩成加密 zip，包名 = 画廊标题 ----
                    if downloaded > 0:
                        self._set_state(gid, status="zipping")
                        zip_name = f"{sanitize_filename(title, 120)}.zip"
                        zip_path = self._download_dir() / zip_name
                        password = generate_zip_password()
                        try:
                            method = await asyncio.to_thread(
                                create_encrypted_zip, folder, zip_path, password
                            )
                        except Exception as exc:
                            logger.warning(
                                f"[{PLUGIN_NAME}] gid={gid} 压缩失败：{exc}"
                            )
                            self._set_state(
                                gid, status="zip_failed", error=str(exc)[:300]
                            )
                            return
                        self._set_state(
                            gid, status="zip_ready", zip_path=str(zip_path),
                            zip_password=password, zip_method=method,
                        )
                        if self._cfg("delete_images_after_zip", False):
                            # 只删图片文件，metadata.json 留着备查
                            for p in folder.iterdir():
                                if p.is_file() and p.name != "metadata.json":
                                    try:
                                        p.unlink()
                                    except Exception:
                                        pass
                        # ---- 压缩完：发到群聊 ----
                        send_status = await self._send_zip_to_target(
                            gid, title, zip_path, password
                        )
                        final_status = "sent" if send_status == "sent" else "zip_ready"
                        self._set_state(
                            gid, status=final_status, send_status=send_status
                        )
                        logger.info(
                            f"[{PLUGIN_NAME}] gid={gid} 压缩包 {zip_path} "
                            f"密码已生成，发送状态：{send_status}"
                        )
            except asyncio.CancelledError:
                self._set_state(gid, status="cancelled")
                raise
            except Exception as exc:
                logger.warning(f"[{PLUGIN_NAME}] gid={gid} 下载失败：{exc}")
                self._set_state(gid, status="failed", error=str(exc)[:300])

    async def _fetch_metadata(self, session, host: str, gid: str, token: str):
        """调官方 gdata 接口拿标题和总页数；失败返回 None，调用方会降级。"""
        payload = {"method": "gdata", "gidlist": [[int(gid), token]], "namespace": 1}
        try:
            async with session.post(
                f"https://{host}/api.php", json=payload, timeout=30
            ) as resp:
                if resp.status != 200:
                    logger.warning(
                        f"[{PLUGIN_NAME}] gdata 返回 {resp.status}，降级为页面解析"
                    )
                    return None
                data = await resp.json(content_type=None)
                items = data.get("gmetadata") or []
                return items[0] if items else None
        except Exception as exc:
            logger.warning(f"[{PLUGIN_NAME}] gdata 请求失败：{exc}")
            return None

    async def _collect_image_pages(self, session, host: str, gid: str,
                                   token: str, total: int) -> list[str]:
        """翻画廊分页，收集全部图片页 URL。

        v0.2.2 之前的 bug：假设每页 40 张、本页不足 40 张就停。
        如果账号设置是每页 20 张，第一页 20 张就会提前停，
        44 张的画廊只会拿到前 20 张，状态就从 20/20 变成 44 这种
        让人困惑的数字。改成：只有本页没解析出任何新图片页时才停，
        否则一直翻到总数（metadata 的 filecount）为止。
        """
        pages: list[str] = []
        seen: set[str] = set()
        page_no = 0
        while True:
            url = f"https://{host}/g/{gid}/{token}/?p={page_no}"
            async with session.get(url, timeout=30) as resp:
                if resp.status != 200:
                    logger.warning(
                        f"[{PLUGIN_NAME}] 画廊页 {url} 返回 {resp.status}"
                    )
                    break
                html = await resp.text(errors="ignore")
            new_urls = self._extract_page_urls(html, gid)
            new_count = 0
            for page_url in new_urls:
                if page_url not in seen:
                    seen.add(page_url)
                    pages.append(page_url)
                    new_count += 1
            if total and len(pages) >= total:
                break
            if new_count == 0:
                break
            page_no += 1
            if page_no > 500:  # 安全上限，防止死循环
                break
            await asyncio.sleep(float(self._cfg("download_delay", 1.0)))
        return sorted(pages, key=lambda u: int(u.rsplit("-", 1)[-1]))

    @staticmethod
    def _extract_page_urls(html: str, gid: str) -> list[str]:
        """从画廊页 HTML 里原样取出属于该 gid 的图片页完整链接，按序号排序。"""
        found: dict[int, str] = {}
        for href in re.findall(r'href="([^"]+/s/[0-9a-fA-F]+/\d+-\d+)"', html or ""):
            m = re.search(r"/(\d+)-(\d+)$", href)
            if m and m.group(1) == gid:
                found.setdefault(int(m.group(2)), href)
        return [found[i] for i in sorted(found)]

    async def _download_one_image(self, session, page_url: str,
                                  folder: Path, idx: int) -> bool:
        """下一张图：图片页 -> 原图地址 -> 落盘，失败重试 5 次，支持断点续传。

        v0.2.4 之前的中断处理是 bug：下载中途 TLS 断掉（比如
        ContentLengthError，27 号每次都卡在 273020/275256 字节，
        只差最后 2KB），旧代码会在 except 里把 .part 删掉，
        下一次又从 0 开始，永远在同一个位置断掉，浪费重试次数。
        改成：.part 不删，下一次带 Range: bytes=<已下>- 续传，
        服务器返回 206 就追加，返回 200（不支持 Range）才从头重下；
        只有完整（大小等于 Content-Length / Content-Range 总数，
        或无长度信息时成功读完）才重命名为正式文件。
        已存在且大于 0 的正式文件才算成功，直接跳过。
        """
        for attempt in range(5):
            try:
                async with session.get(page_url, timeout=30) as resp:
                    if resp.status != 200:
                        if resp.status == 509:
                            logger.warning(
                                f"[{PLUGIN_NAME}] 509 限流，等待 60 秒再试：{page_url}"
                            )
                            await asyncio.sleep(60)
                            continue
                        return False
                    html = await resp.text(errors="ignore")
                src = parse_image_src(html)
                if not src:
                    return False
                ext = Path(urlparse(src).path).suffix or ".jpg"
                if len(ext) > 5 or "/" in ext:
                    ext = ".jpg"
                # 先看有没有同序号的旧文件（扩展名可能和这次不同），
                # 完好的直接跳过，残图/空白图删掉重下，避免旧残图进包。
                for old in folder.glob(f"{idx:04d}.*"):
                    if old.suffix == ".part" or old.name == "metadata.json":
                        continue
                    if _is_valid_image_file(old):
                        return True
                    logger.warning(
                        f"[{PLUGIN_NAME}] 图片 {idx} 旧文件校验失败，删掉重下：{old}"
                    )
                    try:
                        old.unlink()
                    except Exception:
                        pass
                dest = folder / f"{idx:04d}{ext}"
                if dest.exists() and dest.stat().st_size > 0:
                    # 有文件不等于图片完好：先验 magic/尾部/PIL，
                    # 残图/空白图要删掉重下，不能直接跳过。
                    if _is_valid_image_file(dest):
                        return True
                    logger.warning(
                        f"[{PLUGIN_NAME}] 图片 {idx} 已有文件但校验失败，删掉重下：{dest}"
                    )
                    try:
                        dest.unlink()
                    except Exception:
                        pass
                tmp = folder / f"{dest.name}.part"
                resume_from = tmp.stat().st_size if tmp.exists() else 0
                headers = {"Referer": page_url}
                if resume_from > 0:
                    headers["Range"] = f"bytes={resume_from}-"
                async with session.get(
                    src, timeout=60, headers=headers,
                ) as resp:
                    if resp.status == 416:
                        # Range 超出文件实际长度：说明 .part 已经不小于
                        # 服务器上的文件，可能是长度头本身有误，保留 .part
                        # 供人工检查，但不把它当成功，避免发残图。
                        logger.warning(
                            f"[{PLUGIN_NAME}] 图片 {idx} Range 超出范围，"
                            f".part 已有 {resume_from} 字节"
                        )
                        return False
                    if resp.status not in (200, 206):
                        return False
                    if resp.status == 200 and resume_from > 0:
                        # 服务器不支持 Range，从头重下
                        resume_from = 0
                    # 总长度：206 看 Content-Range 的总数，200 看 Content-Length
                    total_size = None
                    content_range = resp.headers.get("Content-Range", "")
                    if "/" in content_range:
                        try:
                            total_size = int(content_range.rsplit("/", 1)[-1])
                        except ValueError:
                            total_size = None
                    if total_size is None:
                        try:
                            cl = int(resp.headers.get("Content-Length") or 0)
                            total_size = (resume_from + cl) if cl else None
                        except ValueError:
                            total_size = None
                    mode = "ab" if resp.status == 206 and resume_from > 0 else "wb"
                    with tmp.open(mode) as fh:
                        async for chunk in resp.content.iter_chunked(65536):
                            fh.write(chunk)
                final_size = tmp.stat().st_size if tmp.exists() else 0
                if final_size > 0 and (total_size is None or final_size >= total_size):
                    tmp.replace(dest)
                    return True
                # 没下完：留着 .part，下一次续传，不删
                logger.warning(
                    f"[{PLUGIN_NAME}] 图片 {idx} 未下完 "
                    f"{final_size}/{total_size or '?'} 字节，留 .part 续传"
                )
                await asyncio.sleep(2 * (attempt + 1))
            except Exception as exc:
                logger.warning(
                    f"[{PLUGIN_NAME}] 图片 {idx} 第 {attempt + 1} 次失败：{exc}"
                )
                # 关键：不要删 .part，留给下一次 Range 续传
                await asyncio.sleep(2 * (attempt + 1))
        return False
