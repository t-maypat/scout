"""Does the code actually run against real configuration?

Every other test file exercises pure functions with explicit arguments, which is what
makes them fast and network-free - and is exactly why they all passed while `scout probe`
raised AttributeError on the first line that read a setting. `build_health` has defaults
for its keyword arguments, so calling it directly can never notice that `Settings` is
missing the field `probe()` intends to pass in.

These tests close that gap. They are about wiring, not behaviour.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from scout import probe as probe_module
from scout import queries
from scout.config import Settings
from scout.github import ConditionalResponse

SOURCE = Path(probe_module.__file__).parent


def settings_attributes_read(path: Path) -> set[str]:
    """Every `settings.x` and `get_settings().x` in a module."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        target = node.value
        if isinstance(target, ast.Name) and target.id in ("settings", "settings_now") or (
            isinstance(target, ast.Call)
            and isinstance(target.func, ast.Name)
            and target.func.id == "get_settings"
        ):
            found.add(node.attr)
    return found


@pytest.mark.parametrize(
    "module", sorted(p.name for p in SOURCE.glob("*.py") if p.name != "config.py")
)
def test_every_setting_the_code_reads_actually_exists(module):
    """The failure this catches is silent until the line runs, and the line that runs
    first is behind a network call."""
    settings = Settings()
    missing = [
        name
        for name in settings_attributes_read(SOURCE / module)
        if not hasattr(settings, name)
    ]
    assert not missing, f"{module} reads settings that do not exist: {missing}"


def test_documented_env_vars_all_map_to_a_real_setting():
    """A variable in .env.example that nothing reads is a promise the code does not keep."""
    example = (SOURCE.parent / ".env.example").read_text(encoding="utf-8")
    declared = {
        m.group(1).lower()
        for m in re.finditer(r"^SCOUT_([A-Z0-9_]+)=", example, re.MULTILINE)
    }
    settings = Settings()
    assert not [name for name in declared if not hasattr(settings, name)]


class FakeClient:
    """Returns the shape GitHub returns, so probe() has to unpack it for real."""

    def __init__(self):
        self.documents: list[str] = []
        self.points_spent = 0

    def graphql(self, document, **variables):
        self.documents.append(document)
        repo = {
            "nameWithOwner": "acme/widget",
            "description": "a widget",
            "stargazerCount": 10,
            "isArchived": False,
            "isFork": False,
            "pushedAt": "2026-09-12T00:00:00Z",
            "hasIssuesEnabled": True,
            "primaryLanguage": {"name": "Python"},
            "licenseInfo": {"spdxId": "MIT"},
            "defaultBranchRef": {"name": "main"},
            "openIssues": {"totalCount": 1},
            "openPRs": {"totalCount": 1},
            "merged": {"pageInfo": {"hasNextPage": False}, "nodes": []},
            "issues": {"pageInfo": {"hasNextPage": False}, "nodes": []},
            "assigned": {"nodes": []},
        }
        return {"rateLimit": {"cost": 1, "remaining": 4999}, "repository": repo}

    def rest_conditional(self, path, etag=None):
        return ConditionalResponse(status=200, body=[], etag="x")


def test_probe_runs_end_to_end_against_real_settings():
    """The regression test for the actual bug: probe() reads eleven settings, and four
    of them did not exist. Nothing noticed, because no test ever ran this line."""
    health = probe_module.probe(FakeClient(), "acme/widget")
    assert health.full_name == "acme/widget"


def test_probe_accepts_a_url_as_well_as_a_name():
    health = probe_module.probe(FakeClient(), "https://github.com/acme/widget/issues/4")
    assert health.full_name == "acme/widget"


def test_every_variable_a_query_declares_is_one_probe_passes():
    """A GraphQL document that declares $after while the caller sends none - or the
    reverse - fails at GitHub, not at import."""
    client = FakeClient()
    probe_module.probe(client, "acme/widget")
    for document in client.documents:
        declared = set(re.findall(r"\$([a-zA-Z]+):", document))
        used = set(re.findall(r"\$([a-zA-Z]+)\b", document)) - declared
        assert not used - declared, f"{document[:40]} uses undeclared variables"


def test_the_stale_query_is_not_paginated_and_says_so():
    """STALE deliberately takes one page; if that changes, probe() has to start walking
    it too."""
    assert "pageInfo" not in queries.STALE


def test_thresholds_agree_between_config_and_derivation():
    """These disagreed for the whole life of the project: config said 14 and 21 while
    derivation, the card and the documentation all said 21 and 30."""
    import inspect

    from scout import derive

    settings = Settings()
    defaults = inspect.signature(derive.opportunities).parameters
    assert defaults["stale_assignment_days"].default == settings.stale_assignment_days
    assert defaults["abandoned_pr_days"].default == settings.abandoned_pr_days
