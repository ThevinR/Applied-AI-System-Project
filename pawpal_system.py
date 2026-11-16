from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from uuid import uuid4


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

#: Minimum gap between two feedings for the same pet, so a day's food is not
#: served all at once (a bloat / overfeeding risk).
MIN_FEEDING_GAP_MINUTES = 240
#: Feeding, walking and medication should fall inside waking hours.
SAFE_WINDOW = (time(6, 0), time(22, 0))
#: Medication is time-critical: the executor will not move it.
PINNED_KEYWORDS = ("medicat", "medicine", "insulin", "dose")

#: Where the executor tries to put an untimed or displaced task, in order.
_SLOT_PREFERENCE = {
    "feed": (time(7, 0), time(17, 0), time(12, 0)),
    "walk": (time(8, 0), time(18, 0), time(12, 30)),
    "water": (time(7, 30), time(16, 0)),
    "litter": (time(9, 0), time(19, 0)),
    "clean": (time(10, 0), time(16, 30)),
}
_FALLBACK_SLOTS = tuple(time(h, m) for h in range(6, 22) for m in (0, 30))


def _matches(task: Task, keyword: str) -> bool:
    """True when a task's title covers the given care keyword."""
    return keyword in task.title.lower()


def _is_pinned(task: Task) -> bool:
    """Medication-like tasks must keep their exact time."""
    title = task.title.lower()
    return any(k in title for k in PINNED_KEYWORDS)


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

    def render(self) -> str:
        head = f"Iter {self.iteration} | {self.stage:<9} | {self.summary}"
        return "\n".join([head] + [f"    - {d}" for d in self.details])


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
                 max_iterations: int = 3):
        self.owner = owner
        self.scheduler = scheduler or Scheduler(owner)
        self.max_iterations = max_iterations
        self.log: List[LoopStep] = []
        self.schedule: List[Task] = []
        self.approved = False
        self.iterations_used = 0
        self.started_at: Optional[datetime] = None
        self.finished_at: Optional[datetime] = None

    # -- helpers ---------------------------------------------------------

    def _record(self, iteration: int, stage: str, summary: str,
                details: Optional[List[str]] = None) -> None:
        self.log.append(LoopStep(iteration, stage, summary, list(details or [])))

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

    def _free_slot(self, task: Task, schedule: List[Task],
                   avoid: Optional[List[Tuple[int, int]]] = None,
                   late_first: bool = False) -> Optional[time]:
        """Find the first slot for `task` that clashes with nothing.

        Tries the care-type's preferred slots first, then every half hour inside
        the safe window. `avoid` adds extra windows to stay clear of (used for
        feeding spacing). `late_first` scans the day backwards, for a task that
        should land as late as possible. Returns None when the day is full.
        """
        busy = self._occupied(schedule, exclude=task) + list(avoid or [])
        low, high = _minutes(SAFE_WINDOW[0]), _minutes(SAFE_WINDOW[1])
        keyword = next((k for k in _SLOT_PREFERENCE if _matches(task, k)), None)
        preferred = _SLOT_PREFERENCE.get(keyword, ())
        fallback = tuple(reversed(_FALLBACK_SLOTS)) if late_first else _FALLBACK_SLOTS
        if late_first:
            preferred = tuple(sorted(preferred, reverse=True))
        candidates = preferred + fallback
        for candidate in candidates:
            start = _minutes(candidate)
            end = start + task.duration_minutes
            if start < low or end > high:
                continue
            if all(end <= b_start or start >= b_end for b_start, b_end in busy):
                return candidate
        return None

    # -- 1. Planner ------------------------------------------------------

    def plan(self, schedule: List[Task], iteration: int) -> List[Issue]:
        """Analyze the schedule for missing care steps and time conflicts.

        The Planner only diagnoses; it never edits the schedule. It reports:
          * missing_care - a required step for a pet is absent from the schedule
          * untimed      - a task has no preferred_time, so it cannot be checked
          * conflict     - two tasks overlap in the clock

        Returns the issues found, highest severity first.
        """
        issues: List[Issue] = []

        for pet in self.owner.pets:
            pet_tasks = [t for t in schedule if t.pet_name == pet.name]
            for keyword in self._required_for(pet):
                if not any(_matches(t, keyword) for t in pet_tasks):
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
        self._record(
            iteration, "PLANNER",
            f"analyzed {len(schedule)} task(s); found {len(issues)} issue(s)",
            [str(i) for i in issues] or ["schedule looks complete and conflict-free"],
        )
        return issues

    # -- 2. Executor -----------------------------------------------------

    def execute(self, schedule: List[Task], issues: List[Issue],
                iteration: int) -> List[Task]:
        """Adjust or reorder tasks to resolve the Planner's issues.

        Adds missing care tasks, gives untimed tasks a slot, and moves the later
        task of each conflicting pair to the first free slot. Medication-style
        tasks are pinned and never moved. Returns a new schedule list; the Task
        objects themselves are mutated in place so the Pet keeps the change.
        """
        working = list(schedule)
        actions: List[str] = []

        # 2a. Add any missing care step.
        for issue in (i for i in issues if i.kind == "missing_care"):
            pet = self._pet(issue.pet_name)
            if pet is None:
                continue
            new_task = Task(
                title=f"{issue.keyword.capitalize()} {pet.name}",
                duration_minutes=15,
                priority=Priority.HIGH,
                frequency=Frequency.DAILY,
            )
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
                new_task = Task(
                    title=f"Second feed {pet.name}",
                    duration_minutes=15,
                    priority=Priority.HIGH,
                    frequency=Frequency.DAILY,
                )
                pet.add_task(new_task)
                avoid = [
                    (_minutes(f.preferred_time) - MIN_FEEDING_GAP_MINUTES,
                     _minutes(f.preferred_time) + MIN_FEEDING_GAP_MINUTES)
                    for f in feedings
                ]
                # A supplementary feeding belongs on the far side of the day from
                # the one that already exists, so search later slots first.
                slot = self._free_slot(new_task, working, avoid=avoid, late_first=True)
                if slot is None:
                    pet.remove_task(new_task.id)
                    actions.append(
                        f"could not add a second feeding for {pet.name}: no slot "
                        f"{MIN_FEEDING_GAP_MINUTES} min clear of the existing one"
                    )
                    continue
                new_task.preferred_time = slot
                working.append(new_task)
                actions.append(
                    f"added '{new_task.title}' at {slot:%H:%M} so {pet.name} gets two feedings"
                )
                continue

            mover = next((t for t in feedings if t.id == issue.task_id), None)
            if mover is None or _is_pinned(mover):
                continue
            others = [f for f in feedings if f is not mover]
            avoid = [
                (_minutes(f.preferred_time) - MIN_FEEDING_GAP_MINUTES,
                 _minutes(f.preferred_time) + MIN_FEEDING_GAP_MINUTES)
                for f in others
            ]
            old = mover.preferred_time
            slot = self._free_slot(mover, working, avoid=avoid)
            if slot is None:
                actions.append(
                    f"could not re-space '{mover.title}' ({pet.name}): no slot far "
                    "enough from the other feedings"
                )
                continue
            mover.preferred_time = slot
            actions.append(
                f"moved '{mover.title}' ({pet.name}) {old:%H:%M} -> {slot:%H:%M} "
                "to space out feedings"
            )

        # 2c. Resolve overlaps, moving the later-listed task of each pair.
        for a, b in self.scheduler.detect_conflicts(working):
            mover = b if not _is_pinned(b) else a
            if _is_pinned(mover):
                actions.append(
                    f"left '{a.title}' / '{b.title}' overlapping: both are time-critical"
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

        # 2d. Reorder the schedule chronologically so it reads as a day plan.
        working = self.scheduler.sort_by_time(working)
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
        spaced by at least MIN_FEEDING_GAP_MINUTES.

        Returns the remaining violations; empty means the schedule passes.
        """
        issues: List[Issue] = []

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
        if not issues:
            self._record(iteration, "REFLECTOR",
                         "APPROVED - schedule is safe, complete and well spaced",
                         ["no outstanding issues; ending the loop"])
            self.approved = True
            return False

        if previous_count is not None and len(issues) >= previous_count:
            self._record(
                iteration, "REFLECTOR",
                "NOT APPROVED - no progress this iteration, stopping",
                [f"{len(issues)} issue(s) remain, unchanged from the previous pass",
                 "these need a human decision:"] + [str(i) for i in issues],
            )
            self.approved = False
            return False

        if iteration >= self.max_iterations:
            self._record(
                iteration, "REFLECTOR",
                f"NOT APPROVED - iteration budget ({self.max_iterations}) exhausted",
                [str(i) for i in issues],
            )
            self.approved = False
            return False

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
