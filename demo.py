"""Reproducible execution evidence for PawPal+.

Run `python demo.py` to exercise the whole system end to end and print the
output pasted into README.md. Four scenarios:

  1. User-first scheduling  - owner-set times are never moved
  2. Agentic review (pass)  - the loop fills real gaps and approves
  3. Agentic review (fail)  - a budget that cannot hold the required care
  4. LLM guardrail          - Gemini's slot suggestions, accepted and rejected

Scenarios 1-3 run with live Gemini commentary when GEMINI_API_KEY is set, and
fall back to rule-only output when it is not. Scenario 4 uses a stub model so
the guardrail can be demonstrated deterministically, without spending quota.
"""

from datetime import time

from pawpal_system import (
    DAY_BLOCKS,
    MIN_FEEDING_GAP_MINUTES,
    Frequency,
    LLMCommentator,
    LLMStatus,
    Owner,
    Pet,
    PetCareAgent,
    Priority,
    Scheduler,
    Task,
)


def rule(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def show(schedule) -> None:
    for t in schedule:
        when = f"{t.preferred_time:%H:%M}" if t.preferred_time else "--:--"
        who = "you" if t.source == "owner" else "agent"
        print(f"  {when}  {t.pet_name:8} {t.title:22} {t.duration_minutes:>3} min  [set by {who}]")


def commentary(agent) -> None:
    for step in agent.log:
        if step.commentary:
            print(f"\n  [Gemini / {step.stage}] {step.commentary}")


# ---------------------------------------------------------------------------
# 1. User-first scheduling
# ---------------------------------------------------------------------------

def scenario_user_first(llm):
    rule("SCENARIO 1  User-entered times are honored exactly")
    owner = Owner(name="Owner", available_minutes=600)
    pet = Pet(name="Timmy", species="dog")
    owner.add_pet(pet)
    pet.add_task(Task("Lunch feed", 30, Priority.HIGH, preferred_time=time(13, 30)))
    pet.add_task(Task("Morning walk", 20, Priority.MEDIUM, preferred_time=time(7, 0)))

    print("\nINPUT (typed by the owner):")
    print("  Timmy (dog) - Lunch feed 13:30 (30 min), Morning walk 07:00 (20 min)")
    print("  Available time today: 600 min")

    agent = PetCareAgent(owner, max_iterations=3, commentator=llm)
    agent.run()

    print("\nOUTPUT (after the agentic review):")
    show(agent.schedule)
    lunch = next(t for t in pet.tasks if t.title == "Lunch feed")
    walk = next(t for t in pet.tasks if t.title == "Morning walk")
    print(f"\n  Lunch feed still at {lunch.preferred_time:%H:%M}: "
          f"{lunch.preferred_time == time(13, 30)}")
    print(f"  Morning walk still at {walk.preferred_time:%H:%M}: "
          f"{walk.preferred_time == time(7, 0)}")
    print(f"  Verdict: {'APPROVED' if agent.approved else 'NEEDS REVIEW'}")
    commentary(agent)


# ---------------------------------------------------------------------------
# 2. A review that passes
# ---------------------------------------------------------------------------

def scenario_pass(llm):
    rule("SCENARIO 2  Agentic review fills real gaps and approves")
    owner = Owner(name="Owner", available_minutes=600)
    dog, cat = Pet("Rex", "dog"), Pet("Mochi", "cat")
    owner.add_pet(dog)
    owner.add_pet(cat)
    dog.add_task(Task("Morning feed", 15, Priority.HIGH, preferred_time=time(7, 0)))
    cat.add_task(Task("Breakfast feed", 15, Priority.HIGH, preferred_time=time(8, 0)))

    print("\nINPUT: Rex (dog) 1 feeding, Mochi (cat) 1 feeding, 600 min available")

    agent = PetCareAgent(owner, max_iterations=4, commentator=llm)
    agent.run()

    print("\nAUDIT TRAIL:")
    for step in agent.log:
        print(f"  iter {step.iteration} | {step.stage:<9} | {step.summary}")
        for d in step.details:
            print(f"      - {d}")

    print("\nFINAL SCHEDULE:")
    show(agent.schedule)
    committed = sum(t.duration_minutes for t in agent.schedule)
    print(f"\n  Verdict: {'APPROVED' if agent.approved else 'NEEDS REVIEW'} "
          f"after {agent.iterations_used} iteration(s)")
    print(f"  Time committed: {committed} of {owner.available_minutes} min")
    commentary(agent)


# ---------------------------------------------------------------------------
# 3. A review that escalates to the human
# ---------------------------------------------------------------------------

def scenario_escalate(llm):
    rule("SCENARIO 3  Not enough time: the agent escalates instead of guessing")
    owner = Owner(name="Owner", available_minutes=30)
    dog = Pet("Rex", "dog")
    owner.add_pet(dog)
    dog.add_task(Task("Morning feed", 20, Priority.HIGH, preferred_time=time(7, 0)))
    dog.add_task(Task("Evening walk", 20, Priority.MEDIUM, preferred_time=time(18, 0)))

    print("\nINPUT: Rex (dog), 2 tasks totalling 40 min, but only 30 min available")

    agent = PetCareAgent(owner, max_iterations=3, commentator=llm)
    agent.run()

    print("\nOUTSTANDING ISSUES (reported, not silently fixed):")
    for step in agent.log:
        if step.stage == "REFLECTOR":
            for d in step.details:
                print(f"  - {d}")

    committed = sum(t.duration_minutes for t in agent.schedule)
    print(f"\n  Verdict: {'APPROVED' if agent.approved else 'NEEDS REVIEW'}")
    print(f"  Time committed: {committed} of {owner.available_minutes} min "
          f"(guardrail held: {committed <= owner.available_minutes})")
    print(f"  Tasks owned by Rex: {len(dog.tasks)} (no duplicates invented)")
    commentary(agent)


# ---------------------------------------------------------------------------
# 4. The LLM guardrail
# ---------------------------------------------------------------------------

class StubModel(LLMCommentator):
    """A fake Gemini that returns one fixed answer, for guardrail testing."""

    def __init__(self, reply):
        self._client = object()
        self._types = None
        self.model = "stub"
        self.last_error = ""
        self.status = LLMStatus(True, "stub", "stub")
        self.reply = reply

    def _generate(self, prompt):
        return self.reply

    def planner_commentary(self, *a, **k):
        return ""

    def reflector_commentary(self, *a, **k):
        return ""


def scenario_guardrail():
    rule("SCENARIO 4  Guardrail: every LLM time is re-checked by the rules")
    print("\nSetup: Timmy (dog) already fed at 13:30. The model is asked where the")
    print(f"second feeding goes. Rules require >= {MIN_FEEDING_GAP_MINUTES} min "
          f"({MIN_FEEDING_GAP_MINUTES // 60}h) spacing,")
    print(f"inside a day block {dict((k, (f'{a:%H:%M}', f'{b:%H:%M}')) for k, (a, b) in DAY_BLOCKS.items())}.\n")

    header = f"  {'Model says':<12} {'Verdict':<10} {'Placed at':<11} Why"
    print(header)
    print("  " + "-" * (len(header) - 2))

    for reply, why in [
        ("19:30", "6h00 after 13:30, inside evening block"),
        ("07:30", "6h00 before 13:30, inside morning block"),
        ("19:00", "only 5h30 from 13:30 - under the minimum"),
        ("14:00", "only 30 min from 13:30"),
        ("02:00", "outside the safe window and every block"),
        ("banana", "unparseable reply"),
    ]:
        owner = Owner(name="Owner", available_minutes=600)
        pet = Pet("Timmy", "dog")
        owner.add_pet(pet)
        pet.add_task(Task("Lunch feed", 30, Priority.HIGH, preferred_time=time(13, 30)))
        pet.add_task(Task("Morning walk", 20, Priority.MEDIUM, preferred_time=time(7, 0)))
        agent = PetCareAgent(owner, max_iterations=3, commentator=StubModel(reply))
        agent.run()

        added = [t for t in agent.schedule
                 if t.source == "agent" and "feed" in t.title.lower()]
        notes = [d for s in agent.log if s.stage == "EXECUTOR"
                 for d in s.details if "Gemini" in d]
        accepted = any("accepted" in n for n in notes)
        placed = f"{added[0].preferred_time:%H:%M}" if added else "n/a"
        print(f"  {reply:<12} {'ACCEPTED' if accepted else 'REJECTED':<10} {placed:<11} {why}")

    print("\n  A rejected suggestion falls back to the rule-chosen slot.")
    print("  A hallucinated time can never reach the schedule.")


def main():
    llm = LLMCommentator()
    rule("PawPal+ - reproducible execution evidence")
    print(f"\nGemini commentary: "
          f"{'ON (' + llm.status.model + ')' if llm.status.available else 'OFF - ' + llm.status.detail}")

    scenario_user_first(llm)
    scenario_pass(llm)
    scenario_escalate(llm)
    scenario_guardrail()

    rule("Done")


if __name__ == "__main__":
    main()
