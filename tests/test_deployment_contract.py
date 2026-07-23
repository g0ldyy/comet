import re
import unittest
from pathlib import Path


class DeploymentContractTests(unittest.TestCase):
    def test_both_launchers_accept_forwarded_headers_without_setup(self):
        launcher = Path("comet/main.py").read_text(encoding="utf-8")

        self.assertIn('forwarded_allow_ips="*"', launcher)
        self.assertIn('"forwarded_allow_ips": "*"', launcher)
        self.assertNotIn("TRUSTED_PROXY_IPS", launcher)

    def test_runtime_image_drops_root_before_startup(self):
        dockerfile = Path("Dockerfile").read_text(encoding="utf-8")

        self.assertIn("adduser -S -D -H -G comet comet", dockerfile)
        self.assertIn("COPY --from=builder --chown=comet:comet", dockerfile)
        self.assertLess(dockerfile.index("USER comet"), dockerfile.index("ENTRYPOINT"))
        self.assertIn("PYTHONDONTWRITEBYTECODE=1", dockerfile)
        self.assertIn("${FASTAPI_PORT:-8000}/health", dockerfile)

    def test_compose_limits_writable_and_process_privileges(self):
        compose = Path("deployment/docker-compose.yml").read_text(encoding="utf-8")

        self.assertIn("read_only: true", compose)
        self.assertIn("no-new-privileges:true", compose)
        self.assertRegex(compose, r"cap_drop:\s+- ALL")
        self.assertIn("comet_data:/app/data", compose)
        self.assertIn("/tmp:size=64m,mode=1777", compose)
        self.assertIn("${FASTAPI_PORT:-8000}:${FASTAPI_PORT:-8000}", compose)
        self.assertIn("$${FASTAPI_PORT:-8000}/health", compose)
        self.assertIn("${POSTGRES_PASSWORD:?POSTGRES_PASSWORD must be set}", compose)
        self.assertNotIn("comet:comet@postgres", compose)

    def test_proxy_streams_without_body_cap_or_buffering(self):
        nginx = Path("deployment/nginx.conf").read_text(encoding="utf-8")

        self.assertIn("proxy_buffering off;", nginx)
        self.assertIn("proxy_request_buffering off;", nginx)
        self.assertNotIn("client_max_body_size", nginx)
        self.assertNotIn("proxy_max_temp_file_size", nginx)

    def test_remote_actions_are_pinned_to_full_commits(self):
        workflow_paths = sorted(Path(".github/workflows").glob("*.yml"))
        remote_use = re.compile(r"^\s*-?\s*uses:\s*([^\s#]+)", re.MULTILINE)

        seen = 0
        for path in workflow_paths:
            source = path.read_text(encoding="utf-8")
            for target in remote_use.findall(source):
                if target.startswith("./"):
                    continue
                seen += 1
                with self.subTest(path=path, target=target):
                    self.assertRegex(target, r"^[^@\s]+@[0-9a-f]{40}$")
        self.assertGreater(seen, 0)

    def test_cometnet_docker_examples_match_the_runtime_image(self):
        docs = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted(Path("docs/cometnet").glob("*.md"))
        )

        self.assertNotIn(
            'entrypoint: ["uv", "run", "python", "-m", "comet.cometnet.standalone"]',
            docs,
        )
        self.assertNotIn("comet:comet@postgres", docs)
        self.assertNotIn("POSTGRES_PASSWORD: comet", docs)
        self.assertIn(
            "POSTGRES_PASSWORD:?POSTGRES_PASSWORD must be set",
            docs,
        )

    def test_documentation_local_links_resolve(self):
        link_pattern = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")

        for path in sorted(Path("docs").rglob("*.md")):
            if "optimization" in path.parts:
                continue
            source = path.read_text(encoding="utf-8")
            for raw_target in link_pattern.findall(source):
                target = raw_target.strip().split(maxsplit=1)[0].strip("<>")
                if not target or target.startswith(
                    ("http://", "https://", "mailto:", "#")
                ):
                    continue
                local_target = target.split("#", 1)[0]
                if not local_target:
                    continue
                resolved = (path.parent / local_target).resolve()
                with self.subTest(path=path, target=target):
                    self.assertTrue(resolved.exists())


if __name__ == "__main__":
    unittest.main()
