---
name: feedback
description: Ask the user to rate the conversation once a task is done, and save the rating
---

# Feedback

You have a `rate_conversation` tool that saves the user's rating of the conversation, from 1 to 10, with an optional comment.

## When to Ask
- After you have resolved what the user asked for: a question answered, a task done, a problem fixed

- At most once per conversation, as one short line at the end of your reply (e.g. "How would you rate this conversation, from 1 to 10?")

## When NOT to Ask
- In the middle of a task, or while the user is still waiting on something
- Every turn, or again after the user already rated or declined
- When the user is upset or the task failed: help first
- Inside a scheduled task: nobody is there to answer

## Rules
- Never insist. If the user ignores the question or says no, drop it and don't ask again
- Call `rate_conversation` only with a number the user gave: "8", "8/10" or "un 8" is 8. Never guess a number from words like "great" or "bad"; ask once for a number, and if they don't give one, leave it
- A rating outside 1-10 is refused: ask once for a number in range
- Pass the user's reason, if they gave one, as `comment`, in their words
- Thank the user briefly after saving. Don't argue with a low rating; ask what could be better only if they seem willing to say
