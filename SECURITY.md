# Security Policy

## Supported Versions

VirtValidate is in active development. Security updates are provided for the latest minor release only.

| Version | Supported          |
| ------- | ------------------ |
| 0.1.x   | :white_check_mark: |
| < 0.1   | :x:                |

## Reporting a Vulnerability

If you discover a security vulnerability in VirtValidate, please report it responsibly. **Do not open a public GitHub issue.**

### How to Report

Send details privately via GitHub's [Private Vulnerability Reporting](https://github.com/cmalafis/virtvalidation/security/advisories/new) feature, or email the maintainer directly at: chris@example.com (replace with your actual email)

Please include:

- A description of the vulnerability and its potential impact
- Steps to reproduce, or proof-of-concept code
- Affected versions
- Any suggested mitigations

### What to Expect

- Acknowledgment of your report within 72 hours
- An initial assessment within one week
- Regular updates on remediation progress
- Public disclosure coordinated with you after a fix is released
- Credit in the security advisory if desired

### Security Considerations

VirtValidate is designed for air-gapped, federal, and regulated environments. Security is foundational to the project. Areas of particular interest for security review:

- SSH key management and storage
- Container security (rootless Podman)
- LLM prompt injection via baseline data
- Audit log integrity
- Authentication and authorization (when added in future releases)

For the full security model and threat analysis, see [docs/SSH_SETUP.md](docs/SSH_SETUP.md).

### Known Security Limitations

VirtValidate v0.1.x has known security limitations being addressed in upcoming releases:

- SSH private key stored unencrypted on disk (encryption planned for v0.2.0)
- No authentication on the dashboard (multi-tenancy/RBAC planned for v1.0.0)
- Single SSH key for all VMs (per-environment separation planned for v2.0.0)

These are documented and on the roadmap. Do not deploy v0.1.x in environments where these are unacceptable risks.

## Disclosure Policy

We follow a coordinated disclosure model:

1. Reporter submits vulnerability privately
2. Maintainers confirm and develop a fix
3. Fix is released as a patch version
4. Security advisory is published with credit to the reporter
5. CVE is requested for significant vulnerabilities

Thank you for helping keep VirtValidate and its users safe.
