import re
import unittest
from pathlib import Path


class DeploymentContractTests(unittest.TestCase):
    def test_runtime_image_preserves_root_volume_compatibility(self):
        dockerfile = Path("Dockerfile").read_text(encoding="utf-8")

        self.assertNotRegex(dockerfile, r"(?m)^USER\s+comet\s*$")
        self.assertIn("WORKDIR /app", dockerfile)
        self.assertIn("PYTHONDONTWRITEBYTECODE=1", dockerfile)
        self.assertIn("CMD python -m comet.healthcheck", dockerfile)

    def test_docker_context_excludes_native_build_outputs(self):
        dockerignore = Path(".dockerignore").read_text(encoding="utf-8")

        self.assertIn("native/usenet-engine/target", dockerignore.splitlines())
        self.assertIn(
            "native/usenet-engine/fuzz/target",
            dockerignore.splitlines(),
        )

    def test_compose_limits_writable_and_process_privileges(self):
        compose = Path("deployment/docker-compose.yml").read_text(encoding="utf-8")

        self.assertIn("read_only: true", compose)
        self.assertIn("no-new-privileges:true", compose)
        self.assertRegex(compose, r"cap_drop:\s+- ALL")
        self.assertIn("comet_data:/app/data", compose)
        self.assertIn("/tmp:size=64m,mode=1777", compose)
        self.assertIn("/run/comet/usenet:size=16m,mode=0700", compose)
        self.assertIn("${FASTAPI_PORT:-8000}:${FASTAPI_PORT:-8000}", compose)
        self.assertIn("${POSTGRES_PASSWORD:?POSTGRES_PASSWORD must be set}", compose)
        self.assertNotIn("comet:comet@postgres", compose)

    def test_proxy_streams_without_body_cap_or_buffering(self):
        nginx = Path("deployment/nginx.conf").read_text(encoding="utf-8")

        self.assertIn("access_log off;", nginx)
        self.assertIn("proxy_buffering off;", nginx)
        self.assertIn("proxy_request_buffering off;", nginx)
        self.assertNotIn("client_max_body_size", nginx)
        self.assertNotIn("proxy_max_temp_file_size", nginx)

    def test_proxy_compresses_text_payloads_but_never_the_byte_path(self):
        """Range-served media must stay uncompressed so Accept-Ranges survives."""
        nginx = Path("deployment/nginx.conf").read_text(encoding="utf-8")

        self.assertIn("gzip on;", nginx)
        self.assertIn("gzip_vary on;", nginx)
        types_line = next(
            line for line in nginx.splitlines() if line.strip().startswith("gzip_types")
        )
        for compressible in ("application/json", "application/xml"):
            self.assertIn(compressible, types_line)
        for streamed in ("application/octet-stream", "video/", "*"):
            self.assertNotIn(streamed, types_line)

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
