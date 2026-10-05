permit(principal, action, resource)
unless { action == Action::"submit_decision" };

permit(principal, action == Action::"submit_decision", resource)
when {
  context.input.decision == context.session.requested &&
  (context.input.decision != "approve" || (context.session.met && !context.session.broken))
};
