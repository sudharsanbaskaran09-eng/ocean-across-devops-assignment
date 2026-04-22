# AI Usage Log — Oceans Across DevOps Assignment

**Candidate:** Sudharsan B  
**AI Tool Used:** Claude (Anthropic) — claude.ai  
**Date:** April 2026

This log documents every use of AI assistance during this assignment, as required. I used Claude as a senior pair-programmer and reviewer — not to blindly generate output, but to accelerate drafting and catch gaps in my own reasoning.

---

## Session 1 — Assignment Analysis & Planning

**Prompt:**
> [Uploaded the assignment PDF and a screenshot of the HR email]
> "Congrats on the shortlist, brat! This is a solid assignment..."

**AI Output:**
Claude broke down the 6 tasks, identified that a live AWS deployment is not required, and proposed a 6-file repo structure: networking CFN, security/IAM CFN, compute/data CFN, monitoring CFN, GitHub Actions pipeline, and README.

**What I took from it:**
The overall repo structure. I agreed with the recommendation to use CloudFormation (AWS-native, no remote state backend needed for an assignment submission).

**What I changed:**
I asked for CloudFormation specifically rather than Terraform, as I wanted to show AWS-native IaC. Claude had initially offered both options — I made the call.

---

## Session 2 — CloudFormation: Networking Stack (01-networking.yaml)

**Prompt:**
> "Build the full CloudFormation for the VPC — 2 AZs, public/private subnets, NAT, NACLs isolating the DB tier. Export outputs for cross-stack use."

**AI Output:**
Complete `01-networking.yaml` with VPC, public subnets, private app subnets, private DB subnets, IGW, NAT Gateway, route tables, and three separate NACLs (public, private-app, private-db).

**What I took from it:**
The overall structure and NACL configuration. The DB NACL restricting port 5432 to `10.0.11.0/23` (covering both app subnets in a single CIDR) was a good catch — I would have written two separate NACL entries.

**What I changed/verified:**
- Confirmed the `10.0.11.0/23` CIDR correctly covers both `10.0.11.0/24` and `10.0.12.0/24`
- Verified the NAT Gateway is placed in a public subnet with an EIP
- Noted that a single NAT GW is a trade-off (cost vs HA) — I added this to the README trade-offs section explicitly

---

## Session 3 — CloudFormation: Security & IAM Stack (02-security-iam.yaml)

**Prompt:**
> "Per-tenant security groups (Company, Bureau, Employee, RDS, ALB), IAM roles with least-privilege S3 prefix access, explicit Deny for cross-tenant S3 paths, instance profiles, and a CI/CD OIDC role."

**AI Output:**
Full `02-security-iam.yaml` with all SGs, three tenant IAM roles, explicit Deny statements for cross-tenant S3 prefixes, instance profiles, and a CICDDeployRole using GitHub OIDC.

**What I took from it:**
The pattern of using `Effect: Deny` explicitly on the Bureau and Employee roles for other tenants' S3 prefixes — belt-and-suspenders on top of the allow-only policy. I had initially only planned allow policies.

**What I changed:**
- Employee role is read-only for S3 (`s3:GetObject`, `s3:ListBucket`) — employees should not be able to upload or delete payroll documents. I made this explicit after reviewing the output.
- Added `AmazonSSMManagedInstanceCore` to all instance roles — Claude included it, and I kept it because I was already planning to use SSM over SSH.

**What I rejected:**
Claude initially included `ecr:*` on the CI/CD role. I narrowed it to specific ECR push/pull actions (GetAuthorizationToken, BatchCheckLayerAvailability, etc.) to enforce least privilege.

---

## Session 4 — CloudFormation: Compute & Data Stack (03-compute-data.yaml)

**Prompt:**
> "EC2 one per tenant in private subnets with SSM agent in UserData. RDS PostgreSQL db.t3.micro with SSL forced, encryption at rest, no public access, deletion protection. S3 with KMS encryption, versioning, lifecycle rules for 7-year retention, bucket policy enforcing tenant prefix isolation and denying unencrypted uploads."

**AI Output:**
Complete `03-compute-data.yaml` using SSM Parameter Store for the latest Amazon Linux 2023 AMI (dynamic, no hardcoded AMI IDs), RDS with `rds.force_ssl=1` parameter group, and S3 with a bucket policy enforcing 4 rules (encrypted uploads, HTTPS-only, and prefix enforcement per tenant role).

**What I took from it:**
The pattern of using `AWS::SSM::Parameter::Value<AWS::EC2::Image::Id>` for the AMI ID rather than hardcoding — this keeps the template region-agnostic and always uses the latest patched AMI. I hadn't planned to do this.

**What I changed:**
- Added `DeletionPolicy: Snapshot` on the RDS instance — important for production data, easy to forget
- Added a separate `AccessLogsBucket` for S3 server access logs — Claude included it in the output and I agreed it was necessary for compliance
- Changed `AllocatedStorage` to "20" (string) — CloudFormation expects string for this property

**What I rejected:**
Claude's initial draft set `MultiAZ: true` on RDS. I changed this to `false` with a comment noting it should be `true` in production — db.t3.micro with MultiAZ would increase cost and the assignment explicitly requires Free Tier only.

---

## Session 5 — CloudFormation: Monitoring Stack (04-monitoring.yaml)

**Prompt:**
> "CloudWatch alarms for EC2 CPU and RDS connections, log groups per tenant with 365-day retention, SNS topic, and a Config rule that detects if RDS is made publicly accessible, wired to an EventBridge rule that fires SNS."

**AI Output:**
Full `04-monitoring.yaml` with per-tenant CPU alarms, RDS connection and storage alarms, log groups, SNS topic with KMS, and an `AWS::Config::ConfigRule` using `RDS_INSTANCE_PUBLIC_ACCESS_CHECK` wired to EventBridge.

**What I took from it:**
The EventBridge → SNS pattern for the Config rule violation. I had originally planned to handle this with a Lambda, but the EventBridge direct integration to SNS is simpler and more reliable for this use case.

**What I changed:**
- Added the `RDSStorageAlarm` (free storage < 2GB) — Claude only generated the connection alarm initially; I asked for this as a follow-up
- Set `TreatMissingData: notBreaching` on CPU alarms — avoids false alerts during maintenance windows when CloudWatch temporarily loses metrics

---

## Session 6 — Application Stub (app.py)

**Prompt:**
> "Write a Flask app that demonstrates tenant context: extract tenant_id from ALB-injected OIDC headers, validate against the instance's TENANT_TYPE env var, use g.tenant_id in a payroll data endpoint and an S3 document listing endpoint. Include health and readiness probes."

**AI Output:**
`app.py` with a `require_tenant` decorator, `/health`, `/ready`, `/api/v1/payroll`, and `/api/v1/documents` endpoints.

**What I took from it:**
The `require_tenant` decorator pattern — clean separation of auth concern from business logic. The readiness probe checking Secrets Manager reachability was a good operational detail.

**What I changed:**
- Added the `g.tenant_type != TENANT_TYPE` check — so the Company EC2 explicitly rejects requests with `X-Tenant-Type: bureau`. This is a defence-in-depth check at the application layer, separate from the ALB routing rule. Claude's initial version only validated that the tenant type was a valid enum value.
- Added a comment explaining the SQL pattern that would enforce RLS via `SET LOCAL app.current_tenant_id` — this is the most important multi-tenancy detail for reviewers to see.

---

## Session 7 — GitHub Actions Pipeline (deploy.yml)

**Prompt:**
> "GitHub Actions pipeline: OIDC auth to AWS (no static keys), build Docker image tagged with Git SHA, push to ECR, deploy to each EC2 via SSM send-command. Separate jobs per tenant so teams can deploy independently. workflow_dispatch with service and environment inputs."

**AI Output:**
Complete `deploy.yml` with OIDC auth, build-and-test job, three independent deploy jobs.

**What I took from it:**
The artifact upload/download pattern for passing the image tag between jobs — cleaner than output variables across jobs. The `workflow_dispatch` with `service` input was exactly what I needed for multi-team independence.

**What I changed:**
- Added `|| true` on the pytest step with a comment — allows the pipeline to pass before test files are created, with a clear note to remove it when real tests are written. Claude's output failed the pipeline if no tests existed.
- Added `docker stop || true` and `docker rm || true` before the `docker run` — handles clean replacement of existing containers gracefully.

---

## Session 8 — README, Task 6 Compliance, Incident Runbook

**Prompt:**
> "Write the full README covering all 6 tasks. For Task 6: UK GDPR controls, data residency in eu-west-2 (not CloudFront, no cross-region), right to erasure process including S3 versioned objects and RDS snapshots. Incident runbook for RDS made publicly accessible — detection, investigation, containment, recovery, post-incident."

**AI Output:**
Full README with architecture diagram (ASCII), all task explanations, Task 6 compliance section, and incident runbook.

**What I took from it:**
The S3 versioned object deletion script using `list-object-versions` + `jq` in the right-to-erasure section — I had planned to cover this in prose but the concrete CLI command is much stronger. The point about UK GDPR Article 17(3)(b) exemption for legal holds (HMRC audit) was a nuance I wouldn't have included without prompting.

**What I changed:**
- Added the ICO notification mention in the post-incident section (72-hour GDPR breach notification window) — Claude's initial runbook didn't mention this, which is an important gap for a UK payroll platform
- Changed the trade-offs table to include the eu-west-2 vs eu-west-1 decision explicitly — post-Brexit UK data residency is a real distinction that matters for this platform
- Removed a mention of AWS Config Conformance Packs from Task 6 — Claude suggested them but I haven't configured one in the IaC, so mentioning them would be inconsistent with the actual submission

---

## Reflection on AI Usage

Using Claude substantially accelerated the boilerplate drafting — particularly the CloudFormation YAML and the pipeline YAML. These are verbose formats where it's easy to make small syntactic errors that only surface at deploy time.

Where Claude added most value beyond speed:
- The `AWS::SSM::Parameter::Value` AMI pattern (dynamic, region-agnostic)
- The belt-and-suspenders `Effect: Deny` on cross-tenant S3 prefixes
- The EventBridge → SNS pattern for Config rule violations
- The Article 17(3)(b) HMRC legal hold exception

Where I consistently added value over Claude's output:
- Enforcing actual Free Tier constraints (catching MultiAZ, narrowing IAM permissions)
- Application-layer tenant type mismatch validation (defence-in-depth)
- ICO 72-hour notification requirement in the incident runbook
- Removing features mentioned in prose that weren't reflected in actual IaC

My overall approach: use Claude to generate a complete first draft quickly, then review every section critically against the assignment requirements and my own understanding of the system. Nothing was accepted without being read and understood.
