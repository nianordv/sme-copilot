# Rightsize: team brief and build plan

Oct 1, 2026 · @Roni

Rightsize is a copilot inside a bank's business app. It warns a small-business owner weeks before cash runs short, shows how big the gap really is, and lays out the cheapest ways to close it, including not borrowing. We submit to the FinTechathon by Oct 20, 2026, 23:59 Beijing time (15:59 UTC).

## What we are building

Rightsize is our entry for the 2026 Shenzhen FinTechathon, International AI Track, Topic E (SME Finance Copilot). It has three jobs:

1. **Watch.** It forecasts the company's cash every day and speaks first, weeks ahead, when salaries or the tax payment are at risk.
2. **Size.** It shows the gap in three scenarios: likely, cautious and worst case.
3. **Close.** It lists every option with its total cost, including "wait" and "remind the late client", marks one as recommended, and waits for the owner's decision.

The agent advises and the owner acts. It never borrows, repays or pays anyone.

Every number the agent shows comes from ordinary Python code. The AI model reads the owner's message, decides which calculation to run, and explains the result. It does no arithmetic of its own.

**Demo company.** A Tbilisi food-and-drink wholesaler supplying about 40 restaurants, cafés and small hotels, with 12 staff.

- Customers have 30 days to pay and often pay late.
- Salaries go out on the 10th, taxes on the 15th.
- Summer and December are the busy seasons.
- Amounts are in lari. The bank is fictional.

## What the owner asks

These six questions start our test list. Each one gets a correct answer worked out by hand.

1. "Can I pay salaries on the 10th and taxes on the 15th?"
2. "Who owes me money, and who is late?"
3. "Send reminders to the late ones."
4. "I'm short. What are my options?"
5. "Client A paid. Do I still need the overdraft?"
6. "Can I afford a GEL 20,000 stock order next month?"

One exchange, with placeholder numbers:

> **Rightsize:** Heads-up. After salaries on the 10th and taxes on the 15th, you are likely to be GEL 8,000 short for about three weeks.
>
> **Owner:** How bad can it get, and what can I do?
>
> **Rightsize:** Likely case GEL 8,000. Cautious case GEL 14,000: nine in ten scenarios stay under this. Worst case GEL 30,000, if nobody pays. I recommend the reminder now and the overdraft as a fallback. Which do you want?

| Option | Cost | Note |
| --- | --- | --- |
| Remind Client A, who owes GEL 15,000 | Free | Recommended first step |
| Overdraft, GEL 15,000 limit | About GEL 74 if you use GEL 8,000 for three weeks | Fallback |
| 12-month loan of GEL 30,000 | About GEL 2,800 in interest and fees | Covers the worst case and costs the most |

> **Owner:** Remind them, and get the overdraft request ready.
>
> **Rightsize:** Here is the reminder text. Shall I send it? The overdraft request for GEL 15,000 is filled in and waiting in your bank app for you to submit.

The costs use made-up terms: an overdraft at 16% a year, and a loan at 15% a year plus a 1% fee. The finance owners replace them with real terms.

## What the agent may do

The organisers ask for three permission levels. Ours are enforced by code that sits between the agent and its actions, not by instructions to the AI model.

| Action | Who does it | Level |
| --- | --- | --- |
| Look up balances and invoices, forecast, compare options | Agent, without asking | Green |
| Set an alert, save a plan | Agent, after the owner says yes | Yellow |
| Send a payment reminder to a late client | Agent, after the owner approves the text | Yellow |
| Fill in an overdraft or loan request | Agent prepares it; the owner submits it in the bank app | Red: owner only |
| Borrow, repay, pay anyone | Owner, in the bank app | Red: owner only |

The same code records every action in a log and locks the agent after repeated suspicious requests. The log is the evidence we submit.

## How we are scored

The three criteria come from the [official site](https://fintechathon.g-ican.com). Ten teams reach the final.

| Criterion | What judges look at | What we build for it |
| --- | --- | --- |
| Task completion, 40% | Scripted tasks completed in a simulated environment | About 20 owner questions, each with a hand-worked correct answer. A script runs them all and reports the pass rate. |
| Security and compliance, 30% | Resisting four attacks: being talked into a transfer, leaking data, false claims of authority, and hidden instructions in documents. Serious failures cost heavily. | About 20 trick attempts across those four kinds, run by script. The agent cannot move money at all. It always shows the total cost and yearly rate, and says it is the bank's assistant. |
| Innovation and interaction, 30% | Novelty, real-world value, few messages to a result, asking when unclear, accepting corrections, declining out-of-scope requests | The agent speaks first, shows three scenarios and may advise against borrowing. One real business owner tries it. |

Because the agent only advises, the 40% depends on its answers being right. The answer key is our most important document.

## Who owns what

This split is a proposal. Replace the role labels with names and move rows as you see fit.

| Who | Builds | Writes at the end |
| --- | --- | --- |
| Tech 1 (backend, data) | The pretend bank and the company's books, the agent's actions, the permission code and log, the screen | README and execution evidence |
| Tech 2 (ML) | The agent's conversation through the Claude API, and the scripts that run the owner questions and trick attempts | Technical document |
| Fifth member (astrophysics) | The forecast, which simulates the next 60 days thousands of times, and the generator that produces 12 months of believable history | Evaluation part of the technical document |
| Finance 1 | Real product terms and the demo company's numbers | Deck, and the story in the video |
| Finance 2 | The answer key and the list of trick attempts | Security self-assessment |

The forecast row assumes the astrophysics member codes in Python. If not, Tech 2 takes the forecast and they take the documents.

An AI coding assistant, Claude Code, writes much of the code with the tech owners. Each owner must still be able to explain their part at the defence in Shenzhen.

## Build plan

&#91;embedded content: build plan · 4 phases, 5 checkpoints\]

Each column is a phase, and its bottom cell says what must be true before the next phase starts. From 15 Oct nobody adds features, and 19 and 20 Oct are spare days.

If a checkpoint slips, cut in this order: the what-if question, the alerts, then the extra test companies.

## What we are leaving out

Each item below was in an earlier version of the idea and is cut to fit 19 days.

- The agent moving money or taking a loan.
- Reading invoice PDFs or photos. One pasted-text path stays, to show the agent ignoring a hidden instruction inside an invoice.
- Fifty synthetic companies. We build one rich company and two or three variants for tests.
- Factoring, inventory sales, early-payment discounts and multiple currencies.
- Real bank connections, a mobile app and user accounts.

## This week

Registration and the organisers' answers come first, because both can take days.

- [ ] Register by email: the team form, plus each member's ID document and proof of enrolment. All five must be full-time students at universities outside China. The site lists fintechathon@ican-x.com for this track; the rules list wangyr@pbcsf.tsinghua.edu.cn.
- [ ] Ask the organisers three questions in the same email:
  1. Are the scripted tasks supplied by you, or defined by each team?
  2. Does the 0 to 5 point bonus cover demonstrated real-world deployment, or only on-chain work?
  3. Is the demo video required?
- [ ] Finance: write the terms for an overdraft or credit line and for a term loan (rate, fees, minimum term), and confirm the salary and tax dates.
- [ ] Finance: check the demo company and the six owner questions, with the real owner if possible.
- [ ] Tech: create the GitHub repository, check Python runs on each machine, and get a Claude API key.
- [ ] Everyone: read this brief and correct anything that is wrong.

## Sources

- [Official site](https://fintechathon.g-ican.com): judging criteria, schedule and registration process, read on 1 Oct 2026.
- The organisers' rules PDF, held by the team lead: topics, deadline and submission checklist.
- Submission templates served by the official site. They are not linked from its visible pages, so treat them as drafts until the organisers confirm.
  - [Technical document](https://fintechathon.g-ican.com/templates/international-technical-document-en.docx)
  - [Security self-assessment](https://fintechathon.g-ican.com/templates/international-security-self-assessment-en.docx)
  - [Execution evidence](https://fintechathon.g-ican.com/templates/international-execution-evidence-en.docx)
  - [Demo video guide](https://fintechathon.g-ican.com/templates/demo-video-guide-en.docx)
  - [README template](https://fintechathon.g-ican.com/templates/source-code-readme-template-en.md)
  - [Presentation deck](https://fintechathon.g-ican.com/templates/presentation-template.pptx)
  - [Team information form](https://fintechathon.g-ican.com/templates/team-information-form.xlsx)
