# APN Innovation Sandbox – Innovation Plan

**CGI – March 24, 2026 – FAST (AI-Assisted Forms Testing)**

---

## 1  Project Overview

### 1.1  Project Summary

**FAST (AI-Assisted Forms Testing)** is an enterprise-grade, AI-powered platform developed by CGI to automate the validation and regression testing of PDF-based business forms used in the insurance and financial services industries. The platform eliminates manual QA effort associated with form releases by intelligently comparing form versions, detecting visual and content differences, and validating forms against regulatory compliance standards (WCAG 2.1 AA, Section 508, Internal Policy).

**Business Objectives:**
The core business driver is to migrate the AI inference layer of FAST from direct third-party API calls (Google Gemini, OpenAI) to **Amazon Bedrock**, enabling CGI and its clients to process sensitive insurance and financial forms within a secure, compliant, and auditable AWS environment. This supports enterprise requirements around data residency, cost governance, and end-to-end traceability that direct API integrations cannot satisfy.

**Technical Objectives:**
The innovation effort will integrate Amazon Bedrock as a first-class LLM provider within the FAST engine, run the full application stack on AWS (ECS Fargate + RDS + S3), and benchmark performance and cost against the current architecture. The result will be a production-ready, cloud-native deployment that customers can adopt with confidence in security and scalability.

**Customer Benefit:**
Insurance carriers and financial institutions using FAST will gain a fully auditable, AWS-hosted testing utility that validates form releases in minutes rather than days, reduces human error, and produces compliance-ready evidence bundles — all within their existing AWS governance perimeter.

---

### 1.2  Project Details

| Field | Value |
|---|---|
| **Project Start Date** | April 1, 2026 |
| **Project End Date** | June 30, 2026 |
| **Design Win Industry / Vertical** | Insurance / Financial Services |
| **Market Segment Focus** | Enterprise Digital Transformation – Document Intelligence & Compliance QA |
| **Success Criteria** | 1. Amazon Bedrock (Claude 3.x via Bedrock) integrated as a native LLM provider in FAST and all existing test cases pass with ≥95% result parity vs. current Gemini/OpenAI baseline. 2. Full application stack deployed on AWS (ECS Fargate, RDS PostgreSQL, S3). 3. PDF form processing throughput ≥ current baseline with Bedrock inference latency ≤ 30 s per form page. 4. End-to-end audit trail for all AI-generated findings stored in AWS CloudWatch and S3. 5. Total monthly AWS spend within approved Sandbox budget ceiling. |
| **Optional – Marketplace Listing** | Under consideration for AWS Marketplace listing as a SaaS offering post-sandbox validation |
| **AWS Pricing Calculator Link** | *(To be provided by CGI team — estimate 3 months of Bedrock, ECS Fargate, RDS, S3, and CloudWatch usage)* |

---

### 1.3  Design Win / Joint Solution Development Plan

| Activity | # of Days | Delivery Date |
|---|---|---|
| **AWS environment setup** – VPC, IAM roles, ECS cluster, RDS PostgreSQL, S3 buckets, CloudWatch log groups | 3 | Apr 4, 2026 |
| **Amazon Bedrock provider integration** – Add `_run_bedrock()` to `engine/llm_client.py`; support Claude 3 Sonnet / Haiku via `boto3` Bedrock Runtime; wire `LLM_PROVIDER=bedrock` in `.env` | 4 | Apr 10, 2026 |
| **S3 form storage migration** – Replace local `instance/uploads/` with S3 bucket (`fast-forms-{env}`); update upload/download routes in `app/routes/web.py` and `app/routes/api.py` | 3 | Apr 15, 2026 |
| **Containerisation & ECS Fargate deployment** – Dockerfile, `docker-compose.prod.yml`, ECS task definition, ALB configuration, environment variable injection via AWS Secrets Manager | 4 | Apr 22, 2026 |
| **RDS PostgreSQL migration** – Replace SQLite with RDS PostgreSQL; update `SQLALCHEMY_DATABASE_URI`; validate all models and migrations | 2 | Apr 25, 2026 |
| **Performance & cost benchmarking** – Run full test suite (Basic / Specific / Benchmark modes) against Bedrock; compare latency, token cost, and finding accuracy vs. Gemini/OpenAI baseline | 3 | May 2, 2026 |
| **Compliance & audit hardening** – Route all AI inference logs to CloudWatch; generate S3-backed evidence bundles per run; validate WCAG 2.1 / Section 508 compliance pipeline end-to-end | 3 | May 9, 2026 |
| **Security review** – IAM least-privilege audit, Bedrock model access policies, S3 bucket policies, ALB WAF rules, Secrets Manager rotation | 2 | May 14, 2026 |
| **UAT with CGI project team** – End-to-end user acceptance testing on AWS-hosted instance using real insurance form sets (LC_32_425, 90104, P5105080/81 series) | 5 | May 23, 2026 |
| **Documentation & runbook** – AWS deployment guide, Bedrock configuration reference, cost optimisation recommendations, handoff to operations | 3 | May 29, 2026 |
| **Final review & sign-off** – Joint CGI / AWS solution review, cost reconciliation, Marketplace listing assessment | 2 | Jun 4, 2026 |

**Total: 34 days across 3 months**

---

### 1.4  Solution Architecture / Architectural Diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│                          AWS Cloud (VPC)                            │
│                                                                     │
│  ┌──────────┐    ┌─────────────────────────────────────────────┐   │
│  │  Users   │───▶│         Application Load Balancer           │   │
│  └──────────┘    └──────────────────┬──────────────────────────┘   │
│                                     │                               │
│                    ┌────────────────▼────────────────┐             │
│                    │     ECS Fargate (FAST Flask App) │             │
│                    │  • Web routes   (app/routes/)    │             │
│                    │  • REST API     (app/routes/api) │             │
│                    │  • Scheduler    (APScheduler)    │             │
│                    └──┬──────────────────┬────────────┘             │
│                       │                  │                          │
│          ┌────────────▼──┐    ┌──────────▼──────────┐              │
│          │  RDS           │    │  S3 Buckets          │              │
│          │  PostgreSQL    │    │  • fast-forms        │              │
│          │  (fast.db →    │    │  • fast-reports      │              │
│          │   Postgres)    │    │  • fast-evidence     │              │
│          └───────────────┘    └─────────────────────┘              │
│                                                                     │
│                    ┌────────────────────────────────┐              │
│                    │      Amazon Bedrock             │              │
│                    │  LLM_PROVIDER = bedrock         │              │
│                    │  Models:                        │              │
│                    │  • Claude 3 Sonnet (default)    │              │
│                    │  • Claude 3 Haiku  (fast mode)  │              │
│                    │                                 │              │
│                    │  Called by: engine/llm_client   │              │
│                    │  via boto3 bedrock-runtime       │              │
│                    └────────────────────────────────┘              │
│                                                                     │
│   ┌──────────────────┐    ┌───────────────────────────┐            │
│   │  Secrets Manager │    │  CloudWatch Logs           │            │
│   │  • LLM API keys  │    │  • App logs                │            │
│   │  • DB password   │    │  • Bedrock inference logs  │            │
│   │  • SECRET_KEY    │    │  • Audit trail per run     │            │
│   └──────────────────┘    └───────────────────────────┘            │
└─────────────────────────────────────────────────────────────────────┘

Data Flow:
  User uploads PDF form → S3 (fast-forms)
       ↓
  FAST engine extracts fields (engine/extractor.py)
       ↓
  Prompt built (engine/prompt_builder.py)
       ↓
  Amazon Bedrock (Claude 3) validates form fields → findings JSON
       ↓
  Results stored in RDS PostgreSQL + HTML report written to S3 (fast-reports)
       ↓
  Evidence bundle (PDF + findings + visual diffs) archived to S3 (fast-evidence)
       ↓
  Audit log entry written to CloudWatch + RDS audit_log table
```

**AWS Services Used:**

| Service | Purpose |
|---|---|
| Amazon Bedrock | LLM inference for AI-powered form validation (Claude 3 Sonnet/Haiku) |
| Amazon ECS Fargate | Serverless container hosting for the FAST Flask application |
| Amazon RDS (PostgreSQL) | Persistent storage for projects, forms, test cases, runs, results, audit logs |
| Amazon S3 | Storage for uploaded PDF forms, generated HTML reports, and evidence bundles |
| AWS Secrets Manager | Secure injection of API keys and credentials into ECS tasks |
| Amazon CloudWatch | Application logging, Bedrock inference audit trail, cost monitoring |
| AWS ALB | HTTPS load balancing and routing to ECS Fargate tasks |
| AWS IAM | Role-based access control for Bedrock, S3, RDS, and Secrets Manager |
