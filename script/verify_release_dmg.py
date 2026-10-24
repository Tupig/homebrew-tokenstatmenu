#!/usr/bin/env python3
"""只读验收已签名的发布 DMG；不构建、不重签、不安装、不启动 App。

所有发布身份必须由调用者固定提供，不从待验证产物反推信任值。
--size 可额外固定下载字节数；报告始终包含实际 size 和 sha256。
严格要求主 App 和 Widget 都携带 clean 源码修订及 Developer ID profile。
"""

# fmt: off
from __future__ import annotations

import argparse
from dataclasses import dataclass
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import stat
import subprocess
import sys
import tempfile
from xml.parsers.expat import ExpatError

APP_NAME = "TokenStatMenu.app"
WIDGET_NAME = "TokenStatMenuWidget.appex"
APP_ID = "com.tupig.TokenStatMenu"
WIDGET_ID = APP_ID + ".Widget"
MAX_DMG_BYTES = 1024 * 1024 * 1024
MAX_PLIST_BYTES = 8 * 1024 * 1024
TIMEOUT = 120
TOOLS = {
    "hdiutil": "/usr/bin/hdiutil",
    "codesign": "/usr/bin/codesign",
    "lipo": "/usr/bin/lipo",
    "security": "/usr/bin/security",
    "spctl": "/usr/sbin/spctl",
}

class ValidationError(ValueError):
    """任何身份、信任或卸载失败均不可发布。"""

def require(condition, message):
    if not condition:
        raise ValidationError(message)

@dataclass(frozen=True)
class ReleaseIdentity:
    sha256: str
    team: str
    group: str
    version: str
    build: str
    commit: str
    size: int | None = None

    def validate(self):
        patterns = {
            "sha256": r"[0-9a-f]{64}",
            "team": r"[A-Z0-9]{10}",
            "group": r"[A-Za-z0-9][A-Za-z0-9.-]{1,254}",
            "version": r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)",
            "build": r"[1-9][0-9]*",
            "commit": r"[0-9a-f]{40}",
        }
        for name, pattern in patterns.items():
            value = getattr(self, name)
            require(
                isinstance(value, str) and re.fullmatch(pattern, value, re.ASCII),
                "invalid expected " + name,
            )
        require(
            ".." not in self.group and not self.group.endswith("."),
            "invalid expected group",
        )
        require(
            self.size is None
            or (type(self.size) is int and 0 < self.size <= MAX_DMG_BYTES),
            "invalid expected size",
        )

def run_command(tool, *args):
    """唯一外部命令边界；测试必须整体 mock 此函数。"""
    try:
        result = subprocess.run(
            [TOOLS[tool], *map(str, args)],
            capture_output=True,
            check=False,
            timeout=TIMEOUT,
            stdin=subprocess.DEVNULL,
            env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValidationError(f"{tool} could not complete: {error}") from error
    require(
        result.returncode == 0,
        f"{tool} failed ({result.returncode}): "
        + result.stderr.decode("utf-8", errors="replace")[:2048],
    )
    return result

def parse_plist(data, label):
    require(
        isinstance(data, bytes) and 0 < len(data) <= MAX_PLIST_BYTES,
        label + ": plist size",
    )
    try:
        value = plistlib.loads(data)
    except (
        ValueError,
        TypeError,
        OverflowError,
        ExpatError,
        plistlib.InvalidFileException,
    ) as error:
        raise ValidationError(label + ": invalid plist") from error
    require(isinstance(value, dict), label + ": expected plist dictionary")
    return value

def read_plist(path):
    require(
        path.is_file() and not path.is_symlink(), str(path) + ": missing regular plist"
    )
    require(path.stat().st_size <= MAX_PLIST_BYTES, str(path) + ": oversized plist")
    return parse_plist(path.read_bytes(), str(path))

def inspect_dmg(path, identity):
    require(
        path.suffix.lower() == ".dmg" and path.is_file() and not path.is_symlink(),
        "expected regular .dmg",
    )
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        require(
            stat.S_ISREG(before.st_mode) and 0 < before.st_size <= MAX_DMG_BYTES,
            "invalid DMG size/type",
        )
        require(
            identity.size is None or before.st_size == identity.size,
            "DMG size mismatch",
        )
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
        after = os.fstat(stream.fileno())
    stamp = lambda value: (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )
    require(stamp(before) == stamp(after), "DMG changed while hashing")
    require(digest.hexdigest() == identity.sha256, "DMG sha256 mismatch")
    return stamp(after)

def no_bundle_symlinks(bundle):
    require(
        bundle.is_dir() and not bundle.is_symlink(),
        "missing real bundle: " + str(bundle),
    )

    def walk_error(error):
        raise ValidationError("unreadable bundle member: " + str(error)) from error

    for directory, dirs, files in os.walk(
        bundle, followlinks=False, onerror=walk_error
    ):
        for name in dirs + files:
            member = Path(directory) / name
            mode = member.lstat().st_mode
            require(
                stat.S_ISDIR(mode) or stat.S_ISREG(mode),
                "unsafe bundle member: " + str(member),
            )

def signature_fields(data):
    fields = {}
    for line in data.decode("utf-8", errors="strict").splitlines():
        if line.startswith("CodeDirectory "):
            fields.setdefault("CodeDirectory", []).append(line[len("CodeDirectory "):])
        elif "=" in line:
            key, value = line.split("=", 1)
            fields.setdefault(key, []).append(value)
    return fields

def check_profile(bundle, bundle_id, identity):
    path = bundle / "Contents/embedded.provisionprofile"
    require(
        path.is_file() and not path.is_symlink(),
        "missing embedded profile: " + str(bundle),
    )
    profile = parse_plist(
        run_command("security", "cms", "-D", "-i", path).stdout, "profile"
    )
    require(
        profile.get("TeamIdentifier") == [identity.team],
        "profile TeamIdentifier mismatch",
    )
    require(
        profile.get("ApplicationIdentifierPrefix") == [identity.team],
        "profile prefix mismatch",
    )
    require(
        profile.get("ProvisionsAllDevices") is True
        and "ProvisionedDevices" not in profile,
        "profile is not Developer ID distribution",
    )
    now = dt.datetime.now(dt.timezone.utc)
    for key in ("CreationDate", "ExpirationDate"):
        value = profile.get(key)
        require(isinstance(value, dt.datetime), "profile missing " + key)
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.timezone.utc)
        require(
            value <= now if key == "CreationDate" else value > now,
            "profile invalid " + key,
        )
    certificates = profile.get("DeveloperCertificates")
    require(
        isinstance(certificates, list)
        and certificates
        and all(isinstance(value, bytes) and value for value in certificates),
        "profile missing developer certificates",
    )
    check_entitlements(profile.get("Entitlements"), bundle_id, identity, "profile")

def check_entitlements(entitlements, bundle_id, identity, label):
    require(isinstance(entitlements, dict), label + ": missing entitlements")
    # profile 是授权上限，签名则必须仅声明本次发布使用的权限。
    profile = label == "profile"
    groups = entitlements.get("com.apple.security.application-groups")
    require(
        isinstance(groups, list) and identity.group in groups
        if profile else groups == [identity.group],
        label + ": App Group mismatch",
    )
    require(
        entitlements.get("com.apple.developer.team-identifier") == identity.team,
        label + ": entitlement Team mismatch",
    )
    identifiers = [
        entitlements[key]
        for key in ("com.apple.application-identifier", "application-identifier")
        if key in entitlements
    ]
    require(
        identifiers
        and all(value == identity.team + "." + bundle_id for value in identifiers),
        label + ": application identifier mismatch",
    )
    require(
        all(
            entitlements.get(key, False) is False
            for key in (
                "get-task-allow",
                "com.apple.security.get-task-allow",
                "com.apple.security.cs.disable-library-validation",
                "com.apple.security.cs.allow-dyld-environment-variables",
            )
        ),
        label + ": unsafe development entitlement",
    )
    sandbox_key = "com.apple.security.app-sandbox"
    sandbox = bundle_id == WIDGET_ID
    if profile:
        require(
            sandbox_key not in entitlements or entitlements[sandbox_key] is sandbox,
            label + ": sandbox policy mismatch",
        )
    else:
        require(
            entitlements.get(sandbox_key, False) is sandbox,
            label + (": widget is not sandboxed" if sandbox else ": main App must not be sandboxed"),
        )

def check_bundle(bundle, bundle_id, executable, identity):
    info = read_plist(bundle / "Contents/Info.plist")
    expected = {
        "CFBundleIdentifier": bundle_id,
        "CFBundleExecutable": executable,
        "CFBundlePackageType": "APPL" if bundle_id == APP_ID else "XPC!",
        "CFBundleShortVersionString": identity.version,
        "CFBundleVersion": identity.build,
        "TokenStatSourceRevision": identity.commit,
        "TokenStatSourceState": "clean",
        "TokenStatWidgetGroup": identity.group,
    }
    for key, value in expected.items():
        require(info.get(key) == value, bundle_id + ": " + key + " mismatch")
    if bundle_id == WIDGET_ID:
        extension = info.get("NSExtension")
        require(
            isinstance(extension, dict)
            and extension.get("NSExtensionPointIdentifier")
            == "com.apple.widgetkit-extension",
            "unexpected widget extension point",
        )
    binary = bundle / "Contents/MacOS" / executable
    require(
        binary.is_file() and binary.stat().st_mode & 0o111,
        "missing executable: " + str(binary),
    )
    require(
        run_command("lipo", "-archs", binary).stdout.split() == [b"arm64"],
        bundle_id + ": expected arm64 only",
    )
    run_command("codesign", "--verify", "--deep", "--strict", "--verbose=2", bundle)
    details = run_command("codesign", "--display", "--verbose=4", bundle)
    fields = signature_fields(details.stderr)
    require(fields.get("Identifier") == [bundle_id], "signature Identifier mismatch")
    require(
        fields.get("TeamIdentifier") == [identity.team],
        "signature TeamIdentifier mismatch",
    )
    authorities = fields.get("Authority", [])
    require(
        len(authorities) >= 3
        and re.fullmatch(
            r"Developer ID Application: .+ \(" + re.escape(identity.team) + r"\)",
            authorities[0],
        )
        and authorities[1:]
        == ["Developer ID Certification Authority", "Apple Root CA"],
        "expected Developer ID Application certificate chain",
    )
    require(
        "Signature" not in fields
        and len(fields.get("Timestamp", [])) == 1
        and bool(fields["Timestamp"][0].strip()),
        "ad hoc or untimestamped signature",
    )
    require(
        len(fields.get("CodeDirectory", [])) == 1
        and re.search(
            r"flags=0x[0-9a-f]+\([^)]*\bruntime\b", fields["CodeDirectory"][0]
        ),
        "missing hardened runtime",
    )
    flags = re.search(r"flags=0x([0-9a-f]+)", fields["CodeDirectory"][0])
    require(
        int(flags[1], 16) & 0x10002 == 0x10000,
        "ad hoc flags or missing hardened runtime flag",
    )
    entitlements = run_command("codesign", "--display", "--entitlements", ":-", bundle)
    check_entitlements(
        parse_plist(entitlements.stdout, "signature entitlements"),
        bundle_id,
        identity,
        "signature",
    )
    check_profile(bundle, bundle_id, identity)

def check_mounted(mount, identity):
    require(
        {entry.name for entry in mount.iterdir()} == {APP_NAME, "Applications"},
        "DMG root must contain exactly App and Applications symlink",
    )
    applications = mount / "Applications"
    require(
        applications.is_symlink() and os.readlink(applications) == "/Applications",
        "Applications must link exactly to /Applications",
    )
    app = mount / APP_NAME
    no_bundle_symlinks(app)
    plugins = app / "Contents/PlugIns"
    require(
        plugins.is_dir() and {item.name for item in plugins.iterdir()} == {WIDGET_NAME},
        "expected exactly one widget extension",
    )
    check_bundle(app, APP_ID, "TokenStatMenu", identity)
    check_bundle(plugins / WIDGET_NAME, WIDGET_ID, "TokenStatMenuWidget", identity)
    assessment = run_command(
        "spctl", "--assess", "--type", "execute", "--verbose=4", app
    )
    text = (assessment.stdout + assessment.stderr).decode("utf-8", errors="strict")
    require(
        re.search(r"(?m)^source=Notarized Developer ID$", text),
        "Gatekeeper did not confirm notarized Developer ID",
    )
    require(
        re.search(
            r"(?m)^origin=Developer ID Application: .+ \("
            + re.escape(identity.team)
            + r"\)$",
            text,
        ),
        "Gatekeeper origin mismatch",
    )

def verify_release_dmg(dmg, identity):
    identity.validate()
    path = Path(dmg).absolute()
    initial = inspect_dmg(path, identity)
    temporary = Path(tempfile.mkdtemp(prefix="tokenstatmenu-dmg-"))
    mount = temporary / "mount"
    mount.mkdir()
    attempted = False
    detached = True
    try:
        attempted = True
        detached = False
        attached = run_command(
            "hdiutil",
            "attach",
            "-readonly",
            "-nobrowse",
            "-noautoopen",
            "-owners",
            "off",
            "-plist",
            "-mountpoint",
            mount,
            path,
        )
        metadata = parse_plist(attached.stdout, "hdiutil attach")
        entities = metadata.get("system-entities")
        require(
            isinstance(entities, list)
            and entities
            and all(isinstance(entity, dict) for entity in entities),
            "invalid mount entities",
        )
        mounts = [
            entity["mount-point"] for entity in entities if "mount-point" in entity
        ]
        require(mounts == [str(mount)], "unexpected mounted volumes")
        check_mounted(mount, identity)
        require(
            inspect_dmg(path, identity) == initial, "DMG changed during verification"
        )
    finally:
        try:
            if attempted:
                run_command("hdiutil", "detach", mount)
                detached = True
        finally:
            # 绝不递归删除挂载点；detach 失败保留目录用于人工恢复并传播失败。
            if detached:
                mount.rmdir()
                temporary.rmdir()
    return {
        "dmg": str(path),
        "sha256": identity.sha256,
        "size": initial[2],
        "team": identity.team,
        "group": identity.group,
        "version": identity.version,
        "build": identity.build,
        "commit": identity.commit,
        "status": "verified",
    }

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dmg", required=True, type=Path)
    for name in ("sha256", "team", "group", "version", "build", "commit"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument(
        "--size", type=int, help="optional independently pinned DMG byte count"
    )
    args = parser.parse_args(argv)
    identity = ReleaseIdentity(
        **{
            name: getattr(args, name)
            for name in (
                "sha256",
                "team",
                "group",
                "version",
                "build",
                "commit",
                "size",
            )
        }
    )
    try:
        report = verify_release_dmg(args.dmg, identity)
    except (ValidationError, OSError, UnicodeError) as error:
        print("DMG verification failed: " + str(error), file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0

if __name__ == "__main__":
    sys.exit(main())
