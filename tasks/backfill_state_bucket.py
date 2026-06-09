"""
Backfill per-account Terraform state infrastructure for pre-AFT accounts.

Creates the S3 bucket and DynamoDB lock table matching the AFT-provisioned
naming convention. Idempotent — skips resources that already exist.
"""

import logging

from anvil.actions import ActionRecorder

__LOGGER__ = logging.getLogger(__name__)


def _bucket_name(account_id: str, region: str) -> str:
    return f"{account_id}-{region}-terraform-state"


def _lock_table_name(account_id: str, region: str) -> str:
    return f"{account_id}-{region}-terraform-state-locking"


def _bucket_exists(s3_client, bucket: str) -> bool:
    try:
        s3_client.head_bucket(Bucket=bucket)
        return True
    except s3_client.exceptions.ClientError as e:
        if e.response["Error"]["Code"] == "404":
            return False
        raise


def _table_exists(dynamodb_client, table: str) -> bool:
    try:
        dynamodb_client.describe_table(TableName=table)
        return True
    except dynamodb_client.exceptions.ResourceNotFoundException:
        return False


def _create_bucket(s3_client, bucket: str, region: str) -> None:
    params: dict = {"Bucket": bucket}
    if region != "us-east-1":
        params["CreateBucketConfiguration"] = {"LocationConstraint": region}

    s3_client.create_bucket(**params)
    s3_client.put_bucket_tagging(
        Bucket=bucket,
        Tagging={
            "TagSet": [
                {"Key": "Name", "Value": bucket},
                {"Key": "BCDRBackup", "Value": "daily"},
            ]
        },
    )
    s3_client.put_bucket_versioning(
        Bucket=bucket,
        VersioningConfiguration={"Status": "Enabled"},
    )
    s3_client.put_bucket_encryption(
        Bucket=bucket,
        ServerSideEncryptionConfiguration={
            "Rules": [
                {
                    "ApplyServerSideEncryptionByDefault": {
                        "SSEAlgorithm": "AES256"
                    },
                    "BucketKeyEnabled": True,
                }
            ]
        },
    )
    s3_client.put_public_access_block(
        Bucket=bucket,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        },
    )


def _create_lock_table(dynamodb_client, table: str) -> None:
    dynamodb_client.create_table(
        TableName=table,
        KeySchema=[{"AttributeName": "LockID", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "LockID", "AttributeType": "S"}
        ],
        BillingMode="PAY_PER_REQUEST",
        DeletionProtectionEnabled=True,
        Tags=[{"Key": "Name", "Value": table}],
    )
    dynamodb_client.get_waiter("table_exists").wait(
        TableName=table, WaiterConfig={"Delay": 5, "MaxAttempts": 12}
    )


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
    bucket = _bucket_name(account_id, region)
    lock_table = _lock_table_name(account_id, region)

    s3 = session.client("s3")
    dynamodb = session.client("dynamodb")

    bucket_existed = _bucket_exists(s3, bucket)
    table_existed = _table_exists(dynamodb, lock_table)

    if bucket_existed and table_existed:
        __LOGGER__.info(
            f"Bucket {bucket} and table {lock_table} already exist, skipping"
        )
        actions.record(
            f"State infrastructure already exists for account {account_id}"
        )
        return {
            "bucket": bucket,
            "lock_table": lock_table,
            "bucket_created": False,
            "table_created": False,
        }

    if dry_run:
        if not bucket_existed:
            __LOGGER__.info(f"(dry-run) Would create bucket {bucket}")
        if not table_existed:
            __LOGGER__.info(f"(dry-run) Would create lock table {lock_table}")
        actions.record(
            f"(dry-run) Would backfill state infrastructure "
            f"for account {account_id}"
        )
        return {
            "bucket": bucket,
            "lock_table": lock_table,
            "bucket_created": False,
            "table_created": False,
            "planned": True,
        }

    bucket_created = False
    if not bucket_existed:
        __LOGGER__.info(f"Creating bucket {bucket}")
        _create_bucket(s3, bucket, region)
        bucket_created = True

    table_created = False
    if not table_existed:
        __LOGGER__.info(f"Creating lock table {lock_table}")
        _create_lock_table(dynamodb, lock_table)
        table_created = True

    actions.record(
        f"Backfilled state infrastructure for account {account_id} "
        f"(bucket_created={bucket_created}, table_created={table_created})"
    )

    return {
        "bucket": bucket,
        "lock_table": lock_table,
        "bucket_created": bucket_created,
        "table_created": table_created,
    }
