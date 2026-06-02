# FAST Platform — IT Assessment & Governance Document

**Prepared by:** CGI  
**Prepared for:** Liberty  
**Document Version:** 1.0  
**Date:** June 2, 2026  
**Classification:** Confidential

---

## Table of Contents

1. Executive Summary
2. What is FAST?
3. Key Features & Capabilities
4. How the Tool Works
5. How the Tool Will Be Accessed
6. Governance Framework
7. Restrictions & Limitations
8. Data Management
9. Security Controls
10. Data Protection Measures
11. Infrastructure & Deployment Architecture
12. Cost Estimate
13. Summary of Controls

---

## 1. Executive Summary

FAST (Form Analysis and Scrutiny Tool) is an AI-powered document comparison and review platform developed by CGI. It is designed to automate the review of regulatory, compliance, and operational documents by intelligently identifying differences, anomalies, missing content, and signature discrepancies between two versions of a document.

This document is prepared for Liberty's internal IT review team to provide a comprehensive view of how FAST operates, how it will be deployed within Liberty's environment, the controls and governance in place, data management options, security posture, and associated costs.

FAST is built for deployment within Liberty's own AWS cloud infrastructure, meaning **all data — documents, results, and AI processing — remains under Liberty's direct control at all times.**

---

## 2. What is FAST?

FAST is a web-based internal tool that allows authorized users to:

- Upload two versions of a document (Baseline vs. Current)
- Automatically compare them using AI-powered analysis
- Receive a structured report of all differences, anomalies, and missing content
- Review, annotate, and approve findings
- Export results as PDF reports or ZIP archives of annotated differences
- Maintain a full audit trail of all review activity

FAST is not a SaaS product and does not rely on any third-party hosted service for document processing. It is a self-hosted platform deployed entirely within the client's cloud environment.

---

## 3. Key Features & Capabilities

### 3.1 Document Comparison Engine
- Compares PDF documents page-by-page using both text extraction and visual (image-based) analysis
- Detects text changes, field-level differences, table row additions/deletions, and formatting shifts
- AI-powered semantic understanding — distinguishes meaningful changes from formatting noise

### 3.2 Signature & Approval Detection
- Automatically detects presence or absence of signatures, stamps, and approval marks
- Named role detection: identifies PRESIDENT, SECRETARY, AUTHORIZED SIGNATORY, WITNESS, NOTARY
- Reports missing signatures with page number, role, and confidence level

### 3.3 OCR & Scanned Document Support
- Handles both digital and scanned PDF documents
- Optical Character Recognition (OCR) built-in for image-based pages
- AI vision pipeline processes page images directly when text extraction is insufficient

### 3.4 Structured Findings Report
Each comparison produces a structured report containing:
- **Observations**: Page-level findings with change type, confidence, and description
- **Missing Content**: Fields, text blocks, or elements present in Baseline but absent in Current
- **Summary**: High-level overview of document changes
- **Metadata**: Document name, page count, file size, comparison timestamp

### 3.5 Review & Approval Workflow
- Findings can be marked as Reviewed, Approved, or flagged as False Positives
- Comment threads on individual findings
- Multi-user review with role-based assignments

### 3.6 Audit Trail
- Every action (upload, comparison, review, approval, export, deletion) is logged
- Audit logs include user identity, timestamp, and action details
- Logs are immutable and stored separately from application data

### 3.7 Project & User Management
- Documents organized into Projects
- Role-based access: Admin, Reviewer, Viewer
- Project-level membership control — users only see documents in their assigned projects

### 3.8 API Access
- REST API available for integration with internal systems (e.g., iQA, Press'n GO)
- API key authentication with per-key permission scoping
- Webhook support for event-driven integrations

### 3.9 Compliance Standards
- Configurable compliance frameworks per project
- Field inventory management for tracking required document fields
- Scheduled automated runs for recurring review cycles

### 3.10 Export & Reporting
- PDF export of full comparison reports
- ZIP download of visual diff images
- CSV/JSON export of findings for downstream processing

---

## 4. How the Tool Works

### 4.1 High-Level Workflow

```
User uploads Baseline PDF + Current PDF
         ↓
Document Parser extracts text, tables, and structure
         ↓
Semantic Diff identifies field-level and structural changes
         ↓
Vision Pipeline renders page images → sends to AI model
         ↓
AI model analyzes BASELINE/CURRENT page pairs visually
         ↓
Reconciler cross-checks pixel-level diffs with AI findings
         ↓
Structured findings report generated
         ↓
Users review, annotate, and approve findings
         ↓
Results exported or archived
```

### 4.2 AI Processing

FAST uses AWS Bedrock (Claude via Amazon) as its AI backbone in the Liberty deployment. Key characteristics:

- **No data leaves AWS**: All AI inference runs through AWS Bedrock within Liberty's VPC. Traffic never touches the public internet.
- **IAM Role Authentication**: No API keys stored in the application. Access controlled via AWS Identity and Access Management.
- **Model used**: Anthropic Claude (Sonnet class) via Bedrock — a state-of-the-art multimodal model capable of reading text and images.
- **Temperature = 0**: AI is configured for deterministic, reproducible output — not creative or generative.

### 4.3 Document Processing Steps

| Step | What Happens | Technology |
|------|-------------|------------|
| 1. Upload | PDF stored in S3 (encrypted at rest) | AWS S3 + SSE-S3 |
| 2. Parse | Text, tables, structure extracted | LlamaParse / PyMuPDF |
| 3. Semantic Diff | Field-level comparison computed | Python + structured JSON |
| 4. Vision | Pages rendered as images | pdf2image / PyMuPDF |
| 5. AI Analysis | Images sent to Claude via Bedrock | AWS Bedrock (VPC endpoint) |
| 6. Reconcile | Pixel diff + AI findings merged | Deterministic reconciler |
| 7. Store | Results saved to database | AWS RDS (PostgreSQL) |
| 8. Display | Report rendered in web UI | Flask + HTML/CSS |

---

## 5. How the Tool Will Be Accessed

### 5.1 Access Method
FAST is accessed via a standard web browser (Chrome, Edge, Firefox) through an internal URL. No desktop software installation is required on end-user machines.

### 5.2 Authentication Options
- **SSO Integration**: FAST supports integration with corporate identity providers via SAML 2.0 / OAuth 2.0 (e.g., Azure Active Directory, Okta)
- **Local accounts**: Username/password with bcrypt hashing as fallback
- **MFA**: Multi-factor authentication enforced at the identity provider level when SSO is configured

### 5.3 Network Access
- Deployed within Liberty's AWS VPC — accessible only via internal network or VPN
- No public internet exposure required
- Traffic between users and FAST served over HTTPS (TLS 1.2 minimum)
- Optional: AWS WAF (Web Application Firewall) placed in front of the application load balancer

### 5.4 User Roles

| Role | Capabilities |
|------|-------------|
| **Admin** | Full access: manage users, projects, settings, audit logs |
| **Reviewer** | Upload documents, run comparisons, review and annotate findings |
| **Viewer** | View reports and findings only, no upload or edit rights |
| **API User** | Programmatic access via API key, scoped to specific actions |

---

## 6. Governance Framework

### 6.1 Ownership
- **Application Owner**: CGI (development and maintenance)
- **Data Owner**: Liberty — all documents, results, and user data belong to Liberty
- **Infrastructure Owner**: Liberty AWS account — CGI has no access to production infrastructure unless explicitly granted for support

### 6.2 Change Management
- All application updates go through a defined release process
- Changes are tested in a staging environment before production deployment
- Liberty IT team approves production deployments
- Release notes provided for every update

### 6.3 Access Reviews
- User access reviews conducted quarterly
- Dormant accounts (90 days inactive) flagged for deactivation
- Admin access limited to named individuals with documented justification

### 6.4 Incident Management
- Security and availability incidents reported to Liberty within defined SLA windows
- Incident log maintained and shared with Liberty on request
- Post-incident reviews for P1/P2 incidents

### 6.5 Vendor & Third-Party Dependencies

| Component | Vendor | Data Exposure | Notes |
|-----------|--------|---------------|-------|
| Claude AI | Anthropic (via AWS Bedrock) | None — stays in Liberty's AWS | AWS manages the Bedrock API, Anthropic never sees Liberty data |
| LlamaParse | LlamaIndex (optional) | Document content | Can be disabled; PyMuPDF used as local alternative |
| PostgreSQL | AWS RDS | Internal only | Managed by Liberty AWS account |
| Document Storage | AWS S3 | Internal only | Encrypted, in Liberty's account |

**Note on LlamaParse**: If Liberty requires zero third-party data exposure, LlamaParse can be disabled and replaced entirely with the local PyMuPDF parser. This eliminates all external API calls.

---

## 7. Restrictions & Limitations

### 7.1 Supported Document Types
- **Supported**: PDF (digital and scanned)
- **Not supported**: Word (.docx), Excel (.xlsx), images (.jpg/.png) as standalone uploads
- Future roadmap includes multi-format support

### 7.2 Document Size Limits
- Default maximum file size: **50 MB per document**
- Maximum pages processed: **Unlimited** (configurable per deployment)
- Very large documents (200+ pages) will have longer processing times

### 7.3 AI Model Limitations
- AI analysis is probabilistic — findings include confidence levels (certain / probable / possible)
- Handwritten text recognition accuracy varies with scan quality
- Very low resolution scans (below 150 DPI) may reduce accuracy
- AI does not make approval decisions — all findings require human review

### 7.4 Concurrency
- Default: supports up to 10 concurrent comparison jobs
- Scalable horizontally via AWS ECS — additional capacity added as needed

### 7.5 Language Support
- Optimized for English-language documents
- French and other languages supported at reduced accuracy (AI model dependent)

### 7.6 Browser Support
- Supported: Chrome 110+, Edge 110+, Firefox 110+
- Not supported: Internet Explorer

---

## 8. Data Management

### 8.1 What Data is Stored

| Data Type | Storage Location | Retention |
|-----------|-----------------|-----------|
| Uploaded PDF documents | AWS S3 (Liberty account) | Configurable |
| Comparison results & findings | AWS RDS PostgreSQL | Configurable |
| Audit logs | AWS RDS + optional S3 archive | Minimum 1 year recommended |
| User accounts | AWS RDS PostgreSQL | Until deactivated |
| Session data | Server memory / Redis | 24 hours (session timeout) |

### 8.2 Retention Policies — Liberty Has Full Control

Liberty can configure the following retention rules directly within the application:

- **Document auto-deletion**: Set a retention period (e.g., 30, 60, 90 days) after which uploaded documents are automatically deleted from S3
- **Result retention**: Comparison results can be retained independently of source documents
- **Manual deletion**: Admins can delete individual documents, runs, or entire projects at any time
- **Bulk purge**: Admin-level bulk deletion by project, date range, or user

All deletion operations are logged in the audit trail with the identity of the person who initiated them.

### 8.3 Data Residency
- All data stored in **Canada (ca-central-1)** AWS region by default
- Region is configurable at deployment time
- Data does not replicate to other regions unless Liberty explicitly configures cross-region backup

### 8.4 Backup & Recovery
- RDS automated daily snapshots with 7-day retention (configurable up to 35 days)
- S3 versioning enabled — deleted files recoverable within retention window
- Point-in-time recovery available for RDS

### 8.5 Data Classification
FAST does not classify documents automatically. Liberty is responsible for ensuring only appropriately classified documents are uploaded based on their internal data classification policy.

### 8.6 Right to Erasure
- Individual user data can be anonymized or deleted on request
- Full data export available for any project or user (GDPR/PIPEDA compliance support)

---

## 9. Security Controls

### 9.1 Application Security

| Control | Implementation |
|---------|---------------|
| Authentication | SSO (SAML/OAuth) or bcrypt password hashing |
| Session management | Secure HTTP-only cookies, 24h timeout |
| Input validation | All user inputs sanitized server-side |
| SQL injection prevention | ORM-based queries (SQLAlchemy) — no raw SQL |
| XSS prevention | Jinja2 templating with auto-escaping |
| CSRF protection | CSRF tokens on all state-changing forms |
| File upload validation | MIME type checking, file size limits, no executable uploads |
| Dependency scanning | Python packages scanned for known CVEs |

### 9.2 Infrastructure Security

| Control | Implementation |
|---------|---------------|
| Network isolation | All resources in private VPC subnets |
| Internet exposure | Load balancer only — application servers not public |
| AI traffic | Bedrock via VPC endpoint — never traverses public internet |
| Encryption in transit | TLS 1.2+ enforced on all connections |
| Encryption at rest | S3: SSE-S3 or SSE-KMS; RDS: AES-256 |
| IAM | Least-privilege roles — application has no AWS console access |
| Security groups | Strict inbound/outbound rules per service |
| WAF | AWS WAF with OWASP ruleset (optional, recommended) |

### 9.3 Secrets Management
- No API keys or credentials stored in application code or configuration files
- All secrets stored in AWS Secrets Manager or Parameter Store
- Database passwords rotated on a defined schedule
- IAM roles used for all AWS service-to-service authentication (no long-lived access keys)

### 9.4 Vulnerability Management
- Application dependencies reviewed and updated on each release cycle
- AWS Inspector enabled for container image scanning
- Penetration testing available on request prior to go-live

### 9.5 Logging & Monitoring

| Log Type | Tool | Retention |
|----------|------|-----------|
| Application audit logs | FAST built-in + CloudWatch | 1 year+ |
| Infrastructure logs | AWS CloudWatch | 90 days default |
| Access logs (ALB) | S3 | 90 days default |
| Security events | AWS CloudTrail | 1 year |
| Anomaly detection | AWS GuardDuty (optional) | Real-time alerting |

---

## 10. Data Protection Measures

### 10.1 Document Confidentiality
- Documents are stored in a private S3 bucket with no public access policy
- Pre-signed URLs used for temporary document access — expire after 15 minutes
- No documents are ever cached in CDN or public-facing caches

### 10.2 AI Data Handling
When using AWS Bedrock:
- Documents are sent to the Bedrock API **within Liberty's AWS account**
- AWS Bedrock does **not** use customer data for model training (per AWS service terms)
- Anthropic does **not** receive or store Liberty's documents
- All Bedrock traffic is contained within the AWS network via VPC endpoint

### 10.3 Data Isolation
- Each Liberty project is logically isolated within the database
- Users can only access data within their assigned projects
- Database-level row security ensures cross-project data leakage is not possible

### 10.4 Data Sovereignty
Liberty retains full sovereignty over all data:
- All infrastructure is in Liberty's own AWS account
- CGI has no persistent access to production data
- CGI access for support requires explicit, time-limited permission grant by Liberty Admin
- All CGI support access sessions are logged

### 10.5 Compliance Alignment
FAST is designed to support compliance with:
- **PIPEDA** (Personal Information Protection and Electronic Documents Act)
- **GDPR** principles (data minimization, right to erasure, data portability)
- **SOC 2 Type II** controls alignment (confidentiality, availability, processing integrity)
- **AWS Shared Responsibility Model** — Liberty owns data, AWS owns physical infrastructure

---

## 11. Infrastructure & Deployment Architecture

```
                        Liberty Users (Internal / VPN)
                                    │
                              [HTTPS / TLS]
                                    │
                         ┌──────────▼──────────┐
                         │  Application Load   │
                         │  Balancer (AWS ALB) │
                         │  + AWS WAF          │
                         └──────────┬──────────┘
                                    │
              ┌─────────────────────┼─────────────────────┐
              │         Liberty AWS VPC (Private Subnet)   │
              │                     │                      │
              │          ┌──────────▼──────────┐           │
              │          │   FAST Application  │           │
              │          │   (AWS ECS Fargate) │           │
              │          └──────┬──────┬───────┘           │
              │                 │      │                   │
              │    ┌────────────▼─┐  ┌─▼──────────────┐   │
              │    │  PostgreSQL  │  │   AWS S3        │   │
              │    │  (AWS RDS)   │  │  (Documents)    │   │
              │    └──────────────┘  └────────────────-┘   │
              │                     │                      │
              │          ┌──────────▼──────────┐           │
              │          │   AWS Bedrock        │           │
              │          │   (Claude via VPC    │           │
              │          │    Endpoint)         │           │
              │          └─────────────────────┘           │
              │                                            │
              │    ┌──────────────────────────────────┐    │
              │    │  CloudWatch │ CloudTrail │ KMS    │    │
              │    └──────────────────────────────────┘    │
              └────────────────────────────────────────────┘
```

### Infrastructure Components

| Component | AWS Service | Purpose |
|-----------|------------|---------|
| Compute | ECS Fargate | Containerized application — serverless, auto-scaling |
| Database | RDS PostgreSQL | Application data, audit logs, user management |
| Document Storage | S3 | PDF uploads and diff images |
| AI Inference | Bedrock (Claude) | Document analysis via VPC endpoint |
| Load Balancing | ALB | HTTPS termination, routing |
| Security | WAF, GuardDuty, KMS | Threat protection, encryption key management |
| Monitoring | CloudWatch, CloudTrail | Logs, metrics, audit |
| Secrets | Secrets Manager | Database passwords, API keys |
| DNS | Route 53 | Internal domain resolution |

---

## 12. Cost Estimate

All costs are in **CAD** and based on AWS Canada (ca-central-1) region pricing. Estimates assume moderate usage (20–50 document comparisons per day).

### 12.1 AWS Infrastructure (Monthly)

| Service | Specification | Estimated Monthly Cost |
|---------|--------------|----------------------|
| ECS Fargate | 2 vCPU, 4GB RAM, 2 tasks | ~$120 |
| RDS PostgreSQL | db.t3.medium, Multi-AZ | ~$180 |
| S3 Storage | 500 GB documents + lifecycle | ~$15 |
| ALB | Load balancer + data transfer | ~$25 |
| AWS Bedrock (Claude) | ~500 comparisons/month | ~$150–$400 |
| CloudWatch / CloudTrail | Logs and monitoring | ~$30 |
| KMS | Key management | ~$5 |
| Data Transfer | Internal + egress | ~$20 |
| **Total Infrastructure** | | **~$545–$795 / month** |

### 12.2 Bedrock (AI) Cost Detail

Claude Sonnet on Bedrock is billed per token (unit of text/image processed):
- Input: ~$3 per million tokens
- Output: ~$15 per million tokens
- A typical document comparison (10-page PDF): ~15,000–25,000 tokens
- At 500 comparisons/month: ~$150–$400/month in AI costs

**Cost scales linearly with usage** — more comparisons = higher AI cost; infrastructure cost is relatively fixed.

### 12.3 CGI Implementation & Support (One-Time + Ongoing)

| Item | Estimate |
|------|---------|
| Initial deployment & configuration | To be scoped |
| SSO integration | To be scoped |
| UAT support | To be scoped |
| Monthly managed support | To be scoped |

*CGI professional services costs to be provided in a separate Statement of Work.*

### 12.4 Cost Control Mechanisms
- **Auto-scaling**: Fargate scales down during off-hours — compute cost tracks actual usage
- **S3 Lifecycle policies**: Automatically move older documents to Glacier (cheaper tier) or delete per retention policy
- **Document limits**: Configurable per-project upload limits prevent runaway costs
- **AWS Cost Alerts**: Budget alerts configured to notify Liberty if monthly spend exceeds threshold

---

## 13. Summary of Controls

| Area | Control | Liberty Authority |
|------|---------|------------------|
| Data location | Canada AWS region | Liberty can specify region |
| Data retention | Configurable per project | Liberty Admin configures |
| Data deletion | Manual or automated | Liberty Admin initiates |
| User access | Role-based, SSO integrated | Liberty Admin manages |
| AI data exposure | Bedrock VPC endpoint — zero external exposure | Built into architecture |
| CGI access | Time-limited, logged, requires Liberty approval | Liberty grants/revokes |
| Audit logs | Immutable, 1 year+ | Liberty owns and can export |
| Encryption | At rest (AES-256) and in transit (TLS 1.2+) | AWS KMS keys owned by Liberty |
| Third-party parsers | LlamaParse optional — can be fully disabled | Liberty decides at deployment |
| Backup | Automated daily snapshots | Liberty configures schedule |

---

## Appendix A — Glossary

| Term | Definition |
|------|-----------|
| **Baseline** | The reference/original version of a document |
| **Current** | The version being reviewed for changes |
| **Bedrock** | AWS managed AI service — runs Claude without data leaving AWS |
| **VPC Endpoint** | Private network connection to AWS services — no public internet |
| **ECS Fargate** | AWS serverless container platform — no servers to manage |
| **IAM** | AWS Identity and Access Management — controls who can access what |
| **SSE-KMS** | Server-Side Encryption using AWS Key Management Service |
| **OCR** | Optical Character Recognition — reads text from scanned images |
| **PIPEDA** | Canada's federal private sector privacy law |

---

*This document is intended for Liberty's internal IT review purposes. Contents are confidential and proprietary to CGI and Liberty. For questions or clarifications, contact the CGI project team.*

**Document Control**

| Version | Date | Author | Changes |
|---------|------|--------|---------|
| 1.0 | June 2, 2026 | CGI | Initial release |
