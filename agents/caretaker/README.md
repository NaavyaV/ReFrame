# Hold My Hand Caregiver

## Summary
Tells a family member how the person they look after is doing, once they answer that person's security
question, and keeps their notes for next time. It also lets a trusted caregiver make small, approved
adjustments to how Hold My Hand talks with an older adult, such as simpler wording or larger text.

## What it does
- "How is Maria doing?" gets the security question on Maria's record. A right answer gets what she's good
  with and struggles with, from caretaker notes and her recent browser-helper runs. Three wrong answers lock
  that person for an hour.
- Anything said next, such as "Next time go slower on checkout pages", is saved as a caretaker note.
- Creates a one-time invite code (valid 24 hours) when the person asks the Hold My Hand Orchestrator to add a
  caregiver and confirms which settings they may change.
- Links the caregiver who sends that code. Their verified agent address is their identity from then on.
- Lets the caregiver change only the approved settings, to fixed values:
  simpler wording, shorter explanations, larger text (a display hint for the browser extension), preferred
  language, checking with the person before bigger steps, and pointing out clear product features.
- Saves each change with who made it, the old and new value, the time and the person's approval, and tells
  the Orchestrator so the person is notified.
- Shows a caregiver the current settings, lets them undo a change, and lets the person undo everything or
  remove the caregiver's access.

## Constraints and scope
- Refuses anything that could affect money or choices: budgets, blocked shops or brands, orders, payments.
- No free text from a caregiver is ever passed to the Orchestrator's instructions. Saved notes are only
  summarized back to family members who answered the security question.
- Check-ins use the patient and caretaker_note tables. Settings are stored per person in SpacetimeDB. If that storage isn't set up, every change is refused with
  an explanation.
- A caregiver can't see the person's conversations, searches or purchases.

## Example prompts
- "Tell me how Maria Alvarez is doing"
- "Next time point out the pay button"
- "ABCD-EFGH" (the code the person gave you)
- "Turn on simpler wording"
- "Set language to Spanish"
- "Show settings"
- "Undo shorter explanations"

## Keywords
Hold My Hand, caregiver, older adults, accessibility settings, simpler wording, larger text, preferred
language, consent, audit log, user autonomy, Fetch.ai, Agentverse
