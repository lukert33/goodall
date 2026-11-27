"""Every field on the setup page has an accessible name: what a screen reader announces for it.

A review in a real browser found the account mode radio buttons announced
by their values ("dedicated", "mixed"), checkboxes as "on" and text boxes
by their placeholder. These tests catch that without a browser and without
a new dependency:

- The page's fields are drawn by its own script (the second script in
  setup_page.html), so the test runs that script with the JavaScript every
  Mac comes with (`osascript -l JavaScript`), as
  tests/test_setup_page_script.py does for the portable part. A stand-in
  `document` below gives the script the two elements it reads and keeps
  the HTML it draws; nothing else of a browser is needed to draw.
- Each drawing, and the template around it, is read with Python's own
  html.parser, and every input, select and textarea must have a name a
  screen reader can find: `aria-labelledby` pointing at text on the page,
  `aria-label`, a `<label for=...>`, or a `<label>` around it that holds
  only inline content (a `<div>` or a heading inside a label is not valid
  HTML, and browsers then give the field no name). A name that is only the
  placeholder does not count.

The drawings cover every step of the page opened, in each state that
draws different fields: a new eval in an eval account, creating the eval
account, API keys turned on, Try it out, the eval already on this Mac, and
`goodall settings`. Like the other page script tests, these are skipped
where there is no osascript (CI's Linux runners).
"""

import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest

from goodall import setup_page

CODE = "0123456789abcdef"

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("osascript") is None,
    reason="the setup page's script is run with osascript, which only macOS has",
)

# Stands in for the browser's `document` before the page's scripts run: the
# starting answers element, the element the page draws into, and no-op
# event listeners. `querySelector` finds nothing, so the parts the page
# redraws in place (the Finish block, which holds only a button) are not
# drawn here.
DOCUMENT_STAND_IN = """
var appElement = { innerHTML: "" };
var startingAnswersElement = { textContent: STARTING_ANSWERS_TEXT };
var document = {
  getElementById: function (id) { return id === "app" ? appElement : startingAnswersElement; },
  querySelector: function () { return null; },
  querySelectorAll: function () { return []; },
  addEventListener: function () {}
};
"""

# Runs after the page's scripts: draws the page once per scenario and
# returns every drawing. It sets the page script's own globals, as the
# page's event handlers do.
DRIVER = """
function setAt(holder, place, value) {
  var parts = place.split(".");
  for (var index = 0; index < parts.length - 1; index++) {
    holder = holder[parts[index]];
  }
  holder[parts[parts.length - 1]] = value;
}

function run(argv) {
  var scenarios = JSON.parse(argv[0]);
  var drawings = [];
  for (var index = 0; index < scenarios.length; index++) {
    var scenario = scenarios[index];
    starting = scenario.starting;
    state = startingState(starting);
    for (var changeIndex = 0; changeIndex < scenario.changes.length; changeIndex++) {
      setAt(state, scenario.changes[changeIndex][0], scenario.changes[changeIndex][1]);
    }
    openItem = scenario.open;
    render();
    drawings.push({ name: scenario.name, html: appElement.innerHTML });
  }
  return JSON.stringify(drawings);
}
"""

# Elements that have no end tag.
VOID_ELEMENTS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "wbr"}

# Elements a <label> may not hold: a label holds phrasing content only.
NOT_INSIDE_A_LABEL = {"div", "p", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "table", "section", "label"}

FIELD_ELEMENTS = {"input", "select", "textarea"}


@dataclass
class Element:
    tag: str
    attributes: dict[str, str]
    parent: "Element | None" = None
    children: list["Element | str"] = field(default_factory=list)

    def text(self) -> str:
        """The element's text, as a screen reader would read it, with spaces collapsed."""
        pieces = []
        for child in self.children:
            if isinstance(child, str):
                pieces.append(child)
            elif child.attributes.get("aria-hidden") != "true":
                pieces.append(child.text())
        return " ".join(" ".join(pieces).split())

    def descendants(self) -> list["Element"]:
        found = []
        for child in self.children:
            if isinstance(child, Element):
                found.append(child)
                found.extend(child.descendants())
        return found


class TreeBuilder(HTMLParser):
    """Python's html.parser, building just enough of a tree to find each field's name."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Element("root", {})
        self.current = self.root

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {}
        for name, value in attrs:
            attributes[name] = value or ""
        element = Element(tag, attributes, parent=self.current)
        self.current.children.append(element)
        if tag not in VOID_ELEMENTS:
            self.current = element

    def handle_endtag(self, tag: str) -> None:
        walker: Element | None = self.current
        while walker is not None and walker.tag != tag:
            walker = walker.parent
        if walker is not None and walker.parent is not None:
            self.current = walker.parent

    def handle_data(self, data: str) -> None:
        self.current.children.append(data)


def parse(html: str) -> Element:
    builder = TreeBuilder()
    builder.feed(html)
    builder.close()
    return builder.root


def accessible_name(field_element: Element, everything: list[Element]) -> str:
    """The name a screen reader announces for `field_element`, or "" when it has none."""
    by_id = {}
    for element in everything:
        if "id" in element.attributes:
            by_id[element.attributes["id"]] = element
    labelled_by = field_element.attributes.get("aria-labelledby", "").split()
    if labelled_by:
        pieces = []
        for label_id in labelled_by:
            if label_id in by_id:
                pieces.append(by_id[label_id].text())
        return " ".join(pieces).strip()
    if field_element.attributes.get("aria-label", "").strip():
        return field_element.attributes["aria-label"].strip()
    field_id = field_element.attributes.get("id")
    if field_id:
        for element in everything:
            if element.tag == "label" and element.attributes.get("for") == field_id:
                return element.text()
    ancestor = field_element.parent
    while ancestor is not None:
        if ancestor.tag == "label":
            for inside in ancestor.descendants():
                if inside.tag in NOT_INSIDE_A_LABEL:
                    return ""
            return ancestor.text()
        ancestor = ancestor.parent
    return ""


def fields_without_a_name(html: str) -> list[str]:
    """Each field in `html` with no accessible name, or named only by its placeholder, described for a person."""
    root = parse(html)
    everything = root.descendants()
    nameless = []
    for element in everything:
        if element.tag not in FIELD_ELEMENTS or element.attributes.get("type") == "hidden":
            continue
        name = accessible_name(element, everything)
        placeholder = element.attributes.get("placeholder", "").strip()
        if not name or name == placeholder:
            nameless.append(f"<{element.tag} {element.attributes}>")
    return nameless


def page_scripts(tmp_path: Path, starting: setup_page.StartingAnswers) -> list[str]:
    page = setup_page.write_setup_page(tmp_path, starting, CODE)
    scripts = re.findall(r"<script>(.*?)</script>", page.read_text(), re.DOTALL)
    assert len(scripts) == 2
    return scripts


def document_for(starting: setup_page.StartingAnswers, title: str) -> dict[str, Any]:
    return json.loads(json.dumps(setup_page.page_document(starting, CODE, title)))  # type: ignore[no-any-return]


def scenarios() -> list[dict[str, Any]]:
    """Every state of the page that draws different fields, with each step opened in turn."""
    fresh = document_for(setup_page.StartingAnswers(), "Setting up")
    existing = document_for(
        setup_page.StartingAnswers(
            eval_choice="existing",
            workstreams=["engineering", "research"],
            known_workstreams=["engineering", "research"],
            contributor_id="ada-lovelace",
            main_workstream="research",
        ),
        "Setting up",
    )
    settings = document_for(
        setup_page.StartingAnswers(
            eval_choice="existing",
            workstreams=["engineering", "research"],
            known_workstreams=["engineering", "research"],
            contributor_id="ada-lovelace",
            main_workstream="research",
            settings_run=True,
        ),
        "Goodall settings",
    )
    api_keys: list[list[Any]] = [
        ["uses_api_keys", True],
        ["secret_references", [{"name": "ANTHROPIC_API_KEY_RESEARCH", "reference": ""}]],
    ]
    states: list[tuple[str, dict[str, Any], list[list[Any]]]] = [
        ("a new eval in an eval account", fresh, []),
        ("creating the eval account", fresh, [["eval_account.choice", "create"]]),
        ("an eval account made by hand, with API keys", fresh, [["eval_account.choice", "already_made"], *api_keys]),
        ("Try it out, with no workstreams", fresh, [["account_mode", "mixed"], ["new_eval.workstreams", []]]),
        ("the eval already on this Mac", existing, []),
        ("goodall settings", settings, api_keys),
    ]
    found = []
    for state_name, starting, changes in states:
        for item in ["", "eval", "construct", "workstreams", "who", "where", "secrets"]:
            found.append(
                {
                    "name": f"{state_name}, {item or 'no'} step open",
                    "starting": starting,
                    "changes": changes,
                    "open": item,
                }
            )
    return found


def draw_every_scenario(tmp_path: Path) -> list[dict[str, str]]:
    script = tmp_path / "draw.js"
    page_parts = page_scripts(tmp_path, setup_page.StartingAnswers())
    # The page draws itself once as it loads, from the starting answers.
    starting_answers_text = json.dumps(json.dumps(scenarios()[0]["starting"]))
    stand_in = DOCUMENT_STAND_IN.replace("STARTING_ANSWERS_TEXT", starting_answers_text)
    script.write_text(stand_in + "\n" + "\n".join(page_parts) + "\n" + DRIVER)
    completed = subprocess.run(
        ["osascript", "-l", "JavaScript", str(script), json.dumps(scenarios())],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)  # type: ignore[no-any-return]


def test_every_field_the_page_draws_has_an_accessible_name(tmp_path: Path) -> None:
    drawings = draw_every_scenario(tmp_path)

    problems = {}
    for drawing in drawings:
        nameless = fields_without_a_name(drawing["html"])
        if nameless:
            problems[drawing["name"]] = nameless
    assert problems == {}


def test_the_drawings_hold_every_kind_of_field(tmp_path: Path) -> None:
    # Guards the test above: a stand-in that drew nothing would pass it.
    all_html = ""
    for drawing in draw_every_scenario(tmp_path):
        all_html += drawing["html"]

    for kind in [
        'type="radio"',
        'type="checkbox"',
        'type="text"',
        "<select",
        "<textarea",
        "data-new-chip",
        "data-reference-value",
    ]:
        assert kind in all_html, kind


def test_the_template_itself_has_no_nameless_field() -> None:
    assert fields_without_a_name(setup_page.page_template_text()) == []


def test_the_checker_finds_the_fields_the_review_found() -> None:
    # What the review in a real browser heard, drawn as the page used to draw it.
    radio_in_a_label_with_a_div = (
        '<label><input type="radio" name="account_mode" value="dedicated"><strong>An eval account</strong>'
        '<div class="small muted">A macOS user account.</div></label>'
    )
    text_box_with_only_a_placeholder = '<div class="row"><input type="text" placeholder="Add a workstream"></div>'
    checkbox_alone = '<input type="checkbox">'

    for html in [radio_in_a_label_with_a_div, text_box_with_only_a_placeholder, checkbox_alone]:
        assert len(fields_without_a_name(html)) == 1, html


def test_the_checker_accepts_each_proper_way_to_name_a_field() -> None:
    proper = [
        '<label><input type="checkbox"> <span>Record apps</span></label>',
        '<label for="name-box">Your name</label><input type="text" id="name-box">',
        '<span id="title">Workstreams</span><input type="text" aria-labelledby="title" placeholder="Add">',
        '<input type="text" aria-label="Variable name" placeholder="ANTHROPIC_API_KEY">',
    ]

    for html in proper:
        assert fields_without_a_name(html) == [], html


def test_no_two_elements_on_the_page_share_an_id(tmp_path: Path) -> None:
    # aria-labelledby finds its text by id, so a repeated id would name a field after another field's title.
    for drawing in draw_every_scenario(tmp_path):
        ids = []
        for element in parse(drawing["html"]).descendants():
            if "id" in element.attributes:
                ids.append(element.attributes["id"])
        assert len(ids) == len(set(ids)), drawing["name"]
