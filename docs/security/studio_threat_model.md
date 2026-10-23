# Taramandal Studio Security & Threat Model

**Status:** V1 Baseline Audit & Hardening (Issue #130)  
**Parent Roadmap:** #122 (V1 Yukti/IIC Integrated Proof Demonstrator)  
**Scope:** Studio HTTP API, Project Vault, Signing Keys, Data Boundaries  

---

## 1. Executive Summary

Taramandal Studio is an authoring tool and choreography compiler for drone light shows. In V1, Studio operates primarily as a local designer tool running alongside or in proximity to the Ground Control Station (GCS). This document audits the attack surface, outlines the threat model, documents verified safeguards, and defines secure defaults for V1 and beyond.

---

## 2. Assets and Trust Boundaries

### 2.1 Critical Assets

| Asset | Location | Sensitivity | Threat Impact |
|---|---|---|---|
| **Project Vault Designs** | `~/.local/share/taramandal/projects/` | High (IP / Choreography data) | Unauthorized read/overwrite of customer shows, revision tampering |
| **Ed25519 Signing Private Key** | Configured via `STUDIO_SIGNING_KEY` | Critical | Forgery of `.sig.json` manifests, allowing unapproved trajectories to pass GCS upload gates |
| **Compiled Shows / Trajectories** | Memory / Export streams | High | Modification of waypoints could lead to flight safety violations |
| **Local System / Host Resources** | Host filesystem & process | High | Path traversal, arbitrary local file read, denial of service |

### 2.2 Trust Boundaries

1. **Host / Network Boundary:**
   - **Default:** Studio binds strictly to loopback (`127.0.0.1:8001`). It is not exposed to the public internet.
   - **Remote Access (Optional):** If bound to a non-loopback interface (`0.0.0.0`), it MUST sit behind an authenticated reverse proxy or SSH/VPN tunnel.

2. **Browser / Web Origin Boundary (CORS):**
   - The Studio web UI runs on a development origin (e.g. `http://localhost:5173`) or served statically.
   - Cross-Origin Resource Sharing (CORS) must never allow wildcard `*` with credentials enabled (`allow_credentials=True`), preventing unauthorized cross-origin sites from querying or tampering with the local Project Vault.

3. **Filesystem / Vault Boundary:**
   - Vault paths are derived from project IDs. Project IDs MUST be validated against a strict alphanumeric allowlist to prevent directory traversal (`../`).
   - Mesh/geometry imports MUST NOT allow reading arbitrary local files via absolute or relative paths.

4. **Signing Boundary:**
   - The private Ed25519 key NEVER leaves the server process.
   - `/api/signing` only ever surfaces the key ID (fingerprint) and public key.
   - Private key files must have strict POSIX permissions (`0600`).

---

## 3. Threat Analysis & Mitigations

### 3.1 Cross-Origin Data Exfiltration / Tampering (CORS)

- **Threat:** If an operator visits an untrusted website in the same browser, that site could make cross-origin requests to `http://localhost:8001/api/projects` to exfiltrate proprietary choreography or overwrite projects if CORS allows `*` with credentials.
- **Mitigation:**
  - Enforce explicit allowed origins via `STUDIO_ALLOWED_ORIGINS` (defaulting to `http://localhost:5173`, `http://127.0.0.1:5173`, `http://localhost:8001`, `http://127.0.0.1:8001`).
  - If wildcard `*` is explicitly set by an operator, disable `allow_credentials` to prevent authenticated credential sharing per W3C CORS standards.

### 3.2 Path Traversal in Project Vault

- **Threat:** Supplying malicious project IDs (e.g. `../../etc/passwd` or `../other_user`) to `GET /api/projects/{id}` or `PUT /api/projects/{id}` to access or overwrite host files.
- **Mitigation:**
  - `_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")` in `project_store.py`.
  - Rejection with `InvalidProject` (HTTP 422/400) if any slash, dot, or forbidden character is present.
  - Test-verified: Traversal payloads (`../../..`, `..%2F..`) fail closed.

### 3.3 Arbitrary Local File Disclosure via Geometry Importer

- **Threat:** The `/api/voxelize` endpoint accepts `source_type="mesh"` and `content`. Passing a filesystem path (e.g. `/etc/shadow`) to `trimesh.load(content)` could disclose file existence or trigger parse errors with sensitive contents.
- **Mitigation:**
  - Disallow direct filesystem path loading over the web API.
  - Require raw mesh data with explicit `file_type` (`"obj"` or `"stl"`), parsed entirely in-memory via `io.BytesIO`.

### 3.4 Untrusted SVG and XML Attacks (Billion Laughs / XSS)

- **Threat:** Uploaded SVGs could contain `<script>`, external entity references (`XXE`), or deeply nested entities (XML entity expansion / Billion Laughs) designed to consume memory or execute scripts.
- **Mitigation:**
  - SVGs are parsed strictly as geometry data using vector contour extraction, never rendered directly into the DOM as active HTML.
  - Request body and content sizes are bounded (`max_length=500_000` bytes).
  - Scripts, foreign objects, and event handlers are stripped or ignored by vector parsers.

### 3.5 Signing Key Protection

- **Threat:** Compromise or leakage of the operator's show-signing private key.
- **Mitigation:**
  - `tara_studio.signing` reads the key from `STUDIO_SIGNING_KEY` on demand; it is never held in frontend state, local storage, or network responses.
  - File permissions are validated (`mode & 0o077 == 0`). If group/other readable, signing is refused.
  - Only the public key and fingerprint are exposed via `/api/signing`.

---

## 4. Operational & Deployment Guidelines

1. **Local Execution (Default):**
   ```bash
   STUDIO_HOST=127.0.0.1 STUDIO_PORT=8001 python -m tara_studio.server
   ```
2. **Origins Configuration:**
   ```bash
   STUDIO_ALLOWED_ORIGINS="http://localhost:5173,http://127.0.0.1:5173"
   ```
3. **Key Generation & Permissions:**
   ```bash
   tara keygen ~/.config/taramandal/studio-signing.pem --signer "Yukti-V1"
   chmod 600 ~/.config/taramandal/studio-signing.pem
   ```
