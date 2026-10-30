# Phase 34: Incremental Multi-Signature PDF Signing

Phase 34 extends the direct Phase 28 PDF signing workflow so an already
signed, unencrypted PDF can receive one additional detached CMS signature in
a new output file. The additional signature is written as a **true
incremental update**: every byte of the source PDF remains an
exact byte prefix of the output.

## Direct workflow

No new command or registered tool is added. The existing direct commands
automatically choose incremental mode when the source already contains one
or more supported document signatures:

```text
pc-agent pdf sign first-signed.pdf --output second-signed.pdf --pkcs12 signer2.pfx
```

The GUI **Sign PDF** button uses the same controller and masked PKCS#12
password dialog. The PKCS#12 password/private key still never become a CLI
argument, task field, registered-tool argument, result field or log value.

The new signature may use an existing blank signature field or create a new
visible/invisible field, subject to the DocMDP restrictions below. Signing a
field that already has a signature is refused.

## Preservation gate

Before signing, every existing document signature must be supported and
cryptographically valid under the local Phase 27 verifier. Invalid,
malformed, unsupported or document-timestamp (`ETSI.RFC3161`) fields stop the
operation and no output is published.

The temporary output is published only after all of the following verify:

- the output begins with the complete, unchanged source revision bytes;
- each previous signature keeps its original ByteRange, CMS/Contents sizes,
  DocMDP permission and signer-certificate fingerprints;
- each previous signature verifies again over its original signed revision;
- the new signature covers the whole output and verifies against the
  selected PKCS#12 certificate;
- page count and expected form-field set are unchanged except for one
  intentionally created signature field;
- the source file hash did not change during signing.

The result reports `incremental_update`, the number of existing signatures,
how many valid signatures were preserved, the final signed-signature count
and the preserved source-revision byte count.

## DocMDP rules

- An existing **P=1** certification prohibits every change, including
  another signature.
- An existing **P=2** certification permits signing an existing blank
  signature field, but this implementation refuses to create a new field.
- An existing **P=3** certification permits an existing blank field or a
  newly created signature field/annotation.
- A DocMDP certification signature must be first, so `--certify` is refused
  when any signature already exists.

## Honest scope

This is one additional signature per direct command; repeat the command with
a new output for a third or later signer. It adds no registered tool, task,
dynamic-tool/provider authority, schema change, dependency, trust evaluation,
PAdES baseline/LTV profile, Adobe validation or legal-validity claim.

Phase 34 does **not** create a DocTimeStamp-only incremental revision and
refuses to append after an existing `ETSI.RFC3161` document timestamp. RFC
3161 signature timestamps inside a new CMS signature remain the separate
Phase 30 option. Certificate trust, revocation, policy/name constraints and
system anchors remain the direct Phase 29–33 validation workflow.
