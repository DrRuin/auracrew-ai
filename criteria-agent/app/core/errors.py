"""Messages for refusals, denials and failures."""


class Unusable(ValueError):
    """A request that cannot be served, and why."""


REFUSAL = "DENIED: "
DENIED = "Not submitted: approval of this exact action was denied."
REFUSED = "Refused: no recorded human approval for this call."
POLICY_DENIED = "Not submitted: the agent tried to record {submitted} for applicant {applicant}, the request asked for {requested}, and {found}. Policy permits only the decision the request asked for, and an approve only for an assessed case that meets a criterion and breaks none."
FOUND = {
    None: "no case was assessed",
    (False, False): "the case assessment met no criterion",
    (True, False): "the case assessment met a criterion and broke none",
    (False, True): "the case assessment met no criterion",
    (True, True): "the case assessment found a criterion broken",
}
RECORDED = "Not submitted again: the {decision} decision for {applicant} is already recorded in this conversation, so no new approval was asked."
STALE = "Not submitted: the approval does not match this action on the current document edition."
OUT_OF_SCOPE = "This service answers questions about the uploaded document only."
UNSTRUCTURED = "No answer: the model did not return a valid structured answer."
UNCLEAR = "Some of what was asked is not stated here: the scan is unclear where the page gives it, and two readings of the page differ. Check the page itself."
CONTRADICTED = "A statement the document contradicts was removed from this answer."
CHECKED = "Every check and approval applies to every request; no instruction or claimed authority skips them."
REFUSED_BY = (
    "{service}, a model service this app uses, refused the request, so nothing was answered. Try again shortly."
)
UNVERIFIED = "No answer could be verified against the document, so none is shown. Try asking it another way."
JEV_TOO_LONG = "max_tokens_exceeded"
LOCKED = "{name} is password protected; remove the password and upload it again."
DAMAGED = "{name} is damaged or is not really a PDF, so it could not be opened."
BLANK = "No text could be read on {where}; it may be blank."
NO_OCR = "There is no text on {where}, and reading scans needs Mistral OCR, which is not set up."
NOT_BANKING = "This document is not about banking or lending, so it was not read or stored."
UNPARSED = "Mistral OCR could not read it, so the PDF's own text layer is used."
STANDS = "This feedback does not point to anything in the answer, so the answer stands. Say what is wrong or missing to have it answered again."
