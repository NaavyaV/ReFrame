"""A caregiver asks how someone is doing, answers their security question, hears a summary and leaves a note."""

from __future__ import annotations

from caretaker.checkin import MAX_TRIES, CheckIn, answer_hash, is_correct
from caretaker.records import MemoryRecords, Note, Patient, Run
from shared import llm

FAMILY = "agent1qfamilymember0000000000000000000000000000000000000"
STRANGER = "agent1qstrangerperson000000000000000000000000000000000000"

MARIA = Patient("maria-alvarez", "Maria Alvarez", "March 12, 1948", "San Antonio", "Mild hypertension",
                "Lisinopril 10mg each morning", "What city does Maria Alvarez live in?")
JAMES = Patient("james-okonkwo", "James Okonkwo", "November 2, 1952", "Plano", "Type 2 diabetes",
                "Metformin 500mg twice a day", "What year was James Okonkwo born?")
NOBODY = Patient("naavya-vig", "Naavya Vig", security_question="What city does Naavya Vig live in?")


def records() -> MemoryRecords:
    store = MemoryRecords([MARIA, JAMES, NOBODY])
    store.saved["maria-alvarez"] = [Note("Sofia (granddaughter)", "Go slower on checkout pages.")]
    store.tracked["Maria Alvarez"] = [Run("amazon.com", "find a reading lamp", "done")]
    return store


async def summary(facts: str) -> dict:
    assert "Go slower on checkout pages." in facts and "amazon.com" in facts
    return {"good_with": ["Finding products on Amazon."], "struggles_with": ["checkout pages", "pop-up ads"]}


async def test_the_whole_conversation():
    store = records()
    checkin = CheckIn(store, summarize=summary)
    assert await checkin.reply(FAMILY, "Tell me how Maria is doing") == (
        "Answer this security question about Maria: What city does Maria Alvarez live in?")
    assert await checkin.reply(FAMILY, "She lives in San Antonio.") == (
        "Maria is good with finding products on Amazon, and struggles with checkout pages and pop-up ads.")
    assert await checkin.reply(FAMILY, "Next time point out the pay button and read the total out loud") == "Noted!"
    saved = store.saved["maria-alvarez"][-1]
    assert saved.body == "Next time point out the pay button and read the total out loud"
    assert saved.author.startswith("caregiver agent1qfam")


async def test_wrong_answers_are_limited_and_reveal_nothing():
    checkin = CheckIn(records(), summarize=summary)
    await checkin.reply(STRANGER, "How is Maria Alvarez doing?")
    for _ in range(MAX_TRIES - 1):
        reply = await checkin.reply(STRANGER, "Dallas")
        assert reply.startswith("That's not right.") and "Maria is" not in reply
    assert await checkin.reply(STRANGER, "Dallas") == "Too many wrong answers. Try again in an hour."
    assert await checkin.reply(STRANGER, "How is Maria doing?") == "Too many wrong answers. Try again in an hour."
    assert (await checkin.reply(FAMILY, "How is Maria doing?")).startswith("Answer this security question")


async def test_a_note_needs_the_answer_first():
    store = records()
    checkin = CheckIn(store, summarize=summary)
    assert (await checkin.reply(STRANGER, "Next time Maria should use the back button")).startswith(
        "Answer this security question")
    assert len(store.saved["maria-alvarez"]) == 1


async def test_answers_are_checked_against_the_record():
    assert is_correct("Plano", JAMES) is False
    assert is_correct("1952", JAMES) and is_correct("He was born in 1952", JAMES)
    assert is_correct("san antonio", MARIA)
    assert not is_correct("Dallas Plano San Antonio Austin", MARIA)
    assert not is_correct("1950 1951 1952 1953", JAMES)


async def test_a_stored_answer_replaces_the_record_check():
    naavya = Patient("naavya-vig", "Naavya Vig", security_question="What city was he born in?",
                     answer_hash=answer_hash("naavya-vig", "Mumbai"))
    checkin = CheckIn(MemoryRecords([naavya]), summarize=summary)
    assert await checkin.reply(FAMILY, "How is Naavya doing?") == (
        "Answer this security question about Naavya: What city was he born in?")
    assert await checkin.reply(FAMILY, "Delhi") == "That's not right. 2 tries left."
    assert await checkin.reply(FAMILY, "He was born in Mumbai") == "Nothing recorded for Naavya yet."


async def test_the_at_handle_is_not_part_of_the_answer():
    checkin = CheckIn(MemoryRecords([Patient(
        "naavya-vig", "Naavya Vig", security_question="What city was he born in?",
        answer_hash=answer_hash("naavya-vig", "Mumbai"))]), summarize=summary)
    assert await checkin.reply(FAMILY, "@hold-my-hand-care give me stats on naavya vig") == (
        "Answer this security question about Naavya: What city was he born in?")
    assert await checkin.reply(FAMILY, "@hold-my-hand-care Mumbai") == "Nothing recorded for Naavya yet."


async def test_unknown_people_and_missing_answers():
    checkin = CheckIn(records(), summarize=summary)
    assert await checkin.reply(FAMILY, "How is Bob doing?") == "I don't know anyone by that name."
    assert await checkin.reply(FAMILY, "How is Naavya doing?") == "No security answer on file for Naavya Vig."
    assert await checkin.reply(FAMILY, "ABCD-EFGH") is None  # left for the settings flow


async def test_without_groq_it_lists_the_notes():
    async def offline(facts: str) -> dict:
        raise llm.LLMError("GROQ_API_KEY isn't set.")

    checkin = CheckIn(records(), summarize=offline)
    await checkin.reply(FAMILY, "How is Maria doing?")
    reply = await checkin.reply(FAMILY, "San Antonio")
    assert reply == ("Maria is good with tasks on amazon.com.\nNotes for Maria:\n- Go slower on checkout pages.")


async def test_without_records_it_says_so():
    checkin = CheckIn(None)
    assert await checkin.reply(FAMILY, "How is Maria doing?") == "Records aren't available right now."
    assert await checkin.reply(FAMILY, "turn on simpler wording") is None
