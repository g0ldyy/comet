import asyncio
import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import quote

import aiohttp

from comet.core.build_metadata import (
    normalize_branch,
    normalize_build_date,
    normalize_commit,
)
from comet.core.models import settings
from comet.core.provider_json import (
    ProviderJsonError,
    is_success_status,
    read_provider_json,
)
from comet.observability.context import create_detached_task
from comet.utils.http_client import http_client_manager

GITHUB_API_TIMEOUT = 10
GITHUB_REPO = "g0ldyy/comet"
GITHUB_DEVELOPMENT_BRANCH = "development"
_GITHUB_RESPONSE_LIMIT = 64 * 1024
_GITHUB_COMMIT_SHA = re.compile(r"[0-9a-f]{40}")


@dataclass
class VersionInfo:
    commit_hash: str | None = None
    build_date: str | None = None
    branch: str = "main"
    is_docker: bool = False


@dataclass
class UpdateStatus:
    has_update: bool
    latest_commit_hash: str | None = None
    latest_url: str | None = None
    checked_at: datetime | None = None
    error: str | None = None


class UpdateCheckError(RuntimeError):
    pass


class UpdateManager:
    _instance = None
    _version_info: VersionInfo | None = None
    _update_status: UpdateStatus | None = None
    _check_task: asyncio.Task | None = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    @classmethod
    def get_version_info(cls) -> VersionInfo:
        if cls._version_info:
            return cls._version_info

        docker_commit = settings.COMET_COMMIT_HASH
        docker_date = settings.COMET_BUILD_DATE
        docker_branch = settings.COMET_BRANCH

        if docker_commit:
            cls._version_info = VersionInfo(
                commit_hash=(
                    normalized_commit[:7]
                    if (normalized_commit := normalize_commit(docker_commit))
                    else None
                ),
                build_date=normalize_build_date(docker_date),
                branch=normalize_branch(docker_branch) or "main",
                is_docker=True,
            )
            return cls._version_info

        try:
            commit_hash = None
            build_date = None
            branch = "main"

            try:
                commit_hash = (
                    subprocess.check_output(
                        ["git", "rev-parse", "--short", "HEAD"],
                        stderr=subprocess.DEVNULL,
                        timeout=5,
                    )
                    .decode()
                    .strip()
                )
                commit_hash = normalize_commit(commit_hash)
            except Exception:
                pass

            try:
                build_date = (
                    subprocess.check_output(
                        ["git", "show", "-s", "--format=%cI", "HEAD"],
                        stderr=subprocess.DEVNULL,
                        timeout=5,
                    )
                    .decode()
                    .strip()
                )
                build_date = normalize_build_date(build_date)
            except Exception:
                pass

            try:
                branch = (
                    subprocess.check_output(
                        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                        stderr=subprocess.DEVNULL,
                        timeout=5,
                    )
                    .decode()
                    .strip()
                )
                branch = normalize_branch(branch) or "main"
            except Exception:
                pass

            cls._version_info = VersionInfo(
                commit_hash=commit_hash,
                build_date=build_date,
                branch=branch,
                is_docker=False,
            )
        except Exception:
            cls._version_info = VersionInfo()

        return cls._version_info

    @classmethod
    async def check_for_updates(cls) -> UpdateStatus:
        task = cls._check_task
        if task is None or task.done():
            task = create_detached_task(
                cls._fetch_update_status(),
                name="update-check",
            )
            cls._check_task = task

        try:
            return await asyncio.shield(task)
        finally:
            if task.done() and cls._check_task is task:
                cls._check_task = None

    @classmethod
    async def _fetch_update_status(cls) -> UpdateStatus:
        current_info = cls.get_version_info()
        branch = (
            current_info.branch
            if current_info.branch == "main"
            else GITHUB_DEVELOPMENT_BRANCH
        )

        try:
            if normalize_branch(branch) is None:
                raise ValueError("current branch is unavailable or invalid")
            timeout = aiohttp.ClientTimeout(total=GITHUB_API_TIMEOUT)
            session = await http_client_manager.get_session()
            branch_path = quote(branch, safe="")
            url = f"https://api.github.com/repos/{GITHUB_REPO}/commits/{branch_path}"
            async with session.get(
                url,
                timeout=timeout,
                allow_redirects=False,
                headers={
                    "Accept": "application/vnd.github+json",
                    "Accept-Encoding": "identity",
                },
            ) as resp:
                if resp.status == 403:
                    raise UpdateCheckError("GitHub API rate limit exceeded")
                if not is_success_status(resp.status):
                    raise UpdateCheckError(f"GitHub API returned {resp.status}")

                try:
                    data = await read_provider_json(
                        resp,
                        maximum=_GITHUB_RESPONSE_LIMIT,
                    )
                except ProviderJsonError as exc:
                    raise UpdateCheckError(
                        "GitHub API returned an invalid response"
                    ) from exc
                latest_sha, latest_url, latest_date = cls._validate_latest_commit(data)
                current_sha = current_info.commit_hash
                if normalize_commit(current_sha) is None:
                    raise ValueError("current commit hash is unavailable or invalid")

                short_latest_sha = latest_sha[:7]
                has_update = current_sha != short_latest_sha and cls._compare_dates(
                    latest_date,
                    current_info.build_date,
                )
                cls._update_status = UpdateStatus(
                    has_update=has_update,
                    latest_commit_hash=short_latest_sha,
                    latest_url=latest_url,
                    checked_at=datetime.now(UTC),
                )
        except Exception as exc:
            if isinstance(exc, (UpdateCheckError, ValueError)):
                error = str(exc)
            else:
                error = "GitHub API request failed"
            cls._update_status = UpdateStatus(
                has_update=False,
                error=error,
                checked_at=datetime.now(UTC),
            )

        return cls._update_status

    @staticmethod
    def _validate_latest_commit(data) -> tuple[str, str, str]:
        if type(data) is not dict:
            raise ValueError("GitHub commit response must be an object")

        sha = data.get("sha")
        html_url = data.get("html_url")
        commit = data.get("commit")
        if type(sha) is not str or _GITHUB_COMMIT_SHA.fullmatch(sha) is None:
            raise ValueError("GitHub commit response has an invalid SHA")
        expected_url = f"https://github.com/{GITHUB_REPO}/commit/{sha}"
        if html_url != expected_url:
            raise ValueError("GitHub commit response has an invalid URL")
        if type(commit) is not dict or type(commit.get("committer")) is not dict:
            raise ValueError("GitHub commit response has invalid commit metadata")
        commit_date = commit["committer"].get("date")
        if type(commit_date) is not str:
            raise ValueError("GitHub commit response has an invalid commit date")
        try:
            parsed_date = datetime.fromisoformat(commit_date)
        except ValueError as error:
            raise ValueError(
                "GitHub commit response has an invalid commit date"
            ) from error
        if parsed_date.tzinfo is None:
            raise ValueError(
                "GitHub commit response commit date must include a timezone"
            )

        return sha, html_url, commit_date

    @staticmethod
    def _compare_dates(
        latest_date_str: str | None, current_date_str: str | None
    ) -> bool:
        if not latest_date_str or not current_date_str:
            raise ValueError("commit dates are unavailable")

        try:
            latest_date = datetime.fromisoformat(latest_date_str)
            current_date = datetime.fromisoformat(current_date_str)
        except ValueError as error:
            raise ValueError("commit dates are invalid") from error
        if latest_date.tzinfo is None or current_date.tzinfo is None:
            raise ValueError("commit dates must include a timezone")
        return latest_date > current_date
