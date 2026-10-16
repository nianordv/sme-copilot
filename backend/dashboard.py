"""Everything the dashboard page shows, as one read-only answer.

read_dashboard() returns four parts:

  cash      what get_balance, get_receivables and get_upcoming_outflows return
  forecast  what forecast_cash returns for 30 days, or why there is none
  work      what Bill has saved or is waiting to do: requests waiting for the
            owner, reminders, alerts, the newest plan, credit request drafts
  log       the newest rows of the audit log, newest first, and verify_log()

Every figure comes from the function behind the action of the same name, called
directly. Nothing goes through registry.execute(): a page that is refreshed
would fill the audit log with its own rows and bury the agent's. The AI model
takes no part, and nothing here adds, subtracts or rounds. A figure the page
needs and an action lacks belongs in that action's function, so the chat and
the dashboard cannot disagree.

Nothing here writes to the database. Texts the model wrote (a reminder, a plan,
an alert's note, a call's arguments) are plain strings in which a character
that cannot be seen is written as its code. The page draws them as plain text.
"""

import json
import logging
from typing import Any

from backend.actions import registry
from backend.actions.borrowing import saved_credit_requests
from backend.actions.forecast import forecast_cash
from backend.actions.lookups import get_balance, get_receivables, get_upcoming_outflows
from backend.actions.planning import newest_plan, saved_alerts
from backend.actions.reminders import saved_reminders
from backend.bank.db import today

logger = logging.getLogger(__name__)

DAYS_AHEAD = 30
LOG_ROWS = 20
LISTED = 10

NO_FORECAST_FILES = ("The forecast files are missing. Prepare them as the README describes "
                     "under \"Local data and model files\".")
NO_FORECAST = "The forecast could not be worked out. The server's log says why."


def read_dashboard(days=DAYS_AHEAD) -> dict[str, Any]:
    result = {"cash": _cash(), "forecast": _forecast(days), "work": _work(), "log": _log()}
    from backend.insights import evaluate_alerts
    from backend.bank.db import connect
    with connect() as conn:
        result['work']['alert_checks'] = evaluate_alerts(conn, result['forecast'].get('result'))
    return result


def _cash() -> dict[str, Any]:
    return {
        "as_of": today().isoformat(),
        "balance": get_balance(),
        "receivables": get_receivables(),
        "upcoming_outflows": get_upcoming_outflows(DAYS_AHEAD),
    }


def _forecast(days=DAYS_AHEAD) -> dict[str, Any]:
    """The forecast is the one part that needs files from outside the
    database. Without them the other three parts are still worth showing."""
    try:
        return {"available": True, "result": forecast_cash(days=days)}
    except FileNotFoundError:
        return {"available": False, "reason": NO_FORECAST_FILES}
    except Exception as problem:
        # As in the registry: the page gets our own sentence, the server's log the cause.
        logger.error("The dashboard's forecast failed: %s: %r", type(problem).__name__, str(problem)[:300])
        return {"available": False, "reason": NO_FORECAST}


def _work() -> dict[str, Any]:
    return {
        "waiting": registry.waiting_requests(LISTED),
        "reminders": saved_reminders(LISTED),
        "alerts": saved_alerts(LISTED),
        "plan": newest_plan(),
        "credit_request_drafts": saved_credit_requests(LISTED),
    }


def _log() -> dict[str, Any]:
    rows = registry.audit_log()[-LOG_ROWS:]
    return {
        "check": registry.verify_log(),
        "rows": [{
            "log_id": row["log_id"], "time": row["time"], "session_id": row["session_id"],
            "action": row["action"], "level": row["level"], "decision": row["decision"],
            "result": row["result"],
            # The model wrote these. One line of text, as the log itself keeps them.
            "arguments": registry.shown(json.dumps(row["arguments"], ensure_ascii=False)),
        } for row in reversed(rows)],
    }
