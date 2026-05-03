from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from trunks.actions.oidc import mint_oidc_token, verify_oidc_token
from trunks.actions.run import enqueue_command
from trunks.actions.storage import claim_run
from trunks.repository import Repository


class OIDCBridgeTests(unittest.TestCase):
    def test_oidc_token_requires_signing_key_and_contains_run_claims(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = Repository.init(cwd=root, name="demo")
            run = enqueue_command(repo, "true", commit="worktree")
            claim_run(
                repo,
                run.id,
                executor="executor",
                provider="local",
                spec_key=run.spec.key,
                region="local",
                requested_region=None,
                now_s=100,
            )

            with self.assertRaises(RuntimeError):
                mint_oidc_token(repo, run=run.id, audience="aws")

            payload = mint_oidc_token(repo, run=run.id, audience="aws", signing_key="secret", now_s=100)
            claims = verify_oidc_token(str(payload["token"]), signing_key="secret", audience="aws", now_s=100)

            self.assertEqual(claims["sub"], f"repo:demo:run:{run.id}")
            self.assertEqual(claims["repository"], "demo")
            self.assertEqual(claims["provider"], "local")
            self.assertEqual(claims["aud"], "aws")

    def test_oidc_token_rejects_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repository.init(cwd=Path(tmp), name="demo")
            run = enqueue_command(repo, "true")
            payload = mint_oidc_token(repo, run=run.id, audience="aws", signing_key="secret", now_s=100)
            token = str(payload["token"])
            tampered = token[:-1] + ("a" if token[-1] != "a" else "b")

            with self.assertRaises(ValueError):
                verify_oidc_token(tampered, signing_key="secret", audience="aws", now_s=100)


if __name__ == "__main__":
    unittest.main()
