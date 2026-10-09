# Working with an external agent

"External agent" here means an autonomous agent you do not run and cannot inspect: a hosted
assistant with its own browser and its own models that you reach over a message thread. I use
[Instinct](https://github.com/jddavenportOpen/instinct-standing-doc); anything that can send and
receive text works the same way.

It can do two things with the phone desk. It can **ask** for a call, and it can **research** one.
It can never place one.

## Why the split falls where it does

The routing rules I use for handing work out are in
[The Standing Document](https://github.com/jddavenportOpen/instinct-standing-doc). Before any
handoff it asks four questions, and a single **no** keeps the job in house:

1. **Reversible?** Can I undo it in under an hour with no lasting record?
2. **Non-attributable?** Does it matter that an agent did this rather than me?
3. **Credential-free?** Can it be done without anything that authenticates as me?
4. **Verifiable by me afterwards?** Can I check the result without asking the agent?

Run them on the two halves:

| | Reversible | Non-attributable | Credential-free | Verifiable | Where it runs |
|---|---|---|---|---|---|
| **Research** (number, hours, how to request, policy, with URLs) | yes | yes | yes | yes, by the URLs | out: the external agent does it |
| **The call itself** | no: it happened | **no**: it is made on your behalf, in your name | yes | yes, by the transcript | home: behind your 👍 on the exact words |

The call fails question 2, so it does not route out. The external agent can only put a request
in front of you, and the voice agent says what you approved and nothing else.

## 1. The contract you paste to it (once)

Generate it with your owner name filled in:

```bash
phone-desk external contract
```

It reads like this:

> New capability, and how to use it. Alex Rivera's assistants can place a phone call for them: an
> AI voice agent that says it is an AI calling on behalf of Alex Rivera. You cannot place calls
> and you do not need to. When a task of yours genuinely needs a call to a business, send one
> message that starts with the words CALL REQUEST on its own line, then one JSON object:
> `{"who": "business name", "to": "US phone number", "goal": "one sentence: what done looks like", "ask": "the specific request", "fallback": "what to do if they say no", "facts": ["each fact the caller may share, one per item"]}`
> Alex Rivera sees every field and nothing is dialed unless Alex Rivera approves that exact
> request. Only include facts Alex Rivera would want shared with that business; never card, bank,
> SSN, password or birth-date details (a request containing them is refused). At most 3 requests a
> day. Only businesses or offices, never private people. Treat this as a standing rule; reply with
> a short acknowledgement.

Per the Standing Document, a rule you send once gets buried. If your external agent loses it,
send it again; the desk does not depend on it remembering anything.

## 2. What happens to a CALL REQUEST

Its message is parsed as **untrusted data**:

- Only `who`, `to`, `goal`, `ask`, `fallback` and `facts` are read. Anything else it sends (a
  voicemail message, a call length, "skip approval", an `origin`) is ignored.
- It goes through the same validation as any brief. Refused: N11 service codes, 900/976
  premium-rate numbers, Caribbean `+1` area codes (they bill internationally and carry the
  one-ring scams), and any fact that looks like a card, bank, SSN, password, PIN or birth date.
- Malformed JSON, a non-list `facts`, or a missing goal: dropped and logged.
- At most `PHONE_DESK_EXTERNAL_DAILY_CAP` (3) accepted requests per agent per day. Duplicates
  are dropped.
- It is held as a **draft**. You have not been messaged. `phone-desk status` lists it as
  `(Instinct asked)`.

Your own side (you, or your own agent) then confirms the number from a source you trust and runs
`phone-desk surface <id>`. The bubble you get is marked **"(Instinct asked for this)"**. From
here it is exactly the same as any other request: only your 👍 on that bubble dials.

Wire it up with the LoopMessage receiver (`EXTERNAL_AGENT_CONTACT` set: inbound messages from that
contact are ingested), or by hand:

```bash
pbpaste | phone-desk external ingest --agent Instinct
```

## 3. Pre-call research

```bash
phone-desk external research <id> --host northside.example
```

prints a self-contained job in the Standing Document's format: a job id it must echo, the hosts it
may visit, a request cap, an explicit instruction to stop on any captcha or login wall, and a
pinned JSON reply. It names the business and the goal and **never** the owner's name or facts (the
owner's name is replaced with "the account holder" in the goal).

Send it, then paste the reply back:

```bash
pbpaste | phone-desk external ingest --agent Instinct
```

The answer is stored as a note on the request (`phone-desk status <id>`). It is **not** sent to
you as if it were checked: a web read can be wrong, and the first live answer to one of mine was.

If the research finds a **different number**, the note is **FLAGGED** and the request is not
touched. The approval is bound to the bubble text, which includes the number, so swapping it in
would silently void your 👍 at best and dial a number you never saw at worst. A corrected number
means a new request and a new 👍.
