"""Clear Outputs as an explicit event (S5 fix, Task 3)."""

from blaym_jupyter.history.apply import apply
from blaym_jupyter.tests.history.scenario import Scenario


def _cell():
    s = Scenario()
    s.start()
    s.insert("c1", "x")
    return s


def _shown(s):
    return [g.runId for g in s.state_at(s.current, len(s.events)).notebook.cells[0].outputs]


def test_clear_removes_finished_output():
    s = _cell()
    run = s.run("c1")
    s.output(run, "a")
    s.finish(run)
    assert _shown(s) == [run]
    s.clear_cell_outputs("c1")
    assert _shown(s) == []


def test_clear_keeps_a_running_runs_output():
    s = _cell()
    run = s.run("c1")
    s.output(run, "a")
    s.clear_cell_outputs("c1")
    assert _shown(s) == [run]
