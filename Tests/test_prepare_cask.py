"""Cask 准备入口离线测试：合成 DMG、mock 验证器，禁止真实系统命令和网络。"""

# fmt: off
from __future__ import annotations

import contextlib
from dataclasses import replace
import hashlib
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from script import prepare_cask as subject  # noqa: E402
from script import verify_release_dmg as verifier  # noqa: E402

class PrepareCaskTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="test-cask-offline-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.dmg = self.root / "synthetic.dmg"
        self.dmg.write_bytes(b"synthetic DMG, never mounted")
        self.output = self.root / "candidate.rb"
        self.identity = subject.ReleaseIdentity(
            sha256=hashlib.sha256(self.dmg.read_bytes()).hexdigest(),
            team="ABCDE12345",
            group="ABCDE12345.com.tupig.TokenStatMenu",
            version="1.2.3",
            build="99",
            commit="a" * 40,
        )
        for target in (
            "subprocess.run",
            "socket.create_connection",
            "urllib.request.urlopen",
        ):
            patch = mock.patch(target, side_effect=AssertionError("real I/O forbidden"))
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch.object(subject, "verify_release_dmg")
        self.verify = patch.start()
        self.addCleanup(patch.stop)

    def prepare(self, **kwargs):
        return subject.prepare_cask(
            kwargs.get("dmg", self.dmg),
            kwargs.get("identity", self.identity),
            kwargs.get("output", self.output),
        )

    def arguments(self):
        args = ["--dmg", str(self.dmg), "--output", str(self.output)]
        for name in ("sha256", "team", "group", "version", "build", "commit"):
            args.extend(["--" + name, getattr(self.identity, name)])
        return args

    def test_candidate_uses_exact_approved_identity_and_minimal_cask(self):
        self.assertEqual(self.prepare(), self.output)
        self.verify.assert_called_once_with(self.dmg, self.identity)
        text = self.output.read_text()
        self.assertEqual(text, subject.render_cask(self.identity))
        for field in ("sha256", "team", "group", "version", "build", "commit"):
            self.assertIn(getattr(self.identity, field), text)
        self.assertIn(
            'url "https://github.com/Tupig/TokenStatMenu/releases/download/v1.2.3/TokenStatMenu.dmg"',
            text,
        )
        self.assertEqual(text.count("depends_on "), 2)
        self.assertIn('depends_on macos: ">= :ventura"', text)
        self.assertIn("depends_on arch: :arm64", text)
        self.assertIn('app "TokenStatMenu.app"', text)
        self.assertIn("public release availability is NOT verified", text)
        self.assertIn("official public DMG has this same SHA256", text)
        self.assertNotRegex(
            text,
            r"\b(?:system|installer|uninstall|zap|kill|killall|pkill|open|"
            r"xcodebuild|codesign|swift|brew|preflight|postflight)\b",
        )
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o644)
        self.assertEqual(set(self.root.iterdir()), {self.dmg, self.output})

    def test_all_identity_fields_reject_ruby_and_control_injection_before_verification(
        self,
    ):
        for field in ("sha256", "team", "group", "version", "build", "commit"):
            for value in ('"; system "bad"', '#{system("bad")}', "abc\n", "abc\x00"):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(subject.ValidationError):
                        self.prepare(identity=replace(self.identity, **{field: value}))
                    self.assertFalse(self.output.exists())
        self.verify.assert_not_called()

    def test_invalid_identity_syntax_is_not_implicitly_normalized(self):
        for field, values in {
            "sha256": ["A" * 64, "a" * 63],
            "team": ["abcde12345", "A" * 11],
            "group": ["group.*", "group..x", "group."],
            "version": ["v1.2.3", "01.2.3", "1.2.3-rc1", "1.2"],
            "build": ["0", "01", "-1"],
            "commit": ["main", "A" * 40, "a" * 39],
        }.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    with self.assertRaises(subject.ValidationError):
                        self.prepare(identity=replace(self.identity, **{field: value}))
        self.verify.assert_not_called()

    def test_verification_failure_including_detach_never_leaves_output_or_temporary(
        self,
    ):
        for reason in (
            "sha256 mismatch",
            "signature",
            "profile",
            "notarization",
            "arm64",
            "detach",
        ):
            with self.subTest(reason=reason):
                self.verify.side_effect = subject.ValidationError(reason)
                with self.assertRaisesRegex(subject.ValidationError, reason):
                    self.prepare()
                self.assertEqual(list(self.root.iterdir()), [self.dmg])

    def test_real_verifier_rejects_wrong_hash_before_any_mount(self):
        self.verify.side_effect = verifier.verify_release_dmg
        with mock.patch.object(verifier, "run_command") as command:
            with self.assertRaisesRegex(subject.ValidationError, "sha256 mismatch"):
                self.prepare(identity=replace(self.identity, sha256="b" * 64))
        command.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_real_verifier_failure_is_propagated_and_detach_is_required(self):
        self.verify.side_effect = verifier.verify_release_dmg
        real_mkdtemp = tempfile.mkdtemp
        with (
            mock.patch.object(
                verifier,
                "run_command",
                side_effect=subject.ValidationError("mock system failure"),
            ) as command,
            mock.patch.object(
                verifier.tempfile,
                "mkdtemp",
                side_effect=lambda **kwargs: real_mkdtemp(dir=self.root, **kwargs),
            ),
        ):
            with self.assertRaisesRegex(subject.ValidationError, "mock system failure"):
                self.prepare()
        self.assertEqual(
            [call.args[1] for call in command.call_args_list], ["attach", "detach"]
        )
        # detach 失败按验证器约定保留空挂载目录；只清理本测试制造的空目录。
        mount = Path(command.call_args.args[-1])
        mount.rmdir()
        mount.parent.rmdir()
        self.assertFalse(self.output.exists())

    def test_existing_file_directory_and_dangling_symlink_are_not_overwritten(self):
        self.output.write_text("user work")
        with self.assertRaisesRegex(subject.ValidationError, "already exists"):
            self.prepare()
        self.assertEqual(self.output.read_text(), "user work")
        self.output.unlink()
        self.output.mkdir()
        with self.assertRaises(subject.ValidationError):
            self.prepare()
        self.output.rmdir()
        self.output.symlink_to(self.root / "missing")
        with self.assertRaises(subject.ValidationError):
            self.prepare()
        self.assertTrue(self.output.is_symlink())
        self.verify.assert_not_called()

    def test_symlinked_parent_missing_parent_and_invalid_paths_rejected(self):
        parent = self.root / "linked"
        parent.symlink_to(self.root, target_is_directory=True)
        for output in (
            parent / "candidate.rb",
            self.root / "missing/candidate.rb",
            self.root / "candidate.txt",
            self.root / "bad\n.rb",
            self.root / "bad\x00.rb",
            self.root / "sub/../candidate.rb",
        ):
            with (
                self.subTest(output=output),
                self.assertRaises(subject.ValidationError),
            ):
                self.prepare(output=output)
        with self.assertRaises(subject.ValidationError):
            self.prepare(dmg=self.root / "bad\n.dmg")
        self.verify.assert_not_called()

    def test_file_is_only_published_after_verification_and_full_write(self):
        def verify(*_):
            self.assertEqual(list(self.root.iterdir()), [self.dmg])

        self.verify.side_effect = verify
        real_link = subject.os.link

        def publish(source, target, **kwargs):
            self.assertFalse(self.output.exists())
            self.assertEqual((self.root / source).read_text(), subject.render_cask(self.identity))
            self.assertEqual(kwargs["src_dir_fd"], kwargs["dst_dir_fd"])
            self.assertFalse(kwargs["follow_symlinks"])
            real_link(source, target, **kwargs)

        with mock.patch.object(subject.os, "link", side_effect=publish) as link:
            self.prepare()
        link.assert_called_once()

    def test_competing_output_is_preserved_and_temporary_removed(self):
        real_link = subject.os.link

        def compete(source, target, **kwargs):
            self.output.write_text("concurrent user work")
            real_link(source, target, **kwargs)

        with mock.patch.object(subject.os, "link", side_effect=compete):
            with self.assertRaises(FileExistsError):
                self.prepare()
        self.assertEqual(self.output.read_text(), "concurrent user work")
        self.assertEqual(set(self.root.iterdir()), {self.dmg, self.output})

    def test_parent_replacement_during_verification_or_publication_fails_safely(self):
        for phase in ("verification", "publication"):
            for symlink in (True, False):
                with self.subTest(phase=phase, symlink=symlink):
                    case = self.root / (phase + str(symlink))
                    case.mkdir()
                    parent = case / "parent"
                    parent.mkdir()
                    moved = case / "moved"
                    other = case / "other"
                    other.mkdir()
                    sentinel = other / "candidate.rb"
                    sentinel.write_text("unrelated user work")
                    self.output = parent / "candidate.rb"
                    def replace_parent():
                        parent.rename(moved)
                        if symlink:
                            parent.symlink_to(other, target_is_directory=True)
                        else:
                            parent.mkdir()
                            (parent / "candidate.rb").write_text("replacement user work")
                    real_link = subject.os.link
                    def publish(source, target, **kwargs):
                        if phase == "publication":
                            replace_parent()
                        self.assertEqual(kwargs["src_dir_fd"], kwargs["dst_dir_fd"])
                        real_link(source, target, **kwargs)
                        self.assertTrue((moved / "candidate.rb").is_file())
                    self.verify.side_effect = (
                        (lambda *_: replace_parent()) if phase == "verification" else None
                    )
                    with mock.patch.object(subject.os, "link", side_effect=publish):
                        with self.assertRaises(subject.ValidationError):
                            self.prepare()
                    self.assertEqual(list(moved.iterdir()), [])
                    self.assertEqual(list(other.iterdir()), [sentinel])
                    self.assertEqual(sentinel.read_text(), "unrelated user work")
                    if not symlink:
                        self.assertEqual((parent / "candidate.rb").read_text(), "replacement user work")

    def test_failed_path_recheck_does_not_remove_replaced_candidate(self):
        parent = self.root / "parent"
        parent.mkdir()
        moved = self.root / "moved"
        self.output = parent / "candidate.rb"
        real_link = subject.os.link
        def publish(source, target, **kwargs):
            real_link(source, target, **kwargs)
            parent.rename(moved)
            parent.mkdir()
            candidate = moved / "candidate.rb"
            candidate.unlink()
            candidate.write_text("concurrent replacement")
        with mock.patch.object(subject.os, "link", side_effect=publish):
            with self.assertRaisesRegex(subject.ValidationError, "parent changed"):
                self.prepare()
        self.assertEqual((moved / "candidate.rb").read_text(), "concurrent replacement")
        self.assertEqual([item.name for item in moved.iterdir()], ["candidate.rb"])
        self.assertEqual(list(parent.iterdir()), [])

    def test_write_sync_and_link_errors_remove_temporary_and_leave_no_output(self):
        for operation in ("fsync", "fchmod", "link"):
            with self.subTest(operation=operation):
                with mock.patch.object(
                    subject.os, operation, side_effect=OSError("mock failure")
                ):
                    with self.assertRaisesRegex(OSError, "mock failure"):
                        self.prepare()
                self.assertEqual(list(self.root.iterdir()), [self.dmg])

    def test_cli_success_warns_that_public_asset_is_not_verified(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(subject.main(self.arguments()), 0)
        self.assertIn("public DMG availability is NOT verified", output.getvalue())
        self.assertTrue(self.output.is_file())

    def test_cli_requires_every_pin_rejects_extra_flags_and_abbreviations(self):
        arguments = self.arguments()
        cases = [
            arguments + ["--force"],
            ["--ver" if arg == "--version" else arg for arg in arguments],
        ]
        for flag in (
            "dmg",
            "sha256",
            "team",
            "group",
            "version",
            "build",
            "commit",
            "output",
        ):
            incomplete = arguments.copy()
            index = incomplete.index("--" + flag)
            del incomplete[index : index + 2]
            cases.append(incomplete)
        for case in cases:
            with (
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as error,
            ):
                subject.main(case)
            self.assertEqual(error.exception.code, 2)
        self.verify.assert_not_called()

    def test_cli_failure_has_nonzero_status_without_success_output(self):
        self.verify.side_effect = subject.ValidationError("mock verification failed")
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            self.assertEqual(subject.main(self.arguments()), 1)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("mock verification failed", errors.getvalue())
        self.assertFalse(self.output.exists())

if __name__ == "__main__":
    unittest.main()
