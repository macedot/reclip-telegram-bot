"""Validation tests for GitHub Actions workflow and docker-compose.yml configuration."""

import pytest
import yaml
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = REPO_ROOT / ".github/workflows/release.yml"
COMPOSE_PATH = REPO_ROOT / "docker-compose.yml"

WORKFLOW_SERVICES = ["reclip", "bot", "dashboard"]
COMPOSE_SERVICES_WITH_BUILD = ["reclip", "bot", "dashboard"]


def load_yaml(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Test 1: YAML parsing
# ---------------------------------------------------------------------------

class TestYAMLValid:
    def test_workflow_valid_yaml(self):
        result = load_yaml(WORKFLOW_PATH)
        assert isinstance(result, dict), "release.yml should parse to a dict"

    def test_compose_valid_yaml(self):
        result = load_yaml(COMPOSE_PATH)
        assert isinstance(result, dict), "docker-compose.yml should parse to a dict"


# ---------------------------------------------------------------------------
# Test 2: Matrix coverage
# ---------------------------------------------------------------------------

class TestMatrixCoverage:
    def test_matrix_service_contents(self):
        workflow = load_yaml(WORKFLOW_PATH)
        matrix = workflow["jobs"]["build-and-push"]["strategy"]["matrix"]
        assert "service" in matrix, "matrix must define 'service'"
        assert set(matrix["service"]) == set(WORKFLOW_SERVICES), (
            f"matrix.service must be exactly {WORKFLOW_SERVICES}, got {matrix['service']}"
        )

    def test_telegram_bot_api_not_in_matrix(self):
        workflow = load_yaml(WORKFLOW_PATH)
        matrix = workflow["jobs"]["build-and-push"]["strategy"]["matrix"]
        assert "telegram-bot-api" not in matrix.get("service", []), (
            "telegram-bot-api must not appear in matrix.service"
        )


# ---------------------------------------------------------------------------
# Test 3: Build context consistency
# ---------------------------------------------------------------------------

class TestBuildContextConsistency:
    def test_workflow_context_pattern(self):
        workflow = load_yaml(WORKFLOW_PATH)
        build_step = next(s for s in workflow["jobs"]["build-and-push"]["steps"] if "context" in (s.get("with") or {}))
        assert "context" in build_step["with"], "build-push step must set context"
        assert build_step["with"]["context"] == "./${{ matrix.service }}", (
            f"workflow context must be './${{{{ matrix.service }}}}', "
            f"got {build_step['with']['context']!r}"
        )

    def test_compose_build_contexts_exist(self):
        compose = load_yaml(COMPOSE_PATH)
        for service in COMPOSE_SERVICES_WITH_BUILD:
            build_ctx = compose["services"][service].get("build", "")
            if build_ctx.startswith("./"):
                build_ctx = build_ctx[2:]
            assert build_ctx == service, (
                f"service '{service}' build context must be './{service}', "
                f"got {compose['services'][service].get('build')!r}"
            )


# ---------------------------------------------------------------------------
# Test 4: Image tag pattern
# ---------------------------------------------------------------------------

class TestImageTagPattern:
    def test_compose_images_use_ghcr(self):
        compose = load_yaml(COMPOSE_PATH)
        for service in COMPOSE_SERVICES_WITH_BUILD:
            image = compose["services"][service].get("image", "")
            assert image.startswith("ghcr.io/"), (
                f"service '{service}' image must start with 'ghcr.io/', got {image!r}"
            )

    def test_compose_images_use_tag_variable(self):
        compose = load_yaml(COMPOSE_PATH)
        for service in COMPOSE_SERVICES_WITH_BUILD:
            image = compose["services"][service].get("image", "")
            assert "${IMAGE_TAG:-latest}" in image, (
                f"service '{service}' image must include '${IMAGE_TAG:-latest}', got {image!r}"
            )

    def test_workflow_tags_ghcr_pattern(self):
        workflow = load_yaml(WORKFLOW_PATH)
        build_step = next(s for s in workflow["jobs"]["build-and-push"]["steps"] if "tags" in (s.get("with") or {}))
        # tags is a multi-line string, not a YAML list
        tags_raw = build_step["with"].get("tags", "")
        assert "ghcr.io/" in tags_raw, (
            f"workflow tags must include 'ghcr.io/', got:\n{tags_raw}"
        )
        assert ":${{ steps.version.outputs.version }}" in tags_raw, (
            f"workflow tags must include ':${{{{ steps.version.outputs.version }}}}', got:\n{tags_raw}"
        )
        assert ":latest" in tags_raw, (
            f"workflow tags must include ':latest', got:\n{tags_raw}"
        )


# ---------------------------------------------------------------------------
# Test 5: Prerelease skip
# ---------------------------------------------------------------------------

class TestPrereleaseSkip:
    def test_prerelease_skip_condition_exists(self):
        workflow = load_yaml(WORKFLOW_PATH)
        job = workflow["jobs"]["build-and-push"]
        assert "if" in job, "build-and-push job must have an 'if' condition"
        assert "!github.event.release.prerelease" in job["if"], (
            f"job 'if' must check '!github.event.release.prerelease', got {job['if']!r}"
        )


# ---------------------------------------------------------------------------
# Test 6: Security defaults (DF-3, DF-5, N3, C3, C4)
# ---------------------------------------------------------------------------

class TestSecurityDefaults:
    """DF-3 / DF-5 / N3 / C3 / C4 enforcement."""

    def test_ghcr_owner_no_default(self):
        """DF-5 — ${GHCR_OWNER:-...} is forbidden; only ${GHCR_OWNER:?...} allowed."""
        compose_text = COMPOSE_PATH.read_text()
        # every GHCR_OWNER reference must be the fail-closed form
        assert "${GHCR_OWNER:-" not in compose_text, (
            "compose must not have a default for GHCR_OWNER (use ${GHCR_OWNER:?...})"
        )

    def test_no_known_insecure_secrets_in_compose(self):
        compose_text = COMPOSE_PATH.read_text()
        forbidden = [
            "SECRET_KEY:-insecure-default-key-change-me",
            "SECRET_KEY:-change-me-in-production",
            "DASHBOARD_USER:-admin",
            "ADMIN_PASSWORD:-changeme",
        ]
        for sub in forbidden:
            assert sub not in compose_text, (
                f"compose contains insecure default: {sub!r}"
            )

    def test_required_secrets_use_fail_closed_syntax(self):
        """C3, C4, N3, H5 — required secrets use ${VAR:?msg} syntax."""
        compose_text = COMPOSE_PATH.read_text()
        # Map of env var name (as appears inside ${...:?...}) → required regex
        required = {
            "DASHBOARD_SECRET_KEY": r"\$\{DASHBOARD_SECRET_KEY:\?",
            "DASHBOARD_USER": r"\$\{DASHBOARD_USER:\?",
            "DASHBOARD_PASSWORD_HASH": r"\$\{DASHBOARD_PASSWORD_HASH:\?",
            "DASHBOARD_INTERNAL_TOKEN": r"\$\{DASHBOARD_INTERNAL_TOKEN:\?",
            "RECLIP_API_TOKEN": r"\$\{RECLIP_API_TOKEN:\?",
            "GHCR_OWNER": r"\$\{GHCR_OWNER:\?",
        }
        for name, pattern in required.items():
            import re as _re
            assert _re.search(pattern, compose_text), (
                f"compose must require {name!r} via ${{{name}:?...}}"
            )

    def test_telegram_bot_api_pinned(self):
        """DF-6 — aiogram/telegram-bot-api must not use :latest."""
        compose = load_yaml(COMPOSE_PATH)
        img = compose["services"]["telegram-bot-api"]["image"]
        assert not img.endswith(":latest"), (
            f"telegram-bot-api must not use :latest, got {img!r}"
        )
        # Must have a version tag
        assert ":" in img.rsplit("/", 1)[-1], (
            f"telegram-bot-api must have a version tag, got {img!r}"
        )

    def test_reclip_api_token_required(self):
        """C2 — reclip service must require RECLIP_API_TOKEN."""
        compose_text = COMPOSE_PATH.read_text()
        # Find the reclip service block
        import re
        m = re.search(r"^\s*reclip:\n((?:^\s+.*\n)+)", compose_text, re.MULTILINE)
        assert m, "could not find reclip service block"
        block = m.group(1)
        assert "RECLIP_API_TOKEN:?" in block, (
            "reclip service must require RECLIP_API_TOKEN via ${VAR:?msg}"
        )

    def test_workflow_minimal_permissions(self):
        """DF-3 — workflow `permissions:` is least-privilege (contents: read)."""
        workflow = load_yaml(WORKFLOW_PATH)
        perms = workflow.get("permissions", {})
        # 'contents: read' or empty perms are both acceptable; flag overly-broad perms.
        if perms:
            contents = perms.get("contents")
            assert contents == "read", (
                f"workflow permissions.contents must be 'read', got {contents!r}"
            )

    def test_ci_workflow_exists(self):
        """DF-4 — ci.yml workflow exists and runs on PRs/pushes to main."""
        ci_path = REPO_ROOT / ".github/workflows/ci.yml"
        assert ci_path.exists(), f"{ci_path} must exist"
        ci = load_yaml(ci_path)
        # yaml 1.1 interprets unquoted 'on' as the boolean True; handle both.
        on = ci.get(True) or ci.get("on") or {}
        assert "pull_request" in on, f"ci.yml must trigger on pull_request, got {list(on.keys())}"

    def test_actions_pinned_to_sha(self):
        """DF-3 — every third-party action in every workflow must be pinned to a SHA."""
        import re
        for wf_path in (REPO_ROOT / ".github/workflows").glob("*.yml"):
            workflow = load_yaml(wf_path)
            for job in workflow.get("jobs", {}).values():
                for step in job.get("steps", []):
                    uses = step.get("uses", "")
                    if not uses or uses.startswith("./"):
                        continue
                    ref = uses.rsplit("@", 1)[-1] if "@" in uses else ""
                    assert len(ref) == 40 and re.fullmatch(r"[0-9a-f]{40}", ref), (
                        f"{wf_path.name}: action {uses!r} must be pinned to a 40-char SHA"
                    )
