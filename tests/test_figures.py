"""The README's figures: the committed files are the ones the script draws, and it shows each."""

import pytest
from layer import ROOT

from scripts import figures


def test_the_committed_figures_are_the_ones_the_script_draws():
    kept = {path.name: path.read_text(encoding="utf-8") for path in figures.FOLDER.glob("*.svg")}

    assert kept == figures.drawn()


def test_a_text_too_wide_for_its_box_stops_the_drawing():
    figure = figures.Figure(100, 100, "One box.")
    figure.box(0, 0, 40, 40, "A title far too wide for its box")

    with pytest.raises(ValueError, match="too wide"):
        figure.svg(figures.LIGHT)


def test_the_readme_shows_each_figure_in_both_themes_and_says_what_it_shows():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    for name, draw in figures.FIGURES.items():
        assert f'srcset="guide/figures/{name}-dark.svg"' in readme
        assert f'<img src="guide/figures/{name}.svg" alt="{draw().says}"' in readme
