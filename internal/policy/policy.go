// Package policy defines the custom license policy model and JSON loading.
//
// A policy names the licenses, exceptions and license-with-exception
// combinations an organisation allows or denies. Identifiers are matched
// exactly (case-sensitively), as SPDX ids are case-sensitive by spec.
package policy

import (
	"encoding/json"
	"fmt"
	"os"
	"strings"
)

// Policy is the full custom policy.
type Policy struct {
	// Name is purely informational.
	Name string `json:"name"`
	// Licenses maps a license id to "allow" or "deny". Anything absent is
	// unknown to the policy and must not be auto-accepted.
	Licenses map[string]string `json:"licenses"`
	// Exceptions maps an SPDX exception id to "allow" or "deny".
	Exceptions map[string]string `json:"exceptions"`
	// Combos expresses decisions for a specific "license WITH exception"
	// pair and overrides the separate license/exception decisions. Keys
	// use the form "SPDXID WITH EXCEPTION-ID".
	Combos map[string]string `json:"combos"`
}

// Load reads and validates a policy JSON file.
func Load(path string) (*Policy, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read policy file: %w", err)
	}
	var p Policy
	if err := json.Unmarshal(data, &p); err != nil {
		return nil, fmt.Errorf("parse policy file: %w", err)
	}
	if err := p.Validate(); err != nil {
		return nil, err
	}
	return &p, nil
}

// Validate checks that every decision value is allow/deny and that combo
// keys are well formed.
func (p *Policy) Validate() error {
	check := func(where, key, val string) error {
		if val != "allow" && val != "deny" {
			return fmt.Errorf("%s[%q]: decision must be \"allow\" or \"deny\", got %q", where, key, val)
		}
		return nil
	}
	for id, v := range p.Licenses {
		if err := check("licenses", id, v); err != nil {
			return err
		}
	}
	for id, v := range p.Exceptions {
		if err := check("exceptions", id, v); err != nil {
			return err
		}
	}
	for k, v := range p.Combos {
		if err := check("combos", k, v); err != nil {
			return err
		}
		parts := strings.SplitN(k, " WITH ", 2)
		if len(parts) != 2 || parts[0] == "" || parts[1] == "" || strings.Contains(parts[1], " WITH ") {
			return fmt.Errorf("combos[%q]: key must be \"LICENSE WITH EXCEPTION\"", k)
		}
	}
	return nil
}

// LeafDecision classifies a single license, optionally with a WITH
// exception. It returns "allow", "deny" or "unknown".
func (p *Policy) LeafDecision(licenseID, exception string) string {
	if exception != "" {
		comboKey := licenseID + " WITH " + exception
		if d, ok := p.Combos[comboKey]; ok {
			return d
		}
		// No explicit pair: both pieces must be individually allowed.
		// A denied piece is a hard deny; otherwise it is unknown.
		if p.Licenses[licenseID] == "deny" || p.Exceptions[exception] == "deny" {
			return "deny"
		}
		if p.Licenses[licenseID] == "allow" && p.Exceptions[exception] == "allow" {
			return "allow"
		}
		return "unknown"
	}
	if d, ok := p.Licenses[licenseID]; ok {
		return d
	}
	return "unknown"
}
