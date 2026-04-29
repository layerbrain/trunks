from __future__ import annotations

import io
import json
import os
import stat
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from trunks.backends.s3 import S3
from trunks.backends.local import Local
from trunks.cli import _storage_profiles_from_args, _validate_storage_roundtrip, dispatch, init, storage
from trunks.credentials import S3Credentials
from trunks.errors import BackendUnavailable
from trunks.repository import Repository
from trunks.storage import Storage
from trunks.url import backend_from_storage, s3_scheme_defaults


class StorageCommandTests(unittest.IsolatedAsyncioTestCase):
    def test_minio_url_uses_http_endpoint_and_bucket_path(self) -> None:
        defaults = s3_scheme_defaults("minio://127.0.0.1:9200/company-code", endpoint=None, region=None)
        self.assertEqual(defaults.endpoint, "http://127.0.0.1:9200")
        self.assertEqual(defaults.bucket, "company-code")
        self.assertEqual(defaults.prefix, "")

    async def test_show_outside_repo_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                err = io.StringIO()
                with redirect_stderr(err):
                    rc = await storage(None, None)
                self.assertEqual(rc, 1)
                self.assertIn("trunks init", err.getvalue())
            finally:
                os.chdir(cwd)

    async def test_show_with_no_remote(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                init(name=None, backend=None)
                out = io.StringIO()
                with redirect_stdout(out):
                    rc = await storage(None, None)
                self.assertEqual(rc, 0)
                self.assertIn("none (local-only)", out.getvalue())
            finally:
                os.chdir(cwd)

    async def test_storage_list_json_uses_api_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                init(name="app", backend=None)
                target = str(Path(tmp) / "remote.trunk")
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(await storage("add", "primary", target), 0)
                out = io.StringIO()
                with redirect_stdout(out):
                    self.assertEqual(await dispatch(["storage", "list", "--json"]), 0)
                payload = json.loads(out.getvalue())
                self.assertEqual(payload["object"], "list")
                self.assertEqual(payload["data"][0]["object"], "storage_target")
                self.assertEqual(payload["data"][0]["name"], "primary")
            finally:
                os.chdir(cwd)

    async def test_add_binds_current_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                init(name=None, backend=None)
                target = str(Path(tmp) / "remote.trunk")
                rc = await storage("add", "primary", target)
                self.assertEqual(rc, 0)
                self.assertEqual(Repository.find().backend_url(), target)
                self.assertEqual(Repository.find().primary_storage_name(), "primary")
            finally:
                os.chdir(cwd)

    async def test_add_base_url_resolves_to_repo_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                init(name="app", backend=None)
                base = str(Path(tmp) / "remote")
                out = io.StringIO()
                with redirect_stdout(out):
                    rc = await storage("add", "prod", base)
                expected = f"{base}/trunks/app.trunk"
                self.assertEqual(rc, 0)
                self.assertEqual(Repository.find().backend_url(), expected)
                self.assertEqual(Repository.find().primary_storage_name(), "prod")
                self.assertIn(expected, out.getvalue())
            finally:
                os.chdir(cwd)

    async def test_one_backend_root_can_hold_multiple_repos(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote"
            cwd = os.getcwd()
            try:
                for repo_name in ("app-one", "app-two"):
                    worktree = root / repo_name
                    worktree.mkdir()
                    os.chdir(worktree)
                    init(name=repo_name, backend=None)
                    out = io.StringIO()
                    with redirect_stdout(out):
                        rc = await storage("add", "primary", str(remote))
                    expected = f"{remote}/trunks/{repo_name}.trunk"
                    self.assertEqual(rc, 0)
                    self.assertEqual(Repository.find().backend_url(), expected)
                    self.assertIn(expected, out.getvalue())

                self.assertTrue((remote / "trunks" / "app-one.trunk").exists())
                self.assertTrue((remote / "trunks" / "app-two.trunk").exists())
            finally:
                os.chdir(cwd)

    async def test_add_refuses_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                init(name=None, backend=None)
                err = io.StringIO()
                with redirect_stderr(err):
                    rc = await storage("add", "primary", "memory://")
                self.assertEqual(rc, 1)
                self.assertIn("ephemeral", err.getvalue())
            finally:
                os.chdir(cwd)

    async def test_add_refuses_malformed_backend_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                init(name=None, backend=None)
                err = io.StringIO()
                with redirect_stderr(err):
                    rc = await storage("add", "primary", "s3:/missing-slash")
                self.assertEqual(rc, 1)
                self.assertIn("malformed backend URL", err.getvalue())
            finally:
                os.chdir(cwd)

    async def test_add_missing_url_returns_usage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                init(name=None, backend=None)
                err = io.StringIO()
                with redirect_stderr(err):
                    rc = await storage("add", None, None)
                self.assertEqual(rc, 1)
                self.assertIn("trunks storage add", err.getvalue())
            finally:
                os.chdir(cwd)

    async def test_remove_disconnects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                init(name=None, backend=None)
                target = str(Path(tmp) / "remote.trunk")
                await storage("add", "primary", target)
                self.assertEqual(Repository.find().backend_url(), target)

                rc = await storage("remove", "primary")
                self.assertEqual(rc, 0)
                self.assertFalse(Repository.find().backend_url())
            finally:
                os.chdir(cwd)

    async def test_add_validation_performs_real_backend_io(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = Local(Path(tmp) / "repo.trunk")
            await _validate_storage_roundtrip(backend)
            self.assertTrue(any((Path(tmp) / "repo.trunk" / "objects").rglob("*")))
            self.assertTrue(any((Path(tmp) / "repo.trunk" / "journals").glob("*.txn")))

    async def test_add_mirror_validates_and_stores_mirror(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                init(name="app", backend=None)
                base = str(Path(tmp) / "mirror")
                out = io.StringIO()
                with redirect_stdout(out):
                    rc = await storage("add", "backup", base, mirror=True)
                expected = f"{base}/trunks/app.trunk"
                self.assertEqual(rc, 0)
                self.assertEqual(Repository.find().mirror_urls(), [expected])
                self.assertEqual(Repository.find().mirror_targets(), [("backup", expected)])
                self.assertIn(expected, out.getvalue())
            finally:
                os.chdir(cwd)

    async def test_flag_based_storage_add_persists_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                init(name="app", backend=None)
                remote = str(Path(tmp) / "remote")
                out = io.StringIO()
                with redirect_stdout(out):
                    rc = await dispatch(["storage", "add", "--name", "primary", "--backend", "local", "--path", remote])
                self.assertEqual(rc, 0)
                repo = Repository.find()
                profile = repo.storage_profile("primary")
                self.assertIsNotNone(profile)
                assert profile is not None
                self.assertEqual(profile.backend, "local")
                self.assertEqual(profile.settings["path"], remote)
                self.assertEqual(repo.backend_url(), f"local://{remote}/trunks/app.trunk")
                self.assertIn("local://", out.getvalue())
            finally:
                os.chdir(cwd)

    async def test_storage_wizard_configures_local_storage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                init(name="app", backend=None)
                remote = str(Path(tmp) / "remote")
                answers = iter(["", "", "local", remote])
                out = io.StringIO()
                with patch("builtins.input", side_effect=lambda _prompt: next(answers)), redirect_stdout(out):
                    rc = await dispatch(["storage", "wizard"])
                self.assertEqual(rc, 0)
                repo = Repository.find()
                profile = repo.storage_profile("primary")
                self.assertIsNotNone(profile)
                assert profile is not None
                self.assertEqual(profile.backend, "local")
                self.assertEqual(profile.settings["path"], remote)
                self.assertEqual(repo.backend_url(), f"local://{remote}/trunks/app.trunk")
                self.assertIn("Trunks storage wizard", out.getvalue())
            finally:
                os.chdir(cwd)

    def test_local_repository_database_is_owner_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repository.init(tmp, name="app", backend=None)
            mode = stat.S_IMODE(repo.path.stat().st_mode)
            self.assertEqual(mode, 0o600)
            self.assertEqual(stat.S_IMODE(repo.path.parent.stat().st_mode), 0o700)

    def test_storage_args_persist_explicit_credentials_locally(self) -> None:
        args = Namespace(
            values=[],
            storage_name="primary",
            storage_backend="s3",
            mirror=False,
            bucket="company",
            region="us-east-1",
            endpoint="http://127.0.0.1:9100",
            prefix=None,
            account_id=None,
            path=None,
            host=None,
            port=None,
            user=None,
            container=None,
            account_name=None,
            project=None,
            password_env=None,
            ssh_key=None,
            key_passphrase=None,
            service_account_file=None,
            access_key="key",
            secret_key="secret",
            session_token=None,
            password=None,
            account_key=None,
            sas_token=None,
        )
        profile, validation_profile = _storage_profiles_from_args(args)
        self.assertEqual(profile.credentials["access_key"], "key")
        self.assertEqual(profile.credentials["secret_key"], "secret")
        self.assertEqual(validation_profile.credentials, profile.credentials)

    async def test_storage_show_masks_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                init(name="app", backend=None)
                repo = Repository.find()
                repo.set_storage_profile(Storage(
                    name="primary",
                    backend="s3",
                    role="primary",
                    settings={"bucket": "company", "prefix": "trunks/{repo}.trunk"},
                    credentials={"access_key": "public-ish", "secret_key": "secret"},
                ))
                out = io.StringIO()
                with redirect_stdout(out):
                    rc = await storage("show", "primary")
                self.assertEqual(rc, 0)
                self.assertIn("secret_key  ****", out.getvalue())
                self.assertNotIn("secret\n", out.getvalue())
            finally:
                os.chdir(cwd)

    async def test_postgres_dsn_flag_is_not_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                init(name="app", backend=None)
                err = io.StringIO()
                with redirect_stderr(err):
                    rc = await dispatch([
                        "storage",
                        "add",
                        "--name",
                        "primary",
                        "--backend",
                        "postgres",
                        "--dsn",
                        "postgres://user:secret@localhost/db",
                    ])
                self.assertEqual(rc, 1)
                self.assertIn("--dsn is validation-only", err.getvalue())
                self.assertIsNone(Repository.find().storage_profile("primary"))
            finally:
                os.chdir(cwd)

    def test_backend_from_storage_uses_explicit_s3_credentials(self) -> None:
        profile = Storage(
            name="primary",
            backend="s3",
            role="primary",
            settings={"bucket": "company", "prefix": "trunks/{repo}.trunk", "region": "us-east-1"},
            credentials={"access_key": "key", "secret_key": "secret"},
        )
        backend = backend_from_storage(profile, "app")
        self.assertIsNotNone(backend)
        assert backend is not None
        self.assertEqual(getattr(backend, "bucket"), "company")
        self.assertEqual(getattr(backend, "prefix"), "trunks/app.trunk")
        self.assertEqual(getattr(backend, "credentials").access_key, "key")

    def test_missing_s3_credentials_explain_setup_path(self) -> None:
        profile = Storage(
            name="primary",
            backend="s3",
            role="primary",
            settings={"bucket": "company", "prefix": "trunks/{repo}.trunk"},
        )
        old_env = os.environ.copy()
        try:
            for key in list(os.environ):
                if key.startswith("AWS_") or key.startswith("TRUNKS_STORAGE_PRIMARY_"):
                    os.environ.pop(key, None)
            with self.assertRaisesRegex(BackendUnavailable, "trunks storage add --name primary"):
                backend_from_storage(profile, "app")
        finally:
            os.environ.clear()
            os.environ.update(old_env)

    def test_r2_profile_uses_account_id_endpoint(self) -> None:
        profile = Storage(
            name="primary",
            backend="r2",
            role="primary",
            settings={"bucket": "company", "account_id": "acct123"},
            credentials={"access_key": "key", "secret_key": "secret"},
        )
        backend = backend_from_storage(profile, "app")
        self.assertIsNotNone(backend)
        assert backend is not None
        self.assertEqual(getattr(backend, "endpoint"), "https://acct123.r2.cloudflarestorage.com")

    def test_public_storage_config_never_contains_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                init(name="app", backend=None)
                repo = Repository.find()
                repo.set_storage_profile(Storage(
                    name="primary",
                    backend="s3",
                    role="primary",
                    settings={"bucket": "company"},
                    credentials={"access_key": "key", "secret_key": "secret"},
                ))
                config = repo.storage_config()
                encoded = str(config)
                self.assertIn("s3://company/trunks/app.trunk", encoded)
                self.assertNotIn("secret", encoded)
                self.assertNotIn("access_key", encoded)
            finally:
                os.chdir(cwd)

    def test_applying_public_storage_config_does_not_downgrade_local_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                init(name="app", backend=None)
                repo = Repository.find()
                repo.set_storage_profile(Storage(
                    name="primary",
                    backend="s3",
                    role="primary",
                    settings={
                        "bucket": "company",
                        "prefix": "trunks/{repo}.trunk",
                        "endpoint": "http://127.0.0.1:9100",
                        "region": "us-east-1",
                    },
                ))

                public_config = repo.storage_config()
                repo.apply_storage_config(public_config)

                profile = repo.storage_profile("primary")
                self.assertIsNotNone(profile)
                assert profile is not None
                self.assertEqual(profile.backend, "s3")
                self.assertEqual(profile.settings["endpoint"], "http://127.0.0.1:9100")
                self.assertEqual(profile.settings["bucket"], "company")
            finally:
                os.chdir(cwd)

    def test_public_storage_config_teaches_new_repo_non_secret_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                init(name="app", backend=None)
                source = Repository.find()
                source.set_storage_profile(Storage(
                    name="primary",
                    backend="s3",
                    role="primary",
                    settings={
                        "bucket": "company",
                        "prefix": "trunks/{repo}.trunk",
                        "endpoint": "http://127.0.0.1:9100",
                        "region": "us-east-1",
                    },
                    credentials={"access_key": "key", "secret_key": "secret"},
                ))
                public_config = source.storage_config()
                encoded = str(public_config)
                self.assertIn("http://127.0.0.1:9100", encoded)
                self.assertNotIn("secret", encoded)
                self.assertNotIn("access_key", encoded)

                other_root = Path(tmp) / "other"
                other_root.mkdir()
                other = Repository.init(other_root, name="app", backend=None)
                other.apply_storage_config(public_config)

                profile = other.storage_profile("primary")
                self.assertIsNotNone(profile)
                assert profile is not None
                self.assertEqual(profile.backend, "s3")
                self.assertEqual(profile.settings["endpoint"], "http://127.0.0.1:9100")
                self.assertEqual(profile.credentials, {})
            finally:
                os.chdir(cwd)

    async def test_ping_validates_named_primary_and_mirrors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                init(name="app", backend=None)
                primary = str(Path(tmp) / "primary")
                mirror = str(Path(tmp) / "mirror")
                await storage("add", "prod", primary)
                await storage("add", "backup", mirror, mirror=True)

                out = io.StringIO()
                with redirect_stdout(out):
                    rc = await storage("ping", "all")

                self.assertEqual(rc, 0)
                self.assertIn("prod", out.getvalue())
                self.assertIn("backup", out.getvalue())

                out = io.StringIO()
                with redirect_stdout(out):
                    rc = await storage("ping", "backup")
                self.assertEqual(rc, 0)
                self.assertIn("backup", out.getvalue())

                err = io.StringIO()
                with redirect_stderr(err):
                    rc = await storage("ping", "typo")
                self.assertEqual(rc, 1)
                self.assertIn("unknown storage target typo", err.getvalue())
            finally:
                os.chdir(cwd)

    async def test_s3_read_ref_preserves_backend_errors(self) -> None:
        backend = S3(
            bucket="company",
            credentials=S3Credentials("key", "secret"),
            region="us-east-1",
        )

        async def fail_request(*args: object, **kwargs: object) -> bytes:
            raise BackendUnavailable("403 Forbidden")

        backend._request = fail_request  # type: ignore[method-assign]
        with self.assertRaisesRegex(BackendUnavailable, "403 Forbidden"):
            await backend.read_ref("refs/heads/main")


if __name__ == "__main__":
    unittest.main()
