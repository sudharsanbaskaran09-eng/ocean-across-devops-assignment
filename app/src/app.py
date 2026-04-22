"""
Oceans Across — Payroll Platform Portal Stub
A minimal Flask service that demonstrates:
  - Tenant context extraction from JWT
  - Request-scoped tenant ID propagation
  - Health + readiness endpoints for ALB health checks
"""

import os
import json
import logging
import boto3
from functools import wraps
from flask import Flask, request, jsonify, g

app = Flask(__name__)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] tenant=%(tenant_id)s %(message)s",
)

# ── Tenant context ─────────────────────────────────────────────────────────────
# In production: decoded from a verified JWT (Cognito / Auth0).
# Here we read a header set by the ALB after JWT verification.
# The ALB OIDC integration sets X-Amzn-Oidc-Data with the verified claims.

TENANT_TYPE = os.environ.get("TENANT_TYPE", "unknown")   # set in Docker/UserData
VALID_TENANTS = {"company", "bureau", "employee"}


def require_tenant(f):
    """Decorator: extracts tenant_id from ALB-injected OIDC claim header.
    Sets g.tenant_id and g.tenant_type for use in downstream queries.
    Rejects requests where tenant_type doesn't match this instance's role."""
    @wraps(f)
    def decorated(*args, **kwargs):
        # In production: parse and verify X-Amzn-Oidc-Data (JWT from ALB)
        # For demo: read X-Tenant-Id header (set by ALB listener rule condition)
        raw_tenant = request.headers.get("X-Tenant-Id", "")
        raw_type   = request.headers.get("X-Tenant-Type", "")

        if not raw_tenant or raw_type not in VALID_TENANTS:
            return jsonify({"error": "unauthorized", "detail": "missing or invalid tenant context"}), 401

        # Enforce: this instance only serves its own tenant type
        if raw_type != TENANT_TYPE:
            app.logger.warning(
                "Tenant type mismatch — request for '%s' reached '%s' instance",
                raw_type, TENANT_TYPE
            )
            return jsonify({"error": "forbidden", "detail": "tenant type mismatch"}), 403

        g.tenant_id   = raw_tenant
        g.tenant_type = raw_type
        return f(*args, **kwargs)
    return decorated


def get_secret(secret_name: str) -> dict:
    """Fetch a secret from AWS Secrets Manager at runtime (no env var leakage)."""
    client = boto3.client("secretsmanager", region_name=os.environ.get("AWS_REGION", "eu-west-2"))
    response = client.get_secret_value(SecretId=secret_name)
    return json.loads(response["SecretString"])


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    """ALB health check — always 200 if the process is alive."""
    return jsonify({"status": "healthy", "tenant_type": TENANT_TYPE}), 200


@app.route("/ready")
def ready():
    """Readiness probe — checks DB connectivity before accepting traffic."""
    try:
        # Minimal DB ping: attempt to fetch secret and connect
        # (Full implementation would do SELECT 1)
        secret_arn = os.environ.get("DB_SECRET_ARN", "")
        if secret_arn:
            get_secret(secret_arn)   # Will throw if Secrets Manager unreachable
        return jsonify({"status": "ready"}), 200
    except Exception as e:
        app.logger.error("Readiness check failed: %s", str(e))
        return jsonify({"status": "not_ready", "error": str(e)}), 503


@app.route("/api/v1/payroll")
@require_tenant
def payroll_data():
    """
    Returns payroll records for the authenticated tenant.
    The tenant_id from the JWT is used as a WHERE clause filter —
    the query NEVER returns rows from other tenants.

    Example SQL (SQLAlchemy / psycopg2):
        SELECT * FROM payroll_records
        WHERE tenant_id = :tenant_id
        AND tenant_type = :tenant_type
        ORDER BY pay_date DESC
        LIMIT 50;
    """
    return jsonify({
        "tenant_id":   g.tenant_id,
        "tenant_type": g.tenant_type,
        "records":     [],    # Populated from DB in real implementation
        "message":     f"Payroll data for {g.tenant_type} tenant {g.tenant_id}",
    }), 200


@app.route("/api/v1/documents")
@require_tenant
def list_documents():
    """
    Lists documents from S3 under the tenant's own prefix.
    The IAM role on this EC2 instance already restricts S3 access to
    the correct prefix — this is a second enforcement boundary.
    """
    s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION", "eu-west-2"))
    bucket = os.environ.get("DOCS_BUCKET", "")
    prefix = f"{g.tenant_type}s/{g.tenant_id}/"

    try:
        resp = s3.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=100)
        keys = [obj["Key"] for obj in resp.get("Contents", [])]
        return jsonify({"prefix": prefix, "documents": keys}), 200
    except Exception as e:
        app.logger.error("S3 list failed: %s", str(e))
        return jsonify({"error": "could not list documents"}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
