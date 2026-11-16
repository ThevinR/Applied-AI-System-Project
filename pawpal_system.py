import os
import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from uuid import uuid4

# Load .env (GEMINI_API_KEY, GEMINI_MODEL) if python-dotenv is installed.
# Optional: the scheduling core works without it, and without any API key.
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - only hit in a bare environment
    pass


class Priority(Enum):
    LOW = 1
    MEDIUM = 2
    HIGH = 3


class Frequency(Enum):
    ONCE = "once"
    DAILY = "daily"
    WEEKLY = "weekly"


@dataclass
class Task:
    title: str
    duration_minutes: int
    priority: Priority
    pet_name: str = ""  # set by Pet.add_task so plans can say whose task it is
    preferred_time: Optional[time] = None
    frequency: Frequency = Frequency.DAILY
    is_completed: bool = False
    due_date: date = field(default_factory=date.today)
    #: Who created this task. "owner" means a person typed it in, so its
    #: preferred_time is a commitment the agent must not quietly rewrite.
    #: "agent" marks a task the loop added to fill a gap, which it may move.
    source: str = "owner"
    id: str = field(default_factory=lambda: uuid4().hex)

    def mark_complete(self) -> Optional["Task"]:
        """Mark this task as done; return the next occurrence if it recurs.

        DAILY tasks recur one day later and WEEKLY tasks one week later, as a
        fresh pending copy with a new id. ONCE tasks return None.
        """
        self.is_completed = True
        if self.frequency == Frequency.DAILY:
            step = timedelta(days=1)
        elif self.frequency == Frequency.WEEKLY:
            step = timedelta(weeks=1)
        else:
            return None
        return replace(
            self, is_completed=False, due_date=self.due_date + step, id=uuid4().hex
        )


@dataclass
class Pet:
    name: str
    species: str
    tasks: List[Task] = field(default_factory=list)

    def add_task(self, task: Task) -> None:
        """Assign a task to this pet and add it to the pet's task list."""
        task.pet_name = self.name
        self.tasks.append(task)

    def complete_task(self, task_id: str) -> Optional[Task]:
        """Complete a task by id; recurring tasks get their next occurrence added.

        Returns the newly added next occurrence, or None if the task does not
        recur or no task has that id.
        """
        for task in self.tasks:
            if task.id == task_id:
                nxt = task.mark_complete()
                if nxt is not None:
                    self.add_task(nxt)
                return nxt
        return None

    def remove_task(self, task_id: str) -> None:
        """Remove the task with the given id from this pet's task list."""
        self.tasks = [t for t in self.tasks if t.id != task_id]


@dataclass
class Owner:
    name: str
    available_minutes: int
    pets: List[Pet] = field(default_factory=list)

    def add_pet(self, pet: Pet) -> None:
        """Add a pet to this owner's list of pets."""
        self.pets.append(pet)

    def get_all_tasks(self) -> List[Task]:
        """Every task across all of the owner's pets, in one list."""
        return [task for pet in self.pets for task in pet.tasks]


def _minutes(t: time) -> int:
    """Convert a time of day to minutes since midnight."""
    return t.hour * 60 + t.minute


class Scheduler:
    """The brain: pulls tasks across all of an owner's pets and organizes them."""

    def __init__(self, owner: Owner):
        self.owner = owner
        self.plan: List[Task] = []
        self.skipped: List[Task] = []

    def get_tasks(self) -> List[Task]:
        """Read tasks from the Owner so every pet is included."""
        return self.owner.get_all_tasks()

    def sort_tasks(self, tasks: List[Task]) -> List[Task]:
        """Order tasks for planning: highest priority first, then earliest
        preferred time, then shortest duration.

        Tasks with no preferred time sort after timed tasks of the same priority.
        Returns a new list; the input is not modified.
        """
        return sorted(
            tasks,
            key=lambda t: (
                -t.priority.value,
                t.preferred_time is None,
                t.preferred_time or time.max,
                t.duration_minutes,
            ),
        )

    def sort_by_time(self, tasks: List[Task]) -> List[Task]:
        """Order tasks chronologically by preferred time, ignoring priority.

        Tasks with no preferred time go last. The sort is stable, so tasks
        sharing a time keep their original relative order. Returns a new list.
        """
        return sorted(
            tasks,
            key=lambda t: (t.preferred_time is None, t.preferred_time or time.max),
        )

    def filter_tasks(
        self,
        tasks: List[Task],
        pet_name: Optional[str] = None,
        completed: Optional[bool] = None,
    ) -> List[Task]:
        """Keep only tasks matching the given pet name and/or completion status.

        Args:
            tasks: Tasks to filter.
            pet_name: If given, keep only this pet's tasks.
            completed: If True keep done tasks, if False keep pending ones,
                if None do not filter on completion.

        Both filters combine (AND). Returns a list; the input is not modified.
        """
        result = tasks
        if pet_name is not None:
            result = [t for t in result if t.pet_name == pet_name]
        if completed is not None:
            result = [t for t in result if t.is_completed == completed]
        return result

    def detect_conflicts(self, tasks: List[Task]) -> List[tuple]:
        """Find pairs of tasks whose preferred-time windows overlap.

        A task occupies [preferred_time, preferred_time + duration). Two tasks
        conflict when each starts before the other ends, so back-to-back tasks
        do not conflict. Works across pets and within one pet. Tasks without a
        valid preferred time are skipped rather than raising.

        Compares every pair, so it is O(n^2); fine for a day's worth of tasks.

        Returns:
            A list of (earlier_listed_task, later_listed_task) tuples.
        """
        # Skip anything without a usable time instead of raising on bad data.
        timed = [t for t in tasks if isinstance(t.preferred_time, time)]
        conflicts = []
        for i, a in enumerate(timed):
            a_start = _minutes(a.preferred_time)
            a_end = a_start + a.duration_minutes
            for b in timed[i + 1:]:
                b_start = _minutes(b.preferred_time)
                b_end = b_start + b.duration_minutes
                if a_start < b_end and b_start < a_end:
                    conflicts.append((a, b))
        return conflicts

    def conflict_warnings(self, tasks: List[Task]) -> List[str]:
        """Turn detected conflicts into warning strings; never raises.

        Each message says whether the clash is within one pet or between
        different pets. Returns an empty list when nothing overlaps.
        """
        return [
            f"Warning ({'same pet' if a.pet_name == b.pet_name else 'different pets'}): "
            f"'{a.title}' ({a.pet_name}, {a.preferred_time:%H:%M}) overlaps "
            f"'{b.title}' ({b.pet_name}, {b.preferred_time:%H:%M})"
            for a, b in self.detect_conflicts(tasks)
        ]

    def generate_plan(self, today: Optional[date] = None) -> List[Task]:
        """Greedily fit pending tasks due today into the owner's available time.

        Tasks are taken in sort_tasks order. Each one that fits in the remaining
        minutes goes into self.plan; any that does not goes into self.skipped
        (a later, shorter task can still fit). Clock times are not enforced, so
        planned tasks may still overlap; see conflict_warnings.

        Args:
            today: The date to plan for; defaults to date.today().

        Returns:
            The planned tasks, in planning order.
        """
        today = today or date.today()
        pending = [
            t
            for t in self.filter_tasks(self.get_tasks(), completed=False)
            if t.due_date <= today
        ]
        self.plan = []
        self.skipped = []
        remaining = self.owner.available_minutes
        for task in self.sort_tasks(pending):
            if task.duration_minutes <= remaining:
                self.plan.append(task)
                remaining -= task.duration_minutes
            else:
                self.skipped.append(task)
        return self.plan

    def explain_plan(self) -> str:
        """Describe what was planned, what was skipped and why, and any conflicts."""
        if not self.plan and not self.skipped:
            return "No plan yet, or no pending tasks."
        used = sum(t.duration_minutes for t in self.plan)
        lines = [
            f"Planned {used} of {self.owner.available_minutes} available minutes "
            "(highest priority first, then earliest preferred time, then shortest)."
        ]
        for t in self.plan:
            at = f" at {t.preferred_time:%H:%M}" if t.preferred_time else ""
            lines.append(
                f"- {t.pet_name}: {t.title} ({t.duration_minutes} min, "
                f"{t.priority.name.lower()}){at}"
            )
        for t in self.skipped:
            lines.append(
                f"- Skipped {t.pet_name}: {t.title} "
                f"({t.duration_minutes} min, {t.priority.name.lower()}): not enough time left"
            )
        lines.extend(self.conflict_warnings(self.plan))
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Care rules: the domain knowledge the agentic loop reasons against.
# ---------------------------------------------------------------------------

#: Care steps every pet of a species needs on a normal day. Matching is done on
#: lowercase substrings of the task title, so "Morning feed" satisfies "feed".
REQUIRED_CARE: Dict[str, Tuple[str, ...]] = {
    "dog": ("feed", "water", "walk"),
    "cat": ("feed", "water", "litter"),
    "rabbit": ("feed", "water", "clean"),
}
#: Used for any species not listed above.
DEFAULT_REQUIRED_CARE: Tuple[str, ...] = ("feed", "water")

#: Words that count as each care step. A task satisfies a step when its title
#: contains any of these substrings, so "Dinner for Mochi" covers "feed" and
#: "Bath time" covers "clean". Keep entries specific: a short word such as
#: "eat" or "run" would also match inside other words ("treat", "morning").
CARE_WORDS: Dict[str, Tuple[str, ...]] = {
    "feed": ("feed", "food", "meal", "breakfast", "lunch", "dinner", "supper",
             "kibble", "snack", "bowl"),
    "water": ("water", "drink", "hydrat", "fountain"),
    "walk": ("walk", "stroll", "jog", "hike", "exercise", "park"),
    "litter": ("litter", "poop", "scoop"),
    "clean": ("clean", "cage", "bedding", "hutch", "bath"),
    "vet": ("vet", "veterin", "checkup", "check-up", "clinic", "vaccin"),
}

#: Minimum gap between two feedings for the same pet, so a day's food is not
#: served all at once (a bloat / overfeeding risk). Six hours is the usual
#: floor for a twice-a-day feeding routine.
MIN_FEEDING_GAP_MINUTES = 360
#: What the Executor aims for when it places a supplementary feeding: roughly
#: half the waking day, so morning and evening meals sit opposite each other.
#: The Evaluator only enforces the minimum; this is the target, not a rule.
IDEAL_FEEDING_GAP_MINUTES = 420
#: Feeding, walking and medication should fall inside waking hours.
SAFE_WINDOW = (time(6, 0), time(22, 0))
#: Medication is time-critical: the executor will not move it.
PINNED_KEYWORDS = ("medicat", "medicine", "insulin", "dose")

#: The parts of the day a person actually recognises. Daily care is placed
#: inside one of these blocks so a generated schedule reads like a real
#: routine ("dinner at 18:00") instead of landing on whichever half-hour
#: happened to be free first ("second feed at 10:00").
DAY_BLOCKS: Dict[str, Tuple[time, time]] = {
    "morning": (time(7, 0), time(9, 0)),
    "afternoon": (time(13, 0), time(15, 0)),
    "evening": (time(18, 0), time(20, 0)),
}

#: Which blocks suit which kind of care, best first. A second feeding should
#: land in the evening rather than be squeezed in beside breakfast, so "feed"
#: prefers morning then evening, keeping afternoon as the third meal slot.
_BLOCK_PREFERENCE: Dict[str, Tuple[str, ...]] = {
    "feed": ("morning", "evening", "afternoon"),
    "walk": ("morning", "evening", "afternoon"),
    "water": ("morning", "afternoon", "evening"),
    "litter": ("morning", "afternoon", "evening"),
    "clean": ("afternoon", "morning", "evening"),
}
#: Used for care that matches none of the keywords above.
_DEFAULT_BLOCK_ORDER: Tuple[str, ...] = ("morning", "afternoon", "evening")

#: Start times are snapped to this grid, so plans read in round numbers.
_BLOCK_GRID_MINUTES = 15

#: Last resort when every block is full: any half hour in the safe window.
_FALLBACK_SLOTS = tuple(time(h, m) for h in range(6, 22) for m in (0, 30))


def _matches(task: Task, keyword: str) -> bool:
    """True when a task's title covers the given care keyword or any synonym."""
    title = task.title.lower()
    words = CARE_WORDS.get(keyword, (keyword,))
    return any(word in title for word in words)


def _is_medication(task: Task) -> bool:
    """Medication-like tasks are time-critical and must keep their exact time."""
    title = task.title.lower()
    return any(k in title for k in PINNED_KEYWORDS)


def _is_user_fixed(task: Task) -> bool:
    """True when a person chose this task's time themselves.

    User-first rule: a time the owner typed in is a commitment, not a
    suggestion. The agent may add care around it, but never silently shifts
    it. A task the owner left untimed is not fixed — they expressed no
    preference, so the agent is free to place it.
    """
    return task.source == "owner" and isinstance(task.preferred_time, time)


def _is_pinned(task: Task) -> bool:
    """True when the Executor must not move this task.

    Covers both medication (clinically time-critical) and any time the owner
    entered by hand (their decision to make, not the agent's).
    """
    return _is_medication(task) or _is_user_fixed(task)


def is_user_fixed(task: Task) -> bool:
    """Public alias of _is_user_fixed, for the UI to badge owner-set times."""
    return _is_user_fixed(task)


def _pin_reason(task: Task) -> str:
    """Why a task cannot be moved, for the audit trail."""
    if _is_medication(task):
        return "time-critical medication"
    if _is_user_fixed(task):
        return "a time you set yourself"
    return "not pinned"


def _from_minutes(total: int) -> time:
    """Inverse of _minutes, clamped to a valid 24-hour clock."""
    total = max(0, min(total, 23 * 60 + 59))
    return time(total // 60, total % 60)


@dataclass
class Issue:
    """One problem the Planner or Evaluator found with a schedule."""

    kind: str  # "missing_care" | "conflict" | "feeding_gap" | "unsafe_time" | "untimed"
    message: str
    pet_name: str = ""
    task_id: Optional[str] = None
    keyword: str = ""
    severity: Priority = Priority.MEDIUM

    def __str__(self) -> str:
        return f"[{self.severity.name}] {self.kind}: {self.message}"


@dataclass
class LoopStep:
    """A single Planner/Executor/Evaluator/Reflector step, for the audit log."""

    iteration: int
    stage: str
    summary: str
    details: List[str] = field(default_factory=list)
    timestamp: datetime = field(default_factory=datetime.now)
    #: Optional LLM narration of this step. Commentary only — it never
    #: changes what the deterministic rules decided.
    commentary: str = ""

    def render(self) -> str:
        head = f"Iter {self.iteration} | {self.stage:<9} | {self.summary}"
        lines = [head] + [f"    - {d}" for d in self.details]
        if self.commentary:
            lines.append(f"    > {self.commentary}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Optional LLM commentary (Google Gemini via the official google-genai SDK)
# ---------------------------------------------------------------------------

#: Model used for audit commentary. Overridable with GEMINI_MODEL in .env.
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash"
#: Hard ceiling on a single call, so a slow or overloaded model can never hang
#: the Streamlit UI. Some Gemini models deadline on the server side; when that
#: happens the loop simply runs without commentary.
LLM_TIMEOUT_MS = 20_000
LLM_MAX_OUTPUT_TOKENS = 2_000
#: Calls allowed per agentic review. The Gemini free tier permits only 20
#: generate_content requests PER DAY, so an unbounded loop would exhaust a
#: day's quota in two or three reviews. Capping per run keeps the cost of a
#: single review predictable and leaves the rules to fill in the rest.
LLM_MAX_CALLS_PER_RUN = 4

_LLM_SYSTEM_INSTRUCTION = (
    "You are the narrator for PawPal+, a deterministic pet-care scheduling "
    "agent. You are given findings that rule-based code has ALREADY decided. "
    "Explain them to a pet owner in plain, warm, practical English. "
    "Never contradict the findings, never invent tasks, times, pets or "
    "problems, and never claim a schedule is safe when issues are listed. "
    "Be concise: at most 3 short sentences, no headings, no bullet points, "
    "no markdown."
)


def _parse_hhmm(text: str) -> Optional[time]:
    """Pull an HH:MM time out of a model reply, or return None.

    Deliberately forgiving about surrounding text but strict about the value:
    anything that is not a real 24-hour time is rejected rather than guessed at.
    """
    if not text:
        return None
    match = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", text)
    if match is None:
        return None
    return time(int(match.group(1)), int(match.group(2)))


@dataclass
class LLMStatus:
    """Whether LLM commentary is active, and why not when it is not."""

    available: bool
    model: str = ""
    detail: str = ""


class LLMCommentator:
    """Turns the loop's structured findings into human-readable commentary.

    Deliberately advisory only. The deterministic rules in this module remain
    the single source of truth: this class never decides whether a pet is fed,
    whether a time is safe, or whether a run is approved — it only narrates
    decisions already made. If the key is missing, the SDK is absent, or a call
    fails or times out, every method returns "" and the agent behaves exactly
    as it does offline. That keeps the safety-critical path testable and
    independent of a network service.

    The owner's name is never sent to the API; only pet names, species, task
    titles, times and rule findings are.
    """

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None,
                 enabled: bool = True):
        self._client = None
        self._types = None
        self.model = model or os.getenv("GEMINI_MODEL") or DEFAULT_GEMINI_MODEL
        self.status = LLMStatus(available=False, model=self.model)
        #: Set when a call fails, so the UI can show why commentary is absent.
        self.last_error: str = ""
        #: Calls spent in the current review, reset by PetCareAgent.run().
        self.calls_made: int = 0
        self.max_calls_per_run: int = LLM_MAX_CALLS_PER_RUN

        if not enabled:
            self.status.detail = "disabled by caller"
            return

        key = api_key or os.getenv("GEMINI_API_KEY")
        if not key:
            self.status.detail = (
                "no GEMINI_API_KEY found — copy .env.example to .env and add one"
            )
            return

        try:
            from google import genai
            from google.genai import types
        except ImportError:
            self.status.detail = "google-genai is not installed (pip install google-genai)"
            return

        try:
            self._types = types
            self._client = genai.Client(
                api_key=key,
                http_options=types.HttpOptions(timeout=LLM_TIMEOUT_MS),
            )
            self.status = LLMStatus(available=True, model=self.model,
                                    detail="ready")
        except Exception as exc:  # noqa: BLE001 - never break the loop
            self.status.detail = f"client init failed: {type(exc).__name__}"

    # -- internals -------------------------------------------------------

    def _generate(self, prompt: str) -> str:
        """One guarded call. Returns "" on any failure, never raises."""
        if self._client is None:
            return ""
        if self.calls_made >= self.max_calls_per_run:
            self.last_error = (
                f"per-review call budget ({self.max_calls_per_run}) reached - "
                "remaining steps used the rules only"
            )
            return ""
        self.calls_made += 1
        try:
            response = self._client.models.generate_content(
                model=self.model,
                contents=prompt,
                config=self._types.GenerateContentConfig(
                    system_instruction=_LLM_SYSTEM_INSTRUCTION,
                    max_output_tokens=LLM_MAX_OUTPUT_TOKENS,
                    temperature=0.3,
                ),
            )
            text = (response.text or "").strip()
            if text:
                self.last_error = ""
            return text
        except Exception as exc:  # noqa: BLE001 - commentary is best-effort
            text = str(exc)
            if "RESOURCE_EXHAUSTED" in text or "429" in text:
                self.last_error = "Gemini quota exceeded (429) - commentary skipped"
            elif "DEADLINE_EXCEEDED" in text or "504" in text:
                self.last_error = "Gemini timed out (504) - commentary skipped"
            else:
                self.last_error = f"{type(exc).__name__}: {text[:140]}"
            return ""

    def reset_run_budget(self) -> None:
        """Start a fresh per-review call allowance."""
        self.calls_made = 0

    @staticmethod
    def _describe(schedule: List[Task]) -> str:
        if not schedule:
            return "(the plan is empty)"
        return "\n".join(
            f"- {t.preferred_time:%H:%M} {t.pet_name}: {t.title} "
            f"({t.duration_minutes} min, {t.priority.name.lower()})"
            if isinstance(t.preferred_time, time) else
            f"- (no time) {t.pet_name}: {t.title} ({t.duration_minutes} min)"
            for t in schedule
        )

    # -- the two narrated stages ----------------------------------------

    def suggest_care_time(self, pet_name: str, species: str, care: str,
                          existing: List[Task], blocks: Dict[str, Tuple[time, time]],
                          duration_minutes: int, min_gap_minutes: int) -> Optional[time]:
        """Ask the model where a missing care task should go.

        The model only *proposes* a time. The caller re-checks it against the
        same deterministic rules every other slot must pass (inside a day
        block, no clash, far enough from existing feedings), and discards the
        suggestion if it fails. A hallucinated time can therefore never reach
        a schedule — the worst case is falling back to the rule-based slot.

        Returns a parsed time, or None if unavailable or unparseable.
        """
        if self._client is None:
            return None
        current = "\n".join(
            f"- {t.preferred_time:%H:%M} {t.title} ({t.duration_minutes} min)"
            for t in existing if isinstance(t.preferred_time, time)
        ) or "- (none scheduled yet)"
        block_text = ", ".join(
            f"{name} {start:%H:%M}-{end:%H:%M}" for name, (start, end) in blocks.items()
        )
        reply = self._generate(
            f"Choose a time of day for a '{care}' task for {pet_name}, a "
            f"{species}. It lasts {duration_minutes} minutes.\n\n"
            f"That pet's existing scheduled care:\n{current}\n\n"
            f"Allowed blocks: {block_text}.\n"
            f"Rule: it must be at least {min_gap_minutes} minutes "
            f"({min_gap_minutes // 60} hours) away from every existing feeding, "
            "and must sit inside one of the allowed blocks. A supplementary "
            "feeding belongs in the afternoon or evening, not next to the "
            "morning meal.\n\n"
            "Answer with the start time ONLY, as 24-hour HH:MM. No other text."
        )
        return _parse_hhmm(reply)

    def planner_commentary(self, pets: List[Pet], schedule: List[Task],
                           issues: List[Issue]) -> str:
        """Explain what the Planner diagnosed, in an owner's terms."""
        if self._client is None:
            return ""
        roster = ", ".join(f"{p.name} ({p.species})" for p in pets) or "no pets"
        findings = "\n".join(f"- {i}" for i in issues) or "- none"
        return self._generate(
            "A rule-based planner has just inspected today's pet-care plan.\n\n"
            f"Pets: {roster}\n\n"
            f"Current plan:\n{self._describe(schedule)}\n\n"
            f"Issues the rules found:\n{findings}\n\n"
            "In at most 3 sentences, tell the owner what these findings mean "
            "and why they matter for the animals' wellbeing. If there are no "
            "issues, say briefly that the plan looks complete."
        )

    def reflector_commentary(self, verdict: str, iteration: int,
                             issues: List[Issue], schedule: List[Task],
                             available_minutes: int) -> str:
        """Explain the Reflector's verdict and what the owner should do next."""
        if self._client is None:
            return ""
        findings = "\n".join(f"- {i}" for i in issues) or "- none"
        committed = sum(t.duration_minutes for t in schedule)
        return self._generate(
            f"A rule-based reviewer has reached the verdict: {verdict} "
            f"(after iteration {iteration}).\n\n"
            f"Final plan ({committed} of {available_minutes} available minutes):\n"
            f"{self._describe(schedule)}\n\n"
            f"Outstanding issues:\n{findings}\n\n"
            "In at most 3 sentences, explain the verdict to the owner. If "
            "issues remain, say plainly what they should decide or change "
            "(for example allowing more time, or dropping a task). Do not "
            "promise the schedule is fine if issues are listed."
        )


# ---------------------------------------------------------------------------
# The agentic loop
# ---------------------------------------------------------------------------


class PetCareAgent:
    """Planner -> Executor -> Evaluator -> Reflector loop over a pet schedule.

    The agent takes the Scheduler's draft plan and iterates on it until the
    schedule is safe and nutritionally sound, or until max_iterations is spent.
    Every stage appends to self.log, so the whole run can be replayed.
    """

    def __init__(self, owner: Owner, scheduler: Optional[Scheduler] = None,
                 max_iterations: int = 3,
                 commentator: Optional["LLMCommentator"] = None):
        self.owner = owner
        self.scheduler = scheduler or Scheduler(owner)
        self.max_iterations = max_iterations
        #: Optional Gemini narrator. Defaults to None so the loop — and the
        #: test suite — never touches the network unless a caller opts in.
        self.commentator = commentator
        self.log: List[LoopStep] = []
        self.schedule: List[Task] = []
        #: Care the Executor could not fit in the owner's available time, set
        #: each pass and read by the Evaluator.
        self._deferred: List[Issue] = []
        #: Audit notes about Gemini's slot suggestions (accepted or rejected),
        #: rebuilt each Executor pass.
        self._llm_placements: List[str] = []
        self.approved = False
        self.iterations_used = 0
        self.started_at: Optional[datetime] = None
        self.finished_at: Optional[datetime] = None

    # -- helpers ---------------------------------------------------------

    def _record(self, iteration: int, stage: str, summary: str,
                details: Optional[List[str]] = None,
                commentary: str = "") -> None:
        self.log.append(
            LoopStep(iteration, stage, summary, list(details or []),
                     commentary=commentary)
        )

    def _pet(self, name: str) -> Optional[Pet]:
        return next((p for p in self.owner.pets if p.name == name), None)

    def _required_for(self, pet: Pet) -> Tuple[str, ...]:
        return REQUIRED_CARE.get(pet.species.lower(), DEFAULT_REQUIRED_CARE)

    def _occupied(self, schedule: List[Task], exclude: Optional[Task] = None
                  ) -> List[Tuple[int, int]]:
        """Busy [start, end) minute windows, ignoring one task if given."""
        spans = []
        for t in schedule:
            if t is exclude or not isinstance(t.preferred_time, time):
                continue
            start = _minutes(t.preferred_time)
            spans.append((start, start + t.duration_minutes))
        return spans

    @staticmethod
    def _block_order(task: Task, anchors: List[int],
                     crowding: Optional[Dict[str, int]] = None) -> List[str]:
        """Rank the day's blocks for this task, best first.

        Three things decide the order:
          * anchors - the pet's existing feedings. The block furthest from them
            wins, so a second meal is pushed to the opposite end of the day
            rather than clustering beside the first.
          * crowding - how many of this pet's tasks already sit in each block.
            Emptier blocks win, which spreads a pet's day out instead of
            stacking its whole routine into the morning.
          * the care-type preference, which breaks any remaining tie.
        """
        keyword = next((k for k in _BLOCK_PREFERENCE if _matches(task, k)), None)
        preference = _BLOCK_PREFERENCE.get(keyword, _DEFAULT_BLOCK_ORDER)
        ranked = [name for name in preference if name in DAY_BLOCKS]
        ranked += [name for name in DAY_BLOCKS if name not in ranked]
        counts = crowding or {}

        def distance(name: str) -> int:
            if not anchors:
                return 0
            start, end = DAY_BLOCKS[name]
            centre = (_minutes(start) + _minutes(end)) // 2
            return min(abs(centre - anchor) for anchor in anchors)

        return sorted(
            ranked,
            key=lambda name: (-distance(name), counts.get(name, 0), ranked.index(name)),
        )

    @staticmethod
    def _crowding(task: Task, schedule: List[Task]) -> Dict[str, int]:
        """Count how many of this pet's tasks already fall in each day block."""
        counts = {name: 0 for name in DAY_BLOCKS}
        for other in schedule:
            if other is task or other.pet_name != task.pet_name:
                continue
            if not isinstance(other.preferred_time, time):
                continue
            start = _minutes(other.preferred_time)
            for name, (block_start, block_end) in DAY_BLOCKS.items():
                if _minutes(block_start) <= start < _minutes(block_end):
                    counts[name] += 1
                    break
        return counts

    def _slot_in_block(self, task: Task, name: str, busy: List[Tuple[int, int]],
                       anchors: List[int], min_gap: int) -> Optional[time]:
        """First free, correctly spaced start time inside one named block."""
        block_start, block_end = DAY_BLOCKS[name]
        safe_high = _minutes(SAFE_WINDOW[1])
        for start in range(_minutes(block_start), _minutes(block_end),
                           _BLOCK_GRID_MINUTES):
            end = start + task.duration_minutes
            if end > safe_high:
                break
            if any(start < b_end and b_start < end for b_start, b_end in busy):
                continue
            if anchors and min_gap:
                if min(abs(start - anchor) for anchor in anchors) < min_gap:
                    continue
            return _from_minutes(start)
        return None

    def _natural_slot(self, task: Task, schedule: List[Task],
                      anchors: Optional[List[time]] = None,
                      min_gap: int = 0) -> Optional[time]:
        """Place a task at a time a person would actually choose.

        Tries the recognised day blocks (morning / afternoon / evening) in the
        order `_block_order` gives, which pushes supplementary care away from
        the care it supplements. Only when every block is full does it fall
        back to scanning the safe window half hour by half hour, so the
        schedule degrades gracefully on a very busy day rather than failing.

        Args:
            task: The task to place.
            schedule: The current schedule, used for clash detection.
            anchors: Times to stay clear of, e.g. the pet's other feedings.
            min_gap: Minimum minutes between the slot and every anchor.

        Returns:
            A start time, or None when nothing free satisfies the spacing.
        """
        busy = self._occupied(schedule, exclude=task)
        anchor_minutes = [
            _minutes(t) for t in (anchors or []) if isinstance(t, time)
        ]

        crowding = self._crowding(task, schedule)
        for name in self._block_order(task, anchor_minutes, crowding):
            slot = self._slot_in_block(task, name, busy, anchor_minutes, min_gap)
            if slot is not None:
                return slot

        # Every block is taken. Keep the schedule valid even if it reads less
        # tidily, preferring the slot closest to the ideal spacing.
        low, high = _minutes(SAFE_WINDOW[0]), _minutes(SAFE_WINDOW[1])
        best: Optional[time] = None
        best_key: Optional[tuple] = None
        for candidate in _FALLBACK_SLOTS:
            start = _minutes(candidate)
            end = start + task.duration_minutes
            if start < low or end > high:
                continue
            if any(start < b_end and b_start < end for b_start, b_end in busy):
                continue
            if not anchor_minutes:
                return candidate
            gap = min(abs(start - anchor) for anchor in anchor_minutes)
            if min_gap and gap < min_gap:
                continue
            key = (abs(gap - IDEAL_FEEDING_GAP_MINUTES), start)
            if best_key is None or key < best_key:
                best_key, best = key, candidate
        return best

    def _free_slot(self, task: Task, schedule: List[Task]) -> Optional[time]:
        """Place a task at the first natural time that clashes with nothing."""
        return self._natural_slot(task, schedule)

    def _spaced_feeding_slot(self, task: Task, schedule: List[Task],
                             others: List[Task]) -> Optional[time]:
        """Place a feeding in a day block well clear of the pet's other meals.

        Breakfast sits in the morning block, so the second meal is ranked into
        the evening block first and the afternoon block second — never squeezed
        in beside the meal it is meant to complement. Spacing of at least
        MIN_FEEDING_GAP_MINUTES is enforced on top of the block choice.

        Args:
            task: The feeding to place.
            schedule: The current schedule, used for clash detection.
            others: The pet's other timed feedings, used as spacing anchors.

        Returns:
            The chosen slot, or None when nothing is both free and far enough
            from every existing feeding.
        """
        anchors = [
            f.preferred_time
            for f in others
            if f is not task and isinstance(f.preferred_time, time)
        ]
        # AI-augmented: let Gemini propose the slot, but only accept it if it
        # passes exactly the same checks a rule-chosen slot must pass.
        suggested = self._llm_slot(task, schedule, others, anchors)
        if suggested is not None:
            return suggested
        return self._natural_slot(
            task, schedule, anchors=anchors, min_gap=MIN_FEEDING_GAP_MINUTES
        )

    def _slot_acceptable(self, task: Task, slot: time, schedule: List[Task],
                         anchors: List[time], min_gap: int) -> bool:
        """Validate any proposed time against the deterministic rules.

        A slot must sit inside one of the recognised day blocks, fit in the
        safe window, clash with nothing already scheduled, and keep `min_gap`
        from every anchor. This is the gate every LLM suggestion passes
        through, so the model can advise but never override the rules.
        """
        start = _minutes(slot)
        end = start + task.duration_minutes
        if start < _minutes(SAFE_WINDOW[0]) or end > _minutes(SAFE_WINDOW[1]):
            return False
        in_block = any(
            _minutes(b_start) <= start < _minutes(b_end)
            for b_start, b_end in DAY_BLOCKS.values()
        )
        if not in_block:
            return False
        for b_start, b_end in self._occupied(schedule, exclude=task):
            if start < b_end and b_start < end:
                return False
        for anchor in anchors:
            if abs(start - _minutes(anchor)) < min_gap:
                return False
        return True

    def _llm_slot(self, task: Task, schedule: List[Task], others: List[Task],
                  anchors: List[time]) -> Optional[time]:
        """Gemini's proposed slot for a feeding, or None if it is not usable.

        Records the outcome either way, so the audit trail shows whether the
        model's suggestion was taken or rejected — that traceability is the
        point of letting it advise at all.
        """
        if self.commentator is None:
            return None
        pet = self._pet(task.pet_name)
        if pet is None:
            return None
        suggestion = self.commentator.suggest_care_time(
            pet_name=pet.name,
            species=pet.species,
            care="second feeding",
            existing=[t for t in schedule if t.pet_name == pet.name],
            blocks=DAY_BLOCKS,
            duration_minutes=task.duration_minutes,
            min_gap_minutes=MIN_FEEDING_GAP_MINUTES,
        )
        if suggestion is None:
            self._llm_placements.append(
                f"Gemini gave no usable time for '{task.title}'; used the rule-based slot"
            )
            return None
        if not self._slot_acceptable(task, suggestion, schedule, anchors,
                                     MIN_FEEDING_GAP_MINUTES):
            self._llm_placements.append(
                f"rejected Gemini's {suggestion:%H:%M} for '{task.title}' "
                "(fails spacing, block or clash rules); used the rule-based slot"
            )
            return None
        self._llm_placements.append(
            f"accepted Gemini's {suggestion:%H:%M} for '{task.title}' "
            "(passed spacing, block and clash checks)"
        )
        return suggestion

    @staticmethod
    def _committed_minutes(schedule: List[Task]) -> int:
        """Total minutes the schedule asks of the owner."""
        return sum(t.duration_minutes for t in schedule)

    def _fits_budget(self, task: Task, schedule: List[Task]) -> bool:
        """True when adding `task` keeps the plan inside the owner's time.

        Re-timing a task that is already in the plan costs nothing, so this is
        only consulted before something new is brought in.
        """
        committed = self._committed_minutes(schedule)
        return committed + task.duration_minutes <= self.owner.available_minutes

    def _defer(self, task: Task, pet_name: str, schedule: List[Task],
               reason: str) -> str:
        """Record that care had to be left out for lack of time.

        Returns the line for the Executor's action log. The deferral is also
        kept on self._deferred so the Evaluator can fail the run: silently
        dropping required care would be worse than reporting it.
        """
        remaining = self.owner.available_minutes - self._committed_minutes(schedule)
        message = (
            f"{reason} for {pet_name}: needs {task.duration_minutes} min but only "
            f"{max(0, remaining)} of {self.owner.available_minutes} min are free"
        )
        self._deferred.append(Issue(
            kind="over_budget",
            message=message,
            pet_name=pet_name,
            severity=Priority.HIGH,
        ))
        return f"deferred {message}"

    @staticmethod
    def _in_schedule(task: Task, schedule: List[Task]) -> bool:
        """True when this exact task object is already in the schedule.

        Compared by identity and id, never by title: two tasks may legitimately
        share a title, and reusing one must not silently drop the other.
        """
        return any(t is task or t.id == task.id for t in schedule)

    def _unscheduled_match(self, pet: Pet, keyword: str,
                           schedule: List[Task]) -> Optional[Task]:
        """A pending task of this pet covering `keyword` but absent from the plan.

        This is what stops the Executor from fabricating a second 'Walk Rex'
        when the pet already owns an 'Evening walk' that the time budget pushed
        out of the plan. Prefers an already-timed task so the owner's intended
        time is kept.
        """
        candidates = [
            t for t in pet.tasks
            if not t.is_completed
            and _matches(t, keyword)
            and not self._in_schedule(t, schedule)
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda t: (t.preferred_time is None,
                                       t.preferred_time or time.max))
        return candidates[0]

    # -- 1. Planner ------------------------------------------------------

    def plan(self, schedule: List[Task], iteration: int) -> List[Issue]:
        """Analyze the schedule for missing care steps and time conflicts.

        The Planner only diagnoses; it never edits the schedule. It reports:
          * missing_care      - the pet owns no task covering a required step
          * unscheduled_care  - the pet owns one, but it is not in today's plan
            (the Scheduler trimmed it to fit the time budget)
          * untimed           - a task has no preferred_time, so it cannot be checked
          * conflict          - two tasks overlap in the clock

        The missing_care / unscheduled_care split matters: a required step is
        only *missing* when the pet genuinely has no such task. Judging that
        against the budget-trimmed schedule alone made the Executor invent a
        duplicate of a task the pet already owned, once per run.

        Returns the issues found, highest severity first.
        """
        issues: List[Issue] = []

        for pet in self.owner.pets:
            pet_tasks = [t for t in schedule if t.pet_name == pet.name]
            for keyword in self._required_for(pet):
                if any(_matches(t, keyword) for t in pet_tasks):
                    continue
                # Not in the plan — but does the pet already own one?
                existing = self._unscheduled_match(pet, keyword, schedule)
                if existing is not None:
                    issues.append(Issue(
                        kind="unscheduled_care",
                        message=(f"{pet.name} ({pet.species}) has a '{keyword}' task "
                                 f"('{existing.title}') that is not in today's plan"),
                        pet_name=pet.name,
                        task_id=existing.id,
                        keyword=keyword,
                        severity=Priority.HIGH,
                    ))
                else:
                    issues.append(Issue(
                        kind="missing_care",
                        message=f"{pet.name} ({pet.species}) has no '{keyword}' task scheduled",
                        pet_name=pet.name,
                        keyword=keyword,
                        severity=Priority.HIGH,
                    ))

        for task in schedule:
            if not isinstance(task.preferred_time, time):
                issues.append(Issue(
                    kind="untimed",
                    message=f"'{task.title}' ({task.pet_name}) has no scheduled time",
                    pet_name=task.pet_name,
                    task_id=task.id,
                    severity=Priority.MEDIUM,
                ))

        for a, b in self.scheduler.detect_conflicts(schedule):
            issues.append(Issue(
                kind="conflict",
                message=(f"'{a.title}' ({a.pet_name}, {a.preferred_time:%H:%M}) overlaps "
                         f"'{b.title}' ({b.pet_name}, {b.preferred_time:%H:%M})"),
                pet_name=b.pet_name,
                task_id=b.id,
                severity=Priority.HIGH,
            ))

        issues.sort(key=lambda i: -i.severity.value)
        # The rules above have already decided everything; the model only
        # explains them, and an empty string just means no commentary.
        # Only the first pass is narrated. Later passes describe a schedule
        # that is about to change anyway, and each call costs a round trip
        # against a rate-limited quota.
        commentary = (
            self.commentator.planner_commentary(self.owner.pets, schedule, issues)
            if self.commentator is not None and iteration == 1 else ""
        )
        self._record(
            iteration, "PLANNER",
            f"analyzed {len(schedule)} task(s); found {len(issues)} issue(s)",
            [str(i) for i in issues] or ["schedule looks complete and conflict-free"],
            commentary=commentary,
        )
        return issues

    # -- 2. Executor -----------------------------------------------------

    def execute(self, schedule: List[Task], issues: List[Issue],
                iteration: int) -> List[Task]:
        """Adjust or reorder tasks to resolve the Planner's issues.

        Reuses tasks wherever it can: an existing task is pulled into the plan
        or re-timed in place, and a new Task is only ever created when the pet
        owns nothing that covers the required care. Gives untimed tasks a slot
        and moves the later task of each conflicting pair. Medication-style
        tasks are pinned and never moved. Returns a new schedule list; the Task
        objects themselves are mutated in place so the Pet keeps the change.
        """
        working = list(schedule)
        actions: List[str] = []
        # Rebuilt every pass, so a deferral that later becomes affordable is
        # not carried around as a stale complaint.
        self._deferred = []
        self._llm_placements = []

        # 2a-i. Pull care the pet already owns back into the plan, rather than
        # creating a duplicate of it.
        for issue in (i for i in issues if i.kind == "unscheduled_care"):
            pet = self._pet(issue.pet_name)
            if pet is None:
                continue
            task = next((t for t in pet.tasks if t.id == issue.task_id), None)
            if task is None or self._in_schedule(task, working):
                continue
            if not self._fits_budget(task, working):
                actions.append(
                    self._defer(task, pet.name, working, f"'{task.title}'")
                )
                continue
            if not isinstance(task.preferred_time, time):
                task.preferred_time = self._free_slot(task, working) or SAFE_WINDOW[0]
            working.append(task)
            actions.append(
                f"pulled existing '{task.title}' ({pet.name}) at "
                f"{task.preferred_time:%H:%M} into the plan to cover "
                f"'{issue.keyword}' care (no new task created)"
            )

        # 2a-ii. Create a care task only when the pet truly owns none.
        for issue in (i for i in issues if i.kind == "missing_care"):
            pet = self._pet(issue.pet_name)
            if pet is None:
                continue
            # Re-check against the live schedule: an earlier issue in this same
            # pass may already have covered this keyword.
            if any(t.pet_name == pet.name and _matches(t, issue.keyword)
                   for t in working):
                continue
            existing = self._unscheduled_match(pet, issue.keyword, working)
            if existing is not None:
                if not self._fits_budget(existing, working):
                    actions.append(
                        self._defer(existing, pet.name, working, f"'{existing.title}'")
                    )
                    continue
                if not isinstance(existing.preferred_time, time):
                    existing.preferred_time = (
                        self._free_slot(existing, working) or SAFE_WINDOW[0]
                    )
                working.append(existing)
                actions.append(
                    f"reused existing '{existing.title}' ({pet.name}) at "
                    f"{existing.preferred_time:%H:%M} for '{issue.keyword}' care"
                )
                continue
            new_task = Task(
                title=f"{issue.keyword.capitalize()} {pet.name}",
                duration_minutes=15,
                priority=Priority.HIGH,
                frequency=Frequency.DAILY,
                source="agent",
            )
            if not self._fits_budget(new_task, working):
                actions.append(self._defer(
                    new_task, pet.name, working, f"'{issue.keyword}' care"
                ))
                continue
            pet.add_task(new_task)
            new_task.preferred_time = self._free_slot(new_task, working) or SAFE_WINDOW[0]
            working.append(new_task)
            actions.append(
                f"added '{new_task.title}' at {new_task.preferred_time:%H:%M} "
                f"(missing '{issue.keyword}' care for {pet.name})"
            )

        # 2b. Give untimed tasks a slot.
        for issue in (i for i in issues if i.kind == "untimed"):
            task = next((t for t in working if t.id == issue.task_id), None)
            if task is None:
                continue
            slot = self._free_slot(task, working)
            if slot is None:
                actions.append(f"could not place '{task.title}': no free slot in the safe window")
                continue
            task.preferred_time = slot
            actions.append(f"scheduled '{task.title}' ({task.pet_name}) at {slot:%H:%M}")

        # 2b-ii. Fix nutrition gaps carried back from the Evaluator: add a second
        # feeding, or push a too-close feeding out to a properly spaced slot.
        for issue in (i for i in issues if i.kind == "feeding_gap"):
            pet = self._pet(issue.pet_name)
            if pet is None:
                continue
            feedings = sorted(
                (t for t in working
                 if t.pet_name == pet.name and _matches(t, "feed")
                 and isinstance(t.preferred_time, time)),
                key=lambda t: t.preferred_time,
            )
            if len(feedings) < 2:
                # Prefer a feeding the pet already owns but that is not in the
                # plan; only invent one when there is genuinely nothing to use.
                spare = self._unscheduled_match(pet, "feed", working)
                if spare is not None and self._fits_budget(spare, working):
                    slot = self._spaced_feeding_slot(spare, working, feedings)
                    if slot is not None:
                        spare.preferred_time = slot
                        working.append(spare)
                        actions.append(
                            f"pulled existing '{spare.title}' ({pet.name}) into the "
                            f"plan at {slot:%H:%M} as the second feeding "
                            "(no new task created)"
                        )
                        continue
                new_task = Task(
                    title=f"Second feed {pet.name}",
                    duration_minutes=15,
                    priority=Priority.HIGH,
                    frequency=Frequency.DAILY,
                    source="agent",
                )
                if not self._fits_budget(new_task, working):
                    actions.append(self._defer(
                        new_task, pet.name, working, "a second feeding"
                    ))
                    continue
                pet.add_task(new_task)
                # A supplementary feeding belongs roughly half a day from the
                # one that already exists, not just past the minimum gap.
                slot = self._spaced_feeding_slot(new_task, working, feedings)
                if slot is None:
                    pet.remove_task(new_task.id)
                    actions.append(
                        f"could not add a second feeding for {pet.name}: no slot "
                        f"{MIN_FEEDING_GAP_MINUTES} min clear of the existing one"
                    )
                    continue
                new_task.preferred_time = slot
                working.append(new_task)
                gap = (
                    min(abs(_minutes(slot) - _minutes(f.preferred_time)) for f in feedings)
                    if feedings else None
                )
                spacing = f", {gap // 60}h{gap % 60:02d} after the first" if gap else ""
                actions.append(
                    f"added '{new_task.title}' at {slot:%H:%M}{spacing} "
                    f"so {pet.name} gets two evenly spaced feedings"
                )
                continue

            mover = next((t for t in feedings if t.id == issue.task_id), None)
            if mover is None or _is_pinned(mover):
                continue
            others = [f for f in feedings if f is not mover]
            old = mover.preferred_time
            slot = self._spaced_feeding_slot(mover, working, others)
            if slot is None:
                actions.append(
                    f"could not re-space '{mover.title}' ({pet.name}): no slot far "
                    "enough from the other feedings"
                )
                continue
            mover.preferred_time = slot
            gap = min(
                abs(_minutes(slot) - _minutes(f.preferred_time)) for f in others
            )
            actions.append(
                f"moved '{mover.title}' ({pet.name}) {old:%H:%M} -> {slot:%H:%M} "
                f"to space out feedings ({gap // 60}h{gap % 60:02d} apart)"
            )

        # 2c. Resolve overlaps, moving the later-listed task of each pair.
        for a, b in self.scheduler.detect_conflicts(working):
            mover = b if not _is_pinned(b) else a
            if _is_pinned(mover):
                actions.append(
                    f"left '{a.title}' ({_pin_reason(a)}) and '{b.title}' "
                    f"({_pin_reason(b)}) overlapping: neither may be moved — "
                    "this one is yours to resolve"
                )
                continue
            old = mover.preferred_time
            slot = self._free_slot(mover, working)
            if slot is None:
                actions.append(f"could not re-time '{mover.title}': no free slot available")
                continue
            mover.preferred_time = slot
            actions.append(
                f"moved '{mover.title}' ({mover.pet_name}) {old:%H:%M} -> {slot:%H:%M} "
                "to clear an overlap"
            )

        # 2d. Safety net: never hand on a list holding the same task twice.
        # Each stage above already reuses rather than duplicates, but the
        # Evaluator's carried issues are replayed on the next pass, so this
        # guarantees the invariant no matter which path added a task.
        seen: set = set()
        deduped: List[Task] = []
        for task in working:
            if task.id in seen:
                actions.append(f"dropped a duplicate entry for '{task.title}'")
                continue
            seen.add(task.id)
            deduped.append(task)
        working = deduped

        # 2e. Reorder the schedule chronologically so it reads as a day plan.
        working = self.scheduler.sort_by_time(working)
        actions.extend(self._llm_placements)
        self._record(
            iteration, "EXECUTOR",
            f"applied {len(actions)} adjustment(s)",
            actions or ["no adjustments needed"],
        )
        return working

    # -- 3. Evaluator ----------------------------------------------------

    def evaluate(self, schedule: List[Task], iteration: int) -> List[Issue]:
        """Check the revised schedule against safety and nutritional goals.

        Safety: every feeding, walk and medication falls inside SAFE_WINDOW, and
        no two tasks overlap. Nutrition: each pet gets at least two feedings,
        spaced by at least MIN_FEEDING_GAP_MINUTES. Capacity: the plan stays
        inside the owner's available time, and any care the Executor had to
        defer for lack of time is reported rather than quietly dropped.

        Returns the remaining violations; empty means the schedule passes.
        """
        issues: List[Issue] = []

        # Capacity: the plan must not ask for more time than the owner has.
        committed = self._committed_minutes(schedule)
        if committed > self.owner.available_minutes:
            issues.append(Issue(
                kind="over_budget",
                message=(f"the plan commits {committed} min but only "
                         f"{self.owner.available_minutes} min are available"),
                severity=Priority.HIGH,
            ))
        # Care the Executor could not fit needs a human decision: more time,
        # or fewer tasks.
        issues.extend(self._deferred)

        # Safety: nothing left overlapping.
        for a, b in self.scheduler.detect_conflicts(schedule):
            issues.append(Issue(
                kind="conflict",
                message=f"'{a.title}' still overlaps '{b.title}'",
                pet_name=b.pet_name,
                task_id=b.id,
                severity=Priority.HIGH,
            ))

        # Safety: time-sensitive care inside waking hours.
        low, high = _minutes(SAFE_WINDOW[0]), _minutes(SAFE_WINDOW[1])
        for task in schedule:
            if not isinstance(task.preferred_time, time):
                continue
            if not any(_matches(task, k) for k in ("feed", "walk", "medicat", "medicine")):
                continue
            start = _minutes(task.preferred_time)
            if start < low or start + task.duration_minutes > high:
                issues.append(Issue(
                    kind="unsafe_time",
                    message=(f"'{task.title}' ({task.pet_name}) at "
                             f"{task.preferred_time:%H:%M} falls outside the safe window "
                             f"{SAFE_WINDOW[0]:%H:%M}-{SAFE_WINDOW[1]:%H:%M}"),
                    pet_name=task.pet_name,
                    task_id=task.id,
                    severity=Priority.HIGH,
                ))

        # Nutrition: enough feedings, spaced far enough apart.
        for pet in self.owner.pets:
            feedings = sorted(
                (t for t in schedule
                 if t.pet_name == pet.name
                 and _matches(t, "feed")
                 and isinstance(t.preferred_time, time)),
                key=lambda t: t.preferred_time,
            )
            if len(feedings) < 2:
                issues.append(Issue(
                    kind="feeding_gap",
                    message=f"{pet.name} has {len(feedings)} feeding(s); at least 2 are expected",
                    pet_name=pet.name,
                    keyword="feed",
                    severity=Priority.HIGH,
                ))
                continue
            for earlier, later in zip(feedings, feedings[1:]):
                gap = _minutes(later.preferred_time) - _minutes(earlier.preferred_time)
                if gap < MIN_FEEDING_GAP_MINUTES:
                    issues.append(Issue(
                        kind="feeding_gap",
                        message=(f"{pet.name}'s feedings at {earlier.preferred_time:%H:%M} "
                                 f"and {later.preferred_time:%H:%M} are only {gap} min apart "
                                 f"(minimum {MIN_FEEDING_GAP_MINUTES})"),
                        pet_name=pet.name,
                        task_id=later.id,
                        keyword="feed",
                        severity=Priority.MEDIUM,
                    ))

        verdict = "PASS" if not issues else f"FAIL ({len(issues)} violation(s))"
        self._record(
            iteration, "EVALUATOR",
            f"safety + nutrition check: {verdict}",
            [str(i) for i in issues] or ["all safety and nutritional goals met"],
        )
        return issues

    # -- 4. Reflector ----------------------------------------------------

    def reflect(self, issues: List[Issue], iteration: int,
                previous_count: Optional[int]) -> bool:
        """Decide whether the schedule is approved or needs another iteration.

        Returns True to keep looping. The agent stops when there is nothing left
        to fix, when the budget runs out, or when an iteration made no progress
        (the same number of issues as last time), since more passes would only
        repeat themselves.
        """
        def narrate(verdict: str) -> str:
            """Ask the model to explain a verdict the rules have already set."""
            if self.commentator is None:
                return ""
            return self.commentator.reflector_commentary(
                verdict, iteration, issues, self.schedule or [],
                self.owner.available_minutes,
            )

        if not issues:
            self._record(iteration, "REFLECTOR",
                         "APPROVED - schedule is safe, complete and well spaced",
                         ["no outstanding issues; ending the loop"],
                         commentary=narrate("APPROVED"))
            self.approved = True
            return False

        if previous_count is not None and len(issues) >= previous_count:
            self._record(
                iteration, "REFLECTOR",
                "NOT APPROVED - no progress this iteration, stopping",
                [f"{len(issues)} issue(s) remain, unchanged from the previous pass",
                 "these need a human decision:"] + [str(i) for i in issues],
                commentary=narrate("NOT APPROVED (no progress)"),
            )
            self.approved = False
            return False

        if iteration >= self.max_iterations:
            self._record(
                iteration, "REFLECTOR",
                f"NOT APPROVED - iteration budget ({self.max_iterations}) exhausted",
                [str(i) for i in issues],
                commentary=narrate("NOT APPROVED (iteration budget exhausted)"),
            )
            self.approved = False
            return False

        # Mid-loop revisions are not narrated: the model would be describing a
        # schedule that is about to change, and each call costs a round trip.
        self._record(
            iteration, "REFLECTOR",
            f"REVISE - {len(issues)} issue(s) left, running another iteration",
            [str(i) for i in issues],
        )
        return True

    # -- the loop --------------------------------------------------------

    def run(self, today: Optional[date] = None) -> List[Task]:
        """Run the full agentic loop and return the final schedule.

        Starts from the Scheduler's draft plan, then cycles
        Planner -> Executor -> Evaluator -> Reflector until the Reflector
        approves or stops. Check self.approved for the verdict and
        self.explain_run() for the log.
        """
        self.log = []
        self.approved = False
        self.iterations_used = 0
        self._deferred = []
        self._llm_placements = []
        if self.commentator is not None:
            self.commentator.reset_run_budget()
        self.started_at = datetime.now()
        self.finished_at = None
        schedule = list(self.scheduler.generate_plan(today))
        self._record(0, "INPUT", f"draft plan from Scheduler: {len(schedule)} task(s)",
                     [f"{t.pet_name}: {t.title}"
                      + (f" at {t.preferred_time:%H:%M}" if t.preferred_time else " (untimed)")
                      for t in schedule] or ["nothing pending to schedule"])

        previous_count: Optional[int] = None
        carried: List[Issue] = []  # violations the Evaluator left for the next pass
        for iteration in range(1, self.max_iterations + 1):
            self.iterations_used = iteration
            issues = self.plan(schedule, iteration) + carried
            schedule = self.execute(schedule, issues, iteration)
            remaining = self.evaluate(schedule, iteration)
            # Publish the current plan before reflecting, so the Reflector's
            # narration describes the schedule it is actually judging.
            self.schedule = schedule
            if not self.reflect(remaining, iteration, previous_count):
                break
            previous_count = len(remaining)
            carried = remaining

        self.schedule = schedule
        self.scheduler.plan = schedule
        self.finished_at = datetime.now()
        return schedule

    def explain_run(self) -> str:
        """The full audit trail: every stage of every iteration, then the verdict."""
        if not self.log:
            return "The agent has not run yet. Call run() first."
        lines = [step.render() for step in self.log]
        status = "APPROVED" if self.approved else "NEEDS REVIEW"
        lines.append("")
        lines.append(f"Final verdict: {status} after {self.iterations_used} iteration(s).")
        for task in self.schedule:
            at = f"{task.preferred_time:%H:%M}" if task.preferred_time else "--:--"
            lines.append(f"  {at}  {task.pet_name}: {task.title} ({task.duration_minutes} min)")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Run logging
# ---------------------------------------------------------------------------

#: Default log file, resolved next to this module so it lands in the project
#: root no matter which directory Streamlit was started from.
AI_LOG_PATH = Path(__file__).resolve().parent / "ai_interactions.md"

#: Auto-generated runs are appended below this heading. Everything above it is
#: hand-written course notes and is never touched.
AUTO_LOG_HEADING = "## Agentic Loop Run Log (auto-generated)"

_AUTO_LOG_INTRO = (
    "\n\n---\n\n"
    f"{AUTO_LOG_HEADING}\n\n"
    "> Appended automatically by `log_agent_run()` every time the agentic review\n"
    "> runs in the Streamlit app. Each entry records the stage-by-stage reasoning\n"
    "> trace, timestamps and the Reflector's verdict. Do not hand-edit below this\n"
    "> line.\n"
)


def format_agent_run(agent: "PetCareAgent", source: str = "app") -> str:
    """Render one completed agent run as a Markdown log entry.

    Includes a per-stage reasoning trace with timestamps, the verdict, and the
    final schedule. Call after agent.run().
    """
    started = agent.started_at or datetime.now()
    finished = agent.finished_at or started
    elapsed_ms = (finished - started).total_seconds() * 1000
    verdict = "APPROVED" if agent.approved else "NEEDS REVIEW"
    pets = ", ".join(f"{p.name} ({p.species})" for p in agent.owner.pets) or "none"

    lines = [
        "",
        f"### Run {started:%Y-%m-%d %H:%M:%S} — {verdict}",
        "",
        f"- **Source:** {source}",
        f"- **Owner:** {agent.owner.name} ({agent.owner.available_minutes} min available)",
        f"- **Pets:** {pets}",
        f"- **Iterations used:** {agent.iterations_used} of {agent.max_iterations}",
        f"- **Started / finished:** {started:%H:%M:%S} → {finished:%H:%M:%S} "
        f"({elapsed_ms:.1f} ms)",
        f"- **Final schedule:** {len(agent.schedule)} task(s)",
        "",
        "**Reasoning trace**",
        "",
        "| Time | Iter | Stage | Summary | Details |",
        "| --- | --- | --- | --- | --- |",
    ]
    for step in agent.log:
        # Pipes would break the Markdown table, so escape them.
        details = "<br>".join(d.replace("|", "\\|") for d in step.details) or "—"
        if step.commentary:
            details += "<br>_LLM: " + step.commentary.replace("|", "\\|") + "_"
        summary = step.summary.replace("|", "\\|")
        lines.append(
            f"| {step.timestamp:%H:%M:%S} | {step.iteration} | {step.stage} | "
            f"{summary} | {details} |"
        )

    lines += ["", "**Final schedule**", "", "```"]
    for task in agent.schedule:
        at = f"{task.preferred_time:%H:%M}" if task.preferred_time else "--:--"
        lines.append(f"{at}  {task.pet_name}: {task.title} ({task.duration_minutes} min)")
    if not agent.schedule:
        lines.append("(empty)")
    lines += ["```", ""]
    return "\n".join(lines)


def log_agent_run(agent: "PetCareAgent", source: str = "app",
                  path: Optional[Path] = None) -> Path:
    """Append a run's audit trail to ai_interactions.md and return the path.

    The file's hand-written sections are preserved: entries are appended under
    AUTO_LOG_HEADING, which is created once if it is not already there.

    Raises OSError if the file cannot be written; callers that must not fail
    (such as the Streamlit UI) should catch it.
    """
    path = Path(path) if path is not None else AI_LOG_PATH
    existing = path.read_text(encoding="utf-8") if path.exists() else "# AI Interactions Log\n"
    parts = [existing.rstrip("\n")]
    if AUTO_LOG_HEADING not in existing:
        parts.append(_AUTO_LOG_INTRO)
    parts.append(format_agent_run(agent, source=source))
    path.write_text("\n".join(parts).rstrip("\n") + "\n", encoding="utf-8")
    return path
