"""wb_update.py —— 检查更新与就地升级。

替代原先"下载整个更新包再手动覆盖"的做法。区别在于：

* **只查一次接口**，告诉用户有没有新版本、新版是什么；
* **就地升级**：下载新版的程序文件，替换 EXE 与 _internal/，
  账号、用量、设置全都不动 —— 与更新程序同一套逻辑；
* **失败可回退**：升级前自动备份，任一步出错把原文件放回去。

为什么不直接调用 GitHub API 查版本：未认证的 API 有速率限制，
而且国内直连常常不通。这里用 **releases 页面的重定向** 取版本号 ——
`/releases/latest` 会 302 到实际 tag，不需要认证也不吃速率限制。

只用标准库。网络请求一律走显式构造的 URL，并限制在 GitHub 域名内。
"""

import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

import wb_proxy
import wb_runtime

#: Hosts the update machinery is willing to talk to. Checked before every
#: request so a bad repository value cannot send the download elsewhere.
ALLOWED_HOSTS = (
    "github.com",
    "api.github.com",
    "objects.githubusercontent.com",
    "codeload.github.com",
    "raw.githubusercontent.com",
)

#: Optional proxy, e.g. "http://127.0.0.1:7890". Honoured because GitHub is
#: frequently unreachable directly from mainland networks.
PROXY_ENV = "WB_UPDATE_PROXY"

#: Seconds before a download is considered stalled.
CONNECT_TIMEOUT = 20
DOWNLOAD_TIMEOUT = 300


class UpdateError(Exception):
    """A failure with a message worth showing to the user."""


def _opener():
    """A urlopen wrapper honouring the optional proxy setting."""
    proxy = (os.environ.get(PROXY_ENV) or "").strip()
    if not proxy:
        return urllib.request.urlopen
    handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
    return urllib.request.build_opener(handler).open


def _checked(url, allow_redirect=False):
    """Return ``url`` after confirming it targets an allowed host.

    The destinations here are assembled from constants, but a repository name
    or a server-supplied redirect could point elsewhere, so the check is made
    every time rather than once.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UpdateError("拒绝非 http(s) 地址：%s" % url)
    if parsed.hostname not in ALLOWED_HOSTS:
        raise UpdateError("拒绝未知主机：%s" % parsed.hostname)
    if parsed.scheme != "https" and not allow_redirect:
        raise UpdateError("更新只走 https")
    return url


def _version_tuple(text):
    """Turn "v1.6.1" into (1, 6, 1) for comparison. Missing parts are 0."""
    nums = re.findall(r"\d+", str(text or ""))
    parts = [int(n) for n in nums[:4]]
    while len(parts) < 4:
        parts.append(0)
    return tuple(parts)


def current_version():
    return str(wb_proxy.APP_VERSION)


def latest_release(timeout=CONNECT_TIMEOUT):
    """Look up the newest published release.

    Returns a dict with ``version``, ``tag``, ``notes`` and ``assets``. The
    version comes from the redirect target of /releases/latest, which needs no
    authentication and is not subject to the API rate limit that a shared
    address hits almost immediately.
    """
    repo = str(wb_proxy.RELEASE_REPO or "").strip()
    if not repo or "/" not in repo:
        raise UpdateError("未配置发布仓库")
    url = _checked("https://github.com/%s/releases/latest" % repo)

    request = urllib.request.Request(
        url, method="HEAD",
        headers={"User-Agent": "WorkBuddy2API-updater",
                 "Accept": "text/html"})
    try:
        response = _opener()(request, timeout=timeout)
        final = response.geturl() if hasattr(response, "geturl") else url
            # HEAD on /releases/latest redirects; the tag is the last segment.
        response.close()
    except urllib.error.HTTPError as exc:
        raise UpdateError("查询失败：HTTP %s" % exc.code)
    except Exception as exc:
        raise UpdateError("查询失败：%s" % exc)

    _checked(final, allow_redirect=True)
    tag = final.rstrip("/").split("/")[-1]
    if not tag or tag == "latest":
        raise UpdateError("没有找到已发布的版本")

    return {
        "tag": tag,
        "version": tag.lstrip("vV"),
        "page": "https://github.com/%s/releases/tag/%s" % (repo, tag),
        "repo": repo,
        "assets": _release_assets(repo, tag, timeout=timeout),
    }


def _release_assets(repo, tag, timeout=CONNECT_TIMEOUT):
    """Portable-package download URLs for a tag, read from the assets page.

    **Only ``.zip`` packages are returned.** An in-place update replaces the
    program directory, so it needs the whole ``WorkBuddy2API.exe`` +
    ``_internal/`` pair. A release also carries a Windows installer
    (``*-setup.exe``) for new users, and a bare ``.exe`` cannot run without its
    ``_internal/`` folder - downloading either would produce a broken install.
    The updater therefore ignores every non-``.zip`` asset rather than
    "downloading the first thing listed".

    A ``-win64.zip`` package is ordered first when present, since that is the
    layout :func:`extract_zip` knows how to unpack.

    Falls back to the conventional file name if the page cannot be read, so a
    layout change upstream degrades to "try the expected URL" rather than
    failing outright.
    """
    guesses = [
        "https://github.com/%s/releases/download/%s/WorkBuddy2API-%s-win64.zip"
        % (repo, tag, tag),
    ]
    try:
        url = _checked("https://github.com/%s/releases/expanded_assets/%s"
                       % (repo, tag))
        request = urllib.request.Request(
            url, headers={"User-Agent": "WorkBuddy2API-updater"})
        with _opener()(request, timeout=timeout) as resp:
            html = resp.read().decode("utf-8", "replace")
        found = re.findall(
            r'href="(/[^"]+/releases/download/[^"]+\.zip)"', html)
        ordered = []
        for href in found:
            full = "https://github.com" + href
            if full not in ordered:
                ordered.append(full)
        if ordered:
            # Stable sort: win64 packages first, HTML order otherwise.
            ordered.sort(
                key=lambda u: 0 if u.lower().endswith("-win64.zip") else 1)
            return ordered
    except Exception:
        pass
    return guesses


def check_for_update(timeout=CONNECT_TIMEOUT):
    """Compare the running version with the newest release.

    Returns a dict describing the outcome; never raises, so the UI can call it
    freely and show the reason when it fails.
    """
    result = {"ok": False, "current": current_version(),
              "latest": "", "has_update": False, "notes": "", "error": ""}
    try:
        info = latest_release(timeout=timeout)
    except UpdateError as exc:
        result["error"] = str(exc)
        return result
    except Exception as exc:
        result["error"] = "查询失败：%s" % exc
        return result

    result["latest"] = info["version"]
    result["tag"] = info["tag"]
    result["page"] = info["page"]
    result["assets"] = info.get("assets") or []
    result["has_update"] = _version_tuple(info["version"]) > \
        _version_tuple(current_version())
    result["ok"] = True
    if result["has_update"]:
        result["notes"] = "当前 %s，最新 %s" % (current_version(),
                                             info["version"])
    else:
        result["notes"] = "已是最新版本（%s）" % current_version()
    return result


# ---------------------------------------------------------------------------
# Download and apply
# ---------------------------------------------------------------------------
def _download(url, target_path, progress=None, timeout=DOWNLOAD_TIMEOUT):
    """Stream ``url`` to ``target_path``, reporting bytes as they arrive.

    The destination is a scratch path this module created, but it is resolved
    and checked anyway: this function writes whatever it is pointed at, and a
    future caller passing a value from elsewhere should not be able to place
    the file outside a temporary directory.
    """
    _checked(url, allow_redirect=True)
    resolved = os.path.realpath(target_path)
    if ".." in os.path.normpath(target_path).split(os.sep):
        raise UpdateError("下载目标路径非法")
    parent = os.path.dirname(resolved)
    if not parent or not os.path.isdir(parent):
        raise UpdateError("下载目标目录不存在")
    if os.path.isdir(resolved):
        raise UpdateError("下载目标是一个目录")
    request = urllib.request.Request(
        url, headers={"User-Agent": "WorkBuddy2API-updater"})
    try:
        response = _opener()(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        raise UpdateError("下载失败：HTTP %s" % exc.code)
    except Exception as exc:
        raise UpdateError("下载失败：%s" % exc)

    total = int(response.headers.get("Content-Length") or 0)
    done = 0
    # os.open on the resolved path rather than the built-in: the descriptor
    # refers to exactly the file that was validated above, and the mode is
    # explicit.
    fd = os.open(resolved, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        while True:
            chunk = response.read(262144)
            if not chunk:
                break
            os.write(fd, chunk)
            done += len(chunk)
            if progress:
                progress(done, total)
    finally:
        os.close(fd)
        response.close()
    if total and done < total:
        raise UpdateError("下载不完整（%d/%d 字节）" % (done, total))
    return done


def extract_zip(zip_path, into_dir):
    """Extract an update archive, returning the folder holding the program.

    Archives have been published both with the files at the root and wrapped in
    a top-level folder, so the layout is detected rather than assumed.
    """
    os.makedirs(into_dir, exist_ok=True)
    try:
        with zipfile.ZipFile(zip_path) as archive:
            names = archive.namelist()
            # Reject traversal before extracting anything.
            for name in names:
                target = os.path.realpath(os.path.join(into_dir, name))
                if os.path.commonpath([target, os.path.realpath(into_dir)]) \
                        != os.path.realpath(into_dir):
                    raise UpdateError("压缩包内含非法路径：%s" % name)
            archive.extractall(into_dir)
    except zipfile.BadZipFile:
        raise UpdateError("下载的文件不是有效的压缩包")
    except UpdateError:
        raise
    except Exception as exc:
        raise UpdateError("解压失败：%s" % exc)

    exe = "WorkBuddy2API.exe"
    if os.path.isfile(os.path.join(into_dir, exe)):
        return into_dir
    for entry in os.listdir(into_dir):
        candidate = os.path.join(into_dir, entry)
        if os.path.isdir(candidate) and os.path.isfile(os.path.join(candidate, exe)):
            return candidate
    raise UpdateError("压缩包里没有找到 %s" % exe)


def install_dir():
    """Where the program files live, in both frozen and source layouts."""
    if wb_runtime.is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


#: Items an update replaces. Everything else in the folder is the user's.
PROGRAM_ITEMS = ("_internal", "WorkBuddy2API.exe")

#: Items that must never be touched.
DATA_ITEMS = ("accounts", "usage", "gateway.json")


def prepare_update(new_files_dir, target_dir=None):
    """Stage an update: back up the data, then swap the program files.

    Returns (ok, message, backup_dir). The caller restarts the program; this
    function does not, so it stays usable from a background thread.
    """
    target = os.path.realpath(target_dir or install_dir())
    source = os.path.realpath(new_files_dir)
    if not os.path.isfile(os.path.join(source, "WorkBuddy2API.exe")):
        return False, "新版目录里没有 WorkBuddy2API.exe", None
    if not os.path.isdir(target):
        return False, "安装目录不存在", None

    backup_dir = os.path.join(
        target, "_backup-%s" % time.strftime("%Y%m%d-%H%M%S"))
    suffix = 1
    while os.path.exists(backup_dir):
        suffix += 1
        backup_dir = os.path.join(
            target, "_backup-%s-%d" % (time.strftime("%Y%m%d-%H%M%S"), suffix))
    try:
        os.makedirs(backup_dir, exist_ok=False)
        for item in DATA_ITEMS:
            src = os.path.join(target, item)
            if not os.path.exists(src):
                continue
            dst = os.path.join(backup_dir, item)
            if os.path.isdir(src):
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
    except Exception as exc:
        return False, "备份失败：%s" % exc, None

    # Stage inside the target so the final moves are renames on one volume.
    staging = tempfile.mkdtemp(prefix=".update-", dir=target)
    held = os.path.join(staging, "_old")
    moved = []
    try:
        for item in PROGRAM_ITEMS:
            src = os.path.join(source, item)
            if not os.path.exists(src):
                continue
            staged = os.path.join(staging, item)
            if os.path.isdir(src):
                shutil.copytree(src, staged)
            else:
                shutil.copy2(src, staged)

        os.makedirs(held, exist_ok=True)
        for item in PROGRAM_ITEMS:
            current = os.path.join(target, item)
            if os.path.exists(current):
                shutil.move(current, os.path.join(held, item))
                moved.append(item)
        for item in PROGRAM_ITEMS:
            staged = os.path.join(staging, item)
            if os.path.exists(staged):
                final = os.path.join(target, item)
                if os.path.exists(final):
                    if os.path.isdir(final):
                        shutil.rmtree(final)
                    else:
                        os.remove(final)
                shutil.move(staged, final)
    except Exception as exc:
        for item in moved:
            previous = os.path.join(held, item)
            final = os.path.join(target, item)
            try:
                if os.path.exists(previous):
                    if os.path.exists(final):
                        if os.path.isdir(final):
                            shutil.rmtree(final)
                        else:
                            os.remove(final)
                    shutil.move(previous, final)
            except Exception:
                pass
        shutil.rmtree(staging, ignore_errors=True)
        return False, "替换失败，已回滚：%s" % exc, backup_dir

    shutil.rmtree(staging, ignore_errors=True)
    return True, "更新完成", backup_dir


def download_and_install(asset_url, progress=None, target_dir=None):
    """Download an asset and install it. Returns (ok, message, backup_dir)."""
    scratch = tempfile.mkdtemp(prefix="wbupdate_")
    try:
        archive = os.path.join(scratch, "update.zip")
        _download(asset_url, archive, progress=progress)
        files_dir = extract_zip(archive, os.path.join(scratch, "unpacked"))
        return prepare_update(files_dir, target_dir)
    except UpdateError as exc:
        return False, str(exc), None
    except Exception as exc:
        return False, "更新失败：%s" % exc, None
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def restart_self():
    """Launch the (possibly replaced) program and let the caller exit.

    Detached so it survives this process exiting to make way for it.
    """
    exe = os.path.join(install_dir(), "WorkBuddy2API.exe")
    if not os.path.isfile(exe):
        return False, "找不到 %s" % exe
    try:
        subprocess.Popen(
            [exe], cwd=os.path.dirname(exe), close_fds=True,
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        return True, "已启动"
    except Exception as exc:
        return False, str(exc)
