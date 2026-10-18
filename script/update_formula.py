#!/usr/bin/env python3
"""Validate the project's public Release and stage a source Formula.

Public downloads are unauthenticated. Only the separate workflow publication
job receives its own repository token; this module never reads credentials.
"""
from __future__ import annotations

import argparse
import base64
from dataclasses import asdict, dataclass
import hashlib
import io
import json
import os
from pathlib import Path
import plistlib
import re
import stat
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile

REPOSITORY = "Tupig/TokenStatMenu"
API = "https://api.github.com/repos/" + REPOSITORY
DOWNLOAD = "https://github.com/" + REPOSITORY + "/releases/download/"
MAX_SOURCE_BYTES = 32 * 1024 * 1024
MAX_SOURCE_FILES = 256
MAX_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024
MAX_SUMS_BYTES = 4096
MAX_API_BYTES = 1024 * 1024
NETWORK_TIMEOUT = 30
VERSION = r"(?:0|[1-9][0-9]{0,5})\.(?:0|[1-9][0-9]{0,5})\.(?:0|[1-9][0-9]{0,5})"
COMMIT = r"[0-9a-f]{40}"
SHA256 = r"[0-9a-f]{64}"
PIN_PREFIX = "# release-pin: "
ICON_SHA256 = "edfc3aa520be04f987cc6c8a273995f57bafca68603d89f8265c640d0009e980"


class ValidationError(ValueError):
    def __init__(self, category):
        self.category = category
        super().__init__(category)


def require(value, category):
    if not value:
        raise ValidationError(category)


def exact(value, keys, category):
    require(isinstance(value, dict) and set(value) == set(keys), category)


def text_matches(value, pattern):
    return isinstance(value, str) and re.fullmatch(pattern, value, flags=re.ASCII) is not None


def sha(data):
    return hashlib.sha256(data).hexdigest()


def json_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def parse_json(data, maximum):
    require(isinstance(data, bytes) and len(data) <= maximum, "json_size")
    def pairs(values):
        result = {}
        for key, value in values:
            require(key not in result, "duplicate_key")
            result[key] = value
        return result
    try:
        return json.loads(data, object_pairs_hook=pairs)
    except ValidationError:
        raise
    except (ValueError, UnicodeError, TypeError):
        raise ValidationError("json_format") from None


@dataclass(frozen=True)
class ReleasePin:
    version: str
    build: str
    tag: str
    commit: str
    source_name: str
    source_bytes: int
    source_sha256: str


def validate_pin(pin):
    require(isinstance(pin, ReleasePin), "pin_type")
    require(text_matches(pin.version, VERSION), "version")
    require(text_matches(pin.build, r"[1-9][0-9]{0,8}"), "build")
    require(pin.tag == "v" + pin.version, "tag")
    require(text_matches(pin.commit, COMMIT), "commit")
    require(pin.source_name == f"TokenStatMenu-{pin.version}-source.zip", "asset_name")
    require(type(pin.source_bytes) is int and 0 < pin.source_bytes <= MAX_SOURCE_BYTES, "asset_size")
    require(text_matches(pin.source_sha256, SHA256), "asset_sha")


def pin_from_json(data):
    value = parse_json(data, MAX_MANIFEST_BYTES)
    exact(value, ReleasePin.__dataclass_fields__, "pin_schema")
    pin = ReleasePin(**value)
    validate_pin(pin)
    return pin


def validate_download_url(url, *, redirect=False):
    require(isinstance(url, str) and len(url) <= 8192
            and not any(ord(c) < 32 or ord(c) == 127 for c in url), "download_url")
    try:
        parts = urllib.parse.urlsplit(url)
        require(parts.scheme == "https" and not parts.username and not parts.password
                and parts.port in (None, 443) and not parts.fragment, "download_url")
    except ValueError:
        raise ValidationError("download_url") from None
    require("\\" not in parts.path and not any(p in (".", "..") for p in parts.path.split("/")), "download_path")
    if parts.hostname == "github.com":
        pattern = r"/Tupig/TokenStatMenu/releases/download/v" + VERSION + r"/(?:TokenStatMenu-" + VERSION + r"-(?:source|universal)\.zip|TokenStatMenu-" + VERSION + r"-universal\.dmg|release-manifest\.json|SHA256SUMS)"
        require(not parts.query and re.fullmatch(pattern, parts.path, flags=re.ASCII), "download_path")
    else:
        paths = {
            "release-assets.githubusercontent.com": "/github-production-release-asset/",
            "objects.githubusercontent.com": "/github-production-release-asset-2e65be/",
        }
        require(redirect and parts.hostname in paths
                and parts.path.startswith(paths[parts.hostname])
                and len(parts.path) > len(paths[parts.hostname]), "redirect_origin")


class CheckedRedirectHandler(urllib.request.HTTPRedirectHandler):
    max_redirections = 5
    max_repeats = 2

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        require(not req.full_url.startswith(API + "/"), "api_redirect")
        validate_download_url(newurl, redirect=True)
        result = super().redirect_request(req, fp, code, msg, headers, newurl)
        if result is not None:
            for collection in (result.headers, result.unredirected_hdrs):
                for name in list(collection):
                    if name.lower() in {"authorization", "proxy-authorization", "cookie"}:
                        del collection[name]
        return result


class PublicTransport:
    def __init__(self):
        self.opener = urllib.request.build_opener(CheckedRedirectHandler())

    def get(self, url, *, max_bytes):
        api_pattern = re.escape(API) + r"/(?:releases/latest|git/ref/tags/v" + VERSION + r"|git/tags/" + COMMIT + r")"
        if not re.fullmatch(api_pattern, url, flags=re.ASCII):
            validate_download_url(url, redirect=False)
        request = urllib.request.Request(url, headers={"User-Agent": "TokenStatMenu-Homebrew-Updater",
                                                     "Accept": "application/vnd.github+json" if url.startswith(API) else "application/octet-stream"})
        with self.opener.open(request, timeout=NETWORK_TIMEOUT) as response:
            final = response.geturl()
            if url.startswith(API):
                require(final == url, "api_redirect")
            else:
                validate_download_url(final, redirect=True)
            length = response.headers.get("Content-Length")
            if length is not None:
                require(text_matches(length, r"[0-9]+") and int(length) <= max_bytes, "response_size")
            data = response.read(max_bytes + 1)
            require(len(data) <= max_bytes, "response_size")
            return data


def validate_release(release, manifest_bytes, sums_bytes, tag_commit):
    manifest = parse_json(manifest_bytes, MAX_MANIFEST_BYTES)
    exact(manifest, {"version", "build", "tag", "commit", "assets"}, "manifest_schema")
    version, build, tag, commit = (manifest[key] for key in ("version", "build", "tag", "commit"))
    require(text_matches(version, VERSION), "version")
    require(text_matches(build, r"[1-9][0-9]{0,8}"), "build")
    require(tag == "v" + version, "tag")
    require(text_matches(commit, COMMIT) and tag_commit == commit, "commit")
    require(isinstance(release, dict) and release.get("draft") is False
            and release.get("prerelease") is False and release.get("tag_name") == tag, "release_kind")
    require(release.get("html_url") == f"https://github.com/{REPOSITORY}/releases/tag/{tag}", "release_origin")
    version_tuple = tuple(map(int, version.split(".")))
    asset_kinds = {"source", "universal"}
    if version_tuple >= (1, 1, 5):
        asset_kinds.add("dmg")
    exact(manifest["assets"], asset_kinds, "asset_schema")
    declared = {}
    for kind in sorted(asset_kinds):
        value = manifest["assets"][kind]
        exact(value, {"name", "bytes", "sha256"}, "asset_schema")
        expected = (f"TokenStatMenu-{version}-universal.dmg"
                    if kind == "dmg" else f"TokenStatMenu-{version}-{kind}.zip")
        require(value["name"] == expected, "asset_name")
        maximum = MAX_SOURCE_BYTES if kind == "source" else 128 * 1024 * 1024
        require(type(value["bytes"]) is int and 0 < value["bytes"] <= maximum, "asset_size")
        require(text_matches(value["sha256"], SHA256), "asset_sha")
        declared[expected] = (value["bytes"], value["sha256"])
    require(isinstance(sums_bytes, bytes) and len(sums_bytes) <= MAX_SUMS_BYTES, "sums_size")
    declared["release-manifest.json"] = (len(manifest_bytes), sha(manifest_bytes))
    expected_sums = "".join(f"{declared[name][1]}  {name}\n" for name in sorted(declared)).encode()
    require(sums_bytes == expected_sums, "sums_identity")
    declared["SHA256SUMS"] = (len(sums_bytes), sha(sums_bytes))
    assets = release.get("assets")
    require(isinstance(assets, list) and len(assets) == len(declared), "release_assets")
    observed = set()
    for asset in assets:
        require(isinstance(asset, dict) and isinstance(asset.get("name"), str), "release_assets")
        name = asset["name"]
        require(name in declared and name not in observed and asset.get("state") == "uploaded", "release_assets")
        observed.add(name)
        require(type(asset.get("size")) is int and asset["size"] == declared[name][0], "release_asset_size")
        if asset.get("digest") is not None:
            require(asset["digest"] == "sha256:" + declared[name][1], "release_asset_digest")
        expected_url = DOWNLOAD + tag + "/" + name
        require(asset.get("browser_download_url") == expected_url, "release_asset_url")
        validate_download_url(expected_url, redirect=False)
    source = manifest["assets"]["source"]
    return ReleasePin(version, build, tag, commit, source["name"], source["bytes"], source["sha256"])


def needs_update(current_pin, candidate_pin):
    validate_pin(candidate_pin)
    if current_pin is None:
        return True
    validate_pin(current_pin)
    old = tuple(map(int, current_pin.version.split(".")))
    new = tuple(map(int, candidate_pin.version.split(".")))
    if old == new:
        require(current_pin == candidate_pin, "same_version_conflict")
        return False
    return new > old


def validate_source_archive(source_bytes, pin):
    validate_pin(pin)
    require(isinstance(source_bytes, bytes) and len(source_bytes) <= MAX_SOURCE_BYTES
            and len(source_bytes) == pin.source_bytes and sha(source_bytes) == pin.source_sha256, "source_identity")
    try:
        with zipfile.ZipFile(io.BytesIO(source_bytes)) as archive:
            members = archive.infolist()
            require(0 < len(members) <= MAX_SOURCE_FILES, "source_count")
            require(sum(member.file_size for member in members) <= MAX_UNCOMPRESSED_BYTES, "source_expanded_size")
            names, data = set(), {}
            for member in members:
                name = member.filename
                components = name.rstrip("/").split("/")
                require(member.orig_filename == name and name not in names
                        and "\\" not in name and components[0] == "TokenStatMenu"
                        and all(component not in ("", ".", "..") for component in components)
                        and not any(ord(c) < 32 or ord(c) == 127 for c in name), "source_path")
                names.add(name)
                mode = member.external_attr >> 16
                if member.is_dir():
                    require(stat.S_IFMT(mode) in (0, stat.S_IFDIR), "source_member")
                    continue
                require(len(components) > 1 and stat.S_ISREG(mode), "source_member")
                data["/".join(components[1:])] = archive.read(member)
            required = {"Resources/Info.plist", "Resources/AppIcon.icns", "TokenStatMenu.xcodeproj/project.pbxproj",
                        "TokenStatMenu.xcodeproj/xcshareddata/xcschemes/TokenStatMenu.xcscheme",
                        "Sources/TokenStatMenu/TokenStatMenuApp.swift"}
            require(required <= set(data) and any(name.startswith("Sources/TokenStatCore/") for name in data), "source_files")
            info = plistlib.loads(data["Resources/Info.plist"])
            require(isinstance(info, dict) and info.get("CFBundleShortVersionString") == pin.version
                    and info.get("CFBundleVersion") == pin.build, "source_version")
            require(data["Resources/AppIcon.icns"].startswith(b"icns"), "source_icon")
    except ValidationError:
        raise
    except (ValueError, OSError, KeyError, zipfile.BadZipFile, RuntimeError):
        raise ValidationError("source_archive") from None


def render_formula(pin):
    validate_pin(pin)
    header = PIN_PREFIX + json_bytes(asdict(pin)).decode() + "\n"
    # Only validated scalar metadata is substituted into this fixed recipe.
    return header + f'''require "shellwords"

class Tokenstatmenu < Formula
  desc "Native menu bar usage statistics for Seasun Token Hub"
  homepage "https://github.com/Tupig/TokenStatMenu"
  url "{DOWNLOAD}{pin.tag}/{pin.source_name}"
  sha256 "{pin.source_sha256}"
  license "MIT"

  depends_on xcode: ["15.0", :build]
  depends_on macos: :ventura

  def install
    ENV["DEVELOPER_DIR"] = MacOS::Xcode.prefix.to_s
    architecture = Hardware::CPU.arm? ? "arm64" : "x86_64"
    xcodebuild "-project", "TokenStatMenu.xcodeproj", "-scheme", "TokenStatMenu",
               "-configuration", "Release", "-destination", "generic/platform=macOS",
               "-derivedDataPath", buildpath/"brew-derived",
               "ARCHS=#{{architecture}}", "ONLY_ACTIVE_ARCH=YES",
               "GENERATE_PKGINFO_FILE=NO", "CODE_SIGNING_ALLOWED=NO", "CODE_SIGNING_REQUIRED=NO", "build"
    built = buildpath/"brew-derived/Build/Products/Release/TokenStatMenu.app"
    info = built/"Contents/Info.plist"
    plist = "/usr/libexec/PlistBuddy"
    built_version = Utils.safe_popen_read(plist, "-c", "Print :CFBundleShortVersionString", info).strip
    built_build = Utils.safe_popen_read(plist, "-c", "Print :CFBundleVersion", info).strip
    odie "Release version mismatch" if built_version != version.to_s
    odie "Release build mismatch" if built_build != "{pin.build}"
    libexec.install built
    app = libexec/"TokenStatMenu.app"
    system "/usr/bin/plutil", "-replace", "TokenStatSourceRevision", "-string",
           "{pin.commit}", app/"Contents/Info.plist"
    system "/usr/bin/plutil", "-replace", "TokenStatSourceState", "-string",
           "release-source", app/"Contents/Info.plist"
    system "/usr/bin/codesign", "--force", "--sign", "-", app
    system "/usr/bin/codesign", "--verify", "--deep", "--strict", app
    app_path = (opt_libexec/"TokenStatMenu.app").to_s.shellescape
    (bin/"tokenstatmenu").write <<~SH
      #!/bin/sh
      if [ "$#" -ne 0 ]; then
        echo "Usage: tokenstatmenu" >&2
        exit 64
      fi
      exec /usr/bin/open #{{app_path}}
    SH
    chmod 0755, bin/"tokenstatmenu"
  end

  def caveats
    <<~EOS
      Run tokenstatmenu to open the menu bar app.
      Quit an existing TokenStatMenu before brew upgrade, then run tokenstatmenu again.
      Manual app copies and preferences are kept. No process is stopped automatically.
      Built locally from source with ad-hoc signing; this is not Apple notarization.
    EOS
  end

  test do
    app = libexec/"TokenStatMenu.app"
    expected = %w[
      Contents/Info.plist
      Contents/MacOS/TokenStatMenu
      Contents/Resources/AppIcon.icns
      Contents/_CodeSignature/CodeResources
    ]
    members = Dir.glob("#{{app}}/**/*", File::FNM_DOTMATCH)
    assert members.none? {{ |path| File.symlink?(path) }}
    actual = members.select {{ |path| File.file?(path) }}.map {{ |path| path.delete_prefix("#{{app}}/") }}.sort
    assert_equal expected.sort, actual
    info = app/"Contents/Info.plist"
    plist = "/usr/libexec/PlistBuddy"
    assert_equal version.to_s, Utils.safe_popen_read(plist, "-c", "Print :CFBundleShortVersionString", info).strip
    assert_equal "{pin.build}", Utils.safe_popen_read(plist, "-c", "Print :CFBundleVersion", info).strip
    assert_equal "{pin.commit}",
                 Utils.safe_popen_read(plist, "-c", "Print :TokenStatSourceRevision", info).strip
    assert_equal "release-source", Utils.safe_popen_read(plist, "-c", "Print :TokenStatSourceState", info).strip
    assert_equal "{ICON_SHA256}", Digest::SHA256.file(app/"Contents/Resources/AppIcon.icns").hexdigest
    architecture = Hardware::CPU.arm? ? "arm64" : "x86_64"
    actual_arch = Utils.safe_popen_read("/usr/bin/lipo", "-archs", app/"Contents/MacOS/TokenStatMenu").strip
    assert_equal architecture, actual_arch
    system "/usr/bin/codesign", "--verify", "--deep", "--strict", app
    launcher = bin/"tokenstatmenu"
    app_path = (opt_libexec/"TokenStatMenu.app").to_s.shellescape
    expected_launcher = <<~SH
      #!/bin/sh
      if [ "$#" -ne 0 ]; then
        echo "Usage: tokenstatmenu" >&2
        exit 64
      fi
      exec /usr/bin/open #{{app_path}}
    SH
    assert_equal expected_launcher, launcher.read
    assert_match "Usage: tokenstatmenu", shell_output("#{{launcher.to_s.shellescape}} --not-supported 2>&1", 64)
  end
end
'''


def read_current_pin(formula_text):
    lines = formula_text.splitlines()
    records = [line[len(PIN_PREFIX):] for line in lines if line.startswith(PIN_PREFIX)]
    require(len(records) == 1, "formula_pin")
    pin = pin_from_json(records[0].encode())
    require(formula_text == render_formula(pin), "formula_template")
    return pin


def stage_candidate(output_path, formula_text):
    path = Path(output_path)
    require(not path.is_symlink(), "output_symlink")
    if path.exists() and path.read_text() == formula_text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".tokenstatmenu-stage-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(formula_text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(name, 0o644)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    return True


def prepare_publication(current_pin, tested_pin, latest_pin, expected_blob_sha, current_blob_sha):
    validate_pin(tested_pin); validate_pin(latest_pin)
    require(tested_pin == latest_pin, "tested_pin_changed")
    require(text_matches(expected_blob_sha, COMMIT) and current_blob_sha == expected_blob_sha, "blob_conflict")
    if not needs_update(current_pin, tested_pin):
        return None
    return {"message": "Update tokenstatmenu to " + tested_pin.version,
            "content": base64.b64encode(render_formula(tested_pin).encode()).decode(),
            "sha": expected_blob_sha, "branch": "main"}


def resolve_tag(transport, tag):
    require(text_matches(tag, "v" + VERSION), "tag")
    ref = parse_json(transport.get(API + "/git/ref/tags/" + tag, max_bytes=MAX_API_BYTES), MAX_API_BYTES)
    require(isinstance(ref, dict) and ref.get("ref") == "refs/tags/" + tag, "tag_ref")
    node = ref.get("object"); visited = set()
    for _ in range(5):
        require(isinstance(node, dict) and text_matches(node.get("sha"), COMMIT), "tag_object")
        if node.get("type") == "commit":
            return node["sha"]
        require(node.get("type") == "tag" and node["sha"] not in visited, "tag_object")
        visited.add(node["sha"])
        response = parse_json(transport.get(API + "/git/tags/" + node["sha"], max_bytes=MAX_API_BYTES), MAX_API_BYTES)
        require(isinstance(response, dict), "tag_object")
        node = response.get("object")
    raise ValidationError("tag_depth")


def fetch_pin(transport):
    release = parse_json(transport.get(API + "/releases/latest", max_bytes=MAX_API_BYTES), MAX_API_BYTES)
    require(isinstance(release, dict) and text_matches(release.get("tag_name"), "v" + VERSION), "tag")
    tag = release["tag_name"]
    manifest = transport.get(DOWNLOAD + tag + "/release-manifest.json", max_bytes=MAX_MANIFEST_BYTES)
    sums = transport.get(DOWNLOAD + tag + "/SHA256SUMS", max_bytes=MAX_SUMS_BYTES)
    return validate_release(release, manifest, sums, resolve_tag(transport, tag))


def main(argv=None, *, transport=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--current", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pin-output", type=Path)
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--expected-pin")
    parser.add_argument("--prepare-publication", action="store_true")
    parser.add_argument("--old-blob-sha")
    parser.add_argument("--current-blob-sha")
    args = parser.parse_args(argv)
    try:
        require(not args.current.is_symlink(), "formula_symlink")
        current = read_current_pin(args.current.read_text()) if args.current.exists() else None
        client = transport if transport is not None else PublicTransport()
        latest = fetch_pin(client)
        if args.expected_pin:
            tested = pin_from_json(args.expected_pin.encode())
            require(tested == latest, "tested_pin_changed")
        else:
            tested = latest
        changed = needs_update(current, latest)
        if args.prepare_publication:
            require(args.expected_pin is not None and current is not None, "publication_inputs")
            payload = prepare_publication(current, tested, latest, args.old_blob_sha, args.current_blob_sha)
            if payload is not None:
                stage_candidate(args.output, json_bytes(payload).decode() + "\n")
        elif changed:
            archive = client.get(DOWNLOAD + latest.tag + "/" + latest.source_name, max_bytes=MAX_SOURCE_BYTES)
            validate_source_archive(archive, latest)
            stage_candidate(args.output, render_formula(latest))
            if args.pin_output:
                stage_candidate(args.pin_output, json_bytes(asdict(latest)).decode() + "\n")
        if args.github_output:
            with args.github_output.open("a", encoding="utf-8") as handle:
                handle.write("changed=" + str(changed).lower() + "\n")
                handle.write("pin=" + json_bytes(asdict(latest)).decode() + "\n")
        print(json_bytes({"changed": changed, "version": latest.version}).decode())
        return 0
    except urllib.error.HTTPError as error:
        if error.code == 404 and error.url == API + "/releases/latest":
            if args.github_output:
                with args.github_output.open("a", encoding="utf-8") as handle:
                    handle.write("changed=false\n")
            print('{"changed":false,"category":"no_release"}')
            return 0
        print("release_http", file=__import__("sys").stderr)
    except ValidationError as error:
        print(error.category, file=__import__("sys").stderr)
    except (OSError, ValueError, TypeError, KeyError):
        print("release_unavailable", file=__import__("sys").stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
