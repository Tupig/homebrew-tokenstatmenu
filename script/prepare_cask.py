#!/usr/bin/env python3
"""以调用者显式批准的身份验收本地 DMG，然后生成新的 Cask 候选文件。

本地候选不证明公开正式 DMG 可用；必须确认正式公开资产具有相同 SHA256，
才可将候选提交到 Casks。此入口不联网、不安装、不构建、不重签、不启动。
示例：python3 script/prepare_cask.py --dmg /path/TokenStatMenu.dmg
  --sha256 <64位小写摘要> --team <10位Team> --group <AppGroup>
  --version 1.2.3 --build 99 --commit <40位小写提交> --output /path/candidate.rb
"""

# fmt: off
from __future__ import annotations

import argparse
import os
from pathlib import Path
import secrets
import sys

if __package__:
    from .verify_release_dmg import ReleaseIdentity, ValidationError, verify_release_dmg
else:
    from verify_release_dmg import ReleaseIdentity, ValidationError, verify_release_dmg

def require(condition, message):
    if not condition:
        raise ValidationError(message)

def checked_path(value, suffix):
    text = os.fspath(value)
    require(
        isinstance(text, str)
        and text
        and not any(ord(char) < 32 or ord(char) == 127 for char in text),
        "invalid path",
    )
    path = Path(text).absolute()
    require(path.suffix == suffix and ".." not in path.parts, "invalid path syntax")
    return path

def render_cask(identity):
    identity.validate()
    return f'''# Local candidate only; public release availability is NOT verified.
# Submit to Casks only after the official public DMG has this same SHA256.
# Approved team: {identity.team}; group: {identity.group}
# Approved build: {identity.build}; commit: {identity.commit}
cask "tokenstatmenu" do
  version "{identity.version}"
  sha256 "{identity.sha256}"

  url "https://github.com/Tupig/TokenStatMenu/releases/download/v{identity.version}/TokenStatMenu.dmg"
  name "TokenStatMenu"
  desc "Token usage statistics in the menu bar"
  homepage "https://github.com/Tupig/TokenStatMenu"

  depends_on macos: ">= :ventura"
  depends_on arch: :arm64

  app "TokenStatMenu.app"
end
'''

def open_directory(path):
    """从根目录逐层打开，不允许任意一层跟随符号链接。"""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(path.anchor, flags)
    try:
        for part in path.parts[1:]:
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except OSError as error:
        os.close(fd)
        raise ValidationError("output parent must be an existing directory without symlinks") from error

def file_identity(stamp):
    return stamp.st_dev, stamp.st_ino

def unlink_owned(name, directory, owned):
    try:
        stamp = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return
    if file_identity(stamp) == owned:
        os.unlink(name, dir_fd=directory)

def prepare_cask(dmg, identity, output):
    identity.validate()
    dmg = checked_path(dmg, ".dmg")
    output = checked_path(output, ".rb")
    directory = open_directory(output.parent)
    owned = temporary = None
    published = success = False
    try:
        try:
            os.stat(output.name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise ValidationError("output already exists; refusing overwrite")
        # 验证器完整返回（包括成功 detach）之前不创建候选或临时文件。
        verify_release_dmg(dmg, identity)
        temporary = ".tokenstatmenu-cask-" + secrets.token_hex(16)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=directory)
        owned = file_identity(os.fstat(fd))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(render_cask(identity))
                stream.flush()
                os.fchmod(stream.fileno(), 0o644)
                os.fsync(stream.fileno())
            # 名称解析始终绑定固定目录，目录被改名也不会写向替换路径。
            os.link(temporary, output.name, src_dir_fd=directory,
                    dst_dir_fd=directory, follow_symlinks=False)
            published = True
        finally:
            unlink_owned(temporary, directory, owned)
        current = open_directory(output.parent)
        try:
            require(file_identity(os.fstat(current)) == file_identity(os.fstat(directory)),
                    "output parent changed during preparation")
        finally:
            os.close(current)
        success = True
        return output
    finally:
        try:
            if published and not success:
                unlink_owned(output.name, directory, owned)
        finally:
            os.close(directory)

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    for name in (
        "dmg",
        "sha256",
        "team",
        "group",
        "version",
        "build",
        "commit",
        "output",
    ):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args(argv)
    identity = ReleaseIdentity(
        **{
            name: getattr(args, name)
            for name in ("sha256", "team", "group", "version", "build", "commit")
        }
    )
    try:
        output = prepare_cask(args.dmg, identity, args.output)
    except (ValidationError, OSError, UnicodeError) as error:
        print("Cask preparation failed: " + str(error), file=sys.stderr)
        return 1
    print(f"Local candidate: {output}; public DMG availability is NOT verified.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
