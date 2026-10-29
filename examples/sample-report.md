# Unattached EBS volume report

Generated **2026-10-01 12:00 UTC**. Volumes unattached and created more than **30 days** ago.

## Summary

| Metric | Value |
|---|---|
| Accounts scanned | 3 |
| Account/region pairs scanned | 3 |
| Unattached volumes | 6 |
| Total size | 3,850 GiB |
| Estimated monthly cost | $1,349.00 |
| Estimated annual cost | $16,188.00 |
| Protected (excluded from plan) | 1 |
| Potential monthly savings | $1,329.00 |
| Scan errors | 1 |

## By account

| Name | Volumes | Size (GiB) | Monthly cost |
|---|---|---|---|
| payments-prod (111122223333) | 4 | 3,700 | $1,335.00 |
| payments-dev (444455556666) | 2 | 150 | $14.00 |

## By region

| Name | Volumes | Size (GiB) | Monthly cost |
|---|---|---|---|
| us-east-1 | 5 | 1,850 | $1,259.00 |
| eu-west-1 | 1 | 2,000 | $90.00 |

## By volume type

| Name | Volumes | Size (GiB) | Monthly cost |
|---|---|---|---|
| io2 | 1 | 1,000 | $1,165.00 |
| st1 | 1 | 2,000 | $90.00 |
| gp3 | 2 | 550 | $64.00 |
| gp2 | 2 | 300 | $30.00 |

## Top 6 volumes by cost

| Account | Region | Volume | Name | Type | GiB | Age (days) | Monthly | Protected |
|---|---|---|---|---|---|---|---|---|
| 111122223333 | us-east-1 | `vol-0a1b2c3d4e5f60001` | oracle-restore-test | io2 | 1000 | 212 | $1,165.00 |  |
| 111122223333 | eu-west-1 | `vol-0a1b2c3d4e5f60004` | log-archive-old | st1 | 2000 | 150 | $90.00 |  |
| 111122223333 | us-east-1 | `vol-0a1b2c3d4e5f60002` | etl-scratch | gp3 | 500 | 96 | $60.00 |  |
| 111122223333 | us-east-1 | `vol-0a1b2c3d4e5f60003` | legal-hold | gp2 | 200 | 400 | $20.00 | yes |
| 444455556666 | us-east-1 | `vol-0a1b2c3d4e5f60005` | - | gp2 | 100 | 45 | $10.00 |  |
| 444455556666 | us-east-1 | `vol-0a1b2c3d4e5f60006` | jenkins-agent-old | gp3 | 50 | 33 | $4.00 |  |

## Errors

These accounts or regions could not be fully scanned:

| Account | Region | Stage | Code | Message |
|---|---|---|---|---|
| 777788889999 | - | discover-regions | AccessDenied | User: arn:aws:sts::111122223333:assumed-role/ebs-audit is not authorized to perform: sts:AssumeRole |

---
_Costs are estimates based on on-demand list prices (storage + provisioned IOPS/throughput). Age is measured from the volume's creation time._
