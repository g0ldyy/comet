import asyncio
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote

import aiohttp
from loguru import logger

from comet.utils.http_client import http_client_manager

GITHUB_API_TIMEOUT = 10
GITHUB_REPO = "g0ldyy/comet"
_GITHUB_COMMIT_SHA = re.compile(r"[0-9a-f]{40}")


@dataclass
class VersionInfo:
    commit_hash: Optional[str] = None
    build_date: Optional[str] = None
    branch: str = "main"
    is_docker: bool = False


@dataclass
class UpdateStatus:
    has_update: bool
    latest_commit_hash: Optional[str] = None
    latest_url: Optional[str] = None
    checked_at: Optional[datetime] = None
    error: Optional[str] = None


class UpdateManager:
    _instance = None
    _version_info: Optional[VersionInfo] = None
    _update_status: Optional[UpdateStatus] = None
    _check_task: Optional[asyncio.Task] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(UpdateManager, cls).__new__(cls)
        return cls._instance

    @classmethod
    def get_version_info(cls) -> VersionInfo:
        if cls._version_info:
            return cls._version_info

        docker_commit = os.getenv("COMET_COMMIT_HASH")
        docker_date = os.getenv("COMET_BUILD_DATE")
        docker_branch = os.getenv("COMET_BRANCH", "main")

        if docker_commit:
            cls._version_info = VersionInfo(
                commit_hash=docker_commit[:7]
                if len(docker_commit) > 7
                else docker_commit,
                build_date=docker_date,
                branch=docker_branch,
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
                    )
                    .decode()
                    .strip()
                )
            except Exception:
                pass

            try:
                build_date = (
                    subprocess.check_output(
                        ["git", "show", "-s", "--format=%cI", "HEAD"],
                        stderr=subprocess.DEVNULL,
                    )
                    .decode()
                    .strip()
                )
            except Exception:
                pass

            try:
                branch = (
                    subprocess.check_output(
                        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                        stderr=subprocess.DEVNULL,
                    )
                    .decode()
                    .strip()
                )
            except Exception:
                pass

            cls._version_info = VersionInfo(
                commit_hash=commit_hash,
                build_date=build_date,
                branch=branch,
                is_docker=False,
            )
        except Exception as e:
            logger.warning(f"Could not determine version info: {e}")
            cls._version_info = VersionInfo()

        return cls._version_info

    @classmethod
    async def check_for_updates(cls) -> UpdateStatus:
        task = cls._check_task
        if task is None or task.done():
            task = asyncio.create_task(cls._fetch_update_status())
            cls._check_task = task

        try:
            return await asyncio.shield(task)
        finally:
            if task.done() and cls._check_task is task:
                cls._check_task = None

    @classmethod
    async def _fetch_update_status(cls) -> UpdateStatus:
        current_info = cls.get_version_info()
        branch = current_info.branch

        try:
            if (
                type(branch) is not str
                or not branch
                or len(branch) > 255
                or any(character.isspace() for character in branch)
            ):
                raise ValueError("current branch is unavailable or invalid")
            timeout = aiohttp.ClientTimeout(total=GITHUB_API_TIMEOUT)
            session = await http_client_manager.get_session()
            branch_path = quote(branch, safe="")
            url = f"https://api.github.com/repos/{GITHUB_REPO}/commits/{branch_path}"
            async with session.get(url, timeout=timeout) as resp:
                if resp.status == 403:
                    raise RuntimeError("GitHub API rate limit exceeded")
                if resp.status != 200:
                    raise RuntimeError(f"GitHub API returned {resp.status}")

                data = await resp.json()
                latest_sha, latest_url, latest_date = cls._validate_latest_commit(data)
                current_sha = current_info.commit_hash
                if type(current_sha) is not str or not re.fullmatch(
                    r"[0-9a-f]{7,40}", current_sha
                ):
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
                    checked_at=datetime.now(timezone.utc),
                )
        except Exception as e:
            logger.warning(f"Failed to check for updates: {e}")
            cls._update_status = UpdateStatus(
                has_update=False,
                error=str(e),
                checked_at=datetime.now(timezone.utc),
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
            parsed_date = datetime.fromisoformat(commit_date.replace("Z", "+00:00"))
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
        latest_date_str: Optional[str], current_date_str: Optional[str]
    ) -> bool:
        if not latest_date_str or not current_date_str:
            raise ValueError("commit dates are unavailable")

        try:
            latest_date = datetime.fromisoformat(latest_date_str.replace("Z", "+00:00"))
            current_date = datetime.fromisoformat(
                current_date_str.replace("Z", "+00:00")
            )
        except ValueError as error:
            raise ValueError("commit dates are invalid") from error
        if latest_date.tzinfo is None or current_date.tzinfo is None:
            raise ValueError("commit dates must include a timezone")
        return latest_date > current_date
