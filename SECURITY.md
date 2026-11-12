# Security Policy

## Deployment and sensitive data

Use Object Manager and the CLUs on a trusted local network. Restrict access to
their management services; do not forward Object Manager, UDP control, TCP/23
(Telnet), or UDP/69 (TFTP) to the internet. Object Manager imports use HTTP, and
Telnet logs and TFTP transfers are unencrypted. Native UDP encryption does not
replace network access controls.

Home Assistant's Grenton config entry contains the interface PIN and imported
encryption keys. Protect the Home Assistant configuration directory and backups.
The standalone CLI also stores keys in `~/.grenton/config.yaml` and its interface
cache. Newly saved CLI config/cache files use owner-only permissions (`0600`)
and atomic replacement. Previously exported copies and existing files that have
not been saved again retain their old permissions; restrict them separately.

Native logs, interface exports, packet captures and diagnostic dumps can contain
device names, network details, variable values or secrets. Inspect and redact
them before sharing. Automatic diagnostic capture is opt-in, stores files outside
the public `www` directory, and keeps up to five 4 MiB files per controller.
Telnet capture is opt-in and uses Home Assistant's log retention.

Interface downloads are capped at 32 MiB and the extracted interface JSON at
16 MiB. Only `mygrenton/data.json` is read from the ZIP; archive paths are not
extracted onto the filesystem. Error tracebacks suppress HTTP-client details
that could contain the PIN-bearing request URL.

Script actions, scene parameters and Lua-expression arguments intentionally
execute commands on the selected CLU. Grant Home Assistant control permissions
only to trusted users and review expressions imported from other sources.

## Reporting a Vulnerability

If you discover a security issue, we strongly encourage you to report it responsibly and privately.

### 📧 How to Report

Please send all vulnerability reports to:

[**konradobal@gmail.com**](mailto:konradobal@gmail.com)

Alternatively, you may open a **private security advisory** through the project’s GitHub repository:

[**https://github.com/k911/homeassistant-grenton/security/advisories**](https://github.com/k911/homeassistant-grenton/security/advisories)

### 🔒 Responsible Disclosure

Please **do not** publicly disclose the vulnerability before we have confirmed and addressed it.  
Early disclosure may put existing users at risk.

### 🛠 Security Issue Handling

Once a vulnerability is confirmed:

1. We assign a **severity rating** (Low / Medium / High / Critical)  
2. We begin **patch development** for the integration or its documentation
3. We prepare a **coordinated disclosure timeline** with you  
4. A fix is published along with a **security advisory**  
5. Credits are provided to reporters (optional and only with consent)

### 🧪 What to Include in Your Report

To help us investigate efficiently, please include:

- Description of the vulnerability  
- Steps to reproduce  
- Affected integration, Home Assistant and CLU firmware versions
- Expected behavior vs actual behavior  
- Impact assessment (if known)  
- Any proof-of-concept code, test scripts

### 🙏 Thank You

We appreciate the work of security researchers and users who help improve the safety of the project.  
Responsible disclosure ensures the project remains reliable in industrial and automation environments.
