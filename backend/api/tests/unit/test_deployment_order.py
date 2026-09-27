"""The production deployment (.github/workflows/deploy.yml) migrates the
schema before it deploys the code that needs it: new API and worker code
never runs against a schema that's missing what it uses (like the
`rate_limit_counters` table), not even for the seconds between two steps.

Infrastructure side, `infra/terraform/live/tests` checks that the first,
targeted apply includes no function but the migration one."""

from pathlib import Path

import yaml

_WORKFLOW = Path(__file__).resolve().parents[4] / ".github" / "workflows" / "deploy.yml"
_MIGRATIONS_TARGET = """-target='aws_lambda_function.main["migrations"]'"""


def _deploy_steps() -> list[dict]:
    return yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))["jobs"]["deploy"]["steps"]


def _index(steps: list[dict], predicate) -> int:
    [index] = [i for i, step in enumerate(steps) if predicate(step.get("name", ""), step.get("run", ""))]
    return index


def test_schema_is_migrated_before_any_new_code_is_deployed():
    steps = _deploy_steps()

    migrations_plan = _index(steps, lambda name, run: "terraform plan" in run and _MIGRATIONS_TARGET in run)
    migrations_apply = _index(steps, lambda name, run: "terraform apply" in run and "migrations.tfplan" in run)
    migrate = _index(steps, lambda name, run: name == "Database migrations")
    full_plan = _index(steps, lambda name, run: "terraform plan" in run and "-out=tfplan" in run)
    full_apply = _index(steps, lambda name, run: run.strip() == "terraform apply -input=false -no-color tfplan")
    frontend = _index(steps, lambda name, run: name == "Build the frontend")

    assert migrations_plan < migrations_apply < migrate < full_plan < full_apply < frontend


def test_the_first_apply_is_only_the_migration_function():
    runs = [step.get("run", "") for step in _deploy_steps()]
    targeted = [run for run in runs if "-target" in run]

    assert len(targeted) == 1
    assert targeted[0].count("-target") == 1 and _MIGRATIONS_TARGET in targeted[0]


def test_a_failed_migration_stops_the_deployment():
    steps = _deploy_steps()
    migrate = steps[_index(steps, lambda name, run: name == "Database migrations")]

    assert "FunctionError" in migrate["run"] and "exit 1" in migrate["run"]
    assert migrate["env"]["AWS_MAX_ATTEMPTS"] == "1"
    # Nothing in the job is allowed to continue past a failed step.
    assert not any(step.get("continue-on-error") for step in steps)
