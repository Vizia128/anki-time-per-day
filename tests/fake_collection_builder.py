"""
fake_collection_builder.py
==========================
Builds throwaway Anki collections for integration tests.

Each builder returns (col, did, path [, extras...]).
The caller is responsible for col.close(); the temp dir is left on disk
(pytest tmp_path cleans it automatically when used, or the test can delete
the tempfile.mkdtemp() directory manually).

Nothing here touches a real Anki profile.
"""

from __future__ import annotations

import os
import random
import tempfile
import time

from time_budget.scheduler import FSRS6_DEFAULT_PARAMS


def build_fake_collection(
    n_review: int = 250,
    n_new: int = 1750,
    desired_retention: float = 0.9,
    pass_ms: int = 7000,
    lapse_ms: int = 14000,
    new_step_ms: int = 11000,  # per learning step; 2 steps -> ~22 s/new card
    seed: int = 7,
):
    """Return (col, did, path). Caller is responsible for col.close()."""
    # Import collection first: it bootstraps anki.cards fully via the hook chain,
    # preventing "Card from partially initialized module anki.cards" circular import.
    # isort: off
    from anki.collection import Collection
    from anki.cards import FSRSMemoryState
    # isort: on

    rng = random.Random(seed)
    path = os.path.join(tempfile.mkdtemp(prefix="tbsched_"), "fake.anki2")
    col = Collection(path)

    col.set_config("fsrs", True)
    did = col.decks.id("FakeDeck")
    conf = col.decks.config_dict_for_deck_id(did)
    conf["desiredRetention"] = desired_retention
    conf["fsrsParams6"] = list(FSRS6_DEFAULT_PARAMS)
    try:
        col.decks.update_config(conf)
    except AttributeError:
        col.decks.save(conf)

    nt = col.models.by_name("Basic")

    def add_card(front: str) -> int:
        note = col.new_note(nt)
        note["Front"] = front
        note["Back"] = "."
        col.add_note(note, did)
        return note.card_ids()[0]

    today = col.sched.today
    review_cids: list[int] = []
    for i in range(n_review):
        cid = add_card(f"rev{i}")
        card = col.get_card(cid)
        s = float(rng.choice([2, 4, 8, 15, 30, 60, 120]))
        dval = rng.uniform(4.5, 6.5)
        card.memory_state = FSRSMemoryState(stability=s, difficulty=dval)
        card.decay = FSRS6_DEFAULT_PARAMS[20]
        ivl = max(1, int(s))
        card.type = 2
        card.queue = 2
        card.ivl = ivl
        card.due = today + rng.randint(0, ivl)
        col.update_card(card)
        review_cids.append(cid)

    for i in range(n_new):
        add_card(f"new{i}")

    base_id = int(time.time() * 1000)
    rows = []
    rid = base_id
    for j, cid in enumerate(review_cids):
        rid += 1
        rows.append((rid, cid, -1, 3, 10, 5, 2000, pass_ms, 1))
        if j % 5 == 0:
            rid += 1
            rows.append((rid, cid, -1, 1, 0, 10, 0, lapse_ms, 1))
        rid += 1
        rows.append((rid, cid, -1, 3, 0, 0, 0, new_step_ms, 0))
        rid += 1
        rows.append((rid, cid, -1, 3, 0, 0, 0, new_step_ms, 0))
    col.db.executemany(
        "INSERT INTO revlog (id,cid,usn,ease,ivl,lastIvl,factor,time,type) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        rows,
    )
    return col, did, path


def build_fake_collection_empty():
    """0 review cards, 0 new cards."""
    return build_fake_collection(n_review=0, n_new=0)


def build_fake_collection_no_fsrs(n_review: int = 10, n_new: int = 20):
    """Collection with FSRS globally disabled and no fsrsParams6/fsrsWeights key.

    Simulates a deck using SM-2 scheduling (or FSRS entirely turned off).
    Both the global toggle and the per-preset params are cleared so that
    is_fsrs_enabled returns False under all code paths.
    """
    col, did, path = build_fake_collection(n_review=n_review, n_new=n_new)
    # Disable global FSRS toggle (most important — read_fsrs_params falls back to this)
    col.set_config("fsrs", False)
    conf = col.decks.config_dict_for_deck_id(did)
    conf.pop("fsrsParams6", None)
    conf.pop("fsrsWeights", None)
    try:
        col.decks.update_config(conf)
    except AttributeError:
        col.decks.save(conf)
    return col, did, path


def build_fake_collection_with_suspended(
    n_review: int = 50,
    n_new: int = 50,
    n_suspended: int = 20,
):
    """Suspends `n_suspended` new cards (queue=-1).

    Returns (col, did, path, n_suspended).
    """
    col, did, path = build_fake_collection(n_review=n_review, n_new=n_new)
    cids = col.db.list(
        f"SELECT id FROM cards WHERE did={did} AND type=0 LIMIT {n_suspended}"
    )
    for cid in cids:
        card = col.get_card(cid)
        card.queue = -1
        col.update_card(card)
    return col, did, path, n_suspended


def build_fake_collection_with_overdue(n_review: int = 30, n_new: int = 50):
    """Sets 10 review cards to due = today - 10 (overdue).

    read_existing_cards must clamp due_off to 0 (max(0, due - today)).
    """
    col, did, path = build_fake_collection(n_review=n_review, n_new=n_new)
    today = col.sched.today
    cids = col.db.list(f"SELECT id FROM cards WHERE did={did} AND type=2 LIMIT 10")
    for cid in cids:
        card = col.get_card(cid)
        card.due = today - 10
        col.update_card(card)
    return col, did, path


def build_fake_collection_with_subdecks(
    n_parent_review: int = 5,
    n_child_review: int = 10,
    n_child_new: int = 5,
):
    """Parent deck + one child deck. Parent preset has FSRS enabled.

    Returns (col, parent_did, child_did, path).
    """
    # Collection must be imported before anki.cards (circular import).
    # isort: off
    from anki.collection import Collection
    from anki.cards import FSRSMemoryState
    # isort: on

    path = os.path.join(tempfile.mkdtemp(prefix="tbsched_sub_"), "fake.anki2")
    col = Collection(path)
    col.set_config("fsrs", True)

    parent_did = col.decks.id("Parent")
    child_did = col.decks.id("Parent::Child")

    conf = col.decks.config_dict_for_deck_id(parent_did)
    conf["desiredRetention"] = 0.9
    conf["fsrsParams6"] = list(FSRS6_DEFAULT_PARAMS)
    try:
        col.decks.update_config(conf)
    except AttributeError:
        col.decks.save(conf)

    nt = col.models.by_name("Basic")
    today = col.sched.today
    rng = random.Random(42)

    def add_review(deck_id: int, front: str) -> None:
        note = col.new_note(nt)
        note["Front"] = front
        note["Back"] = "."
        col.add_note(note, deck_id)
        cid = note.card_ids()[0]
        card = col.get_card(cid)
        card.memory_state = FSRSMemoryState(stability=10.0, difficulty=5.0)
        card.decay = FSRS6_DEFAULT_PARAMS[20]
        card.type = 2
        card.queue = 2
        card.ivl = 10
        card.due = today + rng.randint(0, 10)
        col.update_card(card)

    def add_new(deck_id: int, front: str) -> None:
        note = col.new_note(nt)
        note["Front"] = front
        note["Back"] = "."
        col.add_note(note, deck_id)

    for i in range(n_parent_review):
        add_review(parent_did, f"p_rev{i}")
    for i in range(n_child_review):
        add_review(child_did, f"c_rev{i}")
    for i in range(n_child_new):
        add_new(child_did, f"c_new{i}")

    return col, parent_did, child_did, path
