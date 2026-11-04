# Security

## Reporting a vulnerability

Please report vulnerabilities through GitHub's private reporting: the **Security** tab
of this repository, then **Report a vulnerability**. Do not open a public issue.

Include what you found, how to reproduce it, and which version you tested.

## Current scope

This version only reads public chain data. It does not handle private keys or
mnemonics, and it cannot sign or send transactions.

Endpoint URLs and tokens are read from `.env` or the environment, and are masked in
logs and left out of error messages. Keep `.env` out of version control.
