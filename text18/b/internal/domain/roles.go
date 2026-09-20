package domain

const (
	RoleAnalyst   = "analyst"
	RoleResponder = "responder"
	RoleAdmin     = "admin"
)

// Roles allowed to perform the transition INTO each stage. Admins are not
// listed per-entry because they are granted every action.
//
//	detected -> triaged      : analysts run triage
//	disposal progression      : responders drive containment..recovery
//	recovery -> postmortem    : analysts write the postmortem
//	postmortem -> closed      : responders close once gates pass
var transitionRoles = map[string]map[string]bool{
	StageTriaged:    {RoleAnalyst: true},
	StageContained:  {RoleResponder: true},
	StageEradicated: {RoleResponder: true},
	StageRecovered:  {RoleResponder: true},
	StagePostmortem: {RoleAnalyst: true},
	StageClosed:     {RoleResponder: true},
}

// CanTransition reports whether a role may perform from -> to.
func CanTransition(role, from, to string) bool {
	if role == RoleAdmin {
		return true
	}
	allowed, ok := transitionRoles[to]
	if !ok {
		return false
	}
	return allowed[role] && NextStage(from) == to
}

// Evidence is an analyst responsibility; responders may read it.
func CanAddEvidence(role string) bool {
	return role == RoleAnalyst || role == RoleAdmin
}

// Only admins assign people to an incident.
func CanAssign(role string) bool {
	return role == RoleAdmin
}
