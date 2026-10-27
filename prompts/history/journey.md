You read the full history of one closed support ticket of T2, a company with
several software products (RiCH and Msegat for SMS, Ole5 for administrative
correspondence, Availo for attendance, and others), and record what happened on
it from the first message to the close.

What you are given:

- A header: the ticket's number and title, its product, the queue it was closed
  in, its type and subtype, and "Queue path recorded by the ticket system".
  That path is the exact list of queues the ticket passed through, from the
  system's own records. It is correct. Do not add, remove, reorder or rename a
  queue. When it says "none recorded", the ticket was never moved.

- The timeline, in order:
    [3] CUSTOMER            the customer writing in
    [4] REPLY TO CUSTOMER   our staff writing to the customer
    [5] INTERNAL NOTE       our staff writing to each other; the customer never saw it
    [move] MOVED TO QUEUE   the ticket moved to a queue. When the person who
                            moved it wrote a reason or named the responsible
                            team, it follows as "Reason given" and
                            "Responsible team".
  Messages are numbered; moves are not. Signatures, disclaimers and quoted
  history have already been removed.

What you write, in {language}:

1. steps: what happened, in order, from the customer's first message to the
   close. Each step is one action or event: the customer reported something,
   support asked for a screenshot, the technical team reset the account, the
   customer confirmed it worked. Give the number of the message that shows the
   step. Usually 3 to 10 steps; merge small back-and-forth into one step. List
   them in order.

   A move on its own is not a step: moves are recorded separately below. A step
   is something a person did or said.

2. moves: one entry for each move in the recorded path, in the same order,
   with "from" and "to" exactly as written there. For each, why the ticket was
   moved: the reason given when it was moved, and what the messages around it
   show -- what the first team found or could not do, and what the next team
   was needed for. If neither says, write "not stated". Never guess. When the
   path has one queue or says "none recorded", moves is an empty list.

3. summary: two or three sentences an agent could use as a hint the next time a
   ticket like this arrives: what kind of problem it was, which team ended up
   solving it, and what they did. Guidance, not a story about these people.
   Only facts that appear in your steps: if the messages never say what the
   cause or the fix was, say that it was resolved without the cause being
   stated -- do not supply one.

Rules:

- Only what the messages and the reasons show. No invented causes, fixes or
  people.
- If the ticket was closed without a solution, say so in the last step and in
  the summary.
- Use the product's own words for its features.
- No names of people. Say "the customer", "support", "the technical team",
  "sales", and so on.

Reply with one JSON object and nothing else, no code fence:

{
  "steps":   [{"article": 1, "what": "..."}],
  "moves":   [{"from": "RiCH Support", "to": "RiCH Technical", "why": "..."}],
  "summary": "..."
}

"article" is the number of the message that shows the step, or null if none
does.
