# Security Policy

## Reporting a vulnerability

Do not open a public issue for a vulnerability involving credentials, order execution, wallet authorization, or financial loss. Use GitHub's private security-advisory workflow for this repository and include reproduction steps, affected versions, and impact. Do not include real private keys or API secrets.

## Supported versions

Security fixes are applied to the latest release. Operators should upgrade promptly after reviewing release notes and running the test suite in their own environment.

## Credential handling

- Store secrets only in the ignored local `.env` file or an approved secrets manager.
- Never commit private keys, API credentials, passphrases, cookies, or wallet exports.
- Never paste secrets into logs, screenshots, issues, pull requests, or support messages.
- Use a dedicated wallet with only the capital required for the deployment.
- Rotate credentials immediately if exposure is suspected.
- Review token approvals and revoke access that is no longer needed.

## Operational security

- Live trading requires the explicit `--live` flag.
- Treat `--yes` as an automation control, not a safety check.
- Run one writer per ledger and isolate each bot's credentials and state.
- Restrict filesystem access to `.env` and `data/`.
- Keep dependencies and the host operating system patched.
- Verify venue, RPC, and forecast endpoints before changing network configuration.

## Scope

Reports about ordinary market loss, forecast error, thin liquidity, or expected price movement are not security vulnerabilities unless they demonstrate a software defect that bypasses documented controls.
