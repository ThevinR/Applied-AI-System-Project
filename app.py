from datetime import time

import streamlit as st

from pawpal_system import (
    AI_LOG_PATH,
    DAY_BLOCKS,
    IDEAL_FEEDING_GAP_MINUTES,
    MIN_FEEDING_GAP_MINUTES,
    SAFE_WINDOW,
    Frequency,
    LLMCommentator,
    is_user_fixed,
    Owner,
    PetCareAgent,
    Pet,
    Priority,
    Scheduler,
    Task,
    log_agent_run,
)

st.set_page_config(
    page_title="PawPal+ — Agentic Pet Care Dashboard",
    page_icon="🐾",
    layout="wide",
    initial_sidebar_state="expanded",
)

# A small amount of CSS: tighter metric cards and a readable tab bar. Nothing
# here changes behaviour, only spacing and weight.
st.markdown(
    """
    <style>
      div[data-testid="stMetric"] {
          background: rgba(128, 128, 128, 0.08);
          border: 1px solid rgba(128, 128, 128, 0.18);
          border-radius: 12px;
          padding: 14px 16px;
      }
      div[data-testid="stMetricLabel"] p {
          font-size: 0.78rem;
          letter-spacing: 0.04em;
          text-transform: uppercase;
          opacity: 0.75;
      }
      button[data-baseweb="tab"] { font-size: 1rem; font-weight: 600; }
      div[data-testid="stTabs"] { margin-top: 0.5rem; }
    </style>
    """,
    unsafe_allow_html=True,
)

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

# Streamlit reruns this script on every interaction, so the Owner lives in
# session_state and is only created the first time.
# Objects saved by an older version of the code can lack newer fields (like
# Task.due_date), so rebuild the Owner if any saved task looks stale.
if "owner" in st.session_state and any(
    not hasattr(t, "due_date") or not hasattr(t, "source")
    for t in st.session_state.owner.get_all_tasks()
):
    del st.session_state["owner"]
if "owner" not in st.session_state:
    st.session_state.owner = Owner(name="Jordan", available_minutes=90)
owner = st.session_state.owner

# One Scheduler for the whole page, so the task views and the plan agree.
scheduler = Scheduler(owner)


@st.cache_resource(show_spinner=False)
def get_commentator() -> LLMCommentator:
    """One Gemini client for the session; reused across reruns."""
    return LLMCommentator()


commentator = get_commentator()

STAGE_ICONS = {
    "INPUT": "📥",
    "PLANNER": "🧭",
    "EXECUTOR": "🔧",
    "EVALUATOR": "🔍",
    "REFLECTOR": "🪞",
}

#: What each stage is for, shown under its heading in the audit trail.
STAGE_BLURBS = {
    "INPUT": "The draft plan the Scheduler handed to the agent.",
    "PLANNER": "Diagnosed the schedule — it reports problems, it never edits.",
    "EXECUTOR": "Applied fixes: added missing care, re-timed clashes, reordered the day.",
    "EVALUATOR": "Re-checked the result against safety and nutrition rules.",
    "REFLECTOR": "Decided whether to approve the schedule or run another pass.",
}

#: Plain-English labels and icons for the issue kinds the loop reports, so the
#: trail reads as sentences instead of `[HIGH] missing_care: ...` dumps.
ISSUE_KINDS = {
    "missing_care": ("🚫", "Missing care"),
    "unscheduled_care": ("📦", "Care not in plan"),
    "conflict": ("⏱️", "Overlap"),
    "feeding_gap": ("🍽️", "Feeding gap"),
    "unsafe_time": ("🌙", "Unsafe time"),
    "over_budget": ("⏳", "Not enough time"),
    "untimed": ("❓", "No time set"),
}

SEVERITY_ICONS = {"HIGH": "🔴", "MEDIUM": "🟠", "LOW": "🟡"}

#: Verbs the Executor starts its action lines with, mapped to an icon.
ACTION_ICONS = {
    "added": "➕",
    "scheduled": "🕒",
    "moved": "↔️",
    "could not": "⚠️",
    "left": "📌",
    "no adjustments": "✅",
}


def parse_detail(detail: str):
    """Split one log detail into (icon, label, text) for readable rendering.

    Issue lines arrive as ``[SEVERITY] kind: message`` from ``Issue.__str__``;
    Executor lines are plain sentences. Anything unrecognised falls through
    unchanged rather than being dropped.
    """
    if detail.startswith("[") and "] " in detail:
        severity, rest = detail[1:].split("] ", 1)
        kind, _, message = rest.partition(": ")
        icon, label = ISSUE_KINDS.get(kind, ("•", kind.replace("_", " ").title()))
        badge = SEVERITY_ICONS.get(severity.upper(), "")
        return icon, f"{badge} {label}".strip(), message or rest
    lowered = detail.lower()
    for prefix, icon in ACTION_ICONS.items():
        if lowered.startswith(prefix):
            return icon, "", detail
    return "•", "", detail


def render_details(details):
    """Render a step's details as readable markdown lines."""
    if not details:
        st.caption("No details recorded for this step.")
        return
    for detail in details:
        icon, label, text = parse_detail(detail)
        if label:
            st.markdown(f"{icon} **{label}** — {text}")
        else:
            st.markdown(f"{icon} {text}")


def task_rows(tasks):
    """Turn tasks into table rows for st.dataframe."""
    return [
        {
            "Pet": t.pet_name,
            "Task": t.title,
            "Time": f"{t.preferred_time:%H:%M}" if t.preferred_time else "—",
            "Minutes": t.duration_minutes,
            "Priority": t.priority.name.lower(),
            "Repeats": t.frequency.value,
            "Status": "done" if t.is_completed else "pending",
            "Set by": "🔒 you" if is_user_fixed(t) else (
                "🤖 agent" if t.source == "agent" else "— unset"
            ),
        }
        for t in tasks
    ]


def show_tasks(tasks):
    """Render a task table at full width."""
    st.dataframe(task_rows(tasks), use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------------
# Sidebar: owner, pets, tasks, and the agentic review control
# ---------------------------------------------------------------------------

with st.sidebar:
    st.markdown("### 🐾 PawPal+")
    st.caption("Plan the day, then let the agent audit it.")

    with st.expander("👤 Owner", expanded=True):
        owner.name = st.text_input("Owner name", value=owner.name)
        owner.available_minutes = int(
            st.number_input(
                "Available time today (minutes)",
                min_value=1,
                max_value=1440,
                value=owner.available_minutes,
            )
        )

    with st.expander("🐶 Register a pet", expanded=not owner.pets):
        new_pet_name = st.text_input("Pet name", value="Mochi")
        new_pet_species = st.selectbox("Species", ["dog", "cat", "rabbit", "other"])
        if st.button("Add pet", use_container_width=True):
            if not new_pet_name.strip():
                st.error("Give the pet a name.")
            elif any(p.name == new_pet_name.strip() for p in owner.pets):
                st.warning(f"{new_pet_name.strip()} is already added.")
            else:
                owner.add_pet(Pet(name=new_pet_name.strip(), species=new_pet_species))
                st.success(f"Added {new_pet_name.strip()}.")
                st.rerun()

        if owner.pets:
            st.caption(", ".join(f"{p.name} ({p.species})" for p in owner.pets))
        else:
            st.caption("No pets registered yet.")

    with st.expander("📋 Add a care task", expanded=False):
        if owner.pets:
            task_pet = st.selectbox("Pet", [p.name for p in owner.pets])
            task_title = st.text_input("Task title", value="Morning walk")
            duration = st.number_input(
                "Duration (minutes)", min_value=1, max_value=240, value=20
            )
            priority = st.selectbox("Priority", ["low", "medium", "high"], index=2)
            repeats = st.selectbox("Repeats", [f.value for f in Frequency], index=1)
            use_time = st.checkbox("Set a preferred time", value=True)
            preferred = (
                st.time_input("Preferred time", value=time(7, 30)) if use_time else None
            )

            if st.button("Add task", use_container_width=True):
                if not task_title.strip():
                    st.error("Give the task a title.")
                else:
                    pet = next(p for p in owner.pets if p.name == task_pet)
                    pet.add_task(
                        Task(
                            title=task_title.strip(),
                            duration_minutes=int(duration),
                            priority=Priority[priority.upper()],
                            preferred_time=preferred,
                            frequency=Frequency(repeats),
                        )
                    )
                    st.success(f"Added '{task_title.strip()}' for {pet.name}.")
                    st.rerun()
        else:
            st.caption("Register a pet before adding tasks.")

    st.divider()

    st.markdown("#### 🤖 Agentic review")
    st.info(
        "**Run this before you finalise your day.** The 4-stage AI loop reads "
        "your schedule and automatically:\n\n"
        "- 🚫 **detects missing care** — every pet gets food, water and its "
        "species' daily essentials\n"
        "- ⏱️ **catches overlapping tasks** — no two jobs booked at the same minute\n"
        f"- 🍽️ **enforces nutrition safety** — at least 2 feedings a pet, spaced "
        f"{MIN_FEEDING_GAP_MINUTES // 60}+ hours apart\n"
        f"- 🌙 **keeps care in waking hours** — {SAFE_WINDOW[0]:%H:%M}–"
        f"{SAFE_WINDOW[1]:%H:%M}\n\n"
        "It then fixes what it safely can and shows you its full reasoning in "
        "the **Agentic Review** tab.",
        icon="💡",
    )
    st.warning(
        "This edits your schedule: it can add missing care tasks and move "
        "task times. Medication-style tasks are never moved.",
        icon="⚠️",
    )
    if commentator.status.available:
        use_llm = st.toggle(
            "Add AI commentary",
            value=True,
            help=f"Narrate the Planner's findings and the Reflector's verdict "
            f"with Gemini ({commentator.status.model}). The scheduling rules "
            "are unchanged either way — the model only explains them.",
        )
        st.caption(f"🟢 Gemini ready · `{commentator.status.model}`")
    else:
        use_llm = False
        st.caption(f"⚪ AI commentary off — {commentator.status.detail}")

    max_iterations = int(
        st.number_input(
            "Max iterations",
            min_value=1,
            max_value=10,
            value=3,
            help="How many Planner → Executor → Evaluator → Reflector passes the "
            "agent may spend. It stops early once the schedule passes, or when "
            "a pass makes no progress.",
        )
    )
    run_review = st.button(
        "Run agentic review",
        type="primary",
        use_container_width=True,
        help="Audits and repairs today's schedule, then logs the full reasoning trail.",
    )

# ---------------------------------------------------------------------------
# Agentic run (triggered from the sidebar, executed before the main view draws)
# ---------------------------------------------------------------------------

all_tasks = scheduler.get_tasks()

if run_review:
    if not all_tasks:
        st.sidebar.warning("Add at least one task first.")
    else:
        agent = PetCareAgent(
            owner,
            scheduler,
            max_iterations=max_iterations,
            commentator=commentator if use_llm else None,
        )
        spinner = (
            "Running Planner → Executor → Evaluator → Reflector with AI commentary…"
            if use_llm
            else "Running Planner → Executor → Evaluator → Reflector…"
        )
        with st.spinner(spinner):
            agent.run()
        st.session_state.agent = agent
        st.session_state.llm_error = commentator.last_error if use_llm else ""
        # The loop logs itself on every run; a logging failure must not lose
        # the review the user just waited for.
        try:
            st.session_state.agent_log_path = str(
                log_agent_run(agent, source="streamlit app")
            )
            st.session_state.agent_log_error = None
        except OSError as exc:
            st.session_state.agent_log_path = None
            st.session_state.agent_log_error = str(exc)
        # The agent mutates tasks in place, so re-read for the metrics below.
        all_tasks = scheduler.get_tasks()

agent = st.session_state.get("agent")

# ---------------------------------------------------------------------------
# Header + metric cards
# ---------------------------------------------------------------------------

st.title("🐾 PawPal+")
st.caption(
    f"Agentic pet care planner for **{owner.name}** — add pets and care tasks, "
    "build a day plan, then let the four-stage loop audit it."
)

pending_tasks = scheduler.filter_tasks(all_tasks, completed=False)
committed = sum(t.duration_minutes for t in pending_tasks)
conflicts = len(scheduler.detect_conflicts(all_tasks))

if agent is None:
    agent_status, agent_delta = "Idle", "not run yet"
elif agent.approved:
    agent_status, agent_delta = "Approved", f"{agent.iterations_used} iteration(s)"
else:
    agent_status, agent_delta = "Needs review", f"{agent.iterations_used} iteration(s)"

m1, m2, m3, m4, m5 = st.columns(5)
m1.metric("Pets", len(owner.pets))
m2.metric("Active tasks", len(pending_tasks), delta=f"{len(all_tasks)} total",
          delta_color="off")
m3.metric(
    "Time committed",
    f"{committed} min",
    delta=f"of {owner.available_minutes} min",
    delta_color="off",
)
m4.metric("Conflicts", conflicts, delta="clear" if conflicts == 0 else "overlapping",
          delta_color="off")
m5.metric("Agent status", agent_status, delta=agent_delta, delta_color="off")

st.divider()

tab_schedule, tab_review, tab_insights = st.tabs(
    ["🗓️ Schedule & Timeline", "🤖 Agentic Review & Audit Trail", "📊 System Insights"]
)

# ---------------------------------------------------------------------------
# Tab 1 — Schedule & Timeline
# ---------------------------------------------------------------------------

with tab_schedule:
    if not all_tasks:
        st.info("No tasks yet. Register a pet and add a care task from the sidebar.")
    else:
        st.subheader("Task timeline")

        # Filtering and sorting both go through the Scheduler, not through
        # list logic written here, so the UI matches the tested behaviour.
        f1, f2, f3 = st.columns(3)
        with f1:
            pet_choice = st.selectbox(
                "Show pet", ["All pets"] + [p.name for p in owner.pets]
            )
        with f2:
            status_choice = st.selectbox("Show status", ["All", "Pending", "Done"])
        with f3:
            order_choice = st.selectbox(
                "Order by", ["Time of day", "Priority (planning order)"]
            )

        status_filter = {"All": None, "Pending": False, "Done": True}[status_choice]
        visible = scheduler.filter_tasks(
            all_tasks,
            pet_name=None if pet_choice == "All pets" else pet_choice,
            completed=status_filter,
        )
        visible = (
            scheduler.sort_tasks(visible)
            if order_choice.startswith("Priority")
            else scheduler.sort_by_time(visible)
        )

        if visible:
            show_tasks(visible)
            st.caption(f"Showing {len(visible)} of {len(all_tasks)} task(s).")
        else:
            st.info("No tasks match those filters.")

        # Warn about clashes across everything the owner has, not just the plan.
        warnings = scheduler.conflict_warnings(all_tasks)
        if warnings:
            for message in warnings:
                st.warning(message, icon="⚠️")
        else:
            st.success("No scheduling conflicts among your tasks.", icon="✅")

        st.divider()

        left, right = st.columns([1, 1], gap="large")

        with left:
            st.subheader("Mark a task complete")
            pending = scheduler.filter_tasks(all_tasks, completed=False)
            if pending:
                labels = {
                    f"{t.pet_name}: {t.title} ({t.duration_minutes} min)": t
                    for t in pending
                }
                chosen = st.selectbox("Task", list(labels), label_visibility="collapsed")
                if st.button("Complete task", use_container_width=True):
                    task = labels[chosen]
                    pet = next(p for p in owner.pets if p.name == task.pet_name)
                    nxt = pet.complete_task(task.id)
                    if nxt is None:
                        st.success(f"'{task.title}' is done. It does not repeat.")
                    else:
                        st.success(
                            f"'{task.title}' is done. Next one is due {nxt.due_date:%b %d}."
                        )
                    st.rerun()
            else:
                st.caption("Every task is done.")

        with right:
            st.subheader("Build a schedule")
            st.caption(
                "Greedy fit: highest priority first, then earliest preferred time, "
                "then shortest duration."
            )
            generate = st.button(
                "Generate schedule", type="primary", use_container_width=True
            )

        if generate:
            plan = scheduler.generate_plan()
            used = sum(t.duration_minutes for t in plan)

            st.divider()
            st.subheader("Generated plan")

            if plan:
                st.success(
                    f"Planned {len(plan)} task(s) using {used} of "
                    f"{owner.available_minutes} available minutes."
                )
                show_tasks(plan)
            else:
                st.warning("Nothing fits in the available time.")

            if scheduler.skipped:
                st.warning(
                    f"{len(scheduler.skipped)} task(s) did not fit in the time available."
                )
                show_tasks(scheduler.skipped)

            for message in scheduler.conflict_warnings(plan):
                st.warning(message, icon="⚠️")

            with st.expander("Why this plan?"):
                st.text(scheduler.explain_plan())

# ---------------------------------------------------------------------------
# Tab 2 — Agentic Review & Audit Trail
# ---------------------------------------------------------------------------

with tab_review:
    if agent is None:
        st.info(
            "**No review yet.** Use **Run agentic review** in the sidebar to let the "
            "4-stage loop detect missing pet care, catch overlapping tasks and check "
            "nutritional safety before you finalise your day.",
            icon="💡",
        )
        st.markdown(
            "**What each stage does**\n"
            "- 🧭 **Planner** — diagnoses missing care steps, untimed tasks and clashes.\n"
            "- 🔧 **Executor** — adds, re-times and reorders tasks to resolve them.\n"
            "- 🔍 **Evaluator** — re-checks the result against safety and nutrition goals.\n"
            "- 🪞 **Reflector** — approves, or sends it round again."
        )
    else:
        if agent.approved:
            st.success(
                f"Reflector verdict: **APPROVED** after {agent.iterations_used} "
                "iteration(s). The schedule is complete, conflict-free and well spaced.",
                icon="✅",
            )
        else:
            st.warning(
                f"Reflector verdict: **NEEDS REVIEW** after {agent.iterations_used} "
                "iteration(s). Some issues need a human decision — see the trail below.",
                icon="⚠️",
            )

        agent_committed = sum(t.duration_minutes for t in agent.schedule)
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Iterations", agent.iterations_used)
        c2.metric("Tasks scheduled", len(agent.schedule))
        c3.metric(
            "Time committed",
            f"{agent_committed} min",
            delta=f"of {owner.available_minutes} min available",
            delta_color="off",
        )
        c4.metric("Steps logged", len(agent.log))
        if agent_committed > owner.available_minutes:
            st.error(
                f"The reviewed plan asks for {agent_committed} min but only "
                f"{owner.available_minutes} min are available.",
                icon="⏳",
            )

        trail, revised = st.columns([3, 2], gap="large")

        with trail:
            st.subheader("Audit trail")
            st.caption(
                "Every stage of every iteration, in the order the agent ran them."
            )
            current_iteration = None
            for step in agent.log:
                if step.iteration != current_iteration:
                    current_iteration = step.iteration
                    st.markdown(
                        f"**Iteration {step.iteration}**"
                        if step.iteration
                        else "**Starting point**"
                    )
                icon = STAGE_ICONS.get(step.stage, "•")
                label = f"{icon} {step.stage.title()} — {step.summary}"
                # The verdict opens by default; the rest stay collapsed so a
                # long run is still readable.
                with st.expander(label, expanded=step.stage == "REFLECTOR"):
                    blurb = STAGE_BLURBS.get(step.stage)
                    if blurb:
                        st.caption(f"{blurb}  ·  logged at {step.timestamp:%H:%M:%S}")
                    else:
                        st.caption(f"Logged at {step.timestamp:%H:%M:%S}")
                    render_details(step.details)
                    if step.commentary:
                        st.info(step.commentary, icon="💬")

        with revised:
            st.subheader("Revised schedule")
            if agent.schedule:
                show_tasks(agent.schedule)
            else:
                st.info("The agent produced an empty schedule.")

        st.divider()

        llm_error = st.session_state.get("llm_error")
        if llm_error:
            st.caption(
                f"⚠️ AI commentary unavailable for this run ({llm_error}). "
                "The schedule above is unaffected — the rules decide it, not the model."
            )

        log_path = st.session_state.get("agent_log_path")
        log_error = st.session_state.get("agent_log_error")
        if log_error:
            st.error(f"Could not write {AI_LOG_PATH.name}: {log_error}")
        elif log_path:
            st.caption(f"Run appended to `{log_path}`.")

        with st.expander("Full text trace (as written to the log)"):
            st.code(agent.explain_run(), language="text")

# ---------------------------------------------------------------------------
# Tab 3 — System Insights
# ---------------------------------------------------------------------------

with tab_insights:
    st.subheader("Workload by pet")
    if owner.pets:
        rows = []
        for pet in owner.pets:
            pet_tasks = scheduler.filter_tasks(all_tasks, pet_name=pet.name)
            pet_pending = scheduler.filter_tasks(pet_tasks, completed=False)
            rows.append(
                {
                    "Pet": pet.name,
                    "Species": pet.species,
                    "Tasks": len(pet_tasks),
                    "Pending": len(pet_pending),
                    "Pending minutes": sum(t.duration_minutes for t in pet_pending),
                    "High priority": sum(
                        1 for t in pet_pending if t.priority == Priority.HIGH
                    ),
                }
            )
        st.dataframe(rows, use_container_width=True, hide_index=True)
        st.bar_chart(
            {r["Pet"]: r["Pending minutes"] for r in rows},
            height=240,
        )
    else:
        st.info("Register a pet to see workload insights.")

    st.divider()

    st.subheader("Capacity")
    cap1, cap2 = st.columns([1, 2])
    with cap1:
        st.metric(
            "Utilisation",
            f"{(committed / owner.available_minutes * 100):.0f}%"
            if owner.available_minutes
            else "—",
        )
    with cap2:
        st.progress(min(committed / owner.available_minutes, 1.0) if owner.available_minutes else 0.0)
        if committed > owner.available_minutes:
            st.caption(
                f"Over capacity by {committed - owner.available_minutes} min — "
                "the planner will skip the lowest-priority tasks."
            )
        else:
            st.caption(
                f"{owner.available_minutes - committed} min of headroom left today."
            )

    st.divider()

    st.subheader("Care rules the agent enforces")
    r1, r2, r3 = st.columns(3)
    r1.metric("Safe window", f"{SAFE_WINDOW[0]:%H:%M}–{SAFE_WINDOW[1]:%H:%M}")
    r2.metric(
        "Min feeding gap",
        f"{MIN_FEEDING_GAP_MINUTES // 60}h",
        delta=f"targets {IDEAL_FEEDING_GAP_MINUTES // 60}h spacing",
        delta_color="off",
    )
    r3.metric("Min feedings / pet", 2)
    st.caption(
        "Required daily care — dog: feed, water, walk · cat: feed, water, litter · "
        "rabbit: feed, water, clean · other: feed, water. "
        "Medication-style tasks are pinned and never moved."
    )

    st.markdown("**Day blocks the executor places care into**")
    block_cols = st.columns(len(DAY_BLOCKS))
    for col, (name, (start, end)) in zip(block_cols, DAY_BLOCKS.items()):
        col.metric(name.title(), f"{start:%H:%M}–{end:%H:%M}")
    st.caption(
        "New care is placed inside whichever block suits it and is least "
        "crowded for that pet, so a day reads as a routine rather than a "
        "queue. A second feeding is pushed to the block furthest from the "
        "first meal."
    )

    st.divider()

    st.subheader("Architecture")
    st.markdown(
        "```\n"
        "Sidebar inputs ──▶ Owner ──▶ Pet ──▶ Task      (domain model)\n"
        "                     │\n"
        "                     ▼\n"
        "                 Scheduler      sort / filter / conflicts / generate_plan\n"
        "                     │\n"
        "                     ▼\n"
        "               PetCareAgent     Planner → Executor → Evaluator → Reflector\n"
        "                     │\n"
        "                     ▼\n"
        f"             log_agent_run ──▶ {AI_LOG_PATH.name}\n"
        "```"
    )
