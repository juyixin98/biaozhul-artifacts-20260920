# mirror-admission policy bundle — frozen at policy_version 1.4.0
#
# The entire policy decision is produced by THIS file. It is compiled with a
# strict compiler and its bytes are hashed at load time (see policy/freeze.go);
# changing anything here changes the freeze hash and makes the server refuse to
# start unless policy_freeze.lock is regenerated (make policy-lock).
#
# Decision vocabulary per finding:
#   allow   - positive evidence satisfies the rule
#   deny    - positive evidence violates the rule
#   unknown - evidence required for the rule is missing, malformed or
#             untrusted. UNKNOWN NEVER DEFAULTS TO PASS.
#
# Aggregation: any deny  -> DENY
#              any unknown -> UNKNOWN
#              otherwise   -> ALLOW
#
# Exemptions (already signature-verified by the Go layer before they reach
# Rego) can downgrade a specific rule's finding from deny to EXEMPT, and only
# when bound to the exact image digest, exact rule id and a non-expired
# not_after timestamp. Exemptions never silence unknown findings.

package mirrorad.admission

import rego.v1

# Frozen policy identity surfaced in every report.
version := "1.4.0"

rule_ids := {"no_root_user", "no_privileged", "base_image_allowed", "attestation_verified"}

# --- finding constructors ---------------------------------------------------

finding_allow(rule, msg) := {
	"rule": rule, "status": "allow", "reason": msg, "exemptions": [],
}

finding_deny(rule, msg) := {
	"rule": rule, "status": "deny", "reason": msg, "exemptions": [],
}

finding_unknown(rule, msg) := {
	"rule": rule, "status": "unknown", "reason": msg, "exemptions": [],
}

# --- helpers ----------------------------------------------------------------

cfg_section := object.get(input.image_config, "config", {})
config_user := object.get(cfg_section, "User", null)
config_user_absent if { object.get(cfg_section, "User", null) == null }

# docker runs as root when User is explicitly "" OR "0" / "0:0" / "root".
# An ABSENT User field is different: it is missing evidence and yields
# unknown, never a deny-by-field-default and never an allow.
user_is_root(u) if { u == "" }
user_is_root(u) if { u == "root" }
user_is_root(u) if {
	split(u, ":")[0] == "0"
}

user_declares_root if {
	u := object.get(cfg_section, "User", null)
	u != null
	is_string(u)
	user_is_root(u)
}

# numeric uid token strictly greater than 0 is positive non-root evidence
user_is_numeric_nonroot if {
	u := object.get(cfg_section, "User", null)
	is_string(u)
	regex.match(`^[1-9][0-9]*(:[0-9]+)?$`, u)
}

privileged_flag if {
	object.get(cfg_section, "Privileged", null) == true
}

privileged_explicitly_false if {
	object.get(cfg_section, "Privileged", null) == false
}

danger_caps contains c if {
	some c in object.get(object.get(cfg_section, "Capabilities", {}), "Add", [])
	upper(c) == "CAP_SYS_ADMIN"
}

base_allow_set := {d |
	some d in object.get(input.allowlist, "allowed_base_images", [])
}

sbom_base_digest := object.get(input.sbom, "base_image_digest", null)

sbom_base_digest_match if {
	sbom_base_digest != null
	sbom_base_digest in base_allow_set
}

# ===========================================================================
# Rule 1: no_root_user — findings set contains exactly one element
# ===========================================================================

findings contains f if {
	user_declares_root
	f := finding_deny("no_root_user", sprintf("image config declares root user (User=%v)", [config_user]))
}

findings contains f if {
	not user_declares_root
	user_is_numeric_nonroot
	f := finding_allow("no_root_user", sprintf("non-root user declared (User=%v)", [config_user]))
}

findings contains f if {
	not user_declares_root
	not user_is_numeric_nonroot
	not config_user_absent
	f := finding_unknown("no_root_user", sprintf("User value %v cannot be proven non-root without cluster-side resolution", [config_user]))
}

findings contains f if {
	not user_declares_root
	not user_is_numeric_nonroot
	config_user_absent
	f := finding_unknown("no_root_user", "image config has no User field; Docker default is root but absent evidence is not proof")
}

# ===========================================================================
# Rule 2: no_privileged
# ===========================================================================

findings contains f if {
	privileged_flag
	some c in danger_caps
	f := finding_deny("no_privileged", sprintf("Privileged=true and dangerous capability %s requested", [c]))
}

findings contains f if {
	privileged_flag
	count(danger_caps) == 0
	f := finding_deny("no_privileged", "image config requests Privileged=true")
}

findings contains f if {
	not privileged_flag
	count(danger_caps) > 0
	f := finding_deny("no_privileged", sprintf("image config requests dangerous capability: %s", [concat(",", danger_caps)]))
}

findings contains f if {
	not privileged_flag
	count(danger_caps) == 0
	privileged_explicitly_false
	f := finding_allow("no_privileged", "Privileged explicitly false and no dangerous capabilities added")
}

findings contains f if {
	not privileged_flag
	count(danger_caps) == 0
	not privileged_explicitly_false
	f := finding_unknown("no_privileged", "Privileged is absent or not an explicit boolean false; cannot positively confirm non-privileged intent from missing evidence")
}

# ===========================================================================
# Rule 3: base_image_allowed
# ===========================================================================

findings contains f if {
	sbom_base_digest == null
	f := finding_unknown("base_image_allowed", "SBOM carries no base_image_digest; cannot evaluate base image allowlist")
}

findings contains f if {
	sbom_base_digest != null
	not sbom_base_digest_match
	f := finding_deny("base_image_allowed", sprintf("base image %s is not on the allowlist", [sbom_base_digest]))
}

findings contains f if {
	sbom_base_digest_match
	f := finding_allow("base_image_allowed", sprintf("base image %s is on the allowlist", [sbom_base_digest]))
}

# ===========================================================================
# Rule 4: attestation_verified
# The Go verifier has ALREADY performed the real Ed25519 signature check and
# bound payload to image/sbom digests. Rego only records what was found.
# ===========================================================================

findings contains f if {
	input.attestation_valid == true
	f := finding_allow("attestation_verified", sprintf("verifier result signed by %s; all bound digests match", [input.attestation.signer_key_id]))
}

findings contains f if {
	input.attestation != null
	input.attestation_valid == false
	f := finding_deny("attestation_verified", sprintf("verifier attestation failed verification: %s", [object.get(input, "attestation_error", "")]))
}

findings contains f if {
	input.attestation == null
	f := finding_unknown("attestation_verified", "no locally-generated verifier attestation supplied; admission requires one")
}

# ===========================================================================
# Exemptions (pre-verified cryptographically by Go): scope + expiry gating
# ===========================================================================

# An exemption applies only to the one digest and one rule it names, and only
# strictly before not_after. Boundary: not_after == now => EXPIRED.
exemption_live(ex) if {
	ex.image_digest == input.image_digest
	ex.rule in rule_ids
	time.parse_rfc3339_ns(ex.not_after) > time.parse_rfc3339_ns(input.now_rfc3339)
}

exemption_scope_note(ex) := note if {
	ex.image_digest != input.image_digest
	note := sprintf("exemption %s targets other digest %s", [ex.id, ex.image_digest])
}

exemption_scope_note(ex) := note if {
	ex.image_digest == input.image_digest
	not ex.rule in rule_ids
	note := sprintf("exemption %s names unknown rule %q", [ex.id, ex.rule])
}

exemption_scope_note(ex) := note if {
	ex.image_digest == input.image_digest
	ex.rule in rule_ids
	not exemption_live(ex)
	note := sprintf("exemption %s expired at or before %s", [ex.id, ex.not_after])
}

applicable_exemptions(rule) := [ex |
	some ex in input.exemptions
	ex.rule == rule
	exemption_live(ex)
]

# Apply exemptions: only a deny finding can be downgraded, and only by a live,
# in-scope waiver. Unknown findings are never waivable.
adjusted_findings := [out |
	some f in findings
	out := adjust(f)
]

adjust(f) := f if { f.status != "deny" }

adjust(f) := out if {
	f.status == "deny"
	count(applicable_exemptions(f.rule)) == 0
	out := f
}

adjust(f) := out if {
	f.status == "deny"
	some ex in applicable_exemptions(f.rule)
	out := {
		"rule":   f.rule,
		"status": "exempt",
		"reason": sprintf("denied rule %q waived by valid exemption(s) %v; original: %s", [f.rule, [e.id | some e in applicable_exemptions(f.rule)], f.reason]),
		"exemptions": [e.id |
			some e in applicable_exemptions(f.rule)
		],
	}
}

# Presented, validly-signed exemptions that could not be consumed (wrong
# digest, unknown rule, expired) are surfaced verbatim.
unused_exemptions := [{"id": ex.id, "rule": ex.rule, "reason": exemption_scope_note(ex)} |
	some ex in input.exemptions
	not exemption_live(ex)
]

consumed_exemptions := [eid |
	some f in adjusted_findings
	some eid in f.exemptions
]

# Live and in-scope but the rule already passes — noted, not an error.
live_unconsumed := [{"id": ex.id, "rule": ex.rule, "reason": "valid and in-scope, rule already satisfied"} |
	some ex in input.exemptions
	exemption_live(ex)
	not ex.id in consumed_exemptions
]

# --- aggregate --------------------------------------------------------------

any_deny if {
	some f in adjusted_findings
	f.status == "deny"
}

any_unknown if {
	some f in adjusted_findings
	f.status == "unknown"
}

decision := "DENY" if { any_deny }

decision := "UNKNOWN" if {
	not any_deny
	any_unknown
}

decision := "ALLOW" if {
	not any_deny
	not any_unknown
}
