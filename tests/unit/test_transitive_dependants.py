"""Transitive dependants (A35), the catalog part: catalog.dependant_closure on rows of FakeSession.

tests/unit/test_transitive_closed_loop.py runs the command line on FakeDatabase.
"""

from __future__ import annotations

from azsqlcd import catalog, names
from azsqlcd.catalog import Dependant
from support.fake_session import FakeSession

TABLE = names.object_key("TABLE", "sales", "Order")
CHAIN = "azsqlcd:dependant_chain"


def view(name: str) -> str:
    return names.object_key("VIEW", "sales", name)


def chain_rows(edges: dict[str, list[str]]):
    """A rule for the chain query: referenced name -> the views that reference it."""

    def answer(batch: str) -> list[list[tuple]]:
        rows = [
            ("sales", user, "V ", 0, "sales", used)
            for used, users in edges.items()
            if f"OBJECT_ID(N'[sales].[{used}]')" in batch
            for user in users
        ]
        return [rows]

    return answer


# ------------------------------------------------------------------ catalog.dependant_closure
def test_the_closure_follows_views_on_views_in_dependency_order_with_the_path_of_each():
    db = FakeSession()
    db.respond("azsqlcd:dependants_of", [[("sales", "v_a", "V ", 0), ("sales", "v_z", "V ", 0)]])
    # v_z is direct and also uses v_a; v_b uses v_a; v_c uses v_b and v_z
    db.respond(CHAIN, chain_rows({"v_a": ["v_b", "v_z"], "v_b": ["v_c"], "v_z": ["v_c"]}))

    found = catalog.dependant_closure(db, [TABLE])

    assert [d.key for d in found.dependants] == [view("v_a"), view("v_b"), view("v_z"), view("v_c")]
    paths = {d.key: d.via for d in found.dependants}
    assert paths[view("v_a")] == () and paths[view("v_z")] == ()  # direct: no path, as before
    assert paths[view("v_b")] == (view("v_a"), TABLE)
    assert paths[view("v_c")] == (view("v_z"), TABLE)  # the shortest path; v_c still waits for v_b
    assert not found.capped
    # one read for each level: the direct query as before, then the chain query on what was new
    assert len(db.sent("azsqlcd:dependants_of")) == 1 and len(db.sent(CHAIN)) == 2


def test_the_direct_query_is_unchanged_and_a_closure_of_direct_dependants_equals_it():
    db = FakeSession()
    rows = [("sales", "vw_Open", "V ", 0), ("sales", "fn_Total", "IF", 1)]
    db.respond("azsqlcd:dependants_of", [rows])

    found = catalog.dependant_closure(db, [TABLE])

    assert list(found.dependants) == catalog.dependants_of(db, [TABLE])
    assert found.dependants[0] == Dependant("FUNCTION:[sales].[fn_Total]", "FUNCTION", True)
    (direct, again) = db.sent("azsqlcd:dependants_of")
    assert direct == again  # byte-identical: the first level is the query of before
    (chain,) = db.sent(CHAIN)
    assert "OBJECT_ID(N'[sales].[fn_Total]'), OBJECT_ID(N'[sales].[vw_Open]')" in chain


def test_a_cycle_ends_and_each_module_is_read_once():
    db = FakeSession()
    db.respond("azsqlcd:dependants_of", [[("sales", "p1", "P ", 0)]])
    db.respond(
        CHAIN,
        lambda batch: [
            [("sales", "p2", "P ", 0, "sales", "p1")]
            if "[p1]" in batch
            else [("sales", "p1", "P ", 0, "sales", "p2"), ("sales", "p2", "P ", 0, "sales", "p2")]
        ],
    )

    found = catalog.dependant_closure(db, [TABLE])

    keys = [d.key for d in found.dependants]
    assert sorted(keys) == ["PROCEDURE:[sales].[p1]", "PROCEDURE:[sales].[p2]"] and len(db.sent(CHAIN)) == 2


def test_the_depth_limit_stops_the_search_and_says_so():
    db = FakeSession()
    db.respond("azsqlcd:dependants_of", [[("sales", "v0", "V ", 0)]])
    db.respond(CHAIN, chain_rows({f"v{n}": [f"v{n + 1}"] for n in range(40)}))

    found = catalog.dependant_closure(db, [TABLE])

    assert found.capped and len(found.dependants) == catalog.DEPENDANT_DEPTH
    assert found.dependants[-1].key == view(f"v{catalog.DEPENDANT_DEPTH - 1}")
    assert found.dependants[2].via == (view("v1"), view("v0"), TABLE)
    assert len(db.sent(CHAIN)) == catalog.DEPENDANT_DEPTH - 1


def test_a_thousand_views_on_one_view_cost_a_few_reads_per_level():
    db = FakeSession()
    db.respond("azsqlcd:dependants_of", [[("sales", "v_base", "V ", 0)]])
    users = [f"v{n:04}" for n in range(1000)]
    db.respond(CHAIN, chain_rows({"v_base": users}))

    found = catalog.dependant_closure(db, [TABLE])

    assert len(found.dependants) == 1001
    # level 2 is one read; level 3 asks for 1000 keys in chunks of catalog.KEY_CHUNK
    assert len(db.sent(CHAIN)) == 1 + 1000 // catalog.KEY_CHUNK


def test_the_root_of_a_path_is_read_when_the_release_touches_more_than_one_object():
    db = FakeSession()
    db.respond("azsqlcd:dependants_of", [[("sales", "v_a", "V ", 0)]])
    db.respond(
        CHAIN,
        lambda batch: [
            [("sales", "v_a", "V ", 0, "sales", "Customer")]
            if "[Customer]" in batch
            else [("sales", "v_b", "V ", 0, "sales", "v_a")]
        ],
    )
    customer = names.object_key("TABLE", "sales", "Customer")

    found = catalog.dependant_closure(db, [TABLE, customer])

    assert found.dependants[-1].via == (view("v_a"), customer)


def test_the_modules_that_the_release_changes_are_roots_of_the_closure():
    db = FakeSession()
    db.respond(CHAIN, chain_rows({"v_a": ["v_b"]}))

    found = catalog.dependant_closure(db, [], modules=[view("v_a")])

    assert [(d.key, d.via) for d in found.dependants] == [(view("v_b"), (view("v_a"),))]
    assert db.sent("azsqlcd:dependants_of") == []  # no table: the direct query is not sent


def test_a_module_that_waits_on_a_cycle_comes_after_the_modules_of_the_cycle():
    """f_x and f_y use each other; a_fn uses f_x and sorts first by key, but it is not on the cycle.
    Before the fix the first waiting key went first, so a_fn was refreshed before f_x."""
    db = FakeSession()
    db.respond("azsqlcd:dependants_of", [[("sales", "f_x", "TF", 0)]])
    edges = {"f_x": ["f_y", "a_fn"], "f_y": ["f_x"]}
    db.respond(
        CHAIN,
        lambda batch: [
            [
                ("sales", user, "TF", 0, "sales", used)
                for used, users in edges.items()
                if f"OBJECT_ID(N'[sales].[{used}]')" in batch
                for user in users
            ]
        ],
    )

    found = catalog.dependant_closure(db, [TABLE])

    function = "FUNCTION:[sales].[{}]".format
    # f_x opens the cycle (first by key of its members); then a_fn and f_y are ready, in key order
    assert [d.key for d in found.dependants] == [function("f_x"), function("a_fn"), function("f_y")]
