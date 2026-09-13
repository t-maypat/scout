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
    # Pages the prober will walk before giving up on covering the window. One page is
    # one rate-limit point, so the default is cheap and raising it is not expensive.
    # Whether a project is worth your time barely moves week to week, so refresh skips
    # anything probed more recently than this. Polling is the recurring job; judging is
    # not, and re-judging on every run spends budget to confirm what it already knows.
    reprobe_after_days: int = 14
    probe_pages: int = 4
    merged_pr_sample: int = 100
    issue_sample: int = 60
    lookback_days: int = 180
    # 21 and 30 to match derive.py and the documentation. These were 14 and 21 here
    # while everything else said otherwise, and because probe() passes them explicitly
    # the documented numbers were never the ones in use.
    stale_assignment_days: int = 21
    abandoned_pr_days: int = 30
    contest_window_days: int = 90

    # Silence counts as neglect only once a reply was actually due. Without this a fast
    # repository looks negligent purely because most of its recent issues are hours old.
    unanswered_after_hours: float = 72.0

    # Newness. Whether a stranger's first pull request gets merged is the whole question,
    # and it is derived from author history rather than GitHub's authorAssociation, which
    # describes how somebody is associated *now* and cannot be trusted about the past.
    # The burn-in establishes who was already known before anybody is called new.
    newness_burn_in_days: float = 14.0
    newness_min_burn_in_merges: int = 30
    newness_min_scored_merges: int = 40

    # Safety. These exist to make a surprise bill or a throttled token impossible by
    # accident; every one of them refuses loudly rather than degrading quietly.
    enabled: bool = True
    max_poll_repos: int = 25
    rate_limit_floor: int = 500

    # Polling. Observing often and interrupting often are different decisions: poll on
    # this interval to keep the log accurate, but send at digest_hour_local.
    poll_per_page: int = 100
    poll_only_actionable: bool = True
    transition_hours: float = 36.0

    # Notification
    discord_webhook_url: str = ""
    # A webhook created in Discord's UI is not owned by an application, and Discord
    # ignores interactive components from those. Buttons therefore require the bot to
    # post. Set both of these and scout posts as the bot; set neither and it falls back
    # to the webhook, which works fine and simply has no buttons.
    discord_bot_token: str = ""
    discord_channel_id: str = ""

    @property
    def posts_as_bot(self) -> bool:
        return bool(self.discord_bot_token and self.discord_channel_id)
    digest_hour_local: int = 20
    digest_max_items: int = 8

    # Per request. Bounds one call, not the operation.
    timeout_seconds: float = 30.0
    # Whole-operation ceilings. A probe is up to nine requests and a poll one per repo,
    # so without these a single retry storm can block a caller for the better part of an
    # hour. Reached from the dashboard these become a 504 with a readable message.
    probe_deadline_seconds: float = 90.0
    poll_deadline_seconds: float = 180.0
    watchlist_path: str = "./watchlist.toml"
    state_dir: str = "./state"
    events_dir: str = "./events"
    log_level: str = "info"

    @property
    def token(self) -> str:
        """The token, without whatever whitespace an editor left on the line.

        A trailing newline or space rides along into the Authorization header and GitHub
        answers 401, which reads exactly like a wrong token and is not one.
        """
        return self.github_token.strip().strip("\"'")

    @property
    def token_kind(self) -> str:
        token = self.token
        if token.startswith("github_pat_"):
            return "fine-grained"
        if token.startswith(("ghp_", "gho_", "ghu_", "ghs_", "ghr_")):
            return "classic"
        return "unrecognised" if token else "missing"

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
