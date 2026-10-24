"""DMG 验收离线测试：全部系统命令被 mock，仅使用合成临时文件。"""

# fmt: off
from __future__ import annotations

import contextlib
from dataclasses import replace
import datetime as dt
import hashlib
import io
import json
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from script import verify_release_dmg as subject  # noqa: E402

class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="test-dmg-offline-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.dmg = self.root / "synthetic.dmg"
        self.dmg.write_bytes(b"synthetic DMG, not a real image")
        self.identity = subject.ReleaseIdentity(
            sha256=hashlib.sha256(self.dmg.read_bytes()).hexdigest(),
            team="ABCDE12345",
            group="ABCDE12345.com.tupig.TokenStatMenu",
            version="1.2.3",
            build="99",
            commit="a" * 40,
            size=self.dmg.stat().st_size,
        )
        self.mount = None
        self.mounts = []
        self.mutate = lambda: None
        self.override = lambda tool, args: None
        self.calls = []
        self.real_mkdtemp = tempfile.mkdtemp
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(
            subject.tempfile, "mkdtemp", side_effect=self.make_temporary
        ).start()
        mock.patch.object(
            subject.subprocess,
            "run",
            side_effect=AssertionError("real command forbidden"),
        ).start()
        mock.patch.object(subject, "run_command", side_effect=self.command).start()

    def make_temporary(self, **kwargs):
        return self.real_mkdtemp(dir=self.root, **kwargs)

    def bundle(self, widget=False):
        app = self.mount / subject.APP_NAME
        return app / "Contents/PlugIns" / subject.WIDGET_NAME if widget else app

    def entitlements(self, widget):
        result = {
            "com.apple.security.application-groups": [self.identity.group],
            "com.apple.developer.team-identifier": self.identity.team,
            "com.apple.application-identifier": self.identity.team
            + "."
            + (subject.WIDGET_ID if widget else subject.APP_ID),
        }
        if widget:
            result["com.apple.security.app-sandbox"] = True
        return result

    def profile(self, widget):
        return {
            "TeamIdentifier": [self.identity.team],
            "ApplicationIdentifierPrefix": [self.identity.team],
            "ProvisionsAllDevices": True,
            "CreationDate": dt.datetime(2020, 1, 1),
            "ExpirationDate": dt.datetime(2099, 1, 1),
            "DeveloperCertificates": [b"synthetic certificate, never a private key"],
            "Entitlements": self.entitlements(widget),
        }

    def populate(self):
        (self.mount / "Applications").symlink_to("/Applications")
        for widget in (False, True):
            bundle = self.bundle(widget)
            executable = "TokenStatMenuWidget" if widget else "TokenStatMenu"
            (bundle / "Contents/MacOS").mkdir(parents=True)
            binary = bundle / "Contents/MacOS" / executable
            binary.write_bytes(b"synthetic arm64 executable")
            binary.chmod(0o755)
            info = {
                "CFBundleIdentifier": subject.WIDGET_ID if widget else subject.APP_ID,
                "CFBundleExecutable": executable,
                "CFBundlePackageType": "XPC!" if widget else "APPL",
                "CFBundleShortVersionString": self.identity.version,
                "CFBundleVersion": self.identity.build,
                "TokenStatSourceRevision": self.identity.commit,
                "TokenStatSourceState": "clean",
                "TokenStatWidgetGroup": self.identity.group,
            }
            if widget:
                info["NSExtension"] = {
                    "NSExtensionPointIdentifier": "com.apple.widgetkit-extension"
                }
            (bundle / "Contents/Info.plist").write_bytes(plistlib.dumps(info))
            (bundle / "Contents/embedded.provisionprofile").write_bytes(
                b"synthetic CMS"
            )

    def result(self, stdout=b"", stderr=b""):
        return subprocess.CompletedProcess([], 0, stdout, stderr)

    def command(self, tool, *arguments):
        args = tuple(map(str, arguments))
        self.calls.append((tool, args))
        if tool == "hdiutil" and args[0] == "attach":
            self.mount = Path(args[args.index("-mountpoint") + 1])
            self.mounts.append(self.mount)
            self.populate()
            self.mutate()
        overridden = self.override(tool, args)
        if overridden is not None:
            return overridden
        if tool == "hdiutil":
            if args[0] == "attach":
                return self.result(
                    plistlib.dumps(
                        {
                            "system-entities": [
                                {
                                    "dev-entry": "/dev/synthetic",
                                    "mount-point": str(self.mount),
                                }
                            ]
                        }
                    )
                )
            self.assertEqual(args, ("detach", str(self.mount)))
            # 模拟卸载后的空目录，绝不触碰真实挂载或真实用户 App。
            for item in self.mount.iterdir():
                if item.is_symlink() or item.is_file():
                    item.unlink()
                else:
                    shutil.rmtree(item)
            return self.result()
        widget = subject.WIDGET_NAME in args[-1]
        if tool == "lipo":
            return self.result(b"arm64\n")
        if tool == "security":
            return self.result(plistlib.dumps(self.profile(widget)))
        if tool == "codesign":
            if "--verify" in args:
                return self.result()
            if "--entitlements" in args:
                return self.result(plistlib.dumps(self.entitlements(widget)))
            bundle_id = subject.WIDGET_ID if widget else subject.APP_ID
            details = (
                f"Identifier={bundle_id}\nTeamIdentifier={self.identity.team}\n"
                f"Authority=Developer ID Application: Synthetic Owner ({self.identity.team})\n"
                "Authority=Developer ID Certification Authority\nAuthority=Apple Root CA\n"
                "CodeDirectory v=20400 size=123 flags=0x10000(runtime) hashes=1\n"
                "Timestamp=Jan 1, 2025 at 00:00:00\n"
            )
            return self.result(stderr=details.encode())
        if tool == "spctl":
            return self.result(
                stderr=(
                    "synthetic: accepted\nsource=Notarized Developer ID\n"
                    f"origin=Developer ID Application: Synthetic Owner ({self.identity.team})\n"
                ).encode()
            )
        self.fail("unexpected command: " + repr((tool, args)))

    def verify(self):
        return subject.verify_release_dmg(self.dmg, self.identity)

    def assert_rejected(self, message=None):
        with self.assertRaises(subject.ValidationError) as raised:
            self.verify()
        if message:
            self.assertIn(message, str(raised.exception))
        if self.mount:
            self.assertIn(("hdiutil", ("detach", str(self.mount))), self.calls)
        return raised.exception

    def change_info(self, widget, key, value):
        path = self.bundle(widget) / "Contents/Info.plist"
        info = plistlib.loads(path.read_bytes())
        if value is None:
            info.pop(key, None)
        else:
            info[key] = value
        path.write_bytes(plistlib.dumps(info))

    def test_success_reports_pinned_identity_without_signing_or_installing(self):
        report = self.verify()
        self.assertEqual(report["status"], "verified")
        self.assertEqual(report["size"], self.identity.size)
        self.assertEqual(report["sha256"], self.identity.sha256)
        self.assertFalse(self.mount.parent.exists())
        attach = self.calls[0]
        self.assertEqual(attach[0], "hdiutil")
        for flag in ("-readonly", "-nobrowse", "-noautoopen", "-plist", "-mountpoint"):
            self.assertIn(flag, attach[1])
        verifies = [
            args
            for tool, args in self.calls
            if tool == "codesign" and "--verify" in args
        ]
        self.assertEqual(len(verifies), 2)
        self.assertTrue(
            all("--deep" in args and "--strict" in args for args in verifies)
        )
        self.assertFalse(any("--sign" in args for _, args in self.calls))
        self.assertEqual({tool for tool, _ in self.calls}, set(subject.TOOLS))

    def test_distinct_mount_directory_for_each_run(self):
        self.verify()
        self.verify()
        self.assertNotEqual(*self.mounts)

    def test_size_can_be_pinned_or_reported(self):
        self.identity = replace(self.identity, size=None)
        self.assertEqual(self.verify()["size"], self.dmg.stat().st_size)

    def test_bad_outer_identity_rejected_before_mount(self):
        for change in (
            {"sha256": "b" * 64},
            {"size": 1},
            {"team": "bad"},
            {"group": "group.*"},
            {"commit": "main"},
            {"version": "v1.2.3"},
            {"build": "0"},
            {"size": -1},
            {"sha256": "A" * 64},
        ):
            with self.subTest(change=change):
                with self.assertRaises(subject.ValidationError):
                    subject.verify_release_dmg(
                        self.dmg, replace(self.identity, **change)
                    )
                self.assertEqual(self.calls, [])

    def test_empty_oversized_and_symlink_dmg_rejected(self):
        self.dmg.write_bytes(b"")
        self.assert_rejected("size")
        self.dmg.write_bytes(b"too large")
        with mock.patch.object(subject, "MAX_DMG_BYTES", 2):
            self.assert_rejected("size")
        link = self.root / "link.dmg"
        link.symlink_to(self.dmg)
        with self.assertRaisesRegex(subject.ValidationError, "regular"):
            subject.verify_release_dmg(link, self.identity)
        self.assertEqual(self.calls, [])

    def test_exact_root_rejects_unexpected_files(self):
        self.mutate = lambda: (self.mount / ".DS_Store").write_bytes(b"extra")
        self.assert_rejected("exactly")

    def test_applications_must_be_exact_absolute_symlink(self):
        def mutate():
            link = self.mount / "Applications"
            link.unlink()
            link.symlink_to("../../Applications")

        self.mutate = mutate
        self.assert_rejected("Applications")

    def test_symlink_inside_app_rejected(self):
        self.mutate = lambda: (self.bundle() / "Contents/escape").symlink_to("/etc")
        self.assert_rejected("unsafe bundle member")

    def test_extra_appex_rejected(self):
        self.mutate = lambda: (self.bundle() / "Contents/PlugIns/Other.appex").mkdir()
        self.assert_rejected("exactly one")

    def test_each_bundle_identity_field_is_fixed(self):
        cases = {
            "CFBundleIdentifier": "com.other.app",
            "CFBundleExecutable": "../escape",
            "CFBundlePackageType": "BAD!",
            "CFBundleShortVersionString": "9.9.9",
            "CFBundleVersion": "100",
            "TokenStatSourceRevision": "b" * 40,
            "TokenStatSourceState": "modified",
            "TokenStatWidgetGroup": "group.other",
        }
        for widget in (False, True):
            for key, value in cases.items():
                with self.subTest(widget=widget, key=key):
                    self.mutate = lambda w=widget, k=key, v=value: self.change_info(
                        w, k, v
                    )
                    self.assert_rejected(key)

    def test_missing_source_revision_and_profile_fail_closed(self):
        self.mutate = lambda: self.change_info(True, "TokenStatSourceRevision", None)
        self.assert_rejected("TokenStatSourceRevision")
        self.mutate = lambda: (
            self.bundle() / "Contents/embedded.provisionprofile"
        ).unlink()
        self.assert_rejected("profile")

    def test_bad_widget_extension_point(self):
        self.mutate = lambda: self.change_info(
            True, "NSExtension", {"NSExtensionPointIdentifier": "wrong"}
        )
        self.assert_rejected("extension point")

    def test_architecture_rejects_intel_universal_and_empty(self):
        for arch in (b"x86_64\n", b"arm64 x86_64\n", b"", b"arm64e\n"):
            with self.subTest(arch=arch):
                self.override = lambda tool, args, a=arch: (
                    self.result(a) if tool == "lipo" else None
                )
                self.assert_rejected("arm64")

    def test_widget_architecture_is_independently_checked(self):
        self.override = lambda tool, args: (
            self.result(b"x86_64")
            if tool == "lipo" and subject.WIDGET_NAME in args[-1]
            else None
        )
        self.assert_rejected("arm64")

    def test_signature_rejects_adhoc_apple_development_wrong_team_and_no_runtime(self):
        original = self.command_details
        changes = [
            lambda data: data + b"Signature=adhoc\n",
            lambda data: data.replace(
                b"Developer ID Application:", b"Apple Development:"
            ),
            lambda data: data.replace(
                b"TeamIdentifier=ABCDE12345", b"TeamIdentifier=ZZZZZ99999"
            ),
            lambda data: data.replace(b"(runtime)", b"(none)"),
            lambda data: data.replace(b"flags=0x10000", b"flags=0x10002"),
            lambda data: data.replace(b"flags=0x10000", b"flags=0x0"),
            lambda data: data.replace(b"Timestamp=synthetic\n", b""),
            lambda data: data.replace(subject.APP_ID.encode(), b"com.other.app"),
        ]
        for change in changes:
            with self.subTest(change=change):
                self.override = lambda tool, args, c=change: (
                    self.result(stderr=c(original(args)))
                    if tool == "codesign" and "--verbose=4" in args
                    else None
                )
                self.assert_rejected()

    def command_details(self, args):
        bundle_id = (
            subject.WIDGET_ID if subject.WIDGET_NAME in args[-1] else subject.APP_ID
        )
        return (
            f"Identifier={bundle_id}\nTeamIdentifier={self.identity.team}\n"
            f"Authority=Developer ID Application: Synthetic ({self.identity.team})\n"
            "Authority=Developer ID Certification Authority\nAuthority=Apple Root CA\n"
            "CodeDirectory v=20500 size=123 flags=0x10000(runtime) hashes=1\nTimestamp=synthetic\n"
        ).encode()

    def test_real_codesign_code_directory_space_format_is_parsed(self):
        details = self.command_details((subject.APP_NAME,))
        fields = subject.signature_fields(details)
        self.assertEqual(fields["CodeDirectory"], [
            "v=20500 size=123 flags=0x10000(runtime) hashes=1"])
        self.assertNotIn("CodeDirectory v", fields)
        self.assertEqual(self.verify()["status"], "verified")

    def test_duplicate_or_missing_code_directory_is_rejected(self):
        for duplicate in (False, True):
            with self.subTest(duplicate=duplicate):
                def override(tool, args, repeat=duplicate):
                    if tool == "codesign" and "--verbose=4" in args:
                        details = self.command_details(args)
                        line = next(line for line in details.splitlines(keepends=True)
                                    if line.startswith(b"CodeDirectory "))
                        return self.result(stderr=details + line if repeat else details.replace(line, b""))
                    return None
                self.override = override
                self.assert_rejected("hardened runtime")

    def test_both_profiles_allow_omitted_sandbox_and_multiple_authorized_groups(self):
        def override(tool, args):
            if tool == "security":
                widget = subject.WIDGET_NAME in args[-1]
                profile = self.profile(widget)
                profile["Entitlements"].pop("com.apple.security.app-sandbox", None)
                profile["Entitlements"]["com.apple.security.application-groups"] = [
                    "ABCDE12345.com.tupig.other", self.identity.group]
                return self.result(plistlib.dumps(profile))
            return None
        self.override = override
        self.assertEqual(self.verify()["status"], "verified")
        self.assertEqual(sum(tool == "security" for tool, _ in self.calls), 2)

    def test_present_profile_sandbox_must_match_each_bundle(self):
        for widget in (False, True):
            for sandbox in (not widget, "true", 1):
                with self.subTest(widget=widget, sandbox=sandbox):
                    def override(tool, args, w=widget, value=sandbox):
                        if tool == "security" and (subject.WIDGET_NAME in args[-1]) == w:
                            profile = self.profile(w)
                            profile["Entitlements"]["com.apple.security.app-sandbox"] = value
                            return self.result(plistlib.dumps(profile))
                        return None
                    self.override = override
                    self.assert_rejected("profile: sandbox policy mismatch")

    def test_explicit_compatible_profile_sandbox_succeeds_for_both_bundles(self):
        def override(tool, args):
            if tool == "security":
                widget = subject.WIDGET_NAME in args[-1]
                profile = self.profile(widget)
                profile["Entitlements"]["com.apple.security.app-sandbox"] = widget
                return self.result(plistlib.dumps(profile))
            return None
        self.override = override
        self.assertEqual(self.verify()["status"], "verified")

    def test_profile_groups_must_explicitly_contain_target_for_each_bundle(self):
        for widget in (False, True):
            for groups in ([], ["ABCDE12345.other"], ["ABCDE12345.*"], self.identity.group):
                with self.subTest(widget=widget, groups=groups):
                    def override(tool, args, w=widget, value=groups):
                        if tool == "security" and (subject.WIDGET_NAME in args[-1]) == w:
                            profile = self.profile(w)
                            profile["Entitlements"]["com.apple.security.application-groups"] = value
                            return self.result(plistlib.dumps(profile))
                        return None
                    self.override = override
                    self.assert_rejected("profile: App Group mismatch")

    def test_signed_groups_cannot_expand_even_when_profile_authorizes_extra_group(self):
        for widget in (False, True):
            with self.subTest(widget=widget):
                def override(tool, args, w=widget):
                    groups = [self.identity.group, "ABCDE12345.com.tupig.other"]
                    if tool == "security":
                        profile = self.profile(subject.WIDGET_NAME in args[-1])
                        profile["Entitlements"]["com.apple.security.application-groups"] = groups
                        return self.result(plistlib.dumps(profile))
                    if tool == "codesign" and "--entitlements" in args and (subject.WIDGET_NAME in args[-1]) == w:
                        entitlements = self.entitlements(w)
                        entitlements["com.apple.security.application-groups"] = groups
                        return self.result(plistlib.dumps(entitlements))
                    return None
                self.override = override
                self.assert_rejected("signature: App Group mismatch")

    def test_actual_signature_sandbox_is_strict_for_main_and_widget(self):
        for widget, sandbox in ((False, True), (False, "false"), (False, 0),
                                (True, False), (True, None), (True, "true"), (True, 1)):
            with self.subTest(widget=widget, sandbox=sandbox):
                def override(tool, args, w=widget, value=sandbox):
                    if tool == "codesign" and "--entitlements" in args and (subject.WIDGET_NAME in args[-1]) == w:
                        entitlements = self.entitlements(w)
                        if value is None:
                            entitlements.pop("com.apple.security.app-sandbox", None)
                        else:
                            entitlements["com.apple.security.app-sandbox"] = value
                        return self.result(plistlib.dumps(entitlements))
                    return None
                self.override = override
                self.assert_rejected("signature:")

    def test_entitlement_mismatches_and_debug_entitlements(self):
        cases = {
            "com.apple.security.application-groups": ["wrong"],
            "com.apple.developer.team-identifier": "ZZZZZ99999",
            "com.apple.application-identifier": "ABCDE12345.*",
            "get-task-allow": True,
            "com.apple.security.get-task-allow": True,
            "com.apple.security.cs.disable-library-validation": True,
            "com.apple.security.cs.allow-dyld-environment-variables": True,
            "com.apple.security.app-sandbox": False,
        }
        for key, value in cases.items():
            with self.subTest(key=key):

                def override(tool, args, k=key, v=value):
                    if (
                        tool == "codesign"
                        and "--entitlements" in args
                        and subject.WIDGET_NAME in args[-1]
                    ):
                        entitlements = self.entitlements(True)
                        entitlements[k] = v
                        return self.result(plistlib.dumps(entitlements))
                    return None

                self.override = override
                self.assert_rejected()

    def test_profile_identity_expiry_distribution_and_entitlements(self):
        cases = {
            "TeamIdentifier": ["ZZZZZ99999"],
            "ApplicationIdentifierPrefix": ["ZZZZZ99999"],
            "ProvisionsAllDevices": False,
            "ProvisionedDevices": ["synthetic-device"],
            "CreationDate": dt.datetime(2099, 1, 1),
            "ExpirationDate": dt.datetime(2020, 1, 1),
            "DeveloperCertificates": [],
            "Entitlements": {},
            "ExpirationDateMissing": None,
        }
        for widget in (False, True):
            for key, value in cases.items():
                with self.subTest(widget=widget, key=key):

                    def override(tool, args, w=widget, k=key, v=value):
                        if (
                            tool == "security"
                            and (subject.WIDGET_NAME in args[-1]) == w
                        ):
                            profile = self.profile(w)
                            if k == "ExpirationDateMissing":
                                profile.pop("ExpirationDate")
                            else:
                                profile[k] = v
                            return self.result(plistlib.dumps(profile))
                        return None

                    self.override = override
                    self.assert_rejected("profile")

    def test_each_external_command_failure_fails_and_detaches(self):
        for failed_tool in ("hdiutil", "lipo", "codesign", "security", "spctl"):
            with self.subTest(tool=failed_tool):

                def override(tool, args, failed=failed_tool):
                    if tool == failed and not (
                        tool == "hdiutil" and args[0] == "detach"
                    ):
                        raise subject.ValidationError("synthetic command failure")
                    return None

                self.override = override
                self.assert_rejected("synthetic command failure")

    def test_gatekeeper_success_without_notarized_source_or_expected_team_rejected(
        self,
    ):
        for output in (
            b"accepted\n",
            b"source=Developer ID\n",
            b"source=Notarized Developer ID\norigin=Developer ID Application: Other (ZZZZZ99999)\n",
        ):
            with self.subTest(output=output):
                self.override = lambda tool, args, data=output: (
                    self.result(stderr=data) if tool == "spctl" else None
                )
                self.assert_rejected("Gatekeeper")

    def test_malformed_attach_and_unexpected_mounts_still_detach(self):
        for output in (
            b"not a plist",
            plistlib.dumps({"system-entities": []}),
            plistlib.dumps({"system-entities": [{"mount-point": "/elsewhere"}]}),
        ):
            with self.subTest(output=output):
                self.override = lambda tool, args, data=output: (
                    self.result(data)
                    if tool == "hdiutil" and args[0] == "attach"
                    else None
                )
                self.assert_rejected()

    def test_corrupt_bundle_plist_still_detaches(self):
        self.mutate = lambda: (self.bundle() / "Contents/Info.plist").write_bytes(
            b"invalid"
        )
        self.assert_rejected("plist")

    def test_malformed_xml_fails_closed_and_detaches(self):
        self.mutate = lambda: (self.bundle() / "Contents/Info.plist").write_bytes(
            b"<?xml version='1.0'?><plist><dict>"
        )
        self.assert_rejected("plist")

    def test_ad_hoc_with_duplicate_signature_fields_is_rejected(self):
        self.override = lambda tool, args: (
            self.result(
                stderr=self.command_details(args)
                + b"Signature=adhoc\nSignature=other\n"
            )
            if tool == "codesign" and "--verbose=4" in args
            else None
        )
        self.assert_rejected("ad hoc")

    def test_detach_failure_is_failure_even_after_all_checks_pass(self):
        def override(tool, args):
            if tool == "hdiutil" and args[0] == "detach":
                raise subject.ValidationError("detach failed")
            return None

        self.override = override
        self.assert_rejected("detach failed")
        self.assertTrue(self.mount.exists())

    def test_image_change_during_checks_fails(self):
        def override(tool, args):
            if tool == "spctl":
                self.dmg.write_bytes(b"changed")
            return None

        self.override = override
        self.assert_rejected("size mismatch")

    def test_cli_requires_all_pins_and_outputs_json_only_after_detach(self):
        arguments = ["--dmg", str(self.dmg)]
        for key in ("sha256", "team", "group", "version", "build", "commit"):
            arguments.extend(["--" + key, getattr(self.identity, key)])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(subject.main(arguments), 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "verified")
        self.assertFalse(self.mount.exists())
        for key in ("sha256", "team", "group", "version", "build", "commit"):
            incomplete = arguments.copy()
            index = incomplete.index("--" + key)
            del incomplete[index : index + 2]
            with (
                self.subTest(missing=key),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as error,
            ):
                subject.main(incomplete)
            self.assertEqual(error.exception.code, 2)

    def test_cli_failure_has_nonzero_status_and_no_success_json(self):
        output, errors = io.StringIO(), io.StringIO()
        arguments = ["--dmg", str(self.dmg)]
        for key in ("sha256", "team", "group", "version", "build", "commit"):
            arguments.extend(
                [
                    "--" + key,
                    "b" * 64 if key == "sha256" else getattr(self.identity, key),
                ]
            )
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            self.assertEqual(subject.main(arguments), 1)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("sha256 mismatch", errors.getvalue())

class CommandBoundaryTests(unittest.TestCase):
    def test_command_is_read_only_fixed_tool_and_no_shell(self):
        completed = subprocess.CompletedProcess([], 0, b"ok", b"")
        with mock.patch.object(
            subject.subprocess, "run", return_value=completed
        ) as run:
            self.assertIs(
                subject.run_command("codesign", "--verify", "synthetic.app"), completed
            )
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["/usr/bin/codesign", "--verify", "synthetic.app"])
        self.assertNotIn("shell", kwargs)
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["timeout"], subject.TIMEOUT)

    def test_nonzero_exit_missing_tool_and_timeout_fail_closed(self):
        failures = [OSError("missing"), subprocess.TimeoutExpired("synthetic", 1)]
        for failure in failures:
            with (
                self.subTest(failure=failure),
                mock.patch.object(subject.subprocess, "run", side_effect=failure),
            ):
                with self.assertRaises(subject.ValidationError):
                    subject.run_command("spctl", "--assess", "synthetic.app")
        with mock.patch.object(
            subject.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 3, b"", b"rejected"),
        ):
            with self.assertRaisesRegex(subject.ValidationError, "failed"):
                subject.run_command("spctl", "--assess", "synthetic.app")

if __name__ == "__main__":
    unittest.main()
