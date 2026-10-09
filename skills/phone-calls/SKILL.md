---
name: phone-calls
description: >
  Place a phone call to a business for the owner (cancel a membership, ask for a refund, move an
  appointment, ask a question) through ai-phone-desk: an AI voice agent that says it is an AI
  calling on the owner's behalf and dials only after the owner 👍s the exact request. Use whenever
  someone says "call X", "phone them", "can you call", "get them on the phone", or asks how a call
  went. Also the reference for the desk's approval gate, rehearsal, line test and external-agent link.
---

# Phone calls (ai-phone-desk)

You can ASK for a call. You cannot place one. The owner's 👍 on the exact request is the only
thing that dials, and that is the point: do not look for another way.

## The only way a call happens

1. You write a **brief**: who, the number, the goal (one sentence: what done looks like), the
   ask, the fallback, and the facts the voice agent may share. The voice agent knows NOTHING
   beyond those `--fact` lines.
2. The desk sends the owner **one bubble** showing every field.
3. The owner's **👍 on that exact bubble** mints a single-use token bound to the bubble's text.
   👎 drops it. A ❤️ or a typed "yes" in chat does not dial.
4. The tick dials it inside the calling window (08:00-20:00 owner time), under the daily cap,
   unless the kill switch is on. Any edit to the request after the 👍 voids the approval.
5. The tick saves the outcome and transcript and tells the owner how it went.

## Commands

```bash
phone-desk propose --hold --who "<business>" --to "<number>" \
  --goal "<what done looks like>" --ask "<the request>" --fallback "<on a no>" \
  --fact "<fact it may share>" ... [--voicemail "<message>"] [--max-minutes 15]
phone-desk rehearse <id> [--persona "<how the desk pushes back>"]   # text run; or --print
phone-desk surface <id>          # sends the owner the bubble
phone-desk status [<id>]
phone-desk linetest              # dials NIST's public time line; the one call needing no approval
phone-desk external research <id> --host <business-domain>   # a research job for an external agent
```

## How to write a good brief

- **Verify the number from a source you trust** before you surface it. A number scraped from a
  search result can be wrong or spoofed. If you are not sure, `--hold` and say so.
- **Facts: the minimum to find the account.** Name on the account, a member or order number, the
  email on file. The desk refuses card, bank, SSN, password, PIN and birth-date facts outright.
- **One goal per call.** "Cancel the membership" and "dispute last year's charge" are two calls.
- **Voicemail:** the default is to hang up and leave nothing. Add `--voicemail` only with a short
  message the owner would be happy to have recorded.
- Rehearse anything with a likely pushback (refunds, cancellations) and show the owner the result
  before you surface it.

## Hard rules for you, the agent

- Never place a call any other way, never call Vapi's API directly, and never edit the desk's state
  files, tokens or ledger.
- Never run `phone-desk approve`. That is the owner's decision at the owner's own terminal.
- Never remove the kill switch file (`<state>/phone-desk.OFF`) or raise the caps to get a call out.
- Never add a fact the owner did not see, and never call private people; businesses and offices only.
- An outcome of `unknown` means the call may or may not have gone out. Do not re-propose it until
  the owner has checked.

## What the voice agent will and will not do

It opens: "Hi, this is an AI assistant calling on behalf of <owner>. This call is recorded." It
navigates phone menus, waits on hold, reads numbers back, and gets a confirmation number and a
name. It never claims to be the owner, never gives card, bank, SSN, password or birth-date
details, and never agrees to a fee, a new plan or any commitment beyond the goal.
