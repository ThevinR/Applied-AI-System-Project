# PawPal+ Model Card

**System:** PawPal+ agentic pet-care scheduler
**Version:** Capstone build (CodePath AI110)
**Type:** Deterministic, rule-based agentic loop, **augmented by an advisory LLM** (Google Gemini via `google-genai`). Every scheduling decision is made by hand-written rules; the LLM only narrates the audit trail and proposes slots that the rules then validate or reject. The system runs fully without an API key.
**Owner:** Thevin R.

## What this system is

PawPal+ takes a list of pet care tasks and produces a daily schedule. A four-stage agentic loop (**Planner → Executor → Evaluator → Reflector**) then reviews that schedule, repairs what it can, verifies the result against pet-care safety and nutrition rules, and either approves it or escalates the remainder to the human. Every run writes a timestamped reasoning trace to `ai_interactions.md`.

This document covers limitations, biases, misuse and reflection. For architecture, setup and examples, see [`README.md`](README.md); for design reasoning, see [`reflection.md`](reflection.md).

**A note on terminology.** This is a "model card" for a system with no model in it. Every decision comes from explicit constants and comparisons in `pawpal_system.py`, so there is no training data, no inference, no confidence score and no drift. I've kept the format because the *questions* a model card asks — who does this fail for, how could it be misused, what did testing reveal — apply to any system that makes decisions on someone's behalf. A rule-based system can be just as biased as a learned one; the bias is simply written down in the source instead of learned from data. That makes it auditable, not absent.

---

## 1. System limitations and potential biases

### 1.1 The authority shift, and why it's the main limitation

The Module 2 scheduler **warned** about conflicts and refused to resolve them. I defended that in `reflection.md` as the decision I was proudest of: "software that silently moves a medication is worse than software that points at the problem."

The capstone agentic loop **moves tasks and creates new ones.** That is a genuine reversal of the system's authority, and it is the single most consequential limitation here. I kept it because a review that can't act isn't an agent, and I constrained it rather than reversing it (see §2), but a user who trusted the Module 2 version's restraint would be surprised by what the capstone version does to their schedule.

Concretely: **the Executor mutates the real `Pet` and `Task` objects, and there is no undo.** Clicking "Run Agentic Review" rewrites your day. The sidebar warns before you click, which is mitigation, not a fix.

### 1.2 Hard limitations

| Limitation | Effect |
|---|---|
| **Keyword matching on task titles** | A task counts as a feeding only if `"feed"` appears in its title. A task called **"Breakfast"** is invisible to the nutrition rules, so the agent will report the pet as underfed and helpfully add a redundant "Feed Mia" on top of the breakfast that was already there. |
| **No midnight wraparound** | `_minutes()` has no modulo, so a 23:50 task lasting 30 minutes never conflicts with a 00:05 task. Inherited from the Module 2 scheduler and pinned by a test. |
| **Double completion** | Completing an already-completed recurring task spawns a duplicate occurrence. Also inherited and pinned by a test. |
| **Fixed 15-minute default** | Every task the agent creates is 15 minutes long, regardless of pet size, species or task. A great dane's walk and a kitten's water top-up get identical budgets. |
| **No duration validation in the model** | The Streamlit form enforces a 1-minute minimum; `Task` itself accepts anything. A negative duration would corrupt conflict detection silently. |
| **Single-day horizon** | The loop reasons about one day. It cannot see that it just scheduled the third late-night feeding in a row. |
| **No awareness of the owner's time budget** | The agent freely adds tasks without checking `available_minutes`. It can approve a schedule the owner has no time to actually perform. |

### 1.3 Potential biases

These are the assumptions I baked into the rules. Each one is a judgement about whose pet care counts as "normal."

**Species coverage is narrow and silently degrades.** `REQUIRED_CARE` covers dogs, cats and rabbits. Every other species falls back to `DEFAULT_REQUIRED_CARE` — feed and water only. A bird, reptile, ferret or fish therefore gets a schedule the agent will cheerfully mark **APPROVED** while having checked almost nothing. The failure is quiet: the user sees the same green banner a dog owner sees, with far less behind it. This is the bias I'd fix first, and the honest short-term fix is to *refuse* to approve species it has no rules for rather than approving them on a thin check.

**The "safe window" assumes a daytime human.** `SAFE_WINDOW` is 06:00–22:00. A night-shift nurse who feeds their cat at 23:30 before bed will have that flagged as unsafe and moved. The rule encodes a 9-to-5 household as correct, and penalises shift workers, people in very different time zones from the rule's author, and anyone whose animal is nocturnal by nature.

**Nutrition rules encode adult dog/cat norms.** "At least two feedings, at least six hours apart" is reasonable for a healthy adult dog or cat. It is wrong for a puppy or kitten (needs three or four), wrong for a rabbit (grazes continuously), wrong for many reptiles (may eat weekly), and wrong for a diabetic animal on a vet-specified schedule. The agent will actively *add feedings* to a schedule that a vet deliberately restricted.

**Pinning is only as good as the vocabulary.** `PINNED_KEYWORDS` covers `medicat`, `medicine`, `insulin`, `dose`. A task titled **"Apoquel"**, **"eye drops"** or **"inhaler"** is not recognised as time-critical and can be moved. The protection is strongest for users who happen to name tasks the way I did.

**English-only, and literal.** All matching is English substring matching. The system is unusable in another language and brittle even within English.

**Rules written by a non-expert.** I am not a veterinarian. These thresholds came from general knowledge, not clinical guidance. They are plausible defaults, not standards of care, and the system presents them with more confidence than their provenance deserves.

---

### 1.4 The LLM layer, and what it is deliberately not allowed to do

PawPal+ now calls Google Gemini (`google-genai`, `client.models.generate_content()`)
in two places. Both are **advisory**, and that boundary is the most important
design decision in the system:

| Where | What the model does | What it cannot do |
| --- | --- | --- |
| Planner / Reflector | Narrates findings and the verdict in plain English | Change a finding, or claim a plan is safe when issues are listed |
| Executor | Proposes a time for a missing second feeding | Place that time itself — every suggestion passes `_slot_acceptable()` first |

`_slot_acceptable()` re-checks a proposed time against exactly the rules a
rule-chosen slot must satisfy: inside a named day block, inside the safe
window, no clash with existing tasks, and at least `MIN_FEEDING_GAP_MINUTES`
from every other feeding. A suggestion that fails is logged as rejected and
the deterministic slot is used instead. **A hallucinated time cannot reach a
schedule.** The audit trail records whether each suggestion was accepted or
rejected, so the model's influence is always visible.

The system degrades to fully deterministic behaviour when the key is missing,
the SDK is absent, or a call fails. This is not hypothetical: during testing
the API returned `504 DEADLINE_EXCEEDED` and, after repeated calls,
`429 RESOURCE_EXHAUSTED`. In every case the loop completed normally without
commentary. The 34-test suite never touches the network, because
`PetCareAgent` defaults to `commentator=None`.

**User-first rule.** A time the owner typed is treated as a commitment:
`_is_user_fixed()` pins any task with `source == "owner"` and an explicit
`preferred_time`, so the Executor will not move it to resolve a conflict or a
feeding gap. If two owner-set times overlap, the agent reports the clash and
leaves the decision to the person rather than silently rewriting their day.
Only the owner's name is withheld from API calls; pet names, species, task
titles and times are sent.

## 2. Potential misuses and preventive guardrails

### 2.1 Misuses I consider realistic

**Treating an APPROVED verdict as veterinary approval.** This is the misuse that worries me most, because the UI invites it: a green banner reading "the schedule is safe, complete and well spaced." It means *the schedule satisfies four hard-coded rules I wrote*. It does not mean the animal is well cared for, and a user acting on that banner instead of calling a vet is exactly the harm the system could cause.

**Letting the agent reschedule medication.** Medication timing is clinical. A user who adds "give Apoquel at 08:00" — a title that doesn't match `PINNED_KEYWORDS` — could have it moved to 18:00 to clear an overlap, with the change recorded in a log they never read.

**Using `ai_interactions.md` as proof of care.** The log is an append-only record with timestamps, which makes it look evidentiary. It records *what the scheduler decided*, never what the owner actually did. Presenting it in a boarding dispute, a custody arrangement or a shelter audit would be misrepresenting a plan as a record of performance.

**Substituting the agent for attention.** A schedule that fixes itself can make someone feel the problem is handled. The system has no sensor and no feedback — it cannot tell whether the pet ate.

**Scaling it to an institution.** Nothing here is built for a shelter or boarding facility: no multi-user access control, no per-animal medical records, no vet sign-off, no audit integrity (the log is a plain file anyone can edit).

### 2.2 Guardrails that exist today

| Guardrail | Where | What it prevents |
|---|---|---|
| **Pinned medication is never moved** | `_is_pinned()`, `PetCareAgent.execute()` | The Executor moves the *other* task in a clash. If both are pinned it moves neither. |
| **Escalation instead of forced resolution** | `PetCareAgent.reflect()` | Two medications five minutes apart produce a `NEEDS REVIEW` verdict naming the clash, rather than a silent reshuffle. The agent fixes what it legitimately can and hands the rest back. |
| **Independent verification stage** | `PetCareAgent.evaluate()` | The Evaluator judges the Executor's output against goals the Executor wasn't optimising for, so a "fix" that breaks nutrition is caught rather than trusted. |
| **Guaranteed termination** | `PetCareAgent.reflect()` | Stops on approval, budget exhaustion, or a no-progress iteration. It cannot spin indefinitely or keep mutating a schedule. |
| **Full audit trail** | `LoopStep`, `log_agent_run()` | Every change is attributable: which stage made it, in which iteration, at what time, and why. |
| **Determinism** | No LLM in the decision path | Scheduling decisions come only from rules, so the same input yields the same schedule and a reviewer can reproduce any logged run exactly. LLM output is confined to commentary and to suggestions that `_slot_acceptable()` must approve. |
| **Explicit mutation warning** | Sidebar in `app.py` | The user is told before clicking that the review edits their schedule and that medication is exempt. |

### 2.3 Guardrails this system needs and does not have

Listing these because the gap between the two tables above is the honest measure of the system's safety.

1. **A standing disclaimer that this is not veterinary advice**, shown next to the verdict rather than buried in documentation. Cheapest fix with the largest effect on the worst misuse.
2. **Propose-and-confirm instead of direct mutation.** Show the diff, let the human apply it. This restores the Module 2 principle without giving up the agent's usefulness.
3. **A `care_type` field on `Task`,** replacing title keyword matching. This fixes "Breakfast", "Apoquel" and the English-only limitation in one change, and is the clearest next step for the codebase.
4. **Refusing to approve unknown species** rather than approving them against a two-rule check.
5. **A per-pet override for feeding count, spacing and safe window,** so a vet-prescribed schedule can be declared authoritative and left alone.
6. **A check against the owner's available minutes** before approving a schedule.

---

## 3. What surprised me while testing reliability

**The verification stage found a problem that nothing could fix.** The first working version of the loop deadlocked in a way I hadn't anticipated. The Evaluator correctly reported "Rex has 1 feeding; at least 2 are expected" — a real, correct finding — but only the *Planner's* issues were passed to the Executor. The Evaluator's findings went to the Reflector and nowhere else. So the Reflector dutifully ran another iteration, the Planner found nothing structurally wrong, the Executor did nothing, and the Evaluator reported the identical problem again, until the budget ran out. Every individual stage was behaving correctly and the loop as a whole was useless.

What surprised me is that this failure *looked like success* in the logs. There was a verdict, a full audit trail, sensible-sounding reasoning at every step, and a schedule at the end. Only by reading the trace line by line did it become clear that iteration 2 accomplished literally nothing. The fix was one line — carry the Evaluator's remaining issues into the next iteration's Executor — but the lesson was that **a loop can be individually correct at every stage and collectively broken**, and that an articulate audit trail is not evidence that anything happened. The no-progress stop condition in the Reflector exists because of this: an agent that notices it's stuck is strictly better than one that looks busy.

**The agent satisfied my rule while violating my intent.** Once the feedback path worked, the agent fixed the nutrition gap by adding a second feeding — at **06:30**, and named it "Evening feed Rex". It had searched for a free slot from the start of the day forward, found 06:30 open, and confirmed the four-hour gap to the midday feeding. Every rule I had written was satisfied. The result was nonsense: a dog fed at 06:30 and 12:00 with nothing for the following ten hours.

This is specification gaming in a system with no learning in it at all, which I found genuinely clarifying. I had assumed that writing the rules by hand meant the system would do what I meant. It did exactly what I *wrote* — "at least four hours apart" — and the gap between that and "spread across the day" was invisible to me until I read the output. I first fixed it by searching backwards from the end of the day, and later replaced that with named day blocks (morning/afternoon/evening) plus a six-hour minimum, but the general lesson stands: specifying behaviour precisely is hard independent of the technology, and I only caught it because I read the schedule rather than the verdict.

**Refusing to act was the output I trusted most.** The run I'm most confident in is the one where the agent *failed*: two pinned medications five minutes apart, which it could not resolve. It fixed four other problems, left the clash alone, said "left 'Insulin dose' / 'Thyroid medication' overlapping: both are time-critical", and returned `NEEDS REVIEW`. Watching it decline to reshuffle medication did more for my confidence in the whole system than any of the approvals, because it was the first evidence that the constraints held under pressure rather than just in the happy path.

**The environment was less reliable than the code.** While verifying the Streamlit integration, the app crashed with `source code string cannot contain null bytes`. The cause was nothing to do with PawPal+: `pandas/__init__.py` in the virtual environment was 8318 bytes of pure null bytes, apparently from disk corruption. Any `st.table()` call failed, and the pre-existing app had only avoided it by having no tasks to render in that particular test. It was a reminder that "my tests pass" and "this works on this machine" are different claims, and that a green suite tells you nothing about the layer underneath it. Reinstalling pandas fixed it.

**Manual verification kept passing for the component with almost no tests.** 34 of the 35 tests cover the original `Scheduler`. The agentic loop — the newest and most consequential part — has one regression test and otherwise relies on the `demo.py` scenarios and a randomised fuzz harness. Every scenario I tried by hand worked, which is precisely why the gap persisted: the loop kept *looking* reliable. The two real bugs above were both found by reading output, not by a failing test, and neither would be caught today if someone reintroduced them. That's documented in the README's testing section and it's the first thing I'd address next.

---

### 3.1 What the Gemini integration surprised me with

Adding a real external API produced failures I could not have predicted from the docs,
and each one changed the code:

| What happened | What it forced |
| --- | --- |
| `404` — `gemini-2.5-flash` is "no longer available to new users" | Probing the key's 28 visible models instead of trusting a documented default |
| The model the error *recommended* (`gemini-3.8-flash`) hung and returned `504` after 48s | A hard `LLM_TIMEOUT_MS = 20000` client timeout, so the UI can never hang |
| `429` revealed the free tier allows only **20 requests per day** | `LLM_MAX_CALLS_PER_RUN = 4`, capping one review's cost so a day's quota survives ~5 reviews |
| Quota exhausted mid-testing | Proving the fallback path for real: every run still produced a schedule |

The most useful surprise was the smallest. I tested the validation gate with a stub
returning `19:00` for a second feeding, expecting it to pass — it looks perfectly
sensible. It was rejected: 13:30 → 19:00 is 5h30, under the six-hour minimum. A time
that survives human plausibility checking can still break a nutritional rule, which is
the clearest argument I have for keeping the gate rather than trusting good-looking
model output.

## 4. AI collaboration notes

This section covers the capstone phase (the agentic loop, Streamlit integration, logging, diagrams and documentation). The Module 2 collaboration is documented in [`reflection.md` §3](reflection.md).

### How I worked with the assistant

I ran each phase as a separate session with a specific target: implement the loop; integrate it into the UI with logging; produce the architecture diagram; rewrite the README. The pattern that kept working was the one from Module 2 — **give the assistant a narrow goal and make it show its work**. It ran the code it wrote, so the examples in the README are captured output rather than plausible-looking inventions, and it caught both of the bugs in §3 by actually executing the loop and reading the result.

The most valuable thing it did was refuse to report success it hadn't verified. When the Streamlit app wouldn't render, it investigated rather than declaring the integration complete, and traced the failure to corrupted pandas — a cause outside the code entirely.

### A suggestion I accepted

**The Reflector's no-progress stop condition.**

My specification for the Reflector was "decide whether the schedule is approved or needs another iteration." That implies two outcomes. The assistant implemented three, adding a stop condition for when an iteration leaves the same number of issues as the previous pass, on the grounds that further iterations would only repeat themselves.

I accepted it, and on reflection it's load-bearing. My two-outcome version would have burned the full iteration budget on the Example 3 scenario — two pinned medications that *cannot* be resolved — re-running identical work three times and producing an audit trail full of repeated activity. The three-outcome version stops after two and says why. It also turned out to be the correct response to the deadlock bug in §3: the no-progress stop is what converts "spins pointlessly until the budget expires" into "notices it's stuck and escalates."

I accepted it because I could verify it: the Example 3 run shows the escalation working, and the stop condition's reasoning appears explicitly in the log. It's a case where the assistant answered a slightly larger question than I asked and the larger question was the right one.

### A suggestion I modified

**Where the run logs were written.**

I asked for each agent run to log its steps, reasoning and timestamps "into `ai_interactions.md` in the project root." Taken literally, that means appending run output to that file — which would have been wrong, and the assistant flagged it before writing anything: `ai_interactions.md` is the CodePath template for *hand-written* notes on stretch features SF7 and SF11. A few dozen machine-generated run entries appended to it would have buried the sections a grader is actually looking for, and there's no clean way to separate them afterwards.

The modified version appends entries under a dedicated `## Agentic Loop Run Log (auto-generated)` heading at the end of the file, created once, with everything above it untouched. I kept the requirement (logs do go in `ai_interactions.md`) and changed the implementation so it doesn't destroy the file's original purpose.

This is the same judgement call as the Module 2 one about tests that silently "fixed" bugs: the instruction and the intent came apart, and the right move was to satisfy the intent and say so, rather than follow the instruction literally into a mess.

### A suggestion I pushed back on

**Mutating the schedule directly.**

The Executor edits the user's real `Pet` and `Task` objects — it adds tasks and changes times in place, with no undo. The assistant implemented it that way and flagged it twice: once when building the loop, and again when wiring the UI, where it added the sidebar warning unprompted and asked whether I'd prefer a propose-and-confirm diff instead.

I kept the mutation for this build, because a review whose fixes evaporate is theatre and the capstone needed a working agent. But I don't think it's the right long-term design, and it directly contradicts the principle I argued for in `reflection.md` — that this system should advise rather than act on things the owner cares about. It's recorded as a known trade-off in the README, as misuse risk §2.1 here, and as missing guardrail §2.3.2. If I continued this project, replacing direct mutation with a reviewable diff would be the first change I made.

### What I'd tell someone starting this

The assistant was faster than me at everything except deciding what the system should be allowed to do — which is the same conclusion I reached in Module 2, and the capstone sharpened rather than changed it. The two genuine bugs in this build weren't typos; they were **gaps between what I specified and what I meant**: a loop that verified without a path to act, and a feeding rule that was satisfiable at 06:30. The assistant implemented my specification faithfully in both cases. Nothing in the tooling could tell me my specification was the problem — that only showed up when I read the actual output and asked whether it made sense for a dog.

---

## Summary

| | |
|---|---|
| **Intended use** | Personal daily pet-care planning for common household pets, with a human reviewing the result. |
| **Out of scope** | Veterinary guidance, medication timing decisions, institutional or multi-user care, proof of care performed, any species without explicit rules. |
| **Decision basis** | Hand-written deterministic rules. No trained model and no learned parameters. An LLM (Gemini) contributes commentary and rule-validated suggestions only, and is optional. |
| **Known to fail for** | Non-dog/cat/rabbit species, night-shift owners, puppies/kittens/grazing animals, vet-restricted feeding schedules, tasks not named with recognised keywords, non-English users. |
| **Strongest safeguard** | Escalates rather than forcing a resolution it cannot justify, and never moves pinned medication. |
| **Weakest point** | An "APPROVED" verdict reads as broader assurance than the four rules behind it actually provide. |
| **Human oversight** | Required. Every verdict is advisory, and `NEEDS REVIEW` means a human decision is needed, not that the system failed. |
