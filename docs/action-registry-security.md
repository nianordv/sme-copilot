# Action registry: what it does and what it protects

This describes Phase 0, not the completed production security design.

## Plain-language explanation

The language model suggests operations. A separate server component decides
whether an operation is allowed before running it. An instruction produced by
the model is therefore a request, not permission.

The registry is a catalog of named operations. Each has a description, an
argument schema, a permission level and a Python handler. The handler is the
actual code that performs the operation. The model sees descriptions, not
Python functions or an arbitrary-code execution interface.

Only two fictional demo operations are currently registered. Neither reads
real financial records or sends an external message. The existing chat does
not call the registry yet; that connection belongs to B2.

## The three permission levels

| Level | What the server does | Example |
| --- | --- | --- |
| Green | Runs the registered handler immediately | Read receivables |
| Yellow | Stores a request and waits for confirmation | Simulate a reminder |
| Red | Returns a refusal without running the handler | A prohibited transfer |

Developers assign these levels in server code. The model cannot supply a new
level through execute(). A Green label is a developer decision, not an
automatic guarantee that a handler is harmless; real handlers need review.

## Why confirmations are safer than conversational approval

For a Yellow action the server copies the requested arguments and stores them
with the action and session ID under a random confirmation ID. Changing the
original arguments afterward cannot change the stored request.

confirm() accepts that ID, a real Boolean approval and the session ID; it does
not accept replacement action arguments. Another session is rejected without
consuming the owner's pending request. An owner rejection consumes it without
running anything.

When approved, the server consumes the pending request under a lock before
running its handler. Two simultaneous approvals therefore cannot run that
request twice. If execution fails, the confirmation is still consumed; this
is an at-most-once attempt, not a guarantee of exactly-once external delivery
or rollback of partial side effects.

The final application must call confirm() only from a trusted owner-approval
endpoint. There is currently no confirmation endpoint or Yes/No interface.
No registered demo action exposes confirm(), and the string "yes" is not
accepted as a Boolean approval. These facts do not by themselves prove that
all future chat/endpoint integration is secure.

## Errors and argument checks

Unknown names, missing sessions, non-object arguments, missing/extra handler
arguments and non-JSON values are rejected. A handler failure returns a generic
error rather than exposing internal exception text. Results must be JSON
dictionaries.

The JSON Schema is catalog metadata at this stage. Full enforcement of field
types, bounds and business rules is still required in A3. Signature checking
does not prove that an amount, customer or text is safe or valid.

## What tests establish

The backend unittest suite checks Green execution, Yellow waiting, unchanged
stored arguments, rejection, wrong-session attempts, concurrent duplicate
approvals, Red refusal, invalid requests, safe error responses, detached catalog
schemas and clearly fictional demo results. Tests run without Ollama.

Run from the repository root:

```bash
backend/.venv/bin/python -m unittest discover -s backend/tests -v
```

These tests establish behavior of this component, not a comprehensive security
audit or resistance to every prompt-injection attempt.

## Remaining controls

- Authentication and business authorization: a client-provided session ID is
  not proof of who owns a business or permission to access it.
- Database-backed confirmations and a ten-minute expiry: current state is
  process-local, lost at restart and unsuitable for multiple server workers.
- Strict argument-schema and business-rule validation.
- Audit records for calls, confirmations, failures and refusals.
- Session refusal counters and the planned owner-controlled unlock flow.
- Trusted confirmation endpoint and interface, plus agent-loop integration tests.
- Appropriate network access controls, request limits and CSRF protection if
  browser cookie authentication is introduced.

The registry is the intended single execution boundary. Future agent code must
not bypass it by calling financial handlers or querying the database directly.
That separation must be checked in code review and integration tests.

## Suggested explanation for judges

"We separate the AI's suggestions from permission to act. Server code permits
read-only actions, holds actions requiring approval, and refuses prohibited
actions. Approval is tied to the stored request and session, so it cannot change
the arguments or execute the request twice. Our unit tests demonstrate these
controls. The current prototype uses fictional actions; identity checks,
persistent approval records, expiry and audit logging are subsequent layers,
not claims we make about this initial version."
