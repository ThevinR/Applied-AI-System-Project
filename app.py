from datetime import time

import streamlit as st

from pawpal_system import (
    AI_LOG_PATH,
    Frequency,
    Owner,
    PetCareAgent,
    Pet,
    Priority,
    Scheduler,
    Task,
    log_agent_run,
)

st.set_page_config(page_title="PawPal+", page_icon="🐾", layout="centered")

st.title("🐾 PawPal+")

st.markdown(
    """
PawPal+ is a pet care planning assistant. Add your pets and their care tasks,
and it builds a daily plan that fits your available time and explains its choices.
"""
)

st.divider()

# Streamlit reruns this script on every interaction, so the Owner lives in
# session_state and is only created the first time.
# Objects saved by an older version of the code can lack newer fields (like
# Task.due_date), so rebuild the Owner if any saved task looks stale.
if "owner" in st.session_state and any(
    not hasattr(t, "due_date") for t in st.session_state.owner.get_all_tasks()
):
    del st.session_state["owner"]
if "owner" not in st.session_state:
    st.session_state.owner = Owner(name="Jordan", available_minutes=90)
owner = st.session_state.owner

# One Scheduler for the whole page, so the task views and the plan agree.
scheduler = Scheduler(owner)


def task_rows(tasks):
    """Turn tasks into table rows for st.table."""
    return [
        {
            "Pet": t.pet_name,
            "Task": t.title,
            "Time": f"{t.preferred_time:%H:%M}" if t.preferred_time else "—",
            "Minutes": t.duration_minutes,
            "Priority": t.priority.name.lower(),
            "Repeats": t.frequency.value,
            "Status": "done" if t.is_completed else "pending",
        }
        for t in tasks
    ]


st.subheader("Owner")
col1, col2 = st.columns(2)
with col1:
    owner.name = st.text_input("Owner name", value=owner.name)
with col2:
    owner.available_minutes = int(
        st.number_input(
            "Available time today (minutes)",
            min_value=1,
            max_value=1440,
            value=owner.available_minutes,
        )
    )

st.subheader("Pets")
col1, col2, col3 = st.columns([2, 2, 1])
with col1:
    new_pet_name = st.text_input("Pet name", value="Mochi")
with col2:
    new_pet_species = st.selectbox("Species", ["dog", "cat", "other"])
with col3:
    st.write("")
    st.write("")
    add_pet = st.button("Add pet")

if add_pet:
    if not new_pet_name.strip():
        st.error("Give the pet a name.")
    elif any(p.name == new_pet_name.strip() for p in owner.pets):
        st.warning(f"{new_pet_name.strip()} is already added.")
    else:
        owner.add_pet(Pet(name=new_pet_name.strip(), species=new_pet_species))
        st.success(f"Added {new_pet_name.strip()}.")

if owner.pets:
    st.write(", ".join(f"{p.name} ({p.species})" for p in owner.pets))
else:
    st.info("No pets yet. Add one above.")

st.subheader("Tasks")
if owner.pets:
    col1, col2, col3 = st.columns(3)
    with col1:
        task_pet = st.selectbox("Pet", [p.name for p in owner.pets])
    with col2:
        task_title = st.text_input("Task title", value="Morning walk")
    with col3:
        duration = st.number_input("Duration (minutes)", min_value=1, max_value=240, value=20)

    col1, col2, col3 = st.columns(3)
    with col1:
        priority = st.selectbox("Priority", ["low", "medium", "high"], index=2)
    with col2:
        repeats = st.selectbox("Repeats", [f.value for f in Frequency], index=1)
    with col3:
        use_time = st.checkbox("Set a preferred time", value=True)
        preferred = st.time_input("Preferred time", value=time(7, 30)) if use_time else None

    if st.button("Add task"):
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
else:
    st.caption("Add a pet before adding tasks.")

all_tasks = scheduler.get_tasks()

if all_tasks:
    # Filtering and sorting both go through the Scheduler, not through
    # list logic written here, so the UI matches the tested behaviour.
    col1, col2, col3 = st.columns(3)
    with col1:
        pet_choice = st.selectbox("Show pet", ["All pets"] + [p.name for p in owner.pets])
    with col2:
        status_choice = st.selectbox("Show status", ["All", "Pending", "Done"])
    with col3:
        order_choice = st.selectbox("Order by", ["Priority (planning order)", "Time of day"])

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
        st.table(task_rows(visible))
        st.caption(f"Showing {len(visible)} of {len(all_tasks)} task(s).")
    else:
        st.info("No tasks match those filters.")

    # Warn about clashes across everything the owner has, not just the plan.
    warnings = scheduler.conflict_warnings(all_tasks)
    if warnings:
        for message in warnings:
            st.warning(message)
    else:
        st.success("No scheduling conflicts among your tasks.")

    st.markdown("**Mark a task complete**")
    pending = scheduler.filter_tasks(all_tasks, completed=False)
    if pending:
        labels = {
            f"{t.pet_name}: {t.title} ({t.duration_minutes} min)": t for t in pending
        }
        col1, col2 = st.columns([3, 1])
        with col1:
            chosen = st.selectbox("Task", list(labels), label_visibility="collapsed")
        with col2:
            complete = st.button("Complete")
        if complete:
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
else:
    st.info("No tasks yet.")

st.divider()

st.subheader("Build Schedule")

if st.button("Generate schedule"):
    if not all_tasks:
        st.warning("Add at least one task first.")
    else:
        plan = scheduler.generate_plan()
        used = sum(t.duration_minutes for t in plan)

        if plan:
            st.success(
                f"Planned {len(plan)} task(s) using {used} of "
                f"{owner.available_minutes} available minutes."
            )
            st.table(task_rows(plan))
        else:
            st.warning("Nothing fits in the available time.")

        if scheduler.skipped:
            st.warning(
                f"{len(scheduler.skipped)} task(s) did not fit in the time available."
            )
            st.table(task_rows(scheduler.skipped))

        for message in scheduler.conflict_warnings(plan):
            st.warning(message)

        with st.expander("Why this plan?"):
            st.text(scheduler.explain_plan())

# ---------------------------------------------------------------------------
# Agentic review (sidebar): Planner -> Executor -> Evaluator -> Reflector
# ---------------------------------------------------------------------------

STAGE_ICONS = {
    "INPUT": "📥",
    "PLANNER": "🧭",
    "EXECUTOR": "🔧",
    "EVALUATOR": "🔍",
    "REFLECTOR": "🪞",
}

with st.sidebar:
    st.header("Run Agentic Review")
    st.caption(
        "A four-stage loop checks today's schedule for missing care steps, "
        "clashes and feeding gaps, then fixes what it can."
    )
    st.warning(
        "This edits your schedule: it can add missing care tasks and move "
        "task times. Medication-style tasks are never moved.",
        icon="⚠️",
    )
    max_iterations = int(
        st.number_input("Max iterations", min_value=1, max_value=10, value=3)
    )
    run_review = st.button("Run Agentic Review", type="primary")

if run_review:
    if not all_tasks:
        st.sidebar.warning("Add at least one task first.")
    else:
        agent = PetCareAgent(owner, scheduler, max_iterations=max_iterations)
        with st.spinner("Running Planner → Executor → Evaluator → Reflector…"):
            agent.run()
        st.session_state.agent = agent
        # The loop logs itself on every run; a logging failure must not lose
        # the review the user just waited for.
        try:
            st.session_state.agent_log_path = str(log_agent_run(agent, source="streamlit app"))
            st.session_state.agent_log_error = None
        except OSError as exc:
            st.session_state.agent_log_path = None
            st.session_state.agent_log_error = str(exc)

agent = st.session_state.get("agent")

if agent is not None:
    st.divider()
    st.subheader("Agentic Review")

    if agent.approved:
        st.success(
            f"Reflector verdict: **APPROVED** after {agent.iterations_used} iteration(s). "
            "The schedule is complete, conflict-free and well spaced."
        )
    else:
        st.warning(
            f"Reflector verdict: **NEEDS REVIEW** after {agent.iterations_used} "
            "iteration(s). Some issues need a human decision — see the trail below."
        )

    col1, col2, col3 = st.columns(3)
    col1.metric("Iterations", agent.iterations_used)
    col2.metric("Tasks scheduled", len(agent.schedule))
    col3.metric("Steps logged", len(agent.log))

    st.markdown("**Audit trail**")
    for step in agent.log:
        icon = STAGE_ICONS.get(step.stage, "•")
        label = (
            f"{icon} Iteration {step.iteration} · {step.stage.title()} — {step.summary}"
        )
        # The verdict and any unresolved stage open by default; the rest stay
        # collapsed so a long run is still readable.
        expanded = step.stage == "REFLECTOR"
        with st.expander(label, expanded=expanded):
            st.caption(f"Logged at {step.timestamp:%H:%M:%S}")
            for detail in step.details:
                st.markdown(f"- {detail}")

    st.markdown("**Revised schedule**")
    if agent.schedule:
        st.table(task_rows(agent.schedule))
    else:
        st.info("The agent produced an empty schedule.")

    log_path = st.session_state.get("agent_log_path")
    log_error = st.session_state.get("agent_log_error")
    if log_error:
        st.error(f"Could not write {AI_LOG_PATH.name}: {log_error}")
    elif log_path:
        st.caption(f"Run appended to `{log_path}`.")
