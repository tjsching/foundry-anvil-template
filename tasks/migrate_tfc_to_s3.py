"""
Migrate Terraform state from Terraform Cloud to a per-account S3 backend.

Rewrites the backend configuration to target the AFT-provisioned S3 bucket
and DynamoDB lock table, then runs `terraform init -migrate-state` to move
the state file.
"""

import logging
import subprocess
import tempfile
from pathlib import Path

from anvil.actions import ActionRecorder

__LOGGER__ = logging.getLogger(__name__)

_BACKEND_TF = """\
terraform {{
  backend "s3" {{
    bucket         = "{bucket}"
    key            = "{key}"
    region         = "{region}"
    dynamodb_table = "{lock_table}"
    encrypt        = true
  }}
}}
"""


def _bucket_name(account_id: str, region: str) -> str:
    return f"{account_id}-{region}-terraform-state"


def _lock_table_name(account_id: str, region: str) -> str:
    return f"{account_id}-{region}-terraform-state-locking"


def _verify_bucket_exists(session, bucket: str) -> None:
    s3 = session.client("s3")
    s3.head_bucket(Bucket=bucket)


def _verify_table_exists(session, table: str) -> None:
    dynamodb = session.client("dynamodb")
    dynamodb.describe_table(TableName=table)


def _write_backend_override(
    work_dir: Path, bucket: str, key: str, region: str, lock_table: str
) -> Path:
    override = work_dir / "backend_override.tf"
    override.write_text(
        _BACKEND_TF.format(
            bucket=bucket, key=key, region=region, lock_table=lock_table
        )
    )
    return override


def _run_terraform(args: list[str], work_dir: Path, env: dict) -> str:
    cmd = ["terraform", *args]
    __LOGGER__.debug(f"Running: {' '.join(cmd)}")
    result = subprocess.run(
        cmd,
        cwd=work_dir,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        __LOGGER__.error(f"terraform {args[0]} failed:\n{result.stderr}")
        raise RuntimeError(
            f"terraform {args[0]} exited {result.returncode}: {result.stderr.strip()}"
        )
    return result.stdout


def run(
    *,
    account_id: str,
    account_alias: str,
    session,
    dry_run: bool,
    metadata: dict[str, object],
    actions: ActionRecorder,
) -> dict:
    region = session.region_name

    terraform_base_dir = metadata.get("terraform_base_dir")
    if not isinstance(terraform_base_dir, str):
        raise RuntimeError(
            "migrate_tfc_to_s3 requires metadata.terraform_base_dir to be a string"
        )

    dir_prefix = metadata.get("dir_prefix", "aws-cloud-custodian-")
    if not isinstance(dir_prefix, str):
        raise RuntimeError(
            "migrate_tfc_to_s3 requires metadata.dir_prefix to be a string"
        )

    state_key = metadata.get("state_key", "cloud-custodian/terraform.tfstate")
    if not isinstance(state_key, str):
        raise RuntimeError(
            "migrate_tfc_to_s3 requires metadata.state_key to be a string"
        )

    bucket = _bucket_name(account_id, region)
    lock_table = _lock_table_name(account_id, region)
    work_dir = Path(terraform_base_dir) / f"{dir_prefix}{account_id}"

    if not work_dir.is_dir():
        raise RuntimeError(
            f"terraform directory does not exist: {work_dir}"
        )

    __LOGGER__.info(
        f"Verifying S3 bucket {bucket} and DynamoDB table {lock_table} exist"
    )
    _verify_bucket_exists(session, bucket)
    _verify_table_exists(session, lock_table)

    credentials = session.get_credentials().get_frozen_credentials()
    env = {
        "AWS_ACCESS_KEY_ID": credentials.access_key,
        "AWS_SECRET_ACCESS_KEY": credentials.secret_key,
        "AWS_DEFAULT_REGION": region,
        "PATH": subprocess.os.environ.get("PATH", ""),
        "HOME": subprocess.os.environ.get("HOME", ""),
        "TF_IN_AUTOMATION": "1",
        "TF_INPUT": "0",
    }
    if credentials.token:
        env["AWS_SESSION_TOKEN"] = credentials.token

    if dry_run:
        __LOGGER__.info(
            f"(dry-run) Would migrate state to s3://{bucket}/{state_key} "
            f"with lock table {lock_table}"
        )
        actions.record(
            f"(dry-run) Would migrate Terraform state to "
            f"s3://{bucket}/{state_key} for account {account_id}"
        )
        return {
            "planned": True,
            "migrated": False,
            "bucket": bucket,
            "key": state_key,
            "lock_table": lock_table,
        }

    with tempfile.TemporaryDirectory(prefix="anvil_migrate_") as tmp:
        override_path = _write_backend_override(
            Path(tmp), bucket, state_key, region, lock_table
        )
        target_override = work_dir / "backend_override.tf"
        target_override.write_text(override_path.read_text())

        try:
            __LOGGER__.info("Running terraform init -migrate-state")
            _run_terraform(
                [
                    "init",
                    "-migrate-state",
                    "-force-copy",
                    f"-backend-config=bucket={bucket}",
                    f"-backend-config=key={state_key}",
                    f"-backend-config=region={region}",
                    f"-backend-config=dynamodb_table={lock_table}",
                    f"-backend-config=encrypt=true",
                ],
                work_dir,
                env,
            )

            __LOGGER__.info("Running terraform plan to validate migrated state")
            plan_output = _run_terraform(["plan", "-no-color"], work_dir, env)
            has_changes = "No changes." not in plan_output
        finally:
            if target_override.exists():
                target_override.unlink()

    actions.record(
        f"Migrated Terraform state to s3://{bucket}/{state_key} "
        f"for account {account_id}"
    )

    return {
        "planned": False,
        "migrated": True,
        "bucket": bucket,
        "key": state_key,
        "lock_table": lock_table,
        "plan_has_changes": has_changes,
    }
