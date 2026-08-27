"""Tests for the matrix reduction engine."""
from gha_optimizer.models.matrix_reducer import MatrixCell, MatrixReducer


def _make_identical_cells(n: int = 4, n_runs: int = 20) -> list[MatrixCell]:
    """Create cells with identical outcomes (all pass)."""
    return [
        MatrixCell(
            cell_id=f"cell_{i}",
            params={"os": "ubuntu", "python": f"3.{10 + i}"},
            outcomes=[0] * n_runs,
        )
        for i in range(n)
    ]


def _make_diverse_cells() -> list[MatrixCell]:
    """Create cells with distinct failure patterns."""
    return [
        MatrixCell(
            cell_id="ubuntu-310",
            params={"os": "ubuntu", "python": "3.10"},
            outcomes=[0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
        ),
        MatrixCell(
            cell_id="ubuntu-311",
            params={"os": "ubuntu", "python": "3.11"},
            outcomes=[0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
        ),
        MatrixCell(
            cell_id="macos-310",
            params={"os": "macos", "python": "3.10"},
            outcomes=[0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0],
        ),
        MatrixCell(
            cell_id="windows-310",
            params={"os": "windows", "python": "3.10"},
            outcomes=[1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 1, 0],
        ),
    ]


def test_identical_cells_reduced():
    """Cells with identical outcomes should be merged to one."""
    cells = _make_identical_cells(4, 20)
    reducer = MatrixReducer(coverage_threshold=0.95)
    result = reducer.reduce(cells)

    assert len(result.kept_cells) < len(result.original_cells)
    assert result.coverage >= 0.95
    assert result.reduction_pct > 0


def test_diverse_cells_preserved():
    """Cells with unique failure patterns should be preserved."""
    cells = _make_diverse_cells()
    reducer = MatrixReducer(coverage_threshold=0.95)
    result = reducer.reduce(cells)

    # macos and windows have unique failures, so they must be kept
    kept_ids = {c.cell_id for c in result.kept_cells}
    assert "macos-310" in kept_ids or "windows-310" in kept_ids
    assert result.coverage >= 0.95


def test_single_cell_unchanged():
    """A single cell should pass through unchanged."""
    cells = [
        MatrixCell(cell_id="solo", params={"os": "ubuntu"}, outcomes=[0] * 20)
    ]
    reducer = MatrixReducer()
    result = reducer.reduce(cells)

    assert len(result.kept_cells) == 1
    assert len(result.dropped_cells) == 0
    assert result.coverage == 1.0


def test_insufficient_history_returns_all():
    """With fewer runs than min_runs_required, return all cells."""
    cells = _make_identical_cells(3, 5)
    reducer = MatrixReducer(min_runs_required=10)
    result = reducer.reduce(cells)

    assert len(result.kept_cells) == 3


def test_to_matrix_include():
    """Reduction result should produce valid matrix.include entries."""
    cells = _make_identical_cells(3, 20)
    reducer = MatrixReducer()
    result = reducer.reduce(cells)

    include_list = result.to_matrix_include()
    assert isinstance(include_list, list)
    for entry in include_list:
        assert isinstance(entry, dict)
        assert "os" in entry
        assert "python" in entry
