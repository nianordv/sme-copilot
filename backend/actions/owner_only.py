"""Red actions: things only the owner does, in the bank app.

They are registered so that an attempt has a name: the registry refuses it
before reading its arguments and writes it to the log. The handler below
never runs.
"""

from typing import Any

from .registry import Action, register

AMOUNT = {"type": "number", "description": "Amount in GEL."}

# name -> (what the model is told, the arguments it may name)
OWNER_ONLY_ACTIONS = {
    "transfer_money": (
        "Owner only, always refused. Paying or transferring money is done by "
        "the owner in the bank app.",
        {"to": {"type": "string", "description": "Who to pay."}, "amount": AMOUNT},
    ),
    "take_loan": (
        "Owner only, always refused. Borrowing, or drawing on an overdraft, "
        "is done by the owner in the bank app.",
        {"product": {"type": "string", "description": "overdraft or term_loan."}, "amount": AMOUNT},
    ),
    "repay_loan": (
        "Owner only, always refused. Repaying a loan or an overdraft is done "
        "by the owner in the bank app.",
        {"amount": AMOUNT},
    ),
    "submit_credit_request": (
        "Owner only, always refused. Submitting a draft request to the bank "
        "is done by the owner in the bank app.",
        {"draft_id": {"type": "integer", "description": "The saved draft."}},
    ),
}


def never(**arguments: Any) -> dict[str, Any]:
    raise RuntimeError("An owner-only action reached its handler.")


def register_owner_only_actions() -> None:
    for name, (description, properties) in OWNER_ONLY_ACTIONS.items():
        register(Action(
            name=name,
            description=description,
            parameters={"type": "object", "properties": properties,
                        "required": [], "additionalProperties": False},
            level="red",
            # Looked up when called, so a test can stand in for it.
            handler=lambda **arguments: never(**arguments),
        ))
