"""Hostname / IP verification against SAN dNSName / iPAddress (RFC 6125)."""

from __future__ import annotations

import ipaddress
from typing import Optional

from cryptography import x509


def _dns_match(name: str, pattern: str) -> bool:
    name = name.rstrip(".").lower()
    pattern = pattern.rstrip(".").lower()
    if not name or not pattern:
        return False
    if "*" not in pattern:
        return name == pattern
    # Wildcards are honoured only in the left-most label, and match exactly
    # one label ("*.example.com" matches "a.example.com", never "a.b.example.com").
    labels = pattern.split(".")
    name_labels = name.split(".")
    if len(labels) != len(name_labels) or labels[0].count("*") != 1:
        return False
    prefix, _, suffix = labels[0].partition("*")
    if not name_labels[0] or not name_labels[0].startswith(prefix) or not name_labels[0].endswith(suffix):
        return False
    return name_labels[1:] == labels[1:]


def _ip_match(value: str, san_ip) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    packed = getattr(san_ip, "packed", None)
    return packed is not None and packed == address.packed


def check_hostname(
    leaf: x509.Certificate, hostname: Optional[str], *, allow_cn_fallback: bool = False
) -> str | None:
    """Return an error code string, or None when the hostname matches."""
    if not hostname:
        return None  # caller decides whether a hostname was required

    try:
        san = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        san = None

    if san is not None:
        # When a SAN extension exists the CN must not be consulted (RFC 6125).
        try:
            ip_address = ipaddress.ip_address(hostname)
        except ValueError:
            ip_address = None
        if ip_address is not None:
            if any(_ip_match(hostname, value) for value in san.get_values_for_type(x509.IPAddress)):
                return None
            return "HOSTNAME_MISMATCH"
        if any(_dns_match(hostname, value) for value in san.get_values_for_type(x509.DNSName)):
            return None
        return "HOSTNAME_MISMATCH"

    if not allow_cn_fallback:
        return "HOSTNAME_NO_SAN"

    for attribute in leaf.subject.get_attributes_for_oid(x509.oid.NameOID.COMMON_NAME):
        if _dns_match(hostname, attribute.value):
            return None
    return "HOSTNAME_MISMATCH"
