# Security

## Secrets

Do not commit GitHub personal access tokens, server credentials, database URLs,
SSH keys, or panel cookies. GitHub Actions uses the short-lived repository
`GITHUB_TOKEN` with job-level least privilege.

The external upstream source is compiled in a job with `contents: read`. Release
publishing and state commits run in separate jobs that do not execute upstream
code.

## Reporting

Open a private security advisory in this repository for vulnerabilities in the
builder. Report upstream product vulnerabilities to the corresponding upstream
project.
