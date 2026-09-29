# Security Reporting

## Verification test

Before reporting a vulnerability, establish:

1. the attacker-controlled input or authority
2. the reachable vulnerable operation
3. the missing control or how an existing control can be bypassed
4. the concrete confidentiality, integrity, or availability impact
5. evidence in the reviewed revision

If any link is unknown, report it as an open question rather than a vulnerability.

## Severity

- **Critical:** practical compromise with exceptional blast radius, such as broad credential theft or unauthenticated control of a critical system.
- **High:** practical exploitation causing major unauthorized access, code execution, or sensitive data exposure.
- **Medium:** meaningful exploitation requiring constraints, prior access, or limited scope.
- **Low:** limited security impact with a credible attack path.

Best-practice hardening without a demonstrated attack path is not a vulnerability finding.

## Finding fields

For each finding include:

- severity and confidence
- affected location
- attacker preconditions
- evidence and attack path
- impact
- root-cause remediation
- validation needed

Order findings by severity, then confidence. Keep related symptoms under one root-cause finding.

## Remediation sequence

- **Now:** exploitable issues that block merge or release.
- **Next:** material risk reduction that needs coordinated work.
- **Later:** justified defense in depth.
