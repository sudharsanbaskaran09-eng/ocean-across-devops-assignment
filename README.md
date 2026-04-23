
                │       │  │                                      │  │
                │       │  │  Private App Subnets                 │  │
                │       │  │  ┌────────────┐ ┌─────────┐ ┌──────┐ │  │
                └───────┼──┼─►│  Company   │ │ Bureau  │ │ Emp  │ │  │
                        │  │  │  EC2 :8000 │ │EC2:8001 │ │EC2   │ │  │
                 
oceans-across-devops/
├── cfn/
│   ├── 01-networking.yaml       # VPC, subnets, IGW, NAT, route tables, NACLs
│   ├── 02
- GitHub repository with the following secrets set:
  - `AWS_ACCOUNT_ID`
  - `COMPANY_EC2_ID`, `BUREAU_EC2_ID`, `EMPLOYEE_EC2_ID`
  - `DB_SECRET_ARN`
  - `DOCS_BUCKET`

### Step 1 — Create the DB secret in Secrets Manager

```bash
aws secretsmanager create-secret \
  --name "oceans-across/db-creds" \
  --region eu-west-2 \
  --secret-string '{"username":"payroll_admin","password":"<STRONG_PASSWORD>"}'
```

### Step 2 — Deploy stacks in order

```bash
# 1. Networking
aws cloudformation deploy \
  --template-file cfn/01-networking.yaml \
  --stack-name oceans-across-networking \
  --region eu-west-2

# 2. Security & IAM
aws cloudformation deploy \
  --template-file cfn/02-security-iam.yaml \
  --stack-name oceans-across-security \
  --capabilities CAPABILITY_NAMED_IAM \
  --region eu-west-2

# 3. Compute & Data
aws cloudformation deploy \
  --template-file cfn/03-compute-data.yaml \
  --stack-name oceans-across-compute \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides DBMasterPasswordSecret=oceans-across/db-creds \
  --region eu-west-2

# 4. Monitoring
aws cloudformation deploy \
  --template-file cfn/04-monitoring.yaml \
  --stack-name oceans-across-monitoring \
  --region eu-west-2
```

### Step 3 — Set up GitHub OIDC (one-time)

```bash
# Create OIDC provider for GitHub Actions
aws iam create-open-id-connect-provider \
  --url https://token.actions.githubusercontent.com \
  --client-id-list sts.amazonaws.com \
  --thumbprint-list 6938fd4d98bab03faadb97b34396831e3780aea1
```

Then update `YOUR_GITHUB_ORG` in `02-security-iam.yaml` before deploying.

---

## Task 1 — AWS Infrastructure

### CloudFormation Stack Architecture

The infrastructure is split across four stacks using cross-stack exports (`Fn::ImportValue`). This ensures:
- Stacks can be updated independently without redeploying everything
- The networking layer is the most stable and rarely changed
- Security/IAM changes don't require touching compute resources

### Key design decisions

**VPC CIDR `10.0.0.0/16`** — provides 65,536 addresses, enough headroom for future growth without renumbering.

**Three-tier subnet model** — public (ALB, NAT), private-app (EC2), private-db (RDS). The DB tier has no NAT route — it simply cannot initiate outbound internet connections.

**Single NAT Gateway** — a trade-off between cost (Free Tier) and HA. In production this would be one NAT per AZ to eliminate the AZ as a single point of failure.

**NACLs as secondary enforcement** — Security Groups are stateful and the primary control; NACLs are stateless and enforce the same rules at the subnet boundary. The DB NACL only allows port 5432 from the app subnet CIDR range `10.0.11.0/23`, meaning even if a Security Group were misconfigured, the NACL blocks cross-tier access.

---

## Task 2 — Multi-Tenancy Architecture

### 2a. Tenant Isolation Strategy

**Chosen model: Shared database with `tenant_id` row-level scoping + schema-per-tenant-type**

The database uses a hybrid approach:
- Three schemas: `companies`, `bureaus`, `employees` — one per portal type
- Within each schema, every table has a `tenant_id UUID NOT NULL` column
- Application-level Row Level Security (PostgreSQL RLS) enforces that all queries are automatically filtered

```sql
-- Example: enable RLS on the payroll_records table
ALTER TABLE companies.payroll_records ENABLE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation ON companies.payroll_records
  USING (tenant_id = current_setting('app.current_tenant_id')::uuid);
```

**Why not database-per-tenant?**  
On `db.t3.micro`, running 3+ separate RDS instances would immediately breach Free Tier and complicate cross-tenant reporting. Schema-per-type + RLS gives strong logical isolation with a single DB instance, which is appropriate for this scale.

**Tenant context propagation:**  
1. User authenticates via Cognito (OIDC) — JWT issued with `tenant_id` and `tenant_type` claims
2. ALB verifies the JWT using its built-in OIDC integration, rejects unauthenticated requests before they reach EC2
3. ALB injects verified claims as HTTP headers (`X-Tenant-Id`, `X-Tenant-Type`)
4. Flask middleware reads these headers, validates them, and sets `g.tenant_id` for the request lifetime
5. Before any DB query, the app runs: `SET LOCAL app.current_tenant_id = '<uuid>';`
6. PostgreSQL RLS automatically appends the `WHERE tenant_id = ...` condition — the application query never needs to specify it manually

This means cross-tenant leakage requires *both* the application middleware failing *and* the PostgreSQL RLS policy failing simultaneously.

### 2b. Access Boundaries at the Infrastructure Layer

IAM roles are scoped per tenant type with explicit `Deny` statements for other tenants' S3 prefixes. The S3 bucket policy mirrors this with `StringNotLike` prefix conditions. The result: even if application code contains a bug that constructs a path to another tenant's S3 prefix, the IAM policy rejects the API call at the AWS layer before any data is accessed.

### 2c. Tenant Onboarding & Offboarding

**Onboarding a new Company:**
1. Generate UUID `tenant_id`
2. Create Cognito user pool group with `tenant_id` claim
3. Insert row into `system.tenants` table
4. Create S3 prefix `companies/{tenant_id}/` (S3 prefixes are implicit — no explicit creation needed)
5. Run DB migration to ensure RLS policy covers the new tenant (it does automatically — RLS is set at the table level, not per-tenant)
6. Send welcome email with portal URL

**Offboarding:**
1. Disable Cognito user pool group → immediately blocks all logins
2. Revoke active sessions (Cognito global sign-out)
3. Export all tenant data to an encrypted S3 archive (legal hold, 7 years)
4. Delete all rows: `DELETE FROM companies.* WHERE tenant_id = '<uuid>';`
5. Delete S3 prefix: `aws s3 rm s3://bucket/companies/<uuid>/ --recursive`
6. Record audit event in CloudTrail and application audit log
7. After legal hold period expires, delete the archive

---

## Task 3 — Security & Access Control

### 3a. IAM & RBAC

- Each EC2 instance carries an instance profile with an IAM role scoped to its tenant type
- No role has `s3:*` — all S3 permissions are scoped to specific bucket ARNs and prefixes
- CI/CD uses OIDC (no static IAM keys stored in GitHub Secrets)
- `AmazonSSMManagedInstanceCore` is used instead of SSH — no port 22 open to the internet

### 3b. Secrets Management

All sensitive values (DB credentials, API keys) are stored in AWS Secrets Manager under the path `oceans-across/{tenant_type}/`. The application fetches secrets at startup using `boto3` via the instance's IAM role. No secrets appear in:
- CloudFormation templates (parameters reference secret ARNs, not values)
- Docker images (no ENV with secrets in Dockerfile)
- GitHub Actions YAML (OIDC used for AWS auth; only non-secret IDs stored as repo secrets)
- Application logs (Gunicorn and Flask do not log environment variables)

### 3c. Encryption

| Layer | Mechanism |
|-------|-----------|
| RDS at rest | AWS KMS (AES-256), enabled on instance creation |
| S3 at rest | AWS KMS (SSE-KMS), enforced by bucket policy denying unencrypted uploads |
| Data in transit (app → DB) | PostgreSQL SSL enforced via `rds.force_ssl=1` RDS parameter group |
| Data in transit (user → ALB) | TLS 1.2+ on ALB listener; HTTP redirects to HTTPS |
| Data in transit (ALB → EC2) | Internal HTTPS within VPC (can use self-signed cert on internal listener) |
| Secrets Manager API calls | HTTPS enforced by the AWS SDK |

### 3d. Network Security

- DB subnet NACLs only allow port 5432 from app subnet CIDR. Even if an EC2's SG were misconfigured, the NACL blocks cross-tier access.
- No EC2 instance has a public IP (`MapPublicIpOnLaunch: false` on private subnets)
- The only internet-facing resource is the ALB, which terminates TLS and proxies to private instances
- Instance management is done via SSM Session Manager — no bastion host required, no port 22 open

---

## Task 4 — CI/CD Pipeline

The pipeline uses **GitHub Actions with AWS OIDC** — no long-lived AWS credentials stored anywhere.

**Flow:**
1. Push to `main` → pipeline triggers
2. Run unit tests
3. Build Docker image, tag with short Git SHA
4. Authenticate to ECR via OIDC
5. Push image
6. Deploy to each EC2 via SSM `AWS-RunShellScript` document

**Multi-team independence:**  
The `workflow_dispatch` trigger with a `service` input allows the Company, Bureau, and Employee teams to independently redeploy their portal without touching the other services. CI jobs `deploy-company`, `deploy-bureau`, `deploy-employee` are independent and run in parallel after the shared build step.

**Environment secrets:**  
Non-secret configuration (tenant type, region) is passed as Docker environment variables in the SSM command. Sensitive values (DB ARN, bucket name) are stored as GitHub environment secrets scoped to `dev` and `prod` environments.

---

## Task 5 — Monitoring & Incident Readiness

### Alarms configured

| Alarm | Threshold | Action |
|-------|-----------|--------|
| Company/Bureau/Employee EC2 CPU | >80% for 10 min | SNS → Email |
| RDS Connection Count | >80 connections | SNS → Email |
| RDS Free Storage | <2 GB | SNS → Email |
| RDS Publicly Accessible (Config Rule) | Any violation | SNS → Email (immediate) |

### Log Retention

| Log Group | Retention | Rationale |
|-----------|-----------|-----------|
| `/oceans-across/{env}/{tenant}/app` | 365 days | UK GDPR audit requirement |
| `/oceans-across/{env}/infra` | 90 days | Operational debugging |
| RDS PostgreSQL logs | 365 days | Compliance + query audit |

---

## Task 6 — UK Compliance Considerations

### 1. AWS-native controls for UK GDPR (employee PII & bank data)

- **Data minimisation at storage:** S3 bucket tags (`DataClassification: Highly-Sensitive`, `GDPRRelevant: true`) are used to drive automated policies via AWS Config and Lambda rules.
- **Encryption:** All PII encrypted at rest (KMS) and in transit (TLS). KMS key policies restrict who can decrypt.
- **Access logging:** S3 server access logging enabled. CloudTrail enabled for all API calls (including S3 object-level events for the payroll bucket). RDS parameter group logs all connections and queries >1s.
- **IAM least privilege:** No role has unnecessary access to raw PII columns. Application-level field-level encryption can be added for bank account numbers using KMS `GenerateDataKey`.
- **AWS Config rules:** `RDS_INSTANCE_PUBLIC_ACCESS_CHECK`, `S3_BUCKET_PUBLIC_READ_PROHIBITED`, `ENCRYPTED_VOLUMES`, `RDS_STORAGE_ENCRYPTED` — all enabled, violations trigger SNS alerts.
- **GuardDuty:** Recommended addition — detects unusual API call patterns that could indicate a credential compromise targeting PII data.

### 2. Data residency within UK/EU

- All stacks deploy to `eu-west-2` (London) explicitly in every stack parameter and in the GitHub Actions `AWS_REGION` env var.
- S3 bucket replication is disabled — data does not leave the region.
- No cross-region services are used (no Global Accelerator, no us-east-1 IAM-specific endpoints that would route data through the US).
- CloudFront is intentionally excluded from this architecture — it would introduce US edge caches and complicate residency guarantees. ALB is used instead, which keeps traffic within the region.
- If a CloudFront CDN is required in future, `PriceClass_100` (Europe only) with origin in `eu-west-2` would be the compliant configuration.

### 3. Right to erasure (Article 17 UK GDPR)

When an employee requests deletion of their data:

**Step 1 — Identify all data stores:**
```
- RDS: employees schema, all tables with tenant_id = <employee_uuid>
- S3: employees/{employee_uuid}/ prefix
- CloudWatch Logs: any log entries containing the employee's ID or PII
- Secrets Manager: any employee-specific secrets
- Cognito: user pool entry
- Backups: RDS automated snapshots and S3 versioned objects
```

**Step 2 — Application-layer deletion:**
```sql
-- Anonymise rather than hard-delete where referential integrity is required
UPDATE employees.payroll_records
SET employee_name = 'DELETED',
    bank_account = 'DELETED',
    national_insurance = 'DELETED',
    updated_at = NOW(),
    deletion_requested_at = NOW()
WHERE tenant_id = '<employee_uuid>';

-- Hard delete non-essential records
DELETE FROM employees.sessions WHERE tenant_id = '<employee_uuid>';
DELETE FROM employees.preferences WHERE tenant_id = '<employee_uuid>';
```

**Step 3 — S3 deletion:**
```bash
# Delete all current versions
aws s3 rm s3://oceans-across-payroll-docs/employees/<uuid>/ --recursive

# Delete all old versions (versioning enabled)
aws s3api list-object-versions \
  --bucket oceans-across-payroll-docs \
  --prefix "employees/<uuid>/" \
  --query 'Versions[].{Key:Key,VersionId:VersionId}' \
  | jq -r '.[] | "aws s3api delete-object --bucket oceans-across-payroll-docs --key \(.Key) --version-id \(.VersionId)"' \
  | bash
```

**Step 4 — Cognito deletion:**
```bash
aws cognito-idp admin-delete-user \
  --user-pool-id <pool-id> \
  --username <employee_uuid>
```

**Step 5 — Backup handling:**
RDS snapshots older than 7 days that contain the employee's data cannot be selectively purged (RDS backups are full snapshots). The approach: retain snapshots for the legally required period (2 years for payroll records under HMRC rules), then delete. If the erasure request conflicts with a legal hold (e.g., an HMRC audit in progress), document the exemption under UK GDPR Article 17(3)(b) (legal obligation).

**Step 6 — Audit trail:**
Record the deletion request, action taken, and timestamp in a separate `compliance.deletion_audit` table (which itself is exempt from erasure as it's required for regulatory proof).

---

## Incident Response Runbook

### Incident: RDS Instance Made Publicly Accessible

**Severity:** P1 — Critical  
**Detection SLA:** < 5 minutes (automated)  
**Resolution SLA:** < 30 minutes

---

#### Detection

**Automated:**
- AWS Config rule `RDS_INSTANCE_PUBLIC_ACCESS_CHECK` fires immediately when `PubliclyAccessible` is set to `true`
- EventBridge rule routes the Config non-compliance event to SNS → email/PagerDuty alert
- CloudWatch alarm on Config compliance state change sends secondary alert

**Manual indicators:**
- CloudTrail event: `ModifyDBInstance` with `PubliclyAccessible: true` in the request parameters
- Unexpected external connections appearing in RDS enhanced monitoring

---

#### Investigation

1. **Confirm the change via CloudTrail:**
```bash
aws cloudtrail lookup-events \
  --lookup-attributes AttributeKey=EventName,AttributeValue=ModifyDBInstance \
  --start-time $(date -d '1 hour ago' --iso-8601=seconds) \
  --region eu-west-2
```
Note: `userIdentity.arn` — who made the change. Note: `requestParameters.publiclyAccessible`.

2. **Check Security Group for open inbound rules:**
```bash
aws ec2 describe-security-groups \
  --filters Name=group-id,Values=<rds-sg-id> \
  --query 'SecurityGroups[*].IpPermissions'
```

3. **Check for active external connections:**
```bash
# Via RDS Performance Insights or CloudWatch Logs
aws logs filter-log-events \
  --log-group-name "/aws/rds/instance/oceans-across-payroll-db-prod/postgresql" \
  --filter-pattern "connection received" \
  --start-time $(date -d '1 hour ago' +%s000)
```

4. **Assess blast radius:** Were any foreign source IPs connecting during the window?

---

#### Containment (immediate — within 5 minutes)

```bash
# 1. Immediately revert PubliclyAccessible to false
aws rds modify-db-instance \
  --db-instance-identifier oceans-across-payroll-db-prod \
  --no-publicly-accessible \
  --apply-immediately \
  --region eu-west-2

# 2. Revoke any Security Group rules that opened public access (port 5432 from 0.0.0.0/0)
aws ec2 revoke-security-group-ingress \
  --group-id <rds-sg-id> \
  --protocol tcp \
  --port 5432 \
  --cidr 0.0.0.0/0
```

---

#### Recovery

1. Verify `PubliclyAccessible` is now `false`:
```bash
aws rds describe-db-instances \
  --db-instance-identifier oceans-across-payroll-db-prod \
  --query 'DBInstances[0].PubliclyAccessible'
# Expected: false
```

2. Verify Security Group no longer has 0.0.0.0/0 on 5432:
```bash
aws ec2 describe-security-groups --group-ids <rds-sg-id>
```

3. Force rotation of DB master password (precautionary):
```bash
aws secretsmanager rotate-secret \
  --secret-id oceans-across/db-creds
```

4. Invalidate active DB sessions:
```bash
# Connect via SSM to Company EC2 and run
psql -h <rds-endpoint> -U payroll_admin -d payroll \
  -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE client_addr NOT LIKE '10.0.%';"
```

---

#### Post-Incident Actions

- **RCA document** within 24 hours: Who made the change? Was it accidental (console click) or malicious?
- **IAM review:** Should `ModifyDBInstance` be removed from any roles that don't need it?
- **SCPs (Service Control Policy):** If using AWS Organizations, add an SCP to deny `rds:ModifyDBInstance` with `PubliclyAccessible=true` at the account level — this prevents the change regardless of IAM.
- **UK GDPR notification assessment:** If foreign IPs connected during the exposure window, data controller must assess whether a breach notification to ICO is required (within 72 hours of becoming aware).

---

## Trade-offs & Decisions

| Decision | Alternative Considered | Why This Choice |
|----------|----------------------|-----------------|
| CloudFormation over Terraform | Terraform is my primary tool | Assignment context: AWS-native, no state backend to manage |
| Shared RDS with schema isolation | Database-per-tenant | Free Tier constraint; db.t3.micro for 3 databases would exceed limits |
| SSM over SSH for deployments | SSH with EC2 Key Pair | No open port 22, full audit trail in SSM, no key management burden |
| Flask stub app | Real payroll logic | Assignment scope; demonstrates the architectural patterns clearly |
| Single NAT Gateway | One per AZ | Cost (NAT is not free tier); acceptable for dev/assignment |
| OIDC for GitHub Actions | Static IAM access keys | No long-lived credentials stored anywhere; security best practice |
| eu-west-2 (London) | eu-west-1 (Ireland) | UK data residency post-Brexit; stays within UK jurisdiction |
