"""Keeps the main README honest.

The README is the project's public description, so these tests fail when it
drifts from the repository: broken links or anchors, UI labels that no longer
exist, endpoints that are not real routes, and counts (skills, tools,
sub-agents, eval cases) that no longer match the code.
"""

import asyncio
import json
import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]          # repository root
APP_DIR = ROOT / "memoryweaver"
README = (ROOT / "README.md").read_text()


def _relative_targets(markdown: str):
    for target in re.findall(r"\]\(([^)\s]+)\)", markdown):
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        yield target


def _slug(heading: str) -> str:
    # GitHub's anchor rule: lowercase, drop punctuation, spaces -> hyphens
    return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")


def test_every_relative_link_and_image_resolves_from_the_repo_root():
    missing = []
    for target in _relative_targets(README):
        path = target.split("#")[0]
        if path and not (ROOT / path).exists():
            missing.append(target)
    assert not missing, f"README links to paths that do not exist: {missing}"


def test_every_anchor_link_matches_a_heading():
    headings = {_slug(h) for h in re.findall(r"^#{1,6}\s+(.+?)\s*$", README, flags=re.M)}
    broken = [t for t in _relative_targets(README) if t.startswith("#") and t[1:] not in headings]
    assert not broken, f"README anchors with no matching heading: {broken}"


def test_app_folder_readme_only_points_at_the_main_readme():
    text = (APP_DIR / "README.md").read_text()
    assert "../README.md" in text
    for target in _relative_targets(text):
        assert (APP_DIR / target.split("#")[0]).exists(), target


@pytest.mark.parametrize("label", [
    "Write & Build Storybook Now",
    "Proceed to create Trip Highlights",
    "Find Your Photos",
    "Add to Album",
    "Album Photos",
])
def test_ui_labels_named_in_the_readme_exist_in_the_hub(label):
    assert label in README, f"README no longer mentions {label!r}; update this test"
    assert label in (APP_DIR / "frontend" / "upload.html").read_text(), f"Curator Hub has no {label!r}"


def test_endpoints_named_in_the_readme_are_real_routes():
    from app import fast_api_app

    routes = {r.path for r in fast_api_app.app.routes if hasattr(r, "path")}
    named = set()
    prose = re.sub(r"```.*?```", "", README, flags=re.S)  # fenced blocks would break inline-span pairing
    for span in re.findall(r"`([^`]+)`", prose):
        m = re.fullmatch(r"(?:(?:POST|GET) )?(/[A-Za-z0-9_\-/]+)", span.strip())
        if m:
            named.add(m.group(1))
    assert {"/upload", "/generate", "/generate-narrative", "/media"} <= named, "README should name the core endpoints"
    unknown = sorted(p for p in named if p not in routes and not any(r.startswith(p + "/{") for r in routes))
    assert not unknown, f"README names endpoints that are not routes: {unknown}"


def test_agent_counts_in_the_readme_match_the_code():
    from google.adk.a2a.utils.agent_card_builder import AgentCardBuilder
    from a2a.types import AgentCapabilities
    from app.agent import app as adk_app

    root = adk_app.root_agent
    tools, subs = re.search(r"(\d+) tools and (\d+) sub-agents", README).groups()
    assert (int(tools), int(subs)) == (len(root.tools), len(root.sub_agents))

    card = asyncio.run(AgentCardBuilder(
        agent=root, capabilities=AgentCapabilities(streaming=True),
        rpc_url="http://x/a2a/app", agent_version="0",
    ).build())
    claimed = int(re.search(r"exposes (\d+) skills", README).group(1))
    assert claimed == len(card.skills)


def test_eval_case_count_in_the_readme_matches_the_dataset():
    dataset = json.loads((APP_DIR / "tests" / "eval" / "datasets" / "basic-dataset.json").read_text())
    claimed = int(re.search(r"\((\d+) cases\)", README).group(1))
    assert claimed == len(dataset["eval_cases"])
    # the README points at the eval config it describes
    assert (APP_DIR / "tests" / "eval" / "eval_config.yaml").exists()


def test_readme_no_longer_makes_the_claims_the_code_cannot_support():
    lowered = README.lower()
    for stale in ("local clip embeddings", "cross-session profiles", "image maps", "progressive disclosure"):
        assert stale not in lowered, f"README claims {stale!r}, which the code does not do"
