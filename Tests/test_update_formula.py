"""BP26 checks first: synthetic release identities and opt-in brew packaging.

Default execution performs no real HTTP, brew installation, GUI launch or
signals. The opt-in installed-bundle check is read-only and is run by the PM
after each of the isolated old/new brew install stages.
"""

from __future__ import annotations

import contextlib
import copy
import dataclasses
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import urllib.error
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from script import update_formula as subject  # noqa: E402

REPOSITORY = "Tupig/TokenStatMenu"
API = "https://api.github.com/repos/" + REPOSITORY
DOWNLOAD = "https://github.com/" + REPOSITORY + "/releases/download/"
COMMIT = "a" * 40
OLD_COMMIT = "b" * 40
NATIVE_TEST_FORMULA = "Tupig/bp26-tokenstatmenu-local/tokenstatmenu-bp26-check"


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def source_zip(
    version="1.1.3", build="5", *, replacements=None, extra=(), prefix="TokenStatMenu/"
):
    info = plistlib.dumps(
        {
            "CFBundleShortVersionString": version,
            "CFBundleVersion": build,
            "CFBundleIdentifier": "com.tupig.TokenStatMenu",
            "CFBundleExecutable": "TokenStatMenu",
        }
    )
    entries = {
        "Resources/Info.plist": info,
        "Resources/AppIcon.icns": b"icns\x00\x00\x00\x08",
        "TokenStatMenu.xcodeproj/project.pbxproj": b"// synthetic project fixture\n",
        "TokenStatMenu.xcodeproj/xcshareddata/xcschemes/TokenStatMenu.xcscheme": b"<Scheme/>\n",
        "Sources/TokenStatMenu/TokenStatMenuApp.swift": b"// synthetic source\n",
        "Sources/TokenStatCore/Configuration.swift": b"// synthetic source\n",
    }
    if tuple(map(int, version.split("."))) >= (1, 1, 6):
        entries.update(
            {
                "Resources/WidgetExtension-Info.plist": info,
                "Resources/WidgetExtension.entitlements": b"<plist/>",
                "Resources/TokenStatMenu.entitlements": b"<plist/>",
                "Sources/TokenStatWidgetExtension/TokenStatWidget.swift": b"// synthetic widget\n",
                "Sources/TokenStatWidgetShared/WidgetQuotaSnapshot.swift": b"// synthetic snapshot\n",
            }
        )
    for name, value in (replacements or {}).items():
        if value is None:
            entries.pop(name, None)
        else:
            entries[name] = value
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, data in [
            *((prefix + name, value) for name, value in entries.items()),
            *extra,
        ]:
            if isinstance(name, zipfile.ZipInfo):
                archive.writestr(name, data)
            else:
                member = zipfile.ZipInfo(name)
                member.create_system = 3
                member.external_attr = (stat.S_IFREG | 0o644) << 16
                archive.writestr(member, data)
    return output.getvalue()


class ReleaseFixture:
    def __init__(self, version="1.1.3", build="5", commit=COMMIT, source=None):
        self.source = source if source is not None else source_zip(version, build)
        self.universal = b"synthetic universal archive; never executed"
        self.manifest = {
            "version": version,
            "build": build,
            "tag": "v" + version,
            "commit": commit,
            "assets": {
                "source": {
                    "name": f"TokenStatMenu-{version}-source.zip",
                    "bytes": len(self.source),
                    "sha256": digest(self.source),
                },
                "universal": {
                    "name": f"TokenStatMenu-{version}-universal.zip",
                    "bytes": len(self.universal),
                    "sha256": digest(self.universal),
                },
            },
        }
        if tuple(map(int, version.split("."))) >= (1, 1, 5):
            self.dmg = b"synthetic dmg image; never mounted"
            self.manifest["assets"]["dmg"] = {
                "name": f"TokenStatMenu-{version}-universal.dmg",
                "bytes": len(self.dmg),
                "sha256": digest(self.dmg),
            }
        else:
            self.dmg = None
        if tuple(map(int, version.split("."))) >= (1, 1, 7):
            for kind, name in {
                "source": "TokenStatMenu-source.zip",
                "universal": "TokenStatMenu.zip",
                "dmg": "TokenStatMenu.dmg",
            }.items():
                self.manifest["assets"][kind]["name"] = name
        self.refresh()

    def refresh(self, *, manifest_bytes=None):
        # Rebind ALL unrelated integrity metadata when a test deliberately
        # changes the wire JSON. Otherwise a schema test can pass merely
        # because the old manifest digest no longer matches.
        self.manifest_bytes = (
            manifest_bytes if manifest_bytes is not None else encoded(self.manifest)
        )
        self.sums = {
            self.manifest["assets"]["source"]["name"]: self.manifest["assets"][
                "source"
            ]["sha256"],
            self.manifest["assets"]["universal"]["name"]: self.manifest["assets"][
                "universal"
            ]["sha256"],
            "release-manifest.json": digest(self.manifest_bytes),
        }
        if "dmg" in self.manifest["assets"]:
            self.sums[self.manifest["assets"]["dmg"]["name"]] = self.manifest["assets"][
                "dmg"
            ]["sha256"]
        self.sums_bytes = "".join(
            f"{self.sums[name]}  {name}\n" for name in sorted(self.sums)
        ).encode()
        all_assets = {
            self.manifest["assets"]["source"]["name"]: self.source,
            self.manifest["assets"]["universal"]["name"]: self.universal,
            "release-manifest.json": self.manifest_bytes,
            "SHA256SUMS": self.sums_bytes,
        }
        if self.dmg is not None:
            all_assets[self.manifest["assets"]["dmg"]["name"]] = self.dmg
        self.release = {
            "tag_name": self.manifest["tag"],
            "draft": False,
            "prerelease": False,
            "html_url": f"https://github.com/{REPOSITORY}/releases/tag/{self.manifest['tag']}",
            "target_commitish": "main",  # deliberately not used as commit identity
            "assets": [
                {
                    "name": name,
                    "state": "uploaded",
                    "size": len(data),
                    "digest": "sha256:" + digest(data),
                    "browser_download_url": DOWNLOAD
                    + self.manifest["tag"]
                    + "/"
                    + name,
                }
                for name, data in all_assets.items()
            ],
        }

    def pin(self):
        return subject.validate_release(
            self.release, self.manifest_bytes, self.sums_bytes, self.manifest["commit"]
        )

    def responses(self):
        tag = self.manifest["tag"]
        return {
            API + "/releases/latest": encoded(self.release),
            API + "/git/ref/tags/" + tag: encoded(
                {
                    "ref": "refs/tags/" + tag,
                    "object": {"type": "commit", "sha": self.manifest["commit"]},
                }
            ),
            DOWNLOAD + tag + "/release-manifest.json": self.manifest_bytes,
            DOWNLOAD + tag + "/SHA256SUMS": self.sums_bytes,
            DOWNLOAD
            + tag
            + "/"
            + self.manifest["assets"]["source"]["name"]: self.source,
        }


class FakeTransport:
    def __init__(self, responses):
        self.responses = dict(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url not in self.responses:
            raise AssertionError("unapproved request URL")
        value = self.responses[url]
        if isinstance(value, BaseException):
            raise value
        return value


def parse_workflow(text):
    """Use Ruby's standard safe YAML parser, never evaluate workflow Ruby."""
    parser = "require 'yaml'; require 'json'; print JSON.generate(YAML.safe_load(STDIN.read, permitted_classes: [], permitted_symbols: [], aliases: false))"
    result = subprocess.run(
        ["ruby", "-e", parser],
        input=text,
        text=True,
        capture_output=True,
        timeout=10,
        check=True,
    )
    return json.loads(result.stdout)


def workflow_events(document):
    # Psych's YAML 1.1 may represent the unquoted key `on` as true.
    value = document.get("on", document.get("true"))
    if isinstance(value, str):
        return {value}
    if isinstance(value, list):
        return set(value)
    assert isinstance(value, dict), "workflow event map required"
    return set(value)


def validate_update_workflow(document):
    """Bounded BP26 three-job contract; this is not a general YAML scanner."""
    assert workflow_events(document) == {"schedule", "workflow_dispatch"}
    assert document.get("permissions") == {"contents": "read"}
    assert "concurrency" in document
    jobs = document["jobs"]
    assert set(jobs) == {"check", "smoke", "publish"}
    for name in ("check", "smoke"):
        assert jobs[name].get("permissions") == {"contents": "read"}
    assert jobs["publish"].get("permissions") == {"contents": "write"}
    assert jobs["check"].get("runs-on") == "ubuntu-latest"
    assert jobs["publish"].get("runs-on") == "ubuntu-latest"
    assert str(jobs["smoke"].get("runs-on", "")).startswith("macos-")
    assert "needs" not in jobs["check"]
    assert "if" not in jobs["check"]
    required_condition = "needs.check.outputs.changed == 'true'"
    for name, expected in (("smoke", {"check"}), ("publish", {"check", "smoke"})):
        needs = jobs[name].get("needs", [])
        if isinstance(needs, str):
            needs = [needs]
        assert set(needs) == expected
        condition = str(jobs[name].get("if", "")).strip()
        if condition.startswith("${{") and condition.endswith("}}"):
            condition = condition[3:-2].strip()
        assert condition == required_condition
    assert "changed" in jobs["check"].get("outputs", {})
    for job in jobs.values():
        assert not job.get("continue-on-error", False)
        for step in job.get("steps", []):
            assert "if" not in step and not step.get("continue-on-error", False)
            if "uses" in step:
                assert re.fullmatch(r"actions/checkout@[0-9a-f]{40}", step["uses"])
                assert step.get("with", {}).get("persist-credentials") is False

    def commands(job):
        result = []
        for step in job.get("steps", []):
            lines = step.get("run", "").splitlines()
            active_lines = [
                line.strip()
                for line in lines
                if line.strip() and not line.lstrip().startswith("#")
            ]
            if "run" in step:
                assert active_lines and active_lines[0] == "set -euo pipefail"
            for line in lines:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                # BP26 uses straight-line shell calls into the reviewed Python
                # gate, not shell control flow or ignored failures around it.
                assert not any(operator in line for operator in ("||", "&&", ";", "\\"))
                tokens = shlex.split(line, comments=True)
                assert not tokens or tokens[0] not in {
                    "if",
                    "then",
                    "else",
                    "fi",
                    "for",
                    "while",
                    "until",
                    "case",
                    "esac",
                    "!",
                }
                result.append(tokens)
        return result

    check = commands(jobs["check"])
    smoke = commands(jobs["smoke"])
    assert any(
        c[:6] == ["python3", "-B", "-m", "unittest", "discover", "-s"] and "Tests" in c
        for c in check
    )
    assert any(
        c[:2] == ["python3", "script/update_formula.py"]
        and "--current" in c
        and "--output" in c
        for c in check
    )
    assert any(c[:2] == ["brew", "style"] for c in smoke)
    assert any(c[:2] == ["brew", "audit"] for c in smoke)
    assert any(
        c[:2] == ["brew", "install"]
        and "--build-from-source" in c
        and not any(flag in c for flag in ("--force", "--overwrite"))
        for c in smoke
    )
    assert any(c[:2] == ["brew", "test"] for c in smoke)
    write_steps = [step["run"] for step in jobs["publish"]["steps"] if "run" in step]
    assert len(write_steps) == 1, "one reviewed publication block only"
    write = write_steps[0]
    active = commands(jobs["publish"])
    assert any(c == ["set", "-euo", "pipefail"] for c in active)
    gates = [
        index
        for index, c in enumerate(active)
        if c[:2] == ["python3", "script/update_formula.py"]
        and "--prepare-publication" in c
    ]
    puts = [
        index
        for index, c in enumerate(active)
        if c[:2] == ["gh", "api"]
        and "--method" in c
        and c[c.index("--method") + 1] == "PUT"
    ]
    assert len(gates) == len(puts) == 1 and gates[0] < puts[0]
    assert "old_blob_sha" in write
    assert (
        "repos/Tupig/homebrew-tokenstatmenu/contents/Formula/tokenstatmenu.rb" in write
    )
    assert len(re.findall(r"--method\s+PUT\b", write)) == 1
    assert write.index("--prepare-publication") < write.index("--method PUT")
    assert not re.search(
        r"--method\s+(?:POST|PATCH|DELETE)\b|-X\s+(?:POST|PATCH|DELETE)\b", write
    )
    assert not re.search(r"\b(?:brew|xcodebuild|swift)\b", write)
    serialized = json.dumps(document)
    assert "pull_request_target" not in serialized
    assert not re.search(
        r"secrets\.(?!GITHUB_TOKEN\b)[A-Za-z_][A-Za-z0-9_]*", serialized
    )


def valid_workflow_fixture():
    checkout = {
        "uses": "actions/checkout@" + "a" * 40,
        "with": {"persist-credentials": False},
    }
    publish = (
        'set -euo pipefail\npython3 script/update_formula.py --prepare-publication --old-blob-sha "$old_blob_sha" --output publication.json\n'
        "gh api --method PUT repos/Tupig/homebrew-tokenstatmenu/contents/Formula/tokenstatmenu.rb --input publication.json\n"
    )
    condition = "needs.check.outputs.changed == 'true'"
    return {
        "on": {"schedule": [{"cron": "17 * * * *"}], "workflow_dispatch": {}},
        "permissions": {"contents": "read"},
        "concurrency": "update-formula",
        "jobs": {
            "check": {
                "runs-on": "ubuntu-latest",
                "permissions": {"contents": "read"},
                "outputs": {"changed": "synthetic"},
                "steps": [
                    copy.deepcopy(checkout),
                    {
                        "run": "set -euo pipefail\npython3 -B -m unittest discover -s Tests -v\npython3 script/update_formula.py --current Formula/tokenstatmenu.rb --output candidate.rb\n"
                    },
                ],
            },
            "smoke": {
                "runs-on": "macos-15",
                "permissions": {"contents": "read"},
                "needs": ["check"],
                "if": condition,
                "steps": [
                    copy.deepcopy(checkout),
                    {
                        "run": "set -euo pipefail\nbrew style Formula/tokenstatmenu.rb\nbrew audit --strict Tupig/tokenstatmenu/tokenstatmenu\nbrew install --build-from-source Tupig/tokenstatmenu/tokenstatmenu\nbrew test Tupig/tokenstatmenu/tokenstatmenu\n"
                    },
                ],
            },
            "publish": {
                "runs-on": "ubuntu-latest",
                "permissions": {"contents": "write"},
                "needs": ["check", "smoke"],
                "if": condition,
                "steps": [copy.deepcopy(checkout), {"run": publish}],
            },
        },
    }


class ReleaseIdentityChecks(unittest.TestCase):
    def test_117_names_arm64_widget_recipe_and_legacy_migration(self):
        fixture = ReleaseFixture("1.1.7", "21")
        pin = fixture.pin()
        self.assertEqual(pin.source_name, "TokenStatMenu-source.zip")
        subject.validate_source_archive(fixture.source, pin)
        formula = subject.render_formula(pin)
        self.assertEqual(subject.read_current_pin(formula), pin)
        self.assertIn("depends_on arch: :arm64", formula)
        self.assertIn('architecture = "arm64"', formula)
        self.assertNotIn("GENERATE_PKGINFO_FILE=NO", formula)
        self.assertIn("Contents/PkgInfo", formula)
        self.assertIn(
            "TokenStatMenuWidget.appex/Contents/MacOS/TokenStatMenuWidget", formula
        )
        self.assertIn("WidgetExtension.resolved.entitlements", formula)
        self.assertIn("assert_equal expected_keys.sort, parsed.keys.sort", formula)
        for name in [
            "Resources/WidgetExtension-Info.plist",
            "Resources/TokenStatMenu.entitlements",
            "Sources/TokenStatWidgetExtension/TokenStatWidget.swift",
        ]:
            bad_source = source_zip("1.1.7", "21", replacements={name: None})
            bad = ReleaseFixture("1.1.7", "21", source=bad_source)
            self.assertRaises(
                subject.ValidationError,
                subject.validate_source_archive,
                bad.source,
                bad.pin(),
            )
        legacy = ReleaseFixture("1.1.5", "7").pin()
        self.assertEqual(
            subject.render_formula(legacy), subject.render_legacy_formula(legacy)
        )
        subject.validate_download_url(DOWNLOAD + "v1.1.7/TokenStatMenu.dmg")

    def reject(self, callback):
        with self.assertRaises(subject.ValidationError) as caught:
            callback()
        self.assertRegex(caught.exception.category, r"^[a-z][a-z0-9_]+$")
        return caught.exception

    def test_valid_pin_and_fixed_formula_rendering(self):
        fixture = ReleaseFixture()
        pin = fixture.pin()
        self.assertEqual(
            dataclasses.asdict(pin),
            {
                "version": "1.1.3",
                "build": "5",
                "tag": "v1.1.3",
                "commit": COMMIT,
                "source_name": "TokenStatMenu-1.1.3-source.zip",
                "source_bytes": len(fixture.source),
                "source_sha256": digest(fixture.source),
            },
        )
        with self.assertRaises((dataclasses.FrozenInstanceError, AttributeError)):
            pin.version = "9.9.9"
        formula = subject.render_formula(pin)
        self.assertEqual(formula, subject.render_formula(pin))
        self.assertIn(DOWNLOAD + "v1.1.3/TokenStatMenu-1.1.3-source.zip", formula)
        self.assertIn(digest(fixture.source), formula)
        self.assertIn(COMMIT, formula)
        self.assertEqual(subject.read_current_pin(formula), pin)

    def test_next_release_requires_the_verified_dmg_asset(self):
        fixture = ReleaseFixture("1.1.5", "7", "c" * 40)
        pin = fixture.pin()
        self.assertEqual(pin.version, "1.1.5")
        self.assertIn(
            "TokenStatMenu-1.1.5-universal.dmg",
            {asset["name"] for asset in fixture.release["assets"]},
        )
        self.assertEqual(len(fixture.release["assets"]), 5)
        altered = copy.deepcopy(fixture.manifest)
        del altered["assets"]["dmg"]
        fixture.refresh(manifest_bytes=encoded(altered))
        self.reject(fixture.pin)
        fixture = ReleaseFixture("1.1.5", "7", "c" * 40)
        fixture.release["assets"] = [
            asset
            for asset in fixture.release["assets"]
            if not asset["name"].endswith("-universal.dmg")
        ]
        self.reject(fixture.pin)

    def test_numeric_versions_increase_and_whole_pin_noop(self):
        old = ReleaseFixture("1.1.2", "4", OLD_COMMIT).pin()
        new = ReleaseFixture().pin()
        self.assertTrue(subject.needs_update(old, new))
        self.assertFalse(subject.needs_update(new, new))
        self.assertFalse(subject.needs_update(new, old))
        self.assertTrue(
            subject.needs_update(new, ReleaseFixture("1.1.10", "6", "c" * 40).pin())
        )
        for field, value in {
            "build": "6",
            "tag": "v1.1.4",
            "commit": "c" * 40,
            "source_name": "different-source.zip",
            "source_bytes": new.source_bytes + 1,
            "source_sha256": "d" * 64,
        }.items():
            with self.subTest(field=field):
                changed = dataclasses.replace(new, **{field: value})
                self.reject(lambda: subject.needs_update(new, changed))

    def test_draft_prerelease_and_invalid_version_build_commit(self):
        for field, values in {
            "version": [
                "1.01.3",
                "01.1.3",
                "1.1",
                "1.1.3-rc1",
                "1.1.3+meta",
                " 1.1.3",
                "1.1.3\n",
                True,
                113,
            ],
            "build": ["0", "05", "-1", "5x", " 5", "5\n", True, 5],
            "tag": ["1.1.3", "v1.1.4", "v1.1.3;touch bad", "v1.1.3/extra"],
            "commit": ["main", "A" * 40, "a" * 39, "a" * 41, "a" * 39 + "g"],
        }.items():
            for value in values:
                with self.subTest(field=field, value=repr(value)):
                    fixture = ReleaseFixture()
                    altered = copy.deepcopy(fixture.manifest)
                    altered[field] = value
                    fixture.refresh(manifest_bytes=encoded(altered))
                    self.reject(fixture.pin)
        for field in ("draft", "prerelease"):
            for value in (True, "false", None, 0):
                fixture = ReleaseFixture()
                fixture.release[field] = value
                self.reject(fixture.pin)

    def test_exact_manifest_schema_and_numeric_asset_types(self):
        for field in ("version", "build", "tag", "commit", "assets"):
            fixture = ReleaseFixture()
            altered = copy.deepcopy(fixture.manifest)
            del altered[field]
            fixture.refresh(manifest_bytes=encoded(altered))
            self.reject(fixture.pin)
        fixture = ReleaseFixture()
        altered = copy.deepcopy(fixture.manifest)
        altered["environment"] = "forbidden"
        fixture.refresh(manifest_bytes=encoded(altered))
        self.reject(fixture.pin)
        for field, value in [
            ("bytes", True),
            ("bytes", 0),
            ("bytes", -1),
            ("bytes", "123"),
            ("sha256", "A" * 64),
            ("sha256", "g" * 64),
            ("name", "../source.zip"),
            ("extra", "forbidden"),
        ]:
            fixture = ReleaseFixture()
            altered = copy.deepcopy(fixture.manifest)
            altered["assets"]["source"][field] = value
            fixture.refresh(manifest_bytes=encoded(altered))
            self.reject(fixture.pin)
        fixture = ReleaseFixture()
        duplicate = fixture.manifest_bytes.replace(
            b'"version":"1.1.3"', b'"version":"1.1.3","version":"1.1.3"'
        )
        fixture.refresh(manifest_bytes=duplicate)
        self.reject(fixture.pin)

    def test_tag_commit_is_bound_and_target_commitish_is_not_authority(self):
        fixture = ReleaseFixture()
        self.reject(
            lambda: subject.validate_release(
                fixture.release, fixture.manifest_bytes, fixture.sums_bytes, "c" * 40
            )
        )
        fixture.release["target_commitish"] = "another-branch"
        self.assertEqual(fixture.pin().commit, COMMIT)
        fixture.release["tag_name"] = "v1.1.4"
        self.reject(fixture.pin)

    def test_release_assets_are_unique_uploaded_and_fixed_urls(self):
        for field, value in [
            ("state", "new"),
            ("size", 1),
            ("size", True),
            ("digest", "sha256:" + "0" * 64),
            (
                "browser_download_url",
                "https://github.com/Other/Repo/releases/download/v1.1.3/source.zip",
            ),
            ("browser_download_url", "https://github.com.evil.example/source.zip"),
            (
                "browser_download_url",
                "http://github.com/Tupig/TokenStatMenu/source.zip",
            ),
            ("browser_download_url", DOWNLOAD + "v1.1.3/../source.zip"),
        ]:
            fixture = ReleaseFixture()
            fixture.release["assets"][0][field] = value
            self.reject(fixture.pin)
        fixture = ReleaseFixture()
        fixture.release["assets"].append(copy.deepcopy(fixture.release["assets"][0]))
        self.reject(fixture.pin)
        for index in range(4):
            fixture = ReleaseFixture()
            fixture.release["assets"].pop(index)
            self.reject(fixture.pin)

    def test_sum_manifest_bytes_and_asset_hashes_are_bound(self):
        fixture = ReleaseFixture()
        bad_sums = [
            fixture.sums_bytes + fixture.sums_bytes.splitlines(keepends=True)[0],
            b"\n".join(fixture.sums_bytes.splitlines()[:-1]) + b"\n",
            fixture.sums_bytes.replace(b"  ", b" ", 1),
            fixture.sums_bytes.replace(
                digest(fixture.manifest_bytes).encode(), b"0" * 64
            ),
            fixture.sums_bytes + b"0" * 64 + b"  unexpected.txt\n",
            b"\n".join(reversed(fixture.sums_bytes.splitlines())) + b"\n",
        ]
        for sums in bad_sums:
            self.reject(
                lambda: subject.validate_release(
                    fixture.release, fixture.manifest_bytes, sums, COMMIT
                )
            )
        self.reject(
            lambda: subject.validate_release(
                fixture.release,
                fixture.manifest_bytes + b" ",
                fixture.sums_bytes,
                COMMIT,
            )
        )

    def test_source_archive_validates_exact_info_and_safe_regular_entries(self):
        fixture = ReleaseFixture()
        subject.validate_source_archive(fixture.source, fixture.pin())
        for replacements in [
            {"Resources/Info.plist": None},
            {"Resources/Info.plist": b"not a plist"},
            {
                "Resources/Info.plist": plistlib.dumps(
                    {"CFBundleShortVersionString": "1.1.4", "CFBundleVersion": "5"}
                )
            },
            {
                "Resources/Info.plist": plistlib.dumps(
                    {"CFBundleShortVersionString": "1.1.3", "CFBundleVersion": "6"}
                )
            },
            {"Resources/AppIcon.icns": None},
            {"TokenStatMenu.xcodeproj/project.pbxproj": None},
            {
                "TokenStatMenu.xcodeproj/xcshareddata/xcschemes/TokenStatMenu.xcscheme": None
            },
        ]:
            changed = source_zip(replacements=replacements)
            candidate = ReleaseFixture(source=changed)
            self.reject(
                lambda: subject.validate_source_archive(changed, candidate.pin())
            )
        for name in (
            "../escape",
            "/absolute",
            "dir/../../escape",
            "evil\nname",
            "dir\\..\\escape",
        ):
            wire_name = name if name.startswith("/") else "TokenStatMenu/" + name
            changed = source_zip(extra=[(wire_name, b"synthetic")])
            candidate = ReleaseFixture(source=changed)
            self.reject(
                lambda: subject.validate_source_archive(changed, candidate.pin())
            )
        link = zipfile.ZipInfo("TokenStatMenu/source-link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        changed = source_zip(extra=[(link, b"../../escape")])
        candidate = ReleaseFixture(source=changed)
        self.reject(lambda: subject.validate_source_archive(changed, candidate.pin()))
        duplicate = source_zip(
            extra=[("TokenStatMenu/Resources/Info.plist", b"duplicate")]
        )
        candidate = ReleaseFixture(source=duplicate)
        self.reject(lambda: subject.validate_source_archive(duplicate, candidate.pin()))
        self.reject(
            lambda: subject.validate_source_archive(fixture.source[:-10], fixture.pin())
        )
        self.reject(
            lambda: subject.validate_source_archive(
                fixture.source + b"tampering", fixture.pin()
            )
        )
        for wire in (
            source_zip(prefix="WrongRoot/"),
            source_zip(prefix=""),
            source_zip(extra=[("SecondRoot/synthetic.txt", b"synthetic")]),
        ):
            candidate = ReleaseFixture(source=wire)
            self.reject(lambda: subject.validate_source_archive(wire, candidate.pin()))
        corrupt = bytearray(fixture.source)
        corrupt[corrupt.index(b"// synthetic source\n") + 3] ^= 1
        corrupt = bytes(corrupt)
        candidate = ReleaseFixture(source=corrupt)
        # Hash, size, SUMS and asset metadata all match the corrupt archive;
        # only the stored member CRC/content integrity can reject this case.
        self.assertEqual(candidate.pin().source_sha256, digest(corrupt))
        self.reject(lambda: subject.validate_source_archive(corrupt, candidate.pin()))

    def test_archive_size_and_count_limits_are_real(self):
        # Declared production bounds must be finite and comfortably cover the
        # current source export. Small patched bounds exercise the rejection
        # without creating huge public fixtures or consuming excessive memory.
        self.assertGreaterEqual(subject.MAX_SOURCE_BYTES, 4 * 1024 * 1024)
        self.assertLessEqual(subject.MAX_SOURCE_BYTES, 64 * 1024 * 1024)
        self.assertGreaterEqual(subject.MAX_SOURCE_FILES, 100)
        self.assertLessEqual(subject.MAX_SOURCE_FILES, 1024)
        fixture = ReleaseFixture()
        with mock.patch.object(subject, "MAX_SOURCE_BYTES", len(fixture.source) - 1):
            self.reject(
                lambda: subject.validate_source_archive(fixture.source, fixture.pin())
            )
        with mock.patch.object(subject, "MAX_SOURCE_FILES", 2):
            self.reject(
                lambda: subject.validate_source_archive(fixture.source, fixture.pin())
            )
        with mock.patch.object(subject, "MAX_UNCOMPRESSED_BYTES", 2):
            self.reject(
                lambda: subject.validate_source_archive(fixture.source, fixture.pin())
            )
        with mock.patch.object(
            subject, "MAX_MANIFEST_BYTES", len(fixture.manifest_bytes) - 1
        ):
            self.reject(fixture.pin)
        with mock.patch.object(subject, "MAX_SUMS_BYTES", len(fixture.sums_bytes) - 1):
            self.reject(fixture.pin)

    def test_download_url_policy_is_exact_and_redirects_are_bounded(self):
        subject.validate_download_url(
            DOWNLOAD + "v1.1.3/TokenStatMenu-1.1.3-source.zip", redirect=False
        )
        subject.validate_download_url(
            "https://release-assets.githubusercontent.com/github-production-release-asset/1/synthetic?sig=synthetic",
            redirect=True,
        )
        subject.validate_download_url(
            "https://objects.githubusercontent.com/github-production-release-asset-2e65be/1/synthetic?sig=synthetic",
            redirect=True,
        )
        for url in (
            "http://release-assets.githubusercontent.com/object",
            "https://release-assets.githubusercontent.com.evil.example/object",
            "https://evilgithub.com/object",
            "https://user:password@github.com/Tupig/TokenStatMenu/object",
            "https://github.com:444/Tupig/TokenStatMenu/object",
            "https://github.com/Other/Repo/object",
            "https://release-assets.githubusercontent.com/object#fragment",
            "https://github.com/\nOther/Repo",
            "https://release-assets.githubusercontent.com/anywhere/object",
            "https://objects.githubusercontent.com/anywhere/object",
        ):
            self.reject(lambda: subject.validate_download_url(url, redirect=True))

    def test_redirect_handler_validates_hops_drops_auth_and_bounds_redirects(self):
        handler = subject.CheckedRedirectHandler()
        self.assertGreater(handler.max_redirections, 0)
        self.assertLessEqual(handler.max_redirections, 5)
        self.assertLessEqual(handler.max_repeats, 2)
        original = urllib.request.Request(
            DOWNLOAD + "v1.1.3/TokenStatMenu-1.1.3-source.zip",
            headers={"Authorization": "Bearer synthetic-must-drop"},
        )
        allowed = "https://release-assets.githubusercontent.com/github-production-release-asset/1/synthetic?sig=synthetic"
        redirected = handler.redirect_request(
            original, None, 302, "synthetic", {}, allowed
        )
        self.assertEqual(redirected.full_url, allowed)
        self.assertNotIn("authorization", {name.lower() for name in redirected.headers})
        self.assertNotIn(
            "authorization", {name.lower() for name in redirected.unredirected_hdrs}
        )
        for target in (
            "https://evil.example/object",
            "http://release-assets.githubusercontent.com/object",
            "https://github.com/Other/Repo/releases/download/v1.1.3/asset.zip",
        ):
            self.reject(
                lambda: handler.redirect_request(
                    original, None, 302, "synthetic", {}, target
                )
            )

    def test_current_formula_metadata_is_not_evaluated_or_ambiguous(self):
        text = subject.render_formula(ReleaseFixture().pin())
        self.reject(lambda: subject.read_current_pin(text + text))
        self.reject(lambda: subject.read_current_pin(text.replace(COMMIT, "main")))
        self.reject(
            lambda: subject.read_current_pin(text + "\nsystem 'unexpected-command'\n")
        )

    def test_staging_is_noop_or_atomic_and_rejects_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candidate.rb"
            path.write_text("original")
            stamp = path.stat().st_mtime_ns
            self.assertFalse(subject.stage_candidate(path, "original"))
            self.assertEqual(path.stat().st_mtime_ns, stamp)
            with mock.patch.object(
                subject.os, "replace", side_effect=OSError("synthetic failure")
            ):
                with self.assertRaises((OSError, subject.ValidationError)):
                    subject.stage_candidate(path, "replacement")
            self.assertEqual(path.read_text(), "original")
            self.assertTrue(subject.stage_candidate(path, "replacement"))
            self.assertEqual(path.read_text(), "replacement")
            linked = Path(directory) / "linked.rb"
            linked.symlink_to(path)
            self.reject(
                lambda: subject.stage_candidate(linked, "must not replace target")
            )
            self.assertEqual(path.read_text(), "replacement")


class EntrypointNoWriteChecks(unittest.TestCase):
    def run_main(self, fixture, *, current=None, responses=None):
        current = current or ReleaseFixture("1.1.2", "4", OLD_COMMIT).pin()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        source = root / "current.rb"
        output = root / "candidate.rb"
        source.write_text(subject.render_formula(current))
        before = source.read_bytes()
        stamp = source.stat().st_mtime_ns
        transport = FakeTransport(
            responses if responses is not None else fixture.responses()
        )
        stdout, stderr = io.StringIO(), io.StringIO()
        sentinel = "synthetic-private-token-never-print"
        with (
            mock.patch.dict(
                os.environ, {"GH_TOKEN": sentinel, "GITHUB_TOKEN": sentinel}
            ),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            result = subject.main(
                ["--current", str(source), "--output", str(output)], transport=transport
            )
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(source.stat().st_mtime_ns, stamp)
        self.assertNotIn(sentinel, stdout.getvalue() + stderr.getvalue())
        self.assertNotIn(str(source), stdout.getvalue() + stderr.getvalue())
        return result, output, transport

    def test_new_release_stages_only_candidate_via_fixed_public_urls(self):
        fixture = ReleaseFixture()
        result, output, transport = self.run_main(fixture)
        self.assertEqual(result, 0)
        self.assertEqual(subject.read_current_pin(output.read_text()), fixture.pin())
        self.assertTrue(transport.calls)
        for url, kwargs in transport.calls:
            self.assertTrue(
                url.startswith(API + "/") or url.startswith(DOWNLOAD + "v1.1.3/")
            )
            self.assertNotIn("Authorization", str(kwargs))
            self.assertIn("max_bytes", kwargs)
            self.assertGreater(kwargs["max_bytes"], 0)
            self.assertLessEqual(kwargs["max_bytes"], 128 * 1024 * 1024)

    def test_same_version_older_and_no_release_never_stage(self):
        fixture = ReleaseFixture()
        result, output, _ = self.run_main(fixture, current=fixture.pin())
        self.assertEqual(result, 0)
        self.assertFalse(output.exists())
        result, output, _ = self.run_main(
            ReleaseFixture("1.1.2", "4", OLD_COMMIT), current=fixture.pin()
        )
        self.assertEqual(result, 0)
        self.assertFalse(output.exists())
        missing = urllib.error.HTTPError(
            API + "/releases/latest", 404, "synthetic no release", {}, None
        )
        result, output, _ = self.run_main(
            fixture, responses={API + "/releases/latest": missing}
        )
        self.assertEqual(result, 0)
        self.assertFalse(output.exists())

    def test_identity_network_size_and_partial_response_fail_closed(self):
        fixture = ReleaseFixture()
        for url, failure in [
            (API + "/releases/latest", TimeoutError("synthetic timeout")),
            (API + "/releases/latest", b"{not json"),
            (DOWNLOAD + "v1.1.3/release-manifest.json", b"{}"),
            (DOWNLOAD + "v1.1.3/SHA256SUMS", b"malformed"),
            (DOWNLOAD + "v1.1.3/TokenStatMenu-1.1.3-source.zip", b"partial"),
        ]:
            responses = fixture.responses()
            responses[url] = failure
            result, output, _ = self.run_main(fixture, responses=responses)
            self.assertNotEqual(result, 0)
            self.assertFalse(output.exists())
        changed = ReleaseFixture(commit="c" * 40)
        result, output, _ = self.run_main(changed, current=fixture.pin())
        self.assertNotEqual(result, 0)
        self.assertFalse(output.exists())

    def test_annotated_tag_resolves_to_same_commit_and_rejects_cycle_or_wrong_ref(self):
        fixture = ReleaseFixture()
        responses = fixture.responses()
        ref_url = API + "/git/ref/tags/v1.1.3"
        responses[ref_url] = encoded(
            {"ref": "refs/tags/v1.1.3", "object": {"type": "tag", "sha": "c" * 40}}
        )
        responses[API + "/git/tags/" + "c" * 40] = encoded(
            {"object": {"type": "commit", "sha": COMMIT}}
        )
        result, output, _ = self.run_main(fixture, responses=responses)
        self.assertEqual(result, 0)
        self.assertTrue(output.exists())
        responses[API + "/git/tags/" + "c" * 40] = encoded(
            {"object": {"type": "tag", "sha": "c" * 40}}
        )
        result, output, _ = self.run_main(fixture, responses=responses)
        self.assertNotEqual(result, 0)
        self.assertFalse(output.exists())
        responses = fixture.responses()
        responses[ref_url] = encoded(
            {"ref": "refs/heads/main", "object": {"type": "commit", "sha": COMMIT}}
        )
        result, output, _ = self.run_main(fixture, responses=responses)
        self.assertNotEqual(result, 0)
        self.assertFalse(output.exists())


class PublicationGateChecks(unittest.TestCase):
    def test_only_identical_tested_live_pin_and_old_blob_can_prepare_put(self):
        old = ReleaseFixture("1.1.2", "4", OLD_COMMIT).pin()
        tested = ReleaseFixture().pin()
        blob = "e" * 40
        payload = subject.prepare_publication(old, tested, tested, blob, blob)
        self.assertIsInstance(payload, dict)
        self.assertEqual(set(payload), {"message", "content", "sha", "branch"})
        self.assertEqual(payload["sha"], blob)
        self.assertEqual(payload["branch"], "main")
        import base64

        self.assertEqual(
            base64.b64decode(payload["content"], validate=True).decode(),
            subject.render_formula(tested),
        )
        self.assertIsNone(
            subject.prepare_publication(tested, tested, tested, blob, blob)
        )
        self.assertIsNone(subject.prepare_publication(tested, old, old, blob, blob))

    def test_changed_live_identity_and_concurrent_blob_never_prepare_payload(self):
        old = ReleaseFixture("1.1.2", "4", OLD_COMMIT).pin()
        tested = ReleaseFixture().pin()
        blob = "e" * 40
        mutations = {
            "version": "1.1.4",
            "build": "6",
            "tag": "v1.1.4",
            "commit": "c" * 40,
            "source_name": "other-source.zip",
            "source_bytes": tested.source_bytes + 1,
            "source_sha256": "f" * 64,
        }
        for field, value in mutations.items():
            with self.subTest(field=field), self.assertRaises(subject.ValidationError):
                subject.prepare_publication(
                    old,
                    tested,
                    dataclasses.replace(tested, **{field: value}),
                    blob,
                    blob,
                )
        for expected, current in (
            (blob, "f" * 40),
            ("main", blob),
            (blob, "e" * 39),
            (blob, "E" * 40),
        ):
            with self.assertRaises(subject.ValidationError):
                subject.prepare_publication(old, tested, tested, expected, current)


class FormulaAndWorkflowChecks(unittest.TestCase):
    def test_formula_has_native_no_launch_no_signal_build_contract(self):
        formula = subject.render_formula(ReleaseFixture().pin())
        self.assertIn("xcodebuild", formula)
        self.assertIn("GENERATE_PKGINFO_FILE=NO", formula)
        self.assertIn("release-source", formula)
        self.assertIn("libexec", formula)
        self.assertIn("/usr/bin/open", formula)
        self.assertIn("codesign", formula)
        self.assertNotRegex(
            formula,
            r"swift\s+build|pkill|killall|xattr|spctl|post_install|launchctl|sudo",
        )
        self.assertNotIn("open -n", formula)
        self.assertNotIn('system "open"', formula)
        self.assertNotIn("HOMEBREW_NO_REQUIRE_TAP_TRUST", formula)
        self.assertNotIn("~/Applications", formula)

    def test_workflow_write_job_is_after_same_workflow_smoke(self):
        validate_update_workflow(
            parse_workflow((ROOT / ".github/workflows/update.yml").read_text())
        )

    def test_workflow_contract_detects_known_unsafe_mutations(self):
        validate_update_workflow(valid_workflow_fixture())
        mutations = [
            lambda d: d["jobs"].update(
                {"extra-writer": {"permissions": {"contents": "write"}}}
            ),
            lambda d: d["on"].update({"pull_request": {}}),
            lambda d: d["jobs"]["smoke"].update({"permissions": {"contents": "write"}}),
            lambda d: d["jobs"]["publish"].update({"needs": ["check"]}),
            lambda d: d["jobs"]["publish"].update({"if": "always()"}),
            lambda d: d["jobs"]["publish"]["steps"][-1].update(
                {
                    "run": "gh api --method PUT repos/Other/Repo/contents/Formula/tokenstatmenu.rb"
                }
            ),
            lambda d: d["jobs"]["publish"]["steps"][-1].update(
                {
                    "run": d["jobs"]["publish"]["steps"][-1]["run"].replace(
                        "--prepare-publication", "--check"
                    )
                }
            ),
            lambda d: d["jobs"]["publish"]["steps"][-1].update(
                {
                    "run": d["jobs"]["publish"]["steps"][-1]["run"].replace(
                        "python3 script/update_formula.py",
                        "# python3 script/update_formula.py",
                    )
                }
            ),
            lambda d: d["jobs"]["publish"]["steps"][-1].update({"if": False}),
            lambda d: d["jobs"]["publish"]["steps"][-1].update(
                {"continue-on-error": True}
            ),
            lambda d: d["jobs"]["publish"].update({"continue-on-error": True}),
            lambda d: d["jobs"]["publish"]["steps"][-1].update(
                {
                    "run": d["jobs"]["publish"]["steps"][-1]["run"].replace(
                        "--output publication.json", "--output publication.json || true"
                    )
                }
            ),
            lambda d: d["jobs"]["smoke"].update(
                {"steps": [copy.deepcopy(d["jobs"]["smoke"]["steps"][0])]}
            ),
            lambda d: d["jobs"]["check"].update(
                {"steps": [copy.deepcopy(d["jobs"]["check"]["steps"][0])]}
            ),
            lambda d: d["jobs"]["smoke"]["steps"][-1].update({"if": False}),
            lambda d: d["jobs"]["publish"]["steps"][-1].update(
                {
                    "run": d["jobs"]["publish"]["steps"][-1]["run"].replace(
                        "old_blob_sha", "unvalidated_sha"
                    )
                }
            ),
            lambda d: d["jobs"]["publish"]["steps"].append(
                {"run": "gh api --method DELETE repos/Other/Repo"}
            ),
            lambda d: d["jobs"]["check"]["steps"][0].update(
                {"uses": "actions/checkout@main"}
            ),
            lambda d: d["jobs"]["check"]["steps"][0]["with"].update(
                {"persist-credentials": True}
            ),
        ]
        for mutate in mutations:
            value = valid_workflow_fixture()
            mutate(value)
            with self.assertRaises(AssertionError):
                validate_update_workflow(value)

    def test_verify_workflow_cannot_publish_or_use_privileged_pr_event(self):
        document = parse_workflow((ROOT / ".github/workflows/verify.yml").read_text())
        self.assertIn("pull_request", workflow_events(document))
        self.assertNotIn("pull_request_target", workflow_events(document))
        self.assertEqual(document.get("permissions"), {"contents": "read"})
        for job in document["jobs"].values():
            self.assertEqual(
                job.get("permissions", {"contents": "read"}), {"contents": "read"}
            )


@unittest.skipUnless(
    os.environ.get("TOKENSTAT_RUN_BREW_NATIVE") == "1",
    "explicit PM-owned installed-brew verification only",
)
class InstalledBrewPackagingChecks(unittest.TestCase):
    def test_owned_test_formula_bundle_and_rejecting_launcher(self):
        # This check never installs/uninstalls or launches anything. The PM
        # runs it after each isolated brew install/upgrade stage, preserving
        # existing App/config/process identities outside the test namespace.
        self.assertEqual(
            os.environ.get("TOKENSTAT_BREW_TEST_FORMULA"), NATIVE_TEST_FORMULA
        )
        expected = os.environ["TOKENSTAT_BREW_EXPECTED_VERSION"]
        self.assertRegex(
            expected, r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$"
        )
        expected_build = os.environ["TOKENSTAT_BREW_EXPECTED_BUILD"]
        expected_commit = os.environ["TOKENSTAT_BREW_EXPECTED_COMMIT"]
        self.assertRegex(expected_build, r"^[1-9][0-9]*$")
        self.assertRegex(expected_commit, r"^[0-9a-f]{40}$")
        output = subprocess.check_output(
            ["brew", "--prefix", NATIVE_TEST_FORMULA], text=True
        ).strip()
        prefix = Path(output)
        app = prefix / "libexec/TokenStatMenu.app"
        expected_files = {
            "Contents/Info.plist",
            "Contents/MacOS/TokenStatMenu",
            "Contents/Resources/AppIcon.icns",
            "Contents/_CodeSignature/CodeResources",
        }
        if tuple(map(int, expected.split("."))) >= (1, 1, 6):
            expected_files |= {
                "Contents/PkgInfo",
                "Contents/PlugIns/TokenStatMenuWidget.appex/Contents/Info.plist",
                "Contents/PlugIns/TokenStatMenuWidget.appex/Contents/MacOS/TokenStatMenuWidget",
                "Contents/PlugIns/TokenStatMenuWidget.appex/Contents/_CodeSignature/CodeResources",
            }
        actual = set()
        for path in app.rglob("*"):
            self.assertFalse(path.is_symlink())
            if path.is_file():
                actual.add(path.relative_to(app).as_posix())
        self.assertEqual(actual, expected_files)
        info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
        self.assertEqual(info["CFBundleShortVersionString"], expected)
        self.assertEqual(info["CFBundleVersion"], expected_build)
        self.assertEqual(info["TokenStatSourceState"], "release-source")
        self.assertEqual(info["TokenStatSourceRevision"], expected_commit)
        self.assertEqual(
            digest((app / "Contents/Resources/AppIcon.icns").read_bytes()),
            "edfc3aa520be04f987cc6c8a273995f57bafca68603d89f8265c640d0009e980",
        )
        executable = app / "Contents/MacOS/TokenStatMenu"
        architecture = subprocess.check_output(
            ["lipo", "-archs", str(executable)], text=True
        ).split()
        self.assertEqual(architecture, [platform.machine()])
        if tuple(map(int, expected.split("."))) >= (1, 1, 6):
            widget = app / "Contents/PlugIns/TokenStatMenuWidget.appex"
            widget_info = plistlib.loads((widget / "Contents/Info.plist").read_bytes())
            self.assertEqual(widget_info["CFBundleShortVersionString"], expected)
            self.assertEqual(widget_info["CFBundleVersion"], expected_build)
            self.assertEqual(
                widget_info["CFBundleIdentifier"], "com.tupig.TokenStatMenu.Widget"
            )
            self.assertEqual(
                widget_info["TokenStatWidgetGroup"], info["TokenStatWidgetGroup"]
            )
            widget_arch = subprocess.check_output(
                ["lipo", "-archs", str(widget / "Contents/MacOS/TokenStatMenuWidget")],
                text=True,
            ).split()
            self.assertEqual(widget_arch, architecture)
            if tuple(map(int, expected.split("."))) >= (1, 1, 7):
                self.assertEqual(architecture, ["arm64"])
            for bundle in [app, widget]:
                result = subprocess.run(
                    ["codesign", "-d", "--entitlements", ":-", str(bundle)],
                    capture_output=True,
                    check=True,
                )
                entitlements = plistlib.loads(result.stdout)
                self.assertEqual(
                    entitlements["com.apple.security.application-groups"],
                    [info["TokenStatWidgetGroup"]],
                )
                self.assertEqual(
                    entitlements.get("com.apple.security.app-sandbox"),
                    True if bundle == widget else None,
                )
        subprocess.run(
            ["codesign", "--verify", "--deep", "--strict", str(app)], check=True
        )
        launcher = prefix / "bin/tokenstatmenu-bp26-check"
        content = launcher.read_text()
        expected_script = (
            '#!/bin/sh\nif [ "$#" -ne 0 ]; then\n'
            '  echo "Usage: tokenstatmenu" >&2\n  exit 64\nfi\n'
            "exec /usr/bin/open "
            + shlex.quote(str(prefix / "libexec/TokenStatMenu.app"))
            + "\n"
        )
        # Complete byte equality is a hard gate BEFORE invoking even the
        # invalid-argument path. A bad launcher must never open and then exit64.
        self.assertEqual(content, expected_script)
        rejected = subprocess.run(
            [str(launcher), "--not-supported"], capture_output=True, timeout=5
        )
        self.assertEqual(
            rejected.returncode, 64, "arguments must reject before invoking open"
        )


if __name__ == "__main__":
    unittest.main()
