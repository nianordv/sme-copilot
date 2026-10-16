"""Live Ollama checks for funding advice, using disposable company books.

Run: backend/.venv/bin/python -m tests.scenarios.check_funding_chat
No approval is granted and no owner's books are touched.
"""

import json
import os
from pathlib import Path
import secrets
import tempfile
import time
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend import main
from backend.actions import registry
from backend.actions.borrowing import compare_options
from backend.funding_plans import preview as preview_plan
from backend.bank.db import connect
from backend.bank.seed import seed, DEFAULT_CSV, DEFAULT_METADATA, DEFAULT_COMPANY


def run():
    evidence = []
    with tempfile.TemporaryDirectory() as folder:
        database = Path(folder) / 'funding-chat.db'
        seed(DEFAULT_CSV, DEFAULT_METADATA, DEFAULT_COMPANY, database)
        with patch.dict(os.environ, {'RIGHTSIZE_DB': str(database), 'RIGHTSIZE_OWNER_PIN': secrets.token_urlsafe(16)}):
            client = TestClient(main.app, base_url='http://localhost')
            assert client.post('/login', json={'pin': os.environ['RIGHTSIZE_OWNER_PIN']}).status_code == 200
            session = 'funding-chat-check'
            messages = []

            def ask(text):
                messages.append({'role': 'user', 'content': text})
                start = time.monotonic()
                response = client.post('/chat/stream', json={'session_id': session, 'messages': messages})
                assert response.status_code == 200, response.text
                events = [json.loads(line) for line in response.text.splitlines() if line]
                answer = ''.join(event.get('content', '') for event in events if event['type'] == 'chunk')
                messages.append({'role': 'assistant', 'content': answer})
                evidence.append({'question': text, 'answer': answer, 'events': events,
                                 'seconds': round(time.monotonic() - start, 1)})
                assert not any(event['type'] in ('error', 'confirmation') for event in events), events
                assert any(event['type'] == 'done' for event in events), events
                print(answer, flush=True)
                return answer

            answer = ask('I am short GEL 10,000 for 30 days. A bank quoted me 18% annual nominal interest. '
                         'Compare borrowing with offering a customer a discount and borrowing only the remaining gap. '
                         'Name a customer from my books, show the amounts and costs, and explain the conditions. Do not record anything.')
            rows = registry.audit_log(session)
            comparison = next(row for row in reversed(rows) if row['action'] == 'compare_options' and row['decision'] == 'ran')
            assert comparison['arguments']['quoted_annual_rate'] == 18, comparison
            # Audit results deliberately contain only a short summary. Recompute
            # expected arithmetic from the logged arguments, never parse that summary.
            result = compare_options(**comparison['arguments'])
            plans = result['collection_plans']
            assert any(name.casefold() in answer.casefold() for plan in plans for name in plan['customers']), answer
            assert '18' in answer and ('discount' in answer.casefold()), answer
            for plan in plans:
                assert f'GEL {plan["collection_net"]:,.2f}' in answer, answer
                if plan['borrowing']:
                    assert f'Borrowing amount: GEL {plan["borrowing"]["principal"]:,.2f}' in answer, answer
                    assert f'Borrowing cost: GEL {plan["borrowing"]["cost"]:,.2f}' in answer, answer
            print('PASS: quoted rate, real customer and conditional collection advice.', flush=True)

            # The real conversation must use a plan from its previous result.
            answer = ask('Preview the cash impact of the discount plus term loan plan you just compared for GEL 10,000 '
                         'over 30 days at my quoted 18% rate. Show the original cautious shortfall '
                         'and the shortfall with the plan, and explain what has to happen for it to work. Do not save any transactions.')
            rows = registry.audit_log(session)
            preview = next(row for row in reversed(rows) if row['action'] == 'preview_funding_plan' and row['decision'] == 'ran')
            output = preview_plan(**preview['arguments'])
            assert output['plan']['plan_id'] in {p['plan_id'] for p in plans}, output
            assert output['plan']['borrowing']['product'] == 'term_loan', output
            assert preview['arguments']['quoted_annual_rate'] == 18, preview
            assert 'shortfall' in answer.casefold(), answer
            assert f'Original cautious shortfall: GEL {output["baseline"]["cautious"]["shortfall"]:,.2f}' in answer, answer
            assert f'Cautious shortfall with this plan: GEL {output["with_plan"]["cautious"]["shortfall"]:,.2f}' in answer, answer
            assert any(word in answer.casefold() for word in ('assum', 'if ', 'agree', 'conditional')), answer
            assert not any(row['level'] in ('yellow', 'red') for row in rows), rows
            with connect() as conn:
                assert conn.execute('SELECT COUNT(*) FROM confirmations').fetchone()[0] == 0
                for table in ('cash_entries', 'recorded_loans'):
                    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
                    if exists:
                        assert conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == 0
            print('PASS: plan preview through the chat, no financial writes or approvals.', flush=True)

    destination = Path(__file__).parent / 'results' / f'funding-chat-{time.strftime("%Y-%m-%d_%H%M%S")}.json'
    destination.parent.mkdir(exist_ok=True)
    destination.write_text(json.dumps(evidence, indent=2))
    print(f'Saved: {destination}', flush=True)


if __name__ == '__main__':
    run()
