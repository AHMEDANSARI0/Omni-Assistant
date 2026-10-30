"""Certificate-policy and name-constraint checks for PDF trust chains.

Phase 32 adds a bounded RFC 5280-style policy/name-constraint subset to the
Phase 29 direct PDF validator. It intentionally does not claim to be a
complete WebPKI path validator: policy qualifiers are ignored, and an empty
valid-policy graph remains acceptable unless explicit policy (or one or more
caller-required policy OIDs) is in force. Name constraints are fail-closed
for supported name forms and for unsupported forms that cannot be proven to
satisfy an active constraint.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.x509.oid import ExtensionOID, NameOID

from ..errors import ToolError
from .pdf_signatures import _DerError, _oid_string, _parse_der_root

ANY_POLICY_OID = "2.5.29.32.0"
MAX_REQUIRED_POLICY_OIDS = 20
MAX_CERTIFICATE_POLICIES = 100
MAX_POLICY_MAPPINGS = 100
MAX_NAME_CONSTRAINT_SUBTREES = 100
MAX_CERTIFICATE_NAMES = 100


class _PolicyProblem(ValueError):
    """A certificate policy/name-constraint encoding or value problem."""


def _validate_policy_oid(value: str) -> str:
    candidate = value.strip()
    if not candidate or len(candidate) > 128:
        raise ToolError("Certificate policy OID is empty or too long.")
    parts = candidate.split(".")
    if len(parts) < 2 or len(parts) > 32:
        raise ToolError(f"Invalid certificate policy OID: {candidate}")
    numbers: list[int] = []
    for part in parts:
        if not part.isdigit() or str(int(part)) != part:
            raise ToolError(f"Invalid certificate policy OID: {candidate}")
        number = int(part)
        if number > 9_007_199_254_740_991:
            raise ToolError(f"Invalid certificate policy OID: {candidate}")
        numbers.append(number)
    if numbers[0] > 2 or (numbers[0] < 2 and numbers[1] > 39):
        raise ToolError(f"Invalid certificate policy OID: {candidate}")
    return ".".join(str(item) for item in numbers)


def parse_policy_oids(values: Sequence[str] | str | None) -> tuple[str, ...]:
    """Parse repeatable/comma-separated/line-separated policy OID inputs."""

    if values is None:
        return ()
    raw_items: list[str] = []
    if isinstance(values, str):
        raw_items.extend(values.replace(",", "\n").splitlines())
    else:
        for value in values:
            raw_items.extend(str(value).replace(",", "\n").splitlines())
    result: list[str] = []
    for item in raw_items:
        item = item.strip()
        if not item:
            continue
        oid = _validate_policy_oid(item)
        if oid not in result:
            result.append(oid)
        if len(result) > MAX_REQUIRED_POLICY_OIDS:
            raise ToolError(
                f"Too many required certificate policy OIDs; the limit is "
                f"{MAX_REQUIRED_POLICY_OIDS}."
            )
    return tuple(result)


@dataclass(frozen=True, slots=True)
class CertificatePolicyOptions:
    required_policy_oids: tuple[str, ...] = ()
    explicit_policy_required: bool = False
    inhibit_any_policy: bool = False
    inhibit_policy_mapping: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "required_policy_oids",
            parse_policy_oids(self.required_policy_oids),
        )


@dataclass(frozen=True, slots=True)
class ChainConstraintEvaluation:
    policy_status: str
    policy_detail: str
    policy_oids: tuple[str, ...] = ()
    name_constraints_status: str = "not_evaluated"
    name_constraints_detail: str = ""


@dataclass(slots=True)
class _PolicyNode:
    valid_policy: str
    expected: set[str] = field(default_factory=set)
    initial: set[str] = field(default_factory=set)


def _extension(certificate: x509.Certificate, extension_type: Any) -> Any | None:
    try:
        return certificate.extensions.get_extension_for_class(extension_type).value
    except x509.ExtensionNotFound:
        return None
    except (x509.DuplicateExtension, ValueError) as exc:
        raise _PolicyProblem(
            f"certificate {certificate.subject.rfc4514_string()} has a malformed "
            "or duplicated policy/name-constraint extension"
        ) from exc


def _certificate_policies(certificate: x509.Certificate) -> tuple[str, ...] | None:
    extension = _extension(certificate, x509.CertificatePolicies)
    if extension is None:
        return None
    policies = [
        policy.policy_identifier.dotted_string for policy in list(extension)
    ]
    if len(policies) > MAX_CERTIFICATE_POLICIES:
        raise _PolicyProblem("certificate contains too many certificate policies")
    # Duplicate policy identifiers are redundant in the bounded graph below.
    return tuple(dict.fromkeys(policies))


def _policy_mappings(
    certificate: x509.Certificate,
) -> tuple[tuple[str, str], ...]:
    try:
        extension = certificate.extensions.get_extension_for_oid(
            ExtensionOID.POLICY_MAPPINGS
        )
    except x509.ExtensionNotFound:
        return ()
    except (x509.DuplicateExtension, ValueError) as exc:
        raise _PolicyProblem("certificate has a malformed PolicyMappings extension") from exc
    value = extension.value
    raw = getattr(value, "value", None)
    if not isinstance(raw, (bytes, bytearray)):
        # A future cryptography PolicyMappings object would need an explicit
        # adapter. Fail closed rather than guessing at an unknown structure.
        raise _PolicyProblem("PolicyMappings extension uses an unsupported encoding")
    try:
        root = _parse_der_root(bytes(raw))
    except _DerError as exc:
        raise _PolicyProblem("PolicyMappings extension is malformed DER") from exc
    if root.tag_class != 0 or root.tag != 16 or not root.constructed:
        raise _PolicyProblem("PolicyMappings extension is not a DER sequence")
    if len(root.children) > MAX_POLICY_MAPPINGS:
        raise _PolicyProblem("certificate contains too many policy mappings")
    mappings: list[tuple[str, str]] = []
    for child in root.children:
        if (
            child.tag_class != 0
            or child.tag != 16
            or not child.constructed
            or len(child.children) != 2
        ):
            raise _PolicyProblem("PolicyMappings entry is malformed")
        try:
            issuer_policy = _oid_string(child.children[0])
            subject_policy = _oid_string(child.children[1])
        except _DerError as exc:
            raise _PolicyProblem("PolicyMappings entry contains a malformed OID") from exc
        if ANY_POLICY_OID in {issuer_policy, subject_policy}:
            raise _PolicyProblem("PolicyMappings must not map to or from anyPolicy")
        mappings.append((issuer_policy, subject_policy))
    return tuple(mappings)


def _add_node(
    nodes: dict[str, _PolicyNode],
    *,
    valid_policy: str,
    expected: Iterable[str],
    initial: Iterable[str],
) -> None:
    node = nodes.get(valid_policy)
    if node is None:
        nodes[valid_policy] = _PolicyNode(
            valid_policy=valid_policy,
            expected=set(expected),
            initial=set(initial),
        )
        return
    node.expected.update(expected)
    node.initial.update(initial)


def _initial_for_specific(parent: _PolicyNode, policy: str) -> set[str]:
    if policy in parent.initial:
        return {policy}
    if ANY_POLICY_OID in parent.initial:
        return {ANY_POLICY_OID}
    return set()


def _next_policy_frontier(
    parents: dict[str, _PolicyNode],
    certificate_policies: tuple[str, ...],
    *,
    allow_any_policy: bool,
) -> dict[str, _PolicyNode]:
    result: dict[str, _PolicyNode] = {}
    explicit_policies = {
        policy for policy in certificate_policies if policy != ANY_POLICY_OID
    }
    for policy in explicit_policies:
        matching = [node for node in parents.values() if policy in node.expected]
        if matching:
            for parent in matching:
                _add_node(
                    result,
                    valid_policy=policy,
                    expected={policy},
                    initial=_initial_for_specific(parent, policy),
                )
            continue
        any_parent = parents.get(ANY_POLICY_OID)
        if any_parent is not None and allow_any_policy:
            # Retain the caller-initial set on this branch: a PolicyMappings
            # extension in this certificate may map it to a required policy
            # for the next certificate.
            _add_node(
                result,
                valid_policy=policy,
                expected={policy},
                initial=set(any_parent.initial),
            )

    if ANY_POLICY_OID in certificate_policies and allow_any_policy:
        for parent in parents.values():
            for expected in parent.expected:
                if expected in explicit_policies:
                    continue
                if expected == ANY_POLICY_OID:
                    _add_node(
                        result,
                        valid_policy=ANY_POLICY_OID,
                        expected={ANY_POLICY_OID},
                        initial=set(parent.initial),
                    )
                else:
                    _add_node(
                        result,
                        valid_policy=expected,
                        expected={expected},
                        initial=_initial_for_specific(parent, expected),
                    )
    return result


def _apply_policy_mappings(
    nodes: dict[str, _PolicyNode],
    mappings: tuple[tuple[str, str], ...],
    *,
    allowed: bool,
) -> dict[str, _PolicyNode]:
    if not mappings:
        return nodes
    grouped: dict[str, set[str]] = {}
    for issuer_policy, subject_policy in mappings:
        grouped.setdefault(issuer_policy, set()).add(subject_policy)
    if not allowed:
        return {
            policy: node
            for policy, node in nodes.items()
            if policy not in grouped
        }
    result = dict(nodes)
    any_node = result.get(ANY_POLICY_OID)
    for issuer_policy, subject_policies in grouped.items():
        node = result.get(issuer_policy)
        if node is not None:
            node.expected = set(subject_policies)
        elif any_node is not None:
            _add_node(
                result,
                valid_policy=issuer_policy,
                expected=set(subject_policies),
                initial=set(any_node.initial),
            )
    return result


def _decrease_counter(value: int | None) -> int | None:
    return None if value is None else max(0, value - 1)


def _minimum_counter(current: int | None, candidate: int | None) -> int | None:
    if candidate is None:
        return current
    return candidate if current is None else min(current, candidate)


def _counter_active(value: int | None) -> bool:
    return value == 0


def _evaluate_policies(
    path_root_to_leaf: tuple[x509.Certificate, ...],
    options: CertificatePolicyOptions,
) -> tuple[str, str, tuple[str, ...]]:
    if not path_root_to_leaf:
        if options.required_policy_oids:
            return "invalid", "no certificate is available for policy evaluation", ()
        return "valid", "no non-anchor certificate requires policy evaluation", ()

    required = options.required_policy_oids
    initial_set = set(required) if required else {ANY_POLICY_OID}
    frontier: dict[str, _PolicyNode] = {
        ANY_POLICY_OID: _PolicyNode(
            valid_policy=ANY_POLICY_OID,
            expected=set(initial_set),
            initial=set(initial_set),
        )
    }
    explicit_remaining = 0 if options.explicit_policy_required else None
    mapping_remaining = 0 if options.inhibit_policy_mapping else None
    any_remaining = 0 if options.inhibit_any_policy else None
    saw_certificate_policies = False

    for index, certificate in enumerate(path_root_to_leaf):
        is_leaf = index == len(path_root_to_leaf) - 1
        policies = _certificate_policies(certificate)
        mappings = _policy_mappings(certificate)
        if policies is None:
            frontier = {}
        else:
            saw_certificate_policies = True
            allow_any = not _counter_active(any_remaining) or is_leaf
            frontier = _next_policy_frontier(
                frontier,
                policies,
                allow_any_policy=allow_any,
            )
            frontier = _apply_policy_mappings(
                frontier,
                mappings,
                allowed=not _counter_active(mapping_remaining),
            )

        if not is_leaf and certificate.subject != certificate.issuer:
            explicit_remaining = _decrease_counter(explicit_remaining)
            mapping_remaining = _decrease_counter(mapping_remaining)
            any_remaining = _decrease_counter(any_remaining)

        policy_constraints = _extension(certificate, x509.PolicyConstraints)
        if policy_constraints is not None:
            explicit_remaining = _minimum_counter(
                explicit_remaining,
                policy_constraints.require_explicit_policy,
            )
            mapping_remaining = _minimum_counter(
                mapping_remaining,
                policy_constraints.inhibit_policy_mapping,
            )
        inhibit_any = _extension(certificate, x509.InhibitAnyPolicy)
        if inhibit_any is not None:
            any_remaining = _minimum_counter(any_remaining, inhibit_any.skip_certs)

    explicit_required = (
        bool(required)
        or options.explicit_policy_required
        or _counter_active(explicit_remaining)
    )
    if not frontier:
        if explicit_required:
            return (
                "invalid",
                "no valid certificate-policy graph remains while explicit policy is required",
                (),
            )
        detail = (
            "certificate policies were evaluated, but no explicit policy was required"
            if saw_certificate_policies
            else "no certificate policies are present and none are required"
        )
        return "valid", detail, ()

    accepted: set[str] = set()
    required_set = set(required)
    for node in frontier.values():
        if not required:
            accepted.add(node.valid_policy)
            continue
        if node.valid_policy == ANY_POLICY_OID:
            accepted.update(node.initial & required_set)
        elif node.valid_policy in required_set and (
            node.valid_policy in node.initial or ANY_POLICY_OID in node.initial
        ):
            accepted.add(node.valid_policy)

    if required and not accepted:
        return (
            "invalid",
            "the valid certificate-policy graph does not contain a required policy OID",
            tuple(sorted(frontier)),
        )
    accepted_tuple = tuple(sorted(accepted))
    if required:
        detail = "the certificate-policy graph contains a required policy OID"
    elif accepted_tuple == (ANY_POLICY_OID,):
        detail = "the certificate-policy graph resolves to anyPolicy"
    else:
        detail = "the certificate-policy graph is valid"
    return "valid", detail, accepted_tuple


@dataclass(frozen=True, slots=True)
class _GeneralNameValue:
    kind: str
    value: Any
    display: str


def _uri_host(value: str) -> str:
    candidate = value.strip().lower()
    if candidate.startswith("."):
        return candidate.rstrip(".")
    parsed = urlsplit(candidate)
    if parsed.hostname:
        return parsed.hostname.rstrip(".")
    # Some name-constraint encodings use a bare host rather than a full URI.
    return candidate.split("/", 1)[0].rstrip(".")


def _general_name_value(name: Any) -> _GeneralNameValue:
    if isinstance(name, x509.DNSName):
        return _GeneralNameValue(
            "dns", str(name.value).strip().lower().rstrip("."), str(name.value)
        )
    if isinstance(name, x509.RFC822Name):
        return _GeneralNameValue(
            "email", str(name.value).strip().lower(), str(name.value)
        )
    if isinstance(name, x509.UniformResourceIdentifier):
        return _GeneralNameValue(
            "uri", _uri_host(str(name.value)), str(name.value)
        )
    if isinstance(name, x509.IPAddress):
        return _GeneralNameValue("ip", name.value, str(name.value))
    if isinstance(name, x509.DirectoryName):
        return _GeneralNameValue(
            "directory", name.value, name.value.rfc4514_string()
        )
    if isinstance(name, x509.RegisteredID):
        return _GeneralNameValue(
            "oid", name.value.dotted_string, name.value.dotted_string
        )
    if isinstance(name, x509.OtherName):
        return _GeneralNameValue(
            f"other:{name.type_id.dotted_string}",
            bytes(name.value),
            f"otherName {name.type_id.dotted_string}",
        )
    return _GeneralNameValue(
        f"unsupported:{type(name).__name__}",
        None,
        f"unsupported GeneralName {type(name).__name__}",
    )


def _directory_is_within(candidate: x509.Name, constraint: x509.Name) -> bool:
    try:
        candidate_root = _parse_der_root(candidate.public_bytes())
        constraint_root = _parse_der_root(constraint.public_bytes())
    except (_DerError, TypeError, ValueError):
        return False
    if len(constraint_root.children) > len(candidate_root.children):
        return False
    return all(
        candidate_child.raw == constraint_child.raw
        for candidate_child, constraint_child in zip(
            candidate_root.children,
            constraint_root.children,
            strict=False,
        )
    )


def _as_network(
    value: Any,
) -> ipaddress.IPv4Network | ipaddress.IPv6Network | None:
    try:
        if isinstance(value, (ipaddress.IPv4Network, ipaddress.IPv6Network)):
            return value
        if isinstance(value, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
            return ipaddress.ip_network(value)
    except ValueError:
        return None
    return None


def _dns_matches(candidate: str, constraint: str) -> bool:
    candidate = candidate.lower().rstrip(".")
    constraint = constraint.lower().rstrip(".")
    if constraint.startswith("."):
        return candidate.endswith(constraint) and candidate != constraint[1:]
    return candidate == constraint or candidate.endswith("." + constraint)


def _email_matches(candidate: str, constraint: str) -> bool:
    if "@" in constraint:
        return candidate == constraint
    domain = candidate.rsplit("@", 1)[-1] if "@" in candidate else candidate
    if constraint.startswith("."):
        return domain.endswith(constraint) and domain != constraint[1:]
    return domain == constraint


def _uri_matches(candidate_host: str, constraint_host: str) -> bool:
    if constraint_host.startswith("."):
        return candidate_host.endswith(constraint_host) and candidate_host != (
            constraint_host[1:]
        )
    return candidate_host == constraint_host


def _name_matches(candidate: _GeneralNameValue, constraint: _GeneralNameValue) -> bool:
    if candidate.kind != constraint.kind:
        return False
    if candidate.kind == "dns":
        return _dns_matches(str(candidate.value), str(constraint.value))
    if candidate.kind == "email":
        return _email_matches(str(candidate.value), str(constraint.value))
    if candidate.kind == "uri":
        return _uri_matches(str(candidate.value), str(constraint.value))
    if candidate.kind == "ip":
        candidate_network = _as_network(candidate.value)
        constraint_network = _as_network(constraint.value)
        if candidate_network is None or constraint_network is None:
            return False
        if candidate_network.version != constraint_network.version:
            return False
        return candidate_network.subnet_of(constraint_network)
    if candidate.kind == "directory":
        return _directory_is_within(candidate.value, constraint.value)
    if candidate.kind == "oid":
        candidate_oid = str(candidate.value)
        constraint_oid = str(constraint.value)
        return candidate_oid == constraint_oid or candidate_oid.startswith(
            constraint_oid + "."
        )
    if candidate.kind.startswith("other:"):
        return candidate.value == constraint.value
    return False


def _certificate_names(
    certificate: x509.Certificate,
) -> tuple[_GeneralNameValue, ...]:
    names: list[_GeneralNameValue] = []
    if len(certificate.subject) > 0:
        names.append(
            _GeneralNameValue(
                "directory",
                certificate.subject,
                certificate.subject.rfc4514_string(),
            )
        )
    for attribute in certificate.subject.get_attributes_for_oid(NameOID.EMAIL_ADDRESS):
        value = str(attribute.value)
        names.append(_GeneralNameValue("email", value.strip().lower(), value))

    san = _extension(certificate, x509.SubjectAlternativeName)
    if san is not None:
        try:
            general_names = list(san)
        except (TypeError, ValueError, x509.UnsupportedGeneralNameType) as exc:
            raise _PolicyProblem(
                "certificate contains an unsupported subject alternative name"
            ) from exc
        if len(general_names) > MAX_CERTIFICATE_NAMES:
            raise _PolicyProblem("certificate contains too many alternative names")
        names.extend(_general_name_value(item) for item in general_names)
    if len(names) > MAX_CERTIFICATE_NAMES:
        raise _PolicyProblem("certificate contains too many names")
    return tuple(names)


def _subtree_values(
    values: Iterable[Any] | None,
) -> tuple[_GeneralNameValue, ...]:
    if values is None:
        return ()
    items = list(values)
    if len(items) > MAX_NAME_CONSTRAINT_SUBTREES:
        raise _PolicyProblem("certificate contains too many name-constraint subtrees")
    converted = tuple(_general_name_value(item) for item in items)
    if any(item.kind.startswith("unsupported:") for item in converted):
        raise _PolicyProblem("name constraints contain an unsupported GeneralName type")
    return converted


def _evaluate_name_constraints(
    chain_leaf_to_anchor: tuple[x509.Certificate, ...],
) -> tuple[str, str]:
    if not chain_leaf_to_anchor:
        return "not_evaluated", "no certificate chain is available"
    active: list[
        tuple[str, tuple[_GeneralNameValue, ...], tuple[_GeneralNameValue, ...]]
    ] = []
    saw_constraints = False

    for position, certificate in enumerate(reversed(chain_leaf_to_anchor)):
        if position > 0 and active:
            for candidate in _certificate_names(certificate):
                if candidate.kind.startswith("unsupported:"):
                    detail = (
                        "a certificate name form cannot be proven to satisfy "
                        "the active name constraints"
                    )
                    return "invalid", detail
                for source, permitted, excluded in active:
                    if any(
                        _name_matches(candidate, constraint)
                        for constraint in excluded
                        if constraint.kind == candidate.kind
                    ):
                        detail = (
                            f"certificate name {candidate.display} is in an "
                            f"excluded subtree from {source}"
                        )
                        return "invalid", detail
                    permitted_for_kind = [
                        item for item in permitted if item.kind == candidate.kind
                    ]
                    if permitted_for_kind and not any(
                        _name_matches(candidate, constraint)
                        for constraint in permitted_for_kind
                    ):
                        detail = (
                            f"certificate name {candidate.display} is outside "
                            f"the permitted subtree from {source}"
                        )
                        return "invalid", detail

        constraints = _extension(certificate, x509.NameConstraints)
        if constraints is None:
            continue
        saw_constraints = True
        permitted = _subtree_values(constraints.permitted_subtrees)
        excluded = _subtree_values(constraints.excluded_subtrees)
        if not permitted and not excluded:
            raise _PolicyProblem("NameConstraints contains no subtrees")
        active.append(
            (
                certificate.subject.rfc4514_string()[:200],
                permitted,
                excluded,
            )
        )

    if saw_constraints:
        return "valid", "all certificate names satisfy the active name constraints"
    return "valid", "no name constraints are present in the chain"


def evaluate_chain_constraints(
    chain_leaf_to_anchor: tuple[x509.Certificate, ...],
    *,
    anchor_fingerprints: set[str] | frozenset[str] = frozenset(),
    options: CertificatePolicyOptions | None = None,
) -> ChainConstraintEvaluation:
    """Evaluate certificate policies and NC for a leaf-to-anchor chain."""

    policy_options = options or CertificatePolicyOptions()
    if not chain_leaf_to_anchor:
        return ChainConstraintEvaluation(
            policy_status="not_evaluated",
            policy_detail="no certificate chain is available",
            name_constraints_status="not_evaluated",
            name_constraints_detail="no certificate chain is available",
        )

    last = chain_leaf_to_anchor[-1]
    last_is_anchor = (
        last.fingerprint(hashes.SHA256()).hex() in anchor_fingerprints
    )
    policy_path_leaf_to_root = (
        chain_leaf_to_anchor[:-1]
        if last_is_anchor and len(chain_leaf_to_anchor) > 1
        else chain_leaf_to_anchor
    )
    path_root_to_leaf = tuple(reversed(policy_path_leaf_to_root))

    try:
        policy_status, policy_detail, policy_oids = _evaluate_policies(
            path_root_to_leaf, policy_options
        )
    except _PolicyProblem as exc:
        policy_status, policy_detail, policy_oids = "invalid", str(exc), ()

    try:
        name_status, name_detail = _evaluate_name_constraints(
            chain_leaf_to_anchor
        )
    except _PolicyProblem as exc:
        name_status, name_detail = "invalid", str(exc)

    return ChainConstraintEvaluation(
        policy_status=policy_status,
        policy_detail=policy_detail,
        policy_oids=policy_oids,
        name_constraints_status=name_status,
        name_constraints_detail=name_detail,
    )
