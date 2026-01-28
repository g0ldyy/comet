import os
import subprocess
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import aiohttp
from loguru import logger

GITHUB_API_TIMEOUT = 10
GITHUB_REPO = "g0ldyy/comet"


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

        if docker_commit:
            cls._version_info = VersionInfo(
                commit_hash=docker_commit[:7]
                if len(docker_commit) > 7
                else docker_commit,
                build_date=docker_date,
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
        current_info = cls.get_version_info()
        branch = current_info.branch

        try:
            timeout = aiohttp.ClientTimeout(total=GITHUB_API_TIMEOUT)

            async with aiohttp.ClientSession(timeout=timeout) as session:
                url = f"https://api.github.com/repos/{GITHUB_REPO}/commits/{branch}"
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        latest_sha = data.get("sha", "")[:7]
                        current_sha = current_info.commit_hash or ""

                        if current_sha == latest_sha:
                            cls._update_status = UpdateStatus(
                                has_update=False,
                                latest_commit_hash=latest_sha,
                                latest_url=data.get("html_url"),
                                checked_at=datetime.now(),
                            )
                        else:
                            has_update = cls._compare_dates(
                                data.get("commit", {}).get("committer", {}).get("date"),
                                current_info.build_date,
                            )
                            cls._update_status = UpdateStatus(
                                has_update=has_update,
                                latest_commit_hash=latest_sha,
                                latest_url=data.get("html_url"),
                                checked_at=datetime.now(),
                            )
                    elif resp.status == 403:
                        raise Exception("GitHub API rate limit exceeded")
                    else:
                        raise Exception(f"GitHub API returned {resp.status}")
        except Exception as e:
            logger.warning(f"Failed to check for updates: {e}")
            cls._update_status = UpdateStatus(
                has_update=False,
                error=str(e),
                checked_at=datetime.now(),
            )

        return cls._update_status

    @staticmethod
    def _compare_dates(
        latest_date_str: Optional[str], current_date_str: Optional[str]
    ) -> bool:
        if not latest_date_str or not current_date_str:
            return False

        try:
            latest_date = datetime.fromisoformat(latest_date_str.replace("Z", "+00:00"))
            current_date = datetime.fromisoformat(
                current_date_str.replace("Z", "+00:00")
            )
            return latest_date > current_date
        except Exception as e:
            logger.warning(f"Error comparing dates for update check: {e}")
            return False
