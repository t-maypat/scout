"""Configuration. Environment only - no config files for secrets, no absolute paths.

The watchlist itself is a file, deliberately: it is curated data you edit by hand and
commit, not configuration.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SCOUT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    github_token: str = ""
    github_login: str = ""

    # Presentation and scoring
    display_tz: str = "Asia/Kolkata"
    # Local hours you can actually do this in, as "start-end" on a 24h clock. A repo whose
    # maintainers are asleep through all of these is a repo where every round trip costs a day.
    free_hours_local: str = "20-24"
    languages: str = "Python,Go,TypeScript"

    # Probe windows. Wider windows cost more rate limit and blur recent changes in a
    # project's behaviour; these are the defaults every reported metric is measured over.
    merged_pr_sample: int = 100
    issue_sample: int = 60
    lookback_days: int = 180
    stale_assignment_days: int = 14
    abandoned_pr_days: int = 21
    contest_window_days: int = 90

    # Safety. These exist to make a surprise bill or a throttled token impossible by
    # accident; every one of them refuses loudly rather than degrading quietly.
    enabled: bool = True
    max_poll_repos: int = 25
    rate_limit_floor: int = 500

    # Polling. Observing often and interrupting often are different decisions: poll on
    # this interval to keep the log accurate, but send at digest_hour_local.
    poll_per_page: int = 100
    poll_only_actionable: bool = True
    stale_transition_hours: float = 26.0

    # Notification
    discord_webhook_url: str = ""
    digest_hour_local: int = 20
    digest_max_items: int = 8

    timeout_seconds: float = 30.0
    watchlist_path: str = "./watchlist.toml"
    state_dir: str = "./state"
    events_dir: str = "./events"
    log_level: str = "info"

    @property
    def language_list(self) -> list[str]:
        return [x.strip() for x in self.languages.split(",") if x.strip()]

    @property
    def free_hours(self) -> tuple[int, int]:
        start, _, end = self.free_hours_local.partition("-")
        return int(start), int(end)


@lru_cache
def get_settings() -> Settings:
    return Settings()
