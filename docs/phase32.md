# Phase 32 — Certificate Policies and Name Constraints (0.34.0)

Phase 32 strengthens direct PDF signature-trust validation with a bounded
RFC 5280-style certificate-policy graph and X.509 name-constraint evaluation.
It remains direct CLI/GUI functionality only; no registered tool, task type,
AI-planner surface or dynamic-tool capability was added.

## Certificate policies

For every signer and RFC 3161 TSA chain, the validator now reports:

- `policy_status`: `valid`, `invalid`, `not_evaluated` or `not_applicable`;
- `policy_oids`: policy OIDs accepted by the bounded policy graph;
- a human-readable policy detail.

Supported policy processing includes:

- explicit policy OIDs and `anyPolicy`;
- caller-required signer policy OIDs;
- separate caller-required TSA policy OIDs;
- an explicit-policy requirement;
- `InhibitAnyPolicy` and caller inhibition of any-policy wildcard processing;
- `PolicyConstraints` (`requireExplicitPolicy` and
  `inhibitPolicyMapping` counters);
- DER `PolicyMappings` parsing, including rejection of mappings to or from
  `anyPolicy`, plus caller inhibition of policy mapping.

Policy qualifiers are ignored. As in RFC 5280 path validation, a chain with
no remaining policy graph is not rejected merely for that reason unless
explicit policy is required by the caller, by an in-chain policy constraint,
or because the caller supplied required policy OIDs.

## Name constraints

Name constraints are always evaluated for certificates below the CA that
issued the constraint. Supported GeneralName forms are DNS name, RFC 822
name, URI host, IP address/network, directory name, registered OID and exact
`otherName` values. Subject directory names and subject email attributes are
also considered.

Unsupported name forms fail closed if they cannot be proven to satisfy an
active constraint. The top-level and per-signature summaries expose
`name_constraints_status`, and violations can produce:

- `trusted_chain_policy_invalid`
- `trusted_chain_name_constraints_invalid`

These statuses are never accepted as `trusted_valid`; the existing
`--accept-revocation-unknown` option does not bypass them.

## CLI and GUI

New direct `pc-agent pdf validate-signatures` options:

- `--required-policy OID` (repeatable/comma-separated)
- `--timestamp-required-policy OID` (repeatable/comma-separated)
- `--explicit-policy`
- `--inhibit-any-policy`
- `--inhibit-policy-mapping`

The GUI **Validate Signature Trust** panel has signer/TSA policy-OID fields
and matching checkboxes. Name constraints do not have an off switch.

## Explicit non-goals

- No operating-system, browser or Adobe trust-store integration.
- No complete WebPKI/CA-Browser-Forum policy engine and no legal,
  qualified-signature or jurisdiction-specific policy decision.
- No PAdES/LTV validation or signature creation changes.
- No policy-qualifier interpretation.
- Automated tests use a local synthetic PKI; they do not claim evaluation by
  Adobe Acrobat, a public CA program or an external qualified validator.
